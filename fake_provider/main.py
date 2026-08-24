"""A fake, OpenAI-shaped LLM endpoint that enforces real rate limits.

This is the interview harness -- treat it as a third-party service you do not
control. Do not edit this package to make your solution pass.

Behaviour worth knowing about, because a real provider would document it:

* ``POST /v1/chat/completions`` speaks the OpenAI chat-completions dialect,
  including ``tools`` / ``tool_calls`` and a ``usage`` block.
* Two models with deliberately asymmetric quotas -- see ``MODELS`` below.
* Quotas are per (API key, model). Both a request quota and a token quota
  apply, and the token quota is charged for prompt *and* completion tokens --
  the completion half only lands once the response has been generated.
* Breaching a quota returns 429 immediately (before any latency is simulated)
  with a ``Retry-After`` header. Every response, 429 or not, carries
  ``x-ratelimit-*`` headers.
* Latency is lognormal around a per-model base. A small fraction of requests
  return 500, and a smaller fraction hang far longer than you want to wait.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import time
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from fake_provider.limits import ModelLimits, make_bucket

MODELS: dict[str, ModelLimits] = {
    "fake-flash": ModelLimits(
        requests=120,
        request_window=5.0,
        tokens=200_000,
        token_window=60.0,
        base_latency=0.12,
    ),
    "fake-pro": ModelLimits(
        requests=25,
        request_window=5.0,
        tokens=30_000,
        token_window=60.0,
        base_latency=0.45,
    ),
}

WINDOW_MODE = os.environ.get("FAKE_PROVIDER_WINDOW", "fixed")
SEED = int(os.environ.get("FAKE_PROVIDER_SEED", "1337"))
ERROR_RATE = float(os.environ.get("FAKE_PROVIDER_ERROR_RATE", "0.01"))
HANG_RATE = float(os.environ.get("FAKE_PROVIDER_HANG_RATE", "0.005"))
HANG_SECONDS = float(os.environ.get("FAKE_PROVIDER_HANG_SECONDS", "35"))

TOOL_NAMES = ("search_kb", "fetch_document", "summarise")

app = FastAPI(title="fake-provider")

_lock = asyncio.Lock()
_buckets: dict[tuple[str, str], Any] = {}
_rng = random.Random(SEED)
_stats = {
    "requests": 0,
    "admitted": 0,
    "rate_limited": 0,
    "errors": 0,
    "hangs": 0,
    "prompt_tokens": 0,
    "completion_tokens": 0,
}
_admitted_by_model: dict[str, int] = {}
_rejected_by_model: dict[str, int] = {}
_rejected_by_dimension: dict[str, int] = {}
_events: list[dict[str, Any]] = []
_first_request_at: list[float] = []
_EVENT_CAP = 4000


def _bucket(key: str, model: str) -> Any:
    ident = (key, model)
    if ident not in _buckets:
        _buckets[ident] = make_bucket(WINDOW_MODE)
    return _buckets[ident]


def _count_tokens(text: str) -> int:
    """Deliberately crude: roughly four characters to a token."""
    return max(1, (len(text) + 3) // 4)


def _prompt_tokens(messages: list[dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += _count_tokens(content)
        elif isinstance(content, list):
            total += _count_tokens(json.dumps(content))
        for call in message.get("tool_calls") or []:
            total += _count_tokens(json.dumps(call))
        total += 4  # per-message framing overhead
    return total


def _stable_int(text: str) -> int:
    """A deterministic pseudo-random int, so a given prompt behaves the same way."""
    value = 0
    for char in text:
        value = (value * 131 + ord(char)) % 1_000_003
    return value


def _plan(messages: list[dict[str, Any]]) -> tuple[int, int]:
    """Decide how many model calls this conversation takes, and its fan-out.

    Returns ``(planned_calls, tool_calls_per_step)``. Derived from the first
    user message so a run is reproducible, and lands in 1-4 calls per run.
    """
    seed_text = ""
    for message in messages:
        if message.get("role") == "user":
            seed_text = str(message.get("content") or "")
            break
    value = _stable_int(seed_text)
    return 1 + value % 4, 1 + (value // 7) % 2


def _headers(model: str, remaining_requests: int, remaining_tokens: int) -> dict[str, str]:
    limits = MODELS[model]
    return {
        "x-ratelimit-limit-requests": str(limits.requests),
        "x-ratelimit-remaining-requests": str(remaining_requests),
        "x-ratelimit-limit-tokens": str(limits.tokens),
        "x-ratelimit-remaining-tokens": str(remaining_tokens),
        "x-ratelimit-window-requests": str(limits.request_window),
        "x-ratelimit-window-tokens": str(limits.token_window),
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request, authorization: str = Header(default="")) -> JSONResponse:
    body = await request.json()
    model = body.get("model") or "fake-flash"
    if model not in MODELS:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": f"unknown model {model!r}", "type": "invalid_request_error"}},
        )

    api_key = authorization.removeprefix("Bearer ").strip() or "anonymous"
    messages = body.get("messages") or []
    limits = MODELS[model]
    prompt_tokens = _prompt_tokens(messages)

    async with _lock:
        _stats["requests"] += 1
        arrived = time.monotonic()
        if not _first_request_at:
            _first_request_at.append(arrived)
        decision = _bucket(api_key, model).admit(prompt_tokens, limits, arrived)
        if len(_events) < _EVENT_CAP:
            _events.append(
                {
                    "at_ms": round((arrived - _first_request_at[0]) * 1000),
                    "model": model,
                    "admitted": decision.admitted,
                    "dimension": decision.dimension,
                    "prompt_tokens": prompt_tokens,
                }
            )
        if decision.admitted:
            _stats["admitted"] += 1
            _admitted_by_model[model] = _admitted_by_model.get(model, 0) + 1
        else:
            _stats["rate_limited"] += 1
            _rejected_by_model[model] = _rejected_by_model.get(model, 0) + 1
            dim_key = f"{model}:{decision.dimension}"
            _rejected_by_dimension[dim_key] = _rejected_by_dimension.get(dim_key, 0) + 1

    if not decision.admitted:
        headers = _headers(model, decision.remaining_requests, decision.remaining_tokens)
        headers["Retry-After"] = f"{decision.retry_after:.3f}"
        return JSONResponse(
            status_code=429,
            content={
                "error": {
                    "message": f"{decision.dimension} rate limit exceeded for model {model!r}",
                    "type": "rate_limit_error",
                    "dimension": decision.dimension,
                }
            },
            headers=headers,
        )

    async with _lock:
        roll_error = _rng.random()
        roll_hang = _rng.random()
        latency = limits.base_latency * _rng.lognormvariate(0.0, 0.55)
        planned_calls, fan_out = _plan(messages)

    if roll_hang < HANG_RATE:
        async with _lock:
            _stats["hangs"] += 1
        await asyncio.sleep(HANG_SECONDS)

    await asyncio.sleep(latency)

    if roll_error < ERROR_RATE:
        async with _lock:
            _stats["errors"] += 1
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "internal provider error", "type": "server_error"}},
            headers=_headers(model, decision.remaining_requests, decision.remaining_tokens),
        )

    assistant_turns = sum(1 for m in messages if m.get("role") == "assistant")
    wants_tools = bool(body.get("tools")) and assistant_turns < planned_calls - 1

    if wants_tools:
        tool_calls = []
        for index in range(fan_out):
            name = TOOL_NAMES[(assistant_turns + index) % len(TOOL_NAMES)]
            arguments = {"search_kb": {"query": "quota policy"},
                         "fetch_document": {"doc_id": f"doc-{assistant_turns}-{index}"},
                         "summarise": {"text": "the retrieved passages"}}[name]
            tool_calls.append(
                {
                    "id": f"call_{assistant_turns}_{index}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            )
        message: dict[str, Any] = {"role": "assistant", "content": None, "tool_calls": tool_calls}
        completion_tokens = 24 * fan_out
        finish_reason = "tool_calls"
    else:
        message = {
            "role": "assistant",
            "content": (
                "Based on the retrieved passages, the quota policy applies per API key "
                "and per model, and is charged against both a request and a token budget."
            ),
        }
        completion_tokens = 42
        finish_reason = "stop"

    async with _lock:
        _bucket(api_key, model).charge_tokens(completion_tokens, limits, time.monotonic())
        _stats["prompt_tokens"] += prompt_tokens
        _stats["completion_tokens"] += completion_tokens
        remaining_tokens = max(decision.remaining_tokens - completion_tokens, 0)

    return JSONResponse(
        status_code=200,
        content={
            "id": f"chatcmpl-{_stable_int(str(messages))}",
            "object": "chat.completion",
            "model": model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        },
        headers=_headers(model, decision.remaining_requests, remaining_tokens),
    )


@app.get("/admin/stats")
async def admin_stats() -> dict[str, Any]:
    """Ground truth. The load test cross-checks its own numbers against this."""
    async with _lock:
        return {
            "window_mode": WINDOW_MODE,
            "models": {
                name: {
                    "requests": limits.requests,
                    "request_window": limits.request_window,
                    "tokens": limits.tokens,
                    "token_window": limits.token_window,
                }
                for name, limits in MODELS.items()
            },
            "counters": dict(_stats),
            "admitted_by_model": dict(_admitted_by_model),
            "rejected_by_model": dict(_rejected_by_model),
            "rejected_by_dimension": dict(_rejected_by_dimension),
        }


@app.post("/admin/reset")
async def admin_reset() -> dict[str, str]:
    async with _lock:
        _buckets.clear()
        _admitted_by_model.clear()
        _rejected_by_model.clear()
        _rejected_by_dimension.clear()
        _events.clear()
        _first_request_at.clear()
        for key in _stats:
            _stats[key] = 0
    return {"status": "reset"}


@app.get("/admin/events")
async def admin_events() -> dict[str, Any]:
    """Every request the provider saw this session, with arrival offsets.

    Useful when the load test reports rejections and you want to know when and
    under which quota they happened, rather than guessing.
    """
    async with _lock:
        return {"count": len(_events), "capped_at": _EVENT_CAP, "events": list(_events)}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
