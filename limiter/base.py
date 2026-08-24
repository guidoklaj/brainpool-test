"""The seam your limiter plugs into.

``agent/runner.py`` calls a limiter around every provider request. The default
implementation here does nothing at all, which is why ``make loadtest`` fails
out of the box.

You are free to change this interface. If you do, say why in NOTES.md -- the
shape below is a starting point, not a specification, and we are as interested
in your view of the boundary as in the implementation behind it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Limiter(Protocol):
    """Gates provider requests and observes their outcome.

    The runner calls these in order for every request it makes:

        ticket = await limiter.acquire(model, estimated_prompt_tokens)
        ... issue the HTTP request ...
        await limiter.record_usage(ticket, model, prompt_tokens, completion_tokens)

    and, if the provider rejected the request anyway:

        await limiter.record_rejection(model, retry_after)

    ``acquire`` may block for as long as it needs to. Whatever it returns is
    handed straight back to ``record_usage``, so use it to carry any state you
    need between the two -- or ignore it.
    """

    async def acquire(self, model: str, estimated_prompt_tokens: int) -> Any:
        """Block until it is this caller's turn to hit ``model``."""
        ...

    async def record_usage(
        self,
        ticket: Any,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """Report what the request actually consumed, once it is known."""
        ...

    async def record_rejection(self, model: str, retry_after: float) -> None:
        """Report that the provider returned 429 despite ``acquire`` succeeding."""
        ...

    async def aclose(self) -> None:
        """Release any resources held by the limiter."""
        ...


class NoopLimiter:
    """Admits everything, immediately. The starting state of this exercise."""

    async def acquire(self, model: str, estimated_prompt_tokens: int) -> Any:
        return None

    async def record_usage(
        self,
        ticket: Any,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        return None

    async def record_rejection(self, model: str, retry_after: float) -> None:
        return None

    async def aclose(self) -> None:
        return None
