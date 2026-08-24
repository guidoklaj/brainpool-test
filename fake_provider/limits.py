"""Quota bookkeeping for the fake provider.

Two window implementations live here. The provider runs in ``fixed`` mode by
default; ``FAKE_PROVIDER_WINDOW=sliding`` switches it. Both enforce a request
quota and a token quota simultaneously, and a caller has to satisfy both.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelLimits:
    """Per-model quota, as advertised in the response headers."""

    requests: int
    request_window: float
    tokens: int
    token_window: float
    base_latency: float


@dataclass
class Decision:
    admitted: bool
    retry_after: float
    remaining_requests: int
    remaining_tokens: int
    dimension: str = ""
    """Which quota refused the request: ``"requests"`` or ``"tokens"``. Empty on
    admission. Real providers distinguish these and so do we -- they need very
    different responses from a caller."""


@dataclass
class FixedWindowBucket:
    """Resets its counters wholesale when the window rolls over.

    Cheap, and what a lot of real gateways actually do -- which also means it
    permits a burst of ``2 * limit`` straddling a window boundary.
    """

    req_window_start: float = 0.0
    req_count: int = 0
    tok_window_start: float = 0.0
    tok_count: int = 0

    def _roll(self, limits: ModelLimits, now: float) -> None:
        if now - self.req_window_start >= limits.request_window:
            self.req_window_start = now
            self.req_count = 0
        if now - self.tok_window_start >= limits.token_window:
            self.tok_window_start = now
            self.tok_count = 0

    def admit(self, tokens: int, limits: ModelLimits, now: float) -> Decision:
        self._roll(limits, now)

        if self.req_count + 1 > limits.requests:
            wait = self.req_window_start + limits.request_window - now
            return Decision(
                False, max(wait, 0.001), 0, max(limits.tokens - self.tok_count, 0), "requests"
            )

        if self.tok_count + tokens > limits.tokens:
            wait = self.tok_window_start + limits.token_window - now
            return Decision(
                False,
                max(wait, 0.001),
                max(limits.requests - self.req_count, 0),
                max(limits.tokens - self.tok_count, 0),
                "tokens",
            )

        self.req_count += 1
        self.tok_count += tokens
        return Decision(
            True,
            0.0,
            max(limits.requests - self.req_count, 0),
            max(limits.tokens - self.tok_count, 0),
        )

    def charge_tokens(self, tokens: int, limits: ModelLimits, now: float) -> None:
        self._roll(limits, now)
        self.tok_count += tokens


@dataclass
class SlidingWindowBucket:
    """Tracks individual events so the quota holds over any window position."""

    req_events: deque[float] = field(default_factory=deque)
    tok_events: deque[tuple[float, int]] = field(default_factory=deque)

    def _prune(self, limits: ModelLimits, now: float) -> None:
        while self.req_events and now - self.req_events[0] >= limits.request_window:
            self.req_events.popleft()
        while self.tok_events and now - self.tok_events[0][0] >= limits.token_window:
            self.tok_events.popleft()

    def _tokens_used(self) -> int:
        return sum(n for _, n in self.tok_events)

    def admit(self, tokens: int, limits: ModelLimits, now: float) -> Decision:
        self._prune(limits, now)
        used = self._tokens_used()

        if len(self.req_events) + 1 > limits.requests:
            wait = self.req_events[0] + limits.request_window - now
            return Decision(False, max(wait, 0.001), 0, max(limits.tokens - used, 0), "requests")

        if used + tokens > limits.tokens:
            wait = self.tok_events[0][0] + limits.token_window - now if self.tok_events else 0.001
            return Decision(
                False,
                max(wait, 0.001),
                max(limits.requests - len(self.req_events), 0),
                max(limits.tokens - used, 0),
                "tokens",
            )

        self.req_events.append(now)
        self.tok_events.append((now, tokens))
        return Decision(
            True,
            0.0,
            max(limits.requests - len(self.req_events), 0),
            max(limits.tokens - used - tokens, 0),
        )

    def charge_tokens(self, tokens: int, limits: ModelLimits, now: float) -> None:
        self._prune(limits, now)
        self.tok_events.append((now, tokens))


def make_bucket(mode: str) -> FixedWindowBucket | SlidingWindowBucket:
    if mode == "sliding":
        return SlidingWindowBucket()
    if mode == "fixed":
        return FixedWindowBucket()
    raise ValueError(f"unknown window mode {mode!r} (expected 'fixed' or 'sliding')")
