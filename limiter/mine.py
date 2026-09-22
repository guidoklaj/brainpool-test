"""Redis-backed fixed-window rate limiter for multi-process agent workloads.

Algorithm: fixed-window counters in Redis, one pair (request count, token count)
per model. Lua scripts ensure atomic check-and-increment across processes.

Saturation policy: QUEUE. When a model's quota is exhausted, acquire() blocks
until the window resets. To switch to SHED (fail fast), replace the sleep in
the acquire loop with a raise. To FALLBACK (downgrade to a cheaper model),
return a ticket with a different model and adjust the runner — that requires
an interface change.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger(__name__)

MODEL_LIMITS = {
    "fake-flash": {"requests": 120, "request_window": 5.0, "tokens": 200_000, "token_window": 60.0},
    "fake-pro": {"requests": 25, "request_window": 5.0, "tokens": 30_000, "token_window": 60.0},
}

REQUEST_SAFETY = 0.84
TOKEN_SAFETY = 0.95
WINDOW_PADDING_S = 0.05
COMPLETION_TOKEN_BUFFER = 50
ACQUIRE_TIMEOUT_S = float(os.environ.get("LIMITER_TIMEOUT", "60"))

# ---------------------------------------------------------------------------
# Lua scripts — executed atomically inside Redis
# ---------------------------------------------------------------------------

_ACQUIRE_LUA = """\
local req_key  = KEYS[1]
local tok_key  = KEYS[2]
local req_win  = tonumber(ARGV[1])
local req_lim  = tonumber(ARGV[2])
local tok_win  = tonumber(ARGV[3])
local tok_lim  = tonumber(ARGV[4])
local tok_cost = tonumber(ARGV[5])
local now      = tonumber(ARGV[6])

local function bucket(key, win)
  local s = redis.call('HGET', key, 's')
  local c = redis.call('HGET', key, 'c')
  local start = s and tonumber(s) or 0
  local count = c and tonumber(c) or 0
  if start == 0 or (now - start) >= win then return now, 0 end
  return start, count
end

local rs, rc = bucket(req_key, req_win)
local ts, tc = bucket(tok_key, tok_win)

if (rc + 1) <= req_lim and (tc + tok_cost) <= tok_lim then
  redis.call('HMSET', req_key, 's', rs, 'c', rc + 1)
  redis.call('PEXPIRE', req_key, req_win * 2)
  redis.call('HMSET', tok_key, 's', ts, 'c', tc + tok_cost)
  redis.call('PEXPIRE', tok_key, tok_win * 2)
  return {1, 0, 0}
end

redis.call('HMSET', req_key, 's', rs, 'c', rc)
redis.call('PEXPIRE', req_key, req_win * 2)
redis.call('HMSET', tok_key, 's', ts, 'c', tc)
redis.call('PEXPIRE', tok_key, tok_win * 2)

