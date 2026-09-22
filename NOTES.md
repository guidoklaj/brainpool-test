# Notes

## What I built

A Redis-backed fixed-window rate limiter (`limiter/mine.py`) that coordinates
across the four worker processes via atomic Lua scripts. Each model gets its own
pair of Redis hash keys (one for request count, one for token count), and a
single Lua `EVALSHA` atomically checks both quotas and either admits the request
or returns how long to wait. A second Lua script adjusts the token count after
the real usage arrives from the provider.

The prompt-token estimate passed to `acquire()` is exact (same formula as the
provider), so the only gap is the unknown completion tokens. I reserve a flat
buffer of 50 tokens at acquire time and refund or charge the difference in
`record_usage()`. On server errors (usage reported as 0,0), only the completion
buffer is refunded since the provider still charged prompt tokens on admission.

## Load test output

```
limiter=mine runs=80 processes=4 concurrency/process=8 window_mode=fixed

==================================================================
 LOAD TEST RESULT
==================================================================
 wall time                     32.1 s
 runs completed                  80 / 80
 provider requests              205
 provider 429s                    0   <- must be 0
 provider 500s (injected)         1
 provider hangs (injected)        0
 prompt tokens                29910
 completion tokens             7992
 model calls per run           2.55 mean
 run latency p50 / p99         9.83 / 30.90 s
 cost                        0.0547 USD
 throughput                    2.49 runs/s
 quota utilisation             97.2 %

 outcomes: ok=80
==================================================================

 PASS
```

## Decisions and trade-offs

**Saturation policy: queue.** When fake-pro's quota is exhausted, `acquire()`
blocks until the window resets rather than shedding load or falling back to
fake-flash. Queuing maximises throughput (every run eventually completes) and
keeps the code simple. Fallback would require changing the Limiter interface so
`acquire()` can return a different model — worthwhile if flash is an acceptable
substitute, but an interface change for a three-hour exercise felt wrong without
discussing it first. Shedding load would hurt utilisation. The decision point is
a clearly marked `while True` loop; switching to shed or fallback is a
one-line change plus a comment.

**Safety margins.** Request limit is 84% of advertised (pro: 21/25, flash:
100/120). Token limit is 95%. The request margin absorbs two things: (a) the
50 ms window padding that prevents boundary races, and (b) up to 4 "leaked"
requests from the inter-process Redis flush race at startup. The token margin
is tighter because the prompt estimate is exact and the completion buffer is
small relative to the quota.

**Window padding.** I add 50 ms to each window duration so my window always
expires slightly after the provider's. This prevents the classic fixed-window
race where my window resets first and I admit requests that land in the
provider's still-active (and full) old window. The cost is ~50 ms of wasted
capacity per window, negligible over a 30-second run.

## The alert

**Metric:** A counter of 429s received, incremented in `record_rejection()`.

**Alert:** Page if the counter exceeds zero within a session. Threshold is
zero — any 429 means the limiter is failing its single purpose.

**Justification:** The load test fails on a single 429, and in production
each 429 kills an agent run outright (no retry by design). False positives
are impossible: a 429 is ground truth from the provider.

**Runbook:** Check Redis connectivity (if Redis went down, all four processes
lost coordination and started free-firing). Check whether the provider lowered
its quotas (the MODEL_LIMITS dict in the limiter must match). Review the safety
margins if the problem persists. Inspect `/admin/events` to see the exact
arrival pattern that triggered the rejection.

## Where I do not trust this code

**Startup flush race.** Each process does a synchronous `DEL` of the rate-limit
keys in `build()`. If processes start staggered, one process might acquire a
slot, then another process flushes it. The safety margin (4 spare request slots)
covers this in practice, but it is not provably safe under adversarial
scheduling. A proper fix would be a distributed lock or a session-unique key
prefix, at the cost of more Redis round-trips.

**Server-error token accounting.** When the runner reports (0, 0) for a 5xx,
I refund only the completion buffer, assuming the provider charged prompt tokens
on admission. This is correct for the current provider but depends on the
provider charging on admission rather than on completion — an assumption I
cannot verify against a real provider.

**Clock skew.** I use `time.time()` for Redis timestamps (because `monotonic()`
has a different epoch per process). Wall-clock jumps from NTP could cause a
window to reset early or late. On a single machine this is academic; across
real hosts it would need attention.

## With another three hours

1. **Sliding-window support.** The provider offers a sliding-window mode. My
   fixed-window limiter would over-admit at boundaries. A sorted-set-based
   sliding log in Redis would handle both modes.
2. **Adaptive margins.** Parse the `x-ratelimit-remaining-*` response headers
   and use them to self-correct the limiter's counters, removing the need for
   hand-tuned safety margins.
3. **Fallback policy.** Add a `suggested_model` field to the ticket so the
   runner can downgrade from fake-pro to fake-flash when pro is saturated.
   Requires an interface change and a policy knob (max wait before downgrade).
4. **Prometheus metrics.** Expose acquire wait times, token adjustments, and
   rejection counts as histograms/counters for proper dashboarding instead of
   structured logs.
5. **Per-key quotas.** The provider tracks quotas per (api_key, model), but my
   limiter ignores the API key. Multiple callers sharing Redis would need
   key-scoped counters.

## AI use

Used Claude Code to explore the codebase, design the algorithm, implement the
Lua scripts and Python code, write the tests, and draft these notes. Every line
was reviewed and understood; the key design decisions (fixed-window alignment,
safety margins, completion-token buffering, queue-based saturation) were
deliberate choices evaluated against the provider's actual behavior.
