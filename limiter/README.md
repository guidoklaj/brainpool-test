# limiter/

Your work goes here.

`base.py` defines the interface `agent/runner.py` calls, and `NoopLimiter`, which
admits everything. That is why the load test fails before you have written
anything.

To add your own:

1. Create `limiter/mine.py` with a module-level `build()` that returns your
   limiter.
2. Run it: `LIMITER=mine make loadtest`.

You may change the interface in `base.py`. If you do, note why in `NOTES.md` --
where you draw that boundary is part of what we are looking at.
