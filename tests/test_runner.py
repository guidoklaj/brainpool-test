"""Checks on the agent runner, using a stub transport instead of the provider."""

from __future__ import annotations

import json

import httpx
import pytest

from agent.runner import (
    MAX_HISTORY_TOKENS,
    MAX_MODEL_CALLS,
    ProviderRateLimited,
    estimate_tokens,
    run_agent,
)
from limiter.base import NoopLimiter


def _final(content: str = "done") -> dict:
    return {
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
    }


def _tool_call(name: str = "search_kb", call_id: str = "call_0") -> dict:
    return {
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps({"query": "quota policy"})},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
    }


def _client(responses: list[httpx.Response]) -> httpx.AsyncClient:
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return queue.pop(0) if queue else httpx.Response(200, json=_final())

    return httpx.AsyncClient(base_url="http://provider", transport=httpx.MockTransport(handler))


async def test_a_run_loops_through_tool_calls_then_answers():
    async with _client([
        httpx.Response(200, json=_tool_call()),
        httpx.Response(200, json=_final("the answer")),
    ]) as client:
        result = await run_agent("q", client=client, limiter=NoopLimiter())

    assert result.text == "the answer"
    assert result.model_calls == 2
    assert result.prompt_tokens == 80


async def test_a_429_is_surfaced_and_never_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"error": {}}, headers={"Retry-After": "2.5"})

    async with httpx.AsyncClient(base_url="http://p", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderRateLimited) as excinfo:
            await run_agent("q", client=client, limiter=NoopLimiter())

    assert calls == 1
    assert excinfo.value.retry_after == pytest.approx(2.5)


async def test_the_call_budget_is_enforced():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_tool_call())

    async with httpx.AsyncClient(base_url="http://p", transport=httpx.MockTransport(handler)) as client:
        result = await run_agent("q", client=client, limiter=NoopLimiter())

    assert result.model_calls == MAX_MODEL_CALLS
    assert "did not converge" in result.text


async def test_history_is_copied_not_mutated():
    history = [{"role": "user", "content": "earlier"}]
    async with _client([httpx.Response(200, json=_final())]) as client:
        await run_agent("q", client=client, limiter=NoopLimiter(), history=history)

    assert history == [{"role": "user", "content": "earlier"}]


async def test_history_is_truncated_to_the_prompt_budget():
    history = [{"role": "user", "content": "x" * 400} for _ in range(80)]
    async with _client([httpx.Response(200, json=_final())]) as client:
        result = await run_agent("q", client=client, limiter=NoopLimiter(), history=history)

    assert result.truncated_turns > 0
    assert estimate_tokens(result.history) <= MAX_HISTORY_TOKENS


async def test_a_tool_failure_reaches_the_model_as_an_error():
    from agent import tools

    async def boom(**_):
        raise RuntimeError("kaboom")

    original = tools.TOOLS["search_kb"]
    tools.TOOLS["search_kb"] = boom
    try:
        async with _client([
            httpx.Response(200, json=_tool_call()),
            httpx.Response(200, json=_final()),
        ]) as client:
            result = await run_agent("q", client=client, limiter=NoopLimiter())
    finally:
        tools.TOOLS["search_kb"] = original

    tool_messages = [m for m in result.history if m.get("role") == "tool"]
    assert tool_messages and tool_messages[0]["content"].startswith("ERROR:")
