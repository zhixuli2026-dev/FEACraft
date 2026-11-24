"""Native usage snapshots, not a billing ledger or a per-turn token estimate."""
import json

from sqlalchemy import select

from ..models import NativeCliEvent

COUNTERS = ('inputTokens', 'cachedInputTokens', 'outputTokens', 'reasoningOutputTokens', 'totalTokens')
MAX_COUNTER = 2 ** 53 - 1  # Exact integers in the browser as well as Python.


def counter(value):
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise ValueError('Invalid native token counter')
    return value


def normalize(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid native usage snapshot')
    result = {}
    for scope in ('last', 'total'):
        source = value.get(scope)
        if not isinstance(source, dict):
            raise ValueError('Invalid native usage breakdown')
        result[scope] = {key: counter(source.get(key)) for key in COUNTERS}
        result[scope]['cacheWriteInputTokens'] = counter(source.get('cacheWriteInputTokens', 0))
    window = value.get('modelContextWindow')
    result['modelContextWindow'] = None if window is None else counter(window)
    return result


async def record(db, owner, params):
    from . import codex_turn_records as records
    row = await records.current(db, owner)
    # The general notification dispatcher tolerates absent correlation fields;
    # token counts must explicitly identify this registered native attempt.
    if (row.native_turn_id is None or params.get('threadId') != row.thread_id
            or params.get('turnId') != row.native_turn_id):
        return
    payload = {'thread_id': row.thread_id, 'native_turn_id': row.native_turn_id,
               'attempt': len(json.loads(row.attempts_json)), 'source': 'native_cli'}
    try:
        payload.update(status='reported', **normalize(params.get('tokenUsage')))
    except ValueError:
        # Bad telemetry does not turn a successfully delivered answer into a
        # failed run. It also cannot silently leave older counts looking current.
        payload['status'] = 'invalid_report'
    previous = await db.scalar(select(NativeCliEvent).where(NativeCliEvent.turn_id == row.id,
        NativeCliEvent.type == 'usage.updated').order_by(NativeCliEvent.seq.desc()).limit(1))
    if previous is not None and json.loads(previous.payload_json) == payload:
        return
    await records.append(db, row, 'usage.updated', payload)


async def latest(db, row):
    value = {'status': 'not_reported', 'source': 'native_cli', 'seq': row.next_seq - 1}
    event = await db.scalar(select(NativeCliEvent).where(NativeCliEvent.turn_id == row.id,
        NativeCliEvent.type == 'usage.updated', NativeCliEvent.seq < row.next_seq)
        .order_by(NativeCliEvent.seq.desc()).limit(1))
    if event is None:
        return value
    try:
        payload = json.loads(event.payload_json)
        attempts = json.loads(row.attempts_json)
        if not isinstance(payload, dict) or not isinstance(attempts, list):
            raise ValueError('Invalid usage receipt')
        # Prior failed attempts remain in the event stream. A new attempt with
        # no report has unknown usage, never the previous attempt's counters.
        if (payload.get('native_turn_id') != row.native_turn_id
                or payload.get('attempt') != len(attempts)):
            return value
        if (payload.get('thread_id') != row.thread_id or payload.get('source') != 'native_cli'
                or type(payload.get('attempt')) is not int or not 1 <= payload['attempt'] <= 6):
            raise ValueError('Invalid usage binding')
        binding = {key: payload[key] for key in ('thread_id', 'native_turn_id', 'attempt', 'source')}
        if payload.get('status') == 'reported':
            return {**binding, 'status': 'reported', 'seq': event.seq, **normalize(payload)}
        if payload.get('status') != 'invalid_report':
            raise ValueError('Invalid usage status')
        return {**binding, 'status': 'invalid_report', 'seq': event.seq}
    except (ValueError, TypeError, KeyError):
        return {**value, 'status': 'invalid_record'}
