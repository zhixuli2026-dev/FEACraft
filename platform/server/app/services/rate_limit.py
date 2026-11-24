"""Per-client entrance budgets and provider budgets shared by API replicas."""
from __future__ import annotations

import math
import os
import time

from fastapi import Request

from ..security import decode_token


def _limit(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(1, min(value, 1_000_000))


API_LIMIT = _limit("VIBEFLOW_RATE_LIMIT_API_PER_MINUTE", 1200)
LLM_LIMIT = _limit("VIBEFLOW_RATE_LIMIT_LLM_PER_MINUTE", 120)
PROFILE_LIMIT = _limit("VIBEFLOW_RATE_LIMIT_PROFILE_PER_MINUTE", LLM_LIMIT)
BACKEND = os.getenv('VIBEFLOW_RATE_LIMIT_BACKEND', 'memory')
if BACKEND not in {'redis', 'memory'}:
    raise ValueError('VIBEFLOW_RATE_LIMIT_BACKEND must be memory or redis')
NAMESPACE = os.getenv('VIBEFLOW_RATE_LIMIT_NAMESPACE', 'vibeflow:rate:v1')

_REDIS_BUCKET = '''
local capacity = tonumber(ARGV[1])
local now = redis.call('TIME')
local ms = now[1] * 1000 + math.floor(now[2] / 1000)
local previous = redis.call('HMGET', KEYS[1], 'balance', 'at')
local at = tonumber(previous[2]) or ms
local balance = math.min(capacity, (tonumber(previous[1]) or capacity)
                         + math.max(0, ms - at) * capacity / 60000)
local retry = 0
if balance < 1 then
    retry = math.ceil((1 - balance) * 60 / capacity)
else
    balance = balance - 1
end
redis.call('HSET', KEYS[1], 'balance', balance, 'at', math.max(at, ms))
redis.call('PEXPIRE', KEYS[1], 120000)
return retry
'''


class RateLimited(Exception):
    def __init__(self, retry_after: int, category: str):
        self.retry_after = retry_after
        self.category = category


class LimiterUnavailable(Exception):
    pass


_BUCKETS: dict[tuple[str, str], list[float]] = {}
_redis = None


def stats() -> dict:
    return {"buckets": len(_BUCKETS), "api_per_minute": API_LIMIT,
            "llm_per_minute": LLM_LIMIT, "profile_per_minute": PROFILE_LIMIT, "backend": BACKEND}


async def close():
    global _redis
    if _redis is not None:
        client, _redis = _redis, None
        await client.aclose()


async def _redis_take(identity, scope, limit):
    from redis.asyncio import Redis
    from redis.exceptions import RedisError

    global _redis
    if _redis is None:
        _redis = Redis(host=os.getenv('VIBEFLOW_REDIS_HOST', '127.0.0.1'),
                       port=int(os.getenv('VIBEFLOW_REDIS_PORT', '6380')),
                       password=os.getenv('VIBEFLOW_REDIS_PASSWORD') or None,
                       socket_connect_timeout=1, socket_timeout=1)
    try:
        return int(await _redis.eval(_REDIS_BUCKET, 1, f'{NAMESPACE}:{scope}:{identity}', limit))
    except RedisError as error:
        raise LimiterUnavailable from error


def _memory_take(identity, category, limit):
    key = (identity, category)
    now = time.monotonic()
    bucket = _BUCKETS.get(key)
    if bucket is None:
        bucket = [float(limit), now]
        _BUCKETS[key] = bucket
    bucket[0] = min(float(limit), bucket[0] + (now - bucket[1]) * limit / 60.0)
    bucket[1] = now
    if bucket[0] < 1.0:
        return max(1, math.ceil((1.0 - bucket[0]) * 60.0 / limit))
    bucket[0] -= 1.0
    if len(_BUCKETS) > 10000:
        for old_key, _ in sorted(_BUCKETS.items(), key=lambda item: item[1][1])[:1000]:
            _BUCKETS.pop(old_key, None)
    return 0


async def _take(identity, scope, limit, category):
    retry = await _redis_take(identity, scope, limit) if BACKEND == 'redis' else _memory_take(identity, scope, limit)
    if retry:
        raise RateLimited(retry, category)


async def _client_identity(request: Request):
    from ..db import SessionLocal
    from . import siwc_protocol, website_auth

    authorization = request.headers.get('authorization', '')
    scheme, _, token = authorization.partition(' ')
    if scheme.lower() == 'bearer':
        uid = decode_token(token.strip())
        if uid is not None:
            return f'user:{uid}'
    elif raw := request.cookies.get(siwc_protocol.SESSION_COOKIE):
        async with SessionLocal() as db:
            user = await website_auth.cookie_user(db, raw)
            if user is not None:
                return f'user:{user.id}'
    # Uvicorn sets this from the actual peer or an explicitly trusted proxy.
    return f'ip:{request.client.host if request.client else "unknown"}'


async def check(request: Request):
    path = request.url.path
    if not path.startswith('/api/') or path == '/api/health':
        return
    category = 'llm' if path.startswith(('/api/v1/llm', '/api/v1/conversations')) else 'api'
    await _take(await _client_identity(request), category, LLM_LIMIT if category == 'llm' else API_LIMIT, category)


async def provider(user_id: int, profile_id: int):
    """Consume one outbound provider operation after the profile ACL succeeds."""
    await _take(f'user:{user_id}:profile:{profile_id}', 'profile', PROFILE_LIMIT, 'provider')