local rw = 0
if (rc + 1) > req_lim then rw = math.max(0, req_win - (now - rs)) end
local tw = 0
if (tc + tok_cost) > tok_lim then tw = math.max(0, tok_win - (now - ts)) end
return {0, math.ceil(rw), math.ceil(tw)}
"""

_ADJUST_LUA = """\
local key  = KEYS[1]
local diff = tonumber(ARGV[1])
local win  = tonumber(ARGV[2])
local now  = tonumber(ARGV[3])
local s = redis.call('HGET', key, 's')
if not s then return 0 end
if (now - tonumber(s)) >= win then return 0 end
local c = tonumber(redis.call('HGET', key, 'c') or '0') or 0
redis.call('HSET', key, 'c', math.max(0, c + diff))
return 1
"""


class RateLimiter:
    def __init__(self, redis_url: str = "redis://localhost:6380") -> None:
        self._redis = Redis.from_url(redis_url, decode_responses=True)
        self._acq: Any = None
        self._adj: Any = None
        self._rejections = 0
        self._acquires = 0

    def _ensure_scripts(self) -> None:
        if self._acq is None:
            self._acq = self._redis.register_script(_ACQUIRE_LUA)
            self._adj = self._redis.register_script(_ADJUST_LUA)

    async def acquire(self, model: str, estimated_prompt_tokens: int) -> dict:
        self._ensure_scripts()
        limits = MODEL_LIMITS.get(model)
        if limits is None:
            return {"model": model, "est": 0}

        req_lim = int(limits["requests"] * REQUEST_SAFETY)
        tok_lim = int(limits["tokens"] * TOKEN_SAFETY)
        est_total = estimated_prompt_tokens + COMPLETION_TOKEN_BUFFER
        req_win_ms = int((limits["request_window"] + WINDOW_PADDING_S) * 1000)
        tok_win_ms = int((limits["token_window"] + WINDOW_PADDING_S) * 1000)
        req_key = f"rl:{model}:req"
        tok_key = f"rl:{model}:tok"
        deadline = time.monotonic() + ACQUIRE_TIMEOUT_S

        # -- SATURATION POLICY: QUEUE --
        # Block until quota is available.  To shed load, raise here instead.
        # To fall back to fake-flash, return a ticket with model="fake-flash"
        # and have the runner use ticket["model"] (requires interface change).
        while True:
            now_ms = int(time.time() * 1000)
            result = await self._acq(
                keys=[req_key, tok_key],
                args=[req_win_ms, req_lim, tok_win_ms, tok_lim, est_total, now_ms],
            )

            if int(result[0]) == 1:
                self._acquires += 1
                return {"model": model, "est": est_total}

            if time.monotonic() > deadline:
                raise TimeoutError(f"acquire({model}) blocked >{ACQUIRE_TIMEOUT_S}s")

            req_wait = max(int(result[1]), 0)
            tok_wait = max(int(result[2]), 0)
            wait_s = max(req_wait, tok_wait) / 1000
            await asyncio.sleep(max(wait_s * 0.85, 0.010))

    async def record_usage(
        self,
        ticket: Any,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        est = (ticket or {}).get("est", 0)
        if not est:
            return

        actual = prompt_tokens + completion_tokens
        # On server error (0,0) the provider still charged prompt tokens.
        # Only refund the completion buffer we speculatively reserved.
        diff = -COMPLETION_TOKEN_BUFFER if actual == 0 else actual - est

        if diff == 0:
            return

        self._ensure_scripts()
        limits = MODEL_LIMITS.get(model)
        if not limits:
            return
        tok_win_ms = int((limits["token_window"] + WINDOW_PADDING_S) * 1000)
        now_ms = int(time.time() * 1000)
        await self._adj(
            keys=[f"rl:{model}:tok"],
            args=[diff, tok_win_ms, now_ms],
        )

    async def record_rejection(self, model: str, retry_after: float) -> None:
        self._rejections += 1
        # -- THE ALERT --
        # Threshold: any 429 at all.  The limiter's whole job is to prevent
        # these; even one means something is wrong.
        # What to do: check Redis connectivity (if Redis was down, all
        # processes lost coordination). Check whether the provider changed its
        # rate limits (did the quotas shrink?). Review safety margins.
        logger.critical(
            "RATE_LIMIT_BREACH model=%s retry_after=%.3fs breaches=%d | "
            "Page on-call: limiter failed to prevent a 429. "
            "Check Redis connectivity and provider quota changes.",
            model,
            retry_after,
            self._rejections,
        )

    async def aclose(self) -> None:
        if self._rejections:
            logger.critical("session ended with %d rate-limit breaches", self._rejections)
        logger.info("limiter: acquires=%d rejections=%d", self._acquires, self._rejections)
        await self._redis.aclose()


def build() -> RateLimiter:
    import redis as sync_redis

    sr = sync_redis.Redis(host="localhost", port=6380)
    keys = [f"rl:{m}:{d}" for m in MODEL_LIMITS for d in ("req", "tok")]
    sr.delete(*keys)
    sr.close()
    return RateLimiter()
