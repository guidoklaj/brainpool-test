# Take-home: rate limiting agent calls

**Time budget: 3 hours.** Please stop at three hours even if you are not
finished. We would much rather read an honest `NOTES.md` about a partial
solution than see a rushed complete one, and "what I would do next" is a graded
part of the exercise rather than an apology.

There is a follow-up call where you walk us through what you built.

## The situation

An agent answers questions using tools. A single agent run is not one API call
to the model -- the model asks for a tool, we run it, we feed the result back,
and it asks again. In this repo a run takes **between one and four model calls**,
and each call carries the whole conversation so far, so the calls get larger as
the run goes on.

The provider we call meters us on two axes at once, per model:

| model        | requests   | tokens        |
|--------------|------------|---------------|
| `fake-flash` | 120 / 5s   | 200,000 / 60s |
| `fake-pro`   | 25 / 5s    | 30,000 / 60s  |

Token quota counts prompt **and** completion tokens, and the completion half is
only charged once the response exists. Breaching either axis returns `429` with
a `Retry-After` header. Every response carries `x-ratelimit-*` headers.

Right now we run four worker processes and simply fire requests at it. We get
rate limited constantly, and a rate-limited run fails outright -- the agent
runner does not retry a 429, by design.

## Your task

Make `make loadtest` pass, by writing something that sits between the agent and
the provider.

```bash
make install      # venv + dependencies
make up           # redis on 6380, fake provider on 8080
make test         # unit tests
make loadtest     # THE GATE -- fails before you start
```

The gate has two conditions:

1. **Zero 429s.** Not "few". The provider's own counters are the referee, not
   ours -- see `make stats`.
2. **Quota utilisation at or above 50%.** Throttling everything down to a
   trickle is not a solution. Above 80% is a good result. Over 100% is possible,
   because the provider's window allows an initial burst.

Everything else the load test prints is for your judgement, not the gate.

### Where the code goes

`limiter/base.py` defines the interface `agent/runner.py` already calls, and the
`NoopLimiter` that admits everything. Add `limiter/mine.py` with a `build()`
function and run `LIMITER=mine make loadtest`.

**You may change that interface.** If you do, say why in `NOTES.md` -- where you
draw the boundary is something we want to talk about.

### What we would like you to cover

- **It has to work across processes.** The load test runs four of them, each
  with its own limiter instance. Redis is in the compose file for this reason.
  A limiter that only coordinates inside one process will not pass.
- **Both quota axes, per model.** The two models have unrelated limits and one
  is far tighter than the other. Work on `fake-pro` must not stall `fake-flash`.
- **Tokens, not just request counts.** You have to commit to a number before you
  know the real one; the provider tells you what it actually charged, in the
  `usage` block on the response. What you do with that gap is up to you.
- **Decide what happens when `fake-pro` is saturated** -- queue, shed load, or
  fall back to `fake-flash`. We do not have a preferred answer. We do want the
  choice to be deliberate, visible in the code, and easy to change.
- **One alert.** Emit whatever metrics you think matter, pick the single alert
  you would actually page someone for, and justify the threshold. Then write
  down what the person receiving it should do. Any mechanism is fine -- Prometheus,
  OTel, structured logs, a counter you print at exit.
- **Tests.** `tests/test_limiter_contract.py` has a few we already care about;
  they skip until you point `LIMITER` at your implementation.

### Explicitly not wanted

No auth, no persistence beyond Redis, no UI, no dashboards, no multi-provider
abstraction. If you find yourself building a framework, that is the wrong
direction for a three-hour exercise.

## Things that will save you time

- **`make stats` and `/admin/events`.** The provider records every request it
  saw, with arrival offsets and which quota refused it. If you get rejections
  you cannot explain, read that before you start guessing.
- **The provider's clock is not your clock.** You time a request when you send
  it; the provider times it when it arrives. That gap is small and it matters.
- **The provider is deliberately unreliable.** Roughly 1% of calls return 500,
  and about 1 in 200 hangs for longer than the client will wait. Both are
  expected; the load test reports them separately and does not fail you for them.
  A run killed by a timeout is fine. Quota you can never reclaim afterwards is
  less fine.
- **Iterate with a smaller run:** `.venv/bin/python -m loadtest.run --runs 20`.
  Utilisation is only meaningful on a full-length run, so the gate ignores it
  below 20 seconds.
- `FAKE_PROVIDER_WINDOW=sliding` changes how the provider accounts for its
  windows. You do not need to handle it, but you may find it interesting.

## Using AI

Use whatever you normally use -- we do, and we are not interested in a
handwriting test. Two conditions: you need to be able to defend every line on
the call, and `NOTES.md` should say briefly what you leaned on it for.

## What to send back

The repo, with:

- your limiter, and tests for it
- `NOTES.md` (there is a template -- one page is plenty)
- a passing `make loadtest`, with the output pasted into `NOTES.md`

If `make loadtest` does not pass, send it anyway and tell us where you got to.
