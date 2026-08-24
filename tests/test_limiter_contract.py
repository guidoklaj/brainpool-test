"""Contract the runner assumes of any limiter. Run these against your own.

With ``LIMITER=noop`` (the default) the rate-holding test is skipped, because a
limiter that admits everything is expected to fail it.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from limiter import build_limiter

LIMITER_NAME = os.environ.get("LIMITER", "noop").lower()
needs_real_limiter = pytest.mark.skipif(
    LIMITER_NAME == "noop", reason="set LIMITER=<yours> to exercise this"
)


async def test_the_interface_is_satisfied():
    limiter = build_limiter()
    try:
        ticket = await limiter.acquire("fake-pro", 100)
        await limiter.record_usage(ticket, "fake-pro", 100, 20)
        await limiter.record_rejection("fake-pro", 1.0)
    finally:
        await limiter.aclose()


@needs_real_limiter
async def test_a_burst_is_paced_rather_than_admitted_at_once():
    """25 requests per 5s means 40 of them cannot all clear immediately."""
    limiter = build_limiter()
    started = time.monotonic()
    try:
        async def one() -> None:
            ticket = await limiter.acquire("fake-pro", 100)
            await limiter.record_usage(ticket, "fake-pro", 100, 20)

        await asyncio.gather(*(one() for _ in range(40)))
    finally:
        await limiter.aclose()

    elapsed = time.monotonic() - started
    assert elapsed > 2.0, f"40 requests cleared in {elapsed:.2f}s -- the quota is not being held"


@needs_real_limiter
async def test_models_are_accounted_separately():
    """The generous model must not be throttled by pressure on the tight one."""
    limiter = build_limiter()
    try:
        await asyncio.gather(*(limiter.acquire("fake-pro", 100) for _ in range(25)))

        started = time.monotonic()
        ticket = await limiter.acquire("fake-flash", 100)
        elapsed = time.monotonic() - started
        await limiter.record_usage(ticket, "fake-flash", 100, 20)
    finally:
        await limiter.aclose()

    assert elapsed < 1.0, f"fake-flash waited {elapsed:.2f}s behind fake-pro's queue"
