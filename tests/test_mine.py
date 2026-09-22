"""Additional tests for the Redis-backed rate limiter."""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from redis.asyncio import Redis

from limiter import build_limiter

pytestmark = pytest.mark.skipif(
    os.environ.get("LIMITER", "noop").lower() == "noop",
    reason="set LIMITER=mine to run these",
)


@pytest.fixture(autouse=True)
async def _clean_redis():
    """Clear rate-limiter keys so each test starts from a blank slate."""
    r = Redis.from_url("redis://localhost:6380")
    keys = [f"rl:{m}:{d}" for m in ("fake-flash", "fake-pro") for d in ("req", "tok")]
    await r.delete(*keys)
    yield
    await r.delete(*keys)
    await r.aclose()


async def test_token_refund_frees_capacity():
    """Over-estimated tokens are refunded, allowing subsequent requests."""
    limiter = build_limiter()
    try:
        t1 = await limiter.acquire("fake-pro", 25_000)
        # Report actual usage far below estimate → large refund
        await limiter.record_usage(t1, "fake-pro", 500, 50)

        started = time.monotonic()
        t2 = await limiter.acquire("fake-pro", 5_000)
        elapsed = time.monotonic() - started
        await limiter.record_usage(t2, "fake-pro", 5_000, 50)

        assert elapsed < 1.0, f"waited {elapsed:.2f}s — token refund not working"
    finally:
        await limiter.aclose()


async def test_unknown_model_passes_through():
    """Requests for unknown models pass through without blocking."""
    limiter = build_limiter()
    try:
        started = time.monotonic()
        ticket = await limiter.acquire("unknown-model", 500)
        elapsed = time.monotonic() - started
        assert elapsed < 0.5
        assert ticket is not None
    finally:
        await limiter.aclose()


async def test_server_error_preserves_prompt_reservation():
    """On 5xx (usage=0,0), only the completion buffer is refunded."""
    limiter = build_limiter()
    try:
        # Reserve a large chunk of the token budget
        t1 = await limiter.acquire("fake-pro", 20_000)
        # Simulate server error: report 0 usage.
        # Only the completion buffer (50) should be refunded, not the full
        # prompt estimate.  Current token count should be ~20,000.
        await limiter.record_usage(t1, "fake-pro", 0, 0)

        # A second large request (10,000 + 50 = 10,050) would push total to
        # ~30,050 which exceeds the limit (~28,500).  It should block.
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(limiter.acquire("fake-pro", 10_000), timeout=1.0)
    finally:
        await limiter.aclose()


async def test_concurrent_acquires_respect_limit():
    """Multiple concurrent acquires must not exceed the request limit."""
    limiter = build_limiter()
    admitted_times: list[float] = []
    start = time.monotonic()

    try:

        async def one() -> None:
            ticket = await limiter.acquire("fake-pro", 100)
            admitted_times.append(time.monotonic() - start)
            await limiter.record_usage(ticket, "fake-pro", 100, 20)

        await asyncio.gather(*(one() for _ in range(30)))
    finally:
        await limiter.aclose()

    # With limit=21 per 5.05s window, 30 requests need >1 window
    instant = [t for t in admitted_times if t < 0.5]
    assert len(instant) <= 25, f"{len(instant)} requests admitted instantly (expected <=25)"
