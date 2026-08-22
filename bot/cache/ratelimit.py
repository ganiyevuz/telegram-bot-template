from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from redis.commands.core import AsyncScript

# Refill and consume atomically. Redis' own TIME is the clock, so replicas with
# skewed system clocks cannot disagree about how full a bucket is.
_LUA = """
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local requested = tonumber(ARGV[3])

local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000

local bucket = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(bucket[1])
local ts = tonumber(bucket[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

tokens = math.min(capacity, tokens + math.max(0, now - ts) * rate)

local wait = 0
if tokens >= requested then
  tokens = tokens - requested
else
  wait = (requested - tokens) / rate
end

redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', KEYS[1], math.ceil((capacity / rate) * 2000))
return tostring(wait)
"""


class TokenBucket:
    """Distributed token bucket. One bucket per key, shared by every process."""

    def __init__(self, redis: Redis, rate: float, capacity: float, name: str) -> None:
        self._rate = rate
        self._capacity = capacity
        self._name = name
        self._script: AsyncScript = redis.register_script(_LUA)

    async def acquire(self, key: str, tokens: float = 1.0) -> float:
        """Return seconds to wait before `tokens` are available. 0.0 means granted."""
        raw = await self._script(keys=[key], args=[self._rate, self._capacity, tokens])
        return float(raw)

    async def try_acquire(self, key: str, tokens: float = 1.0) -> bool:
        return await self.acquire(key, tokens) == 0.0
