"""Local notifications and optional Redis wakeups for API replicas."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import os
import time
from uuid import uuid4

from redis.asyncio import Redis
from redis.exceptions import RedisError

_SUBSCRIBERS: dict[int, set[asyncio.Queue]] = {}
_CASE_WAKEUPS: dict[int, set[asyncio.Event]] = {}
MAX_QUEUE = 256
_MAX_MESSAGE = 8192
ACCOUNT_NOTICES = frozenset({'quota.warning', 'runtime.degraded'})
_relay = None


def subscribe(user_id: int) -> asyncio.Queue:
    queue = asyncio.Queue(maxsize=MAX_QUEUE)
    _SUBSCRIBERS.setdefault(user_id, set()).add(queue)
    return queue


def unsubscribe(user_id: int, queue: asyncio.Queue):
    subscribers = _SUBSCRIBERS.get(user_id)
    if subscribers is not None:
        subscribers.discard(queue)
        if not subscribers:
            _SUBSCRIBERS.pop(user_id, None)


@contextmanager
def case_wakeup(case_id: int):
    signal = asyncio.Event()
    signals = _CASE_WAKEUPS.setdefault(case_id, set())
    signals.add(signal)
    try:
        yield signal
    finally:
        signals.remove(signal)
        if not signals:
            _CASE_WAKEUPS.pop(case_id)


def _wake(case_id):
    for signal in _CASE_WAKEUPS.get(case_id, ()):
        signal.set()


def _offer(queue, value):
    if queue.full():
        queue.get_nowait()
    queue.put_nowait(value)


def _local(user_id, event):
    for queue in _SUBSCRIBERS.get(user_id, ()):
        _offer(queue, event)


def _relayable(event):
    if not isinstance(event, dict) or 'event_id' in event:
        return False
    kind, case_id = event.get('type'), event.get('case_id')
    return ((isinstance(kind, str) and kind in ACCOUNT_NOTICES and case_id is None)
            or (kind == 'scene.available' and type(case_id) is int and case_id > 0))


def publish(user_id: int, event: dict):
    event = {**event, 'ts': event.get('ts', time.time())}
    _local(user_id, event)
    case_id = event.get('case_id')
    durable = event.get('event_id') and type(case_id) is int and case_id > 0
    if durable:
        _wake(case_id)
        # The committed database page supplies the event and its access checks.
        if _relay is not None:
            _relay.offer(case_id=case_id, seq=event.get('seq'))
    elif _relay is not None and _relayable(event):
        _relay.offer(user_id=user_id, event=event)


class _RedisRelay:
    def __init__(self, client):
        self.client = client
        namespace = os.getenv('VIBEFLOW_EVENT_BUS_NAMESPACE') or 'vibeflow'
        self.channel = f'{namespace}:eventbus:v1'
        self.origin = uuid4().hex
        self.pending = asyncio.Queue(maxsize=MAX_QUEUE)
        self.tasks = (asyncio.create_task(self.send()), asyncio.create_task(self.receive()))

    def offer(self, **payload):
        wire = json.dumps({'origin': self.origin, **payload}, ensure_ascii=False).encode('utf-8')
        if len(wire) <= _MAX_MESSAGE:
            _offer(self.pending, wire)

    async def send(self):
        while True:
            wire = await self.pending.get()
            try:
                await self.client.publish(self.channel, wire)
            except (RedisError, OSError):
                await asyncio.sleep(1)

    async def receive(self):
        while True:
            try:
                async with self.client.pubsub() as stream:
                    await stream.subscribe(self.channel)
                    while True:
                        message = await stream.get_message(ignore_subscribe_messages=True, timeout=1)
                        if message is not None:
                            self.deliver(message['data'])
            except (RedisError, OSError):
                await asyncio.sleep(1)

    def deliver(self, wire):
        if not isinstance(wire, bytes) or len(wire) > _MAX_MESSAGE:
            return
        try:
            message = json.loads(wire)
        except (ValueError, RecursionError):
            return
        if not isinstance(message, dict) or message.get('origin') == self.origin:
            return
        case_id = message.get('case_id')
        if type(case_id) is int and case_id > 0:
            _wake(case_id)
            return
        user_id, event = message.get('user_id'), message.get('event')
        if type(user_id) is int and user_id > 0 and _relayable(event):
            _local(user_id, event)

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.client.aclose()


def start():
    from .distributed_queue import distributed_enabled, redis_settings

    global _relay
    if _relay is None and distributed_enabled():
        _relay = _RedisRelay(Redis(host=redis_settings.host, port=redis_settings.port,
                                 password=redis_settings.password, db=redis_settings.database,
                                 socket_connect_timeout=1, socket_timeout=2))


async def close():
    global _relay
    if _relay is not None:
        relay, _relay = _relay, None
        await relay.close()
