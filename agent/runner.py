"""The agent under test: one model call, then tools, then repeat until done.

A single ``run_agent`` call makes between one and four provider requests. That
is the fact that makes this exercise interesting -- the unit of work you are
budgeting is a *run*, but the thing the provider counts is a *request*, and the
size of each request grows as the conversation accumulates tool output.

This runner does not retry rate-limit rejections. A 429 reaching it is a
failure, and the load test treats it as one. Preventing them is your job, and
the limiter seam in ``limiter/`` is where that happens.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from agent.tools import TOOL_SCHEMAS, TOOLS
from limiter.base import Limiter

logger = logging.getLogger(__name__)

MAX_MODEL_CALLS = 6
"""Hard ceiling on provider calls per run. A model that keeps asking for tools
past this point is misbehaving, and we stop rather than pay for it."""

MAX_HISTORY_TOKENS = 3_000
"""Approximate prompt budget. Older turns are dropped once we exceed it."""

REQUEST_TIMEOUT = 20.0
SERVER_ERROR_ATTEMPTS = 2

PRICES_PER_1K = {
    "fake-flash": {"prompt": 0.000075, "completion": 0.00030},
    "fake-pro": {"prompt": 0.00125, "completion": 0.00500},
}


class ProviderRateLimited(RuntimeError):
    """The provider returned 429. Under a working limiter this should not happen."""

    def __init__(self, model: str, retry_after: float) -> None:
        super().__init__(f"provider rate limited model={model} retry_after={retry_after:.3f}s")
        self.model = model
        self.retry_after = retry_after


class ProviderError(RuntimeError):
    """The provider failed for a reason that is not a quota breach."""


class ToolFailed(RuntimeError):
    """A tool raised. Surfaced to the model as an error, never as empty output."""


@dataclass
class RunResult:
    text: str
    model: str
    model_calls: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    duration_s: float
    truncated_turns: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)


def estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Our side's guess at the prompt size, in the provider's accounting.

    Deliberately approximate. The provider's own count is authoritative and
    arrives with the response; the gap between the two is real and you should
    decide what to do about it.
    """
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += max(1, (len(content) + 3) // 4)
        elif content is not None:
            total += max(1, (len(json.dumps(content)) + 3) // 4)
        for call in message.get("tool_calls") or []:
            total += max(1, (len(json.dumps(call)) + 3) // 4)
        total += 4
    return total


def _truncate(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Drop the oldest turns until the prompt fits the budget.

    The first message is kept if it is a system prompt, and a tool result is
    never separated from the assistant turn that asked for it.
    """
    if estimate_tokens(messages) <= MAX_HISTORY_TOKENS:
        return messages, 0

    head: list[dict[str, Any]] = []
    tail = list(messages)
    if tail and tail[0].get("role") == "system":
        head = [tail.pop(0)]

    dropped = 0
    while tail and estimate_tokens(head + tail) > MAX_HISTORY_TOKENS:
        tail.pop(0)
        dropped += 1
        while tail and tail[0].get("role") == "tool":
            tail.pop(0)
            dropped += 1

    return head + tail, dropped


def _cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    prices = PRICES_PER_1K.get(model)
    if prices is None:
        raise ProviderError(f"no price on file for model {model!r}")
    return (prompt_tokens / 1000) * prices["prompt"] + (completion_tokens / 1000) * prices["completion"]


async def _run_tool(name: str, arguments: dict[str, Any]) -> str:
    """Run one tool, reporting failure to the model rather than hiding it."""
    tool = TOOLS.get(name)
    if tool is None:
        return f"ERROR: no such tool {name!r}"
    try:
        return await tool(**arguments)
    except Exception as exc:  # noqa: BLE001 -- surfaced to the model, not swallowed
        logger.warning("tool %s failed: %s", name, exc)
        return f"ERROR: tool {name!r} failed: {exc}"


async def _call_model(
    client: httpx.AsyncClient,
    limiter: Limiter,
    model: str,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """One provider request, gated by the limiter and never retried on 429."""
    payload = {"model": model, "messages": messages, "tools": TOOL_SCHEMAS}

    for attempt in range(1, SERVER_ERROR_ATTEMPTS + 1):
        ticket = await limiter.acquire(model, estimate_tokens(messages))

        response = await client.post("/v1/chat/completions", json=payload, timeout=REQUEST_TIMEOUT)

        if response.status_code == 429:
            retry_after = float(response.headers.get("Retry-After", "1"))
            await limiter.record_rejection(model, retry_after)
            raise ProviderRateLimited(model, retry_after)

        if response.status_code >= 500:
            await limiter.record_usage(ticket, model, 0, 0)
            if attempt == SERVER_ERROR_ATTEMPTS:
                raise ProviderError(f"provider returned {response.status_code}")
            await asyncio.sleep(0.25 * attempt)
            continue

        if response.status_code != 200:
            await limiter.record_usage(ticket, model, 0, 0)
            raise ProviderError(f"provider returned {response.status_code}: {response.text[:200]}")

        data = response.json()
        usage = data.get("usage") or {}
        await limiter.record_usage(
            ticket,
            model,
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
        )
        return data

    raise ProviderError("exhausted server-error attempts")


async def run_agent(
    prompt: str,
    *,
    client: httpx.AsyncClient,
    limiter: Limiter,
    model: str = "fake-pro",
    history: list[dict[str, Any]] | None = None,
    system_prompt: str = "You answer questions about quota policy using the tools available.",
) -> RunResult:
    """Run one agent turn to completion.

    ``history`` is copied, never mutated -- the updated transcript comes back on
    the result so the caller decides what to persist.
    """
    started = time.monotonic()
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": prompt})

    prompt_tokens = 0
    completion_tokens = 0
    truncated = 0
    text = ""
    calls = 0

    for _ in range(MAX_MODEL_CALLS):
        messages, dropped = _truncate(messages)
        truncated += dropped

        data = await _call_model(client, limiter, model, messages)
        calls += 1

        usage = data.get("usage") or {}
        prompt_tokens += int(usage.get("prompt_tokens", 0))
        completion_tokens += int(usage.get("completion_tokens", 0))

        message = data["choices"][0]["message"]
        messages.append(message)

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            text = message.get("content") or ""
            break

        results = await asyncio.gather(
            *(
                _run_tool(call["function"]["name"], json.loads(call["function"]["arguments"]))
                for call in tool_calls
            )
        )
        for call, result in zip(tool_calls, results, strict=True):
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    else:
        logger.warning("run hit MAX_MODEL_CALLS=%d without a final answer", MAX_MODEL_CALLS)
        text = "ERROR: agent did not converge within its call budget"

    return RunResult(
        text=text,
        model=model,
        model_calls=calls,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=_cost(model, prompt_tokens, completion_tokens),
        duration_s=time.monotonic() - started,
        truncated_turns=truncated,
        history=[m for m in messages if m.get("role") != "system"],
    )
