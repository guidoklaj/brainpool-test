"""The load test. This is the target you are working against.

It fans agent runs across several OS processes, each with its own event loop,
its own HTTP client and its own limiter instance -- so a limiter that only
coordinates within one process will not pass. State has to be shared, and Redis
is running in the compose file for exactly that reason.

Run it with ``make loadtest``. It exits non-zero if any 429 reached an agent, or
if throughput collapsed. Everything else it prints is for your judgement, not
for the gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from agent.runner import ProviderError, ProviderRateLimited, run_agent
from limiter import build_limiter

DEFAULT_BASE_URL = os.environ.get("PROVIDER_URL", "http://localhost:8080")
DEFAULT_API_KEY = os.environ.get("PROVIDER_API_KEY", "interview-key")

FAIL_UTILISATION = 0.5
WARN_UTILISATION = 0.8
MIN_SECONDS_FOR_UTILISATION_GATE = 20.0
"""Below this the fixed-window burst allowance dominates and utilisation is
meaningless -- it can read well over 100%. Short runs are for iterating; the
gate only applies to a full-length run."""


@dataclass
class Record:
    index: int
    model: str
    ok: bool
    outcome: str
    model_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0


async def _one_run(client: httpx.AsyncClient, limiter: Any, index: int, model: str) -> Record:
    prompt = f"Question {index}: what is the quota policy for this account?"
    try:
        result = await run_agent(prompt, client=client, limiter=limiter, model=model)
    except ProviderRateLimited:
        return Record(index, model, False, "rate_limited")
    except ProviderError:
        return Record(index, model, False, "provider_error")
    except httpx.TimeoutException:
        return Record(index, model, False, "timeout")
    except Exception as exc:  # noqa: BLE001 -- a candidate bug should be visible, not fatal
        return Record(index, model, False, f"exception:{type(exc).__name__}")

    return Record(
        index=index,
        model=model,
        ok=True,
        outcome="ok",
        model_calls=result.model_calls,
        prompt_tokens=result.prompt_tokens,
        completion_tokens=result.completion_tokens,
        cost_usd=result.cost_usd,
        duration_s=result.duration_s,
    )


async def _worker_async(
    jobs: list[tuple[int, str]], concurrency: int, base_url: str, api_key: str
) -> list[dict]:
    limiter = build_limiter()
    semaphore = asyncio.Semaphore(concurrency)
    records: list[Record] = []

    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        limits=httpx.Limits(max_connections=concurrency * 2),
    ) as client:

        async def guarded(index: int, model: str) -> Record:
            async with semaphore:
                return await _one_run(client, limiter, index, model)

        results = await asyncio.gather(*(guarded(i, m) for i, m in jobs))
        records.extend(results)

    await limiter.aclose()
    return [asdict(r) for r in records]


def _worker(payload: tuple[list[tuple[int, str]], int, str, str]) -> list[dict]:
    jobs, concurrency, base_url, api_key = payload
    return asyncio.run(_worker_async(jobs, concurrency, base_url, api_key))


def _plan_jobs(runs: int, pro_share: float) -> list[tuple[int, str]]:
    jobs = []
    for index in range(runs):
        model = "fake-pro" if (index % 10) < round(pro_share * 10) else "fake-flash"
        jobs.append((index, model))
    return jobs


def _shard(jobs: list[tuple[int, str]], processes: int) -> list[list[tuple[int, str]]]:
    shards: list[list[tuple[int, str]]] = [[] for _ in range(processes)]
    for position, job in enumerate(jobs):
        shards[position % processes].append(job)
    return shards


def _advertised(stats: dict[str, Any], model: str) -> float:
    limits = stats["models"][model]
    return limits["requests"] / limits["request_window"]


def main() -> int:
    parser = argparse.ArgumentParser(description="Load test the agent against the fake provider.")
    parser.add_argument("--runs", type=int, default=int(os.environ.get("RUNS", "80")))
    parser.add_argument("--processes", type=int, default=int(os.environ.get("PROCESSES", "4")))
    parser.add_argument("--concurrency", type=int, default=int(os.environ.get("CONCURRENCY", "8")),
                        help="concurrent agent runs per process")
    parser.add_argument("--pro-share", type=float, default=float(os.environ.get("PRO_SHARE", "0.7")),
                        help="fraction of runs sent to the tightly limited model")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--api-key", default=DEFAULT_API_KEY)
    parser.add_argument("--json", dest="json_out", default=os.environ.get("LOADTEST_JSON"),
                        help="also write the full result set to this path")
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url, timeout=10.0) as admin:
        try:
            admin.post("/admin/reset").raise_for_status()
            before = admin.get("/admin/stats").json()
        except httpx.HTTPError as exc:
            print(f"cannot reach the fake provider at {args.base_url}: {exc}", file=sys.stderr)
            print("start it with `make up`", file=sys.stderr)
            return 2

    jobs = _plan_jobs(args.runs, args.pro_share)
    shards = _shard(jobs, args.processes)

    print(
        f"limiter={os.environ.get('LIMITER', 'noop')} runs={args.runs} "
        f"processes={args.processes} concurrency/process={args.concurrency} "
        f"window_mode={before['window_mode']}"
    )

    started = time.monotonic()
    with ProcessPoolExecutor(max_workers=args.processes) as pool:
        shard_results = list(
            pool.map(_worker, [(shard, args.concurrency, args.base_url, args.api_key) for shard in shards])
        )
    elapsed = time.monotonic() - started

    records = [Record(**row) for shard in shard_results for row in shard]

    with httpx.Client(base_url=args.base_url, timeout=10.0) as admin:
        after = admin.get("/admin/stats").json()
    counters = after["counters"]

    ok = [r for r in records if r.ok]
    outcomes: dict[str, int] = {}
    for record in records:
        outcomes[record.outcome] = outcomes.get(record.outcome, 0) + 1

    # How long the provider's advertised sustained rate would have needed to
    # serve exactly the requests it actually admitted. Anything above that is
    # burst allowance, not throughput you can rely on.
    ideal_seconds = sum(
        count / _advertised(after, model)
        for model, count in after["admitted_by_model"].items()
    )
    utilisation = (ideal_seconds / elapsed) if elapsed > 0 else 0.0
    gate_utilisation = elapsed >= MIN_SECONDS_FOR_UTILISATION_GATE

    latencies = sorted(r.duration_s for r in ok)

    def pct(fraction: float) -> float:
        if not latencies:
            return 0.0
        return latencies[min(int(fraction * len(latencies)), len(latencies) - 1)]

    print()
    print("=" * 66)
    print(" LOAD TEST RESULT")
    print("=" * 66)
    print(f" wall time                 {elapsed:8.1f} s")
    print(f" runs completed            {len(ok):8d} / {len(records)}")
    print(f" provider requests         {counters['requests']:8d}")
    print(f" provider 429s             {counters['rate_limited']:8d}   <- must be 0")
    print(f" provider 500s (injected)  {counters['errors']:8d}")
    print(f" provider hangs (injected) {counters['hangs']:8d}")
    print(f" prompt tokens             {counters['prompt_tokens']:8d}")
    print(f" completion tokens         {counters['completion_tokens']:8d}")
    if ok:
        print(f" model calls per run       {statistics.mean(r.model_calls for r in ok):8.2f} mean")
        print(f" run latency p50 / p99     {pct(0.50):8.2f} / {pct(0.99):.2f} s")
        print(f" cost                      {sum(r.cost_usd for r in ok):8.4f} USD")
    print(f" throughput                {len(ok) / elapsed if elapsed else 0:8.2f} runs/s")
    print(f" quota utilisation         {utilisation * 100:8.1f} %"
          + ("" if gate_utilisation else "   (run too short to be meaningful)"))
    print()
    print(" outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(outcomes.items())))
    print("=" * 66)

    if args.json_out:
        with open(args.json_out, "w") as handle:
            json.dump(
                {
                    "elapsed_s": elapsed,
                    "utilisation": utilisation,
                    "provider": after,
                    "records": [asdict(r) for r in records],
                },
                handle,
                indent=2,
            )
        print(f" full results written to {args.json_out}")

    failures = []
    if counters["rate_limited"] > 0:
        failures.append(f"{counters['rate_limited']} requests were rate limited by the provider")
    leaked = outcomes.get("rate_limited", 0)
    if leaked:
        failures.append(f"{leaked} agent runs failed with a 429")
    if gate_utilisation and utilisation < FAIL_UTILISATION:
        failures.append(
            f"quota utilisation {utilisation * 100:.1f}% is below the {FAIL_UTILISATION * 100:.0f}% floor "
            "-- the limiter is leaving most of the quota unused"
        )

    if failures:
        print()
        print(" FAIL")
        for failure in failures:
            print(f"   - {failure}")
        return 1

    print()
    if gate_utilisation and utilisation < WARN_UTILISATION:
        print(f" PASS (with a warning: utilisation {utilisation * 100:.1f}% is on the low side)")
    else:
        print(" PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
