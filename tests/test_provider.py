"""Checks on the harness itself, so you can trust what you are measuring."""

from __future__ import annotations

import pytest

from fake_provider.limits import FixedWindowBucket, ModelLimits, SlidingWindowBucket

LIMITS = ModelLimits(requests=3, request_window=10.0, tokens=1_000, token_window=60.0, base_latency=0.0)


def test_fixed_window_admits_up_to_the_request_quota():
    bucket = FixedWindowBucket()
    assert [bucket.admit(10, LIMITS, 0.0).admitted for _ in range(4)] == [True, True, True, False]


def test_fixed_window_reports_a_usable_retry_after():
    bucket = FixedWindowBucket()
    for _ in range(3):
        bucket.admit(10, LIMITS, 0.0)
    decision = bucket.admit(10, LIMITS, 4.0)
    assert not decision.admitted
    assert decision.retry_after == pytest.approx(6.0)


def test_fixed_window_permits_a_boundary_burst():
    """Documented behaviour, not a bug in the harness -- and worth knowing about."""
    bucket = FixedWindowBucket()
    for _ in range(3):
        assert bucket.admit(10, LIMITS, 9.9).admitted
    for _ in range(3):
        assert bucket.admit(10, LIMITS, 10.0).admitted


def test_sliding_window_refuses_the_boundary_burst():
    bucket = SlidingWindowBucket()
    for _ in range(3):
        assert bucket.admit(10, LIMITS, 9.9).admitted
    assert not bucket.admit(10, LIMITS, 10.0).admitted


def test_token_quota_binds_independently_of_the_request_quota():
    bucket = FixedWindowBucket()
    assert bucket.admit(900, LIMITS, 0.0).admitted
    decision = bucket.admit(200, LIMITS, 0.0)
    assert not decision.admitted
    assert decision.remaining_requests > 0


def test_completion_tokens_are_charged_after_the_fact():
    bucket = FixedWindowBucket()
    bucket.admit(500, LIMITS, 0.0)
    bucket.charge_tokens(600, LIMITS, 0.0)
    assert not bucket.admit(1, LIMITS, 0.0).admitted
