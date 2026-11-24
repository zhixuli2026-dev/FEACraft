"""Append and verify an organization audit chain inside shared SQL transactions."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from sqlalchemy import select, update

from ..audit_models import OrganizationAuditHead as Head, OrganizationAuditRecord as Record
from ..models import User, Workspace
from .redaction import redact_text


AUDIT_DIR = Path(os.getenv('VIBEFLOW_AUDIT_DIR', Path(__file__).resolve().parents[2] / 'data' / 'audit'))
LEGACY_SCHEMA = 'vibeflow.audit.v1'
SCHEMA = 'vibeflow.audit.v2'
_ACTION = re.compile(r'[a-z][a-z0-9_.:-]{1,80}')
_PRIVATE = re.compile(r'(?:password|secret|token|ciphertext|api_?key|authorization|cookie)', re.I)


def _clean_detail(detail):
    if detail is None:
        return {}
    if not isinstance(detail, dict) or len(detail) > 20:
        raise ValueError('审计 detail 必须是最多 20 项的平面对象')
    clean = {}
    for key, value in detail.items():
        if not isinstance(key, str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_]{0,40}', key):
            raise ValueError('审计 detail 键无效')
        if _PRIVATE.search(key):
            continue
        if isinstance(value, str):
            clean[key] = redact_text(value, limit=300)
        elif value is None or type(value) in (int, bool):
            clean[key] = value
        else:
            raise ValueError('审计 detail 只允许字符串/整数/bool/null')
    return clean


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _hash(event):
    return hashlib.sha256(_json({key: value for key, value in event.items() if key != 'event_hash'}).encode('utf8')).hexdigest()


def _created(event):
    created = datetime.fromisoformat(event['created_at'])
    if created.utcoffset() is None:
        raise ValueError('Audit timestamp lacks timezone')
    return created.astimezone(timezone.utc)


def _valid(event, org_id, sequence, previous):
    return (isinstance(event, dict) and event.get('org_id') == org_id and
            event.get('schema') in (LEGACY_SCHEMA, SCHEMA) and
            (event['schema'] == LEGACY_SCHEMA or event.get('sequence') == sequence) and
            type(event.get('actor_user_id')) is int and event['actor_user_id'] > 0 and
            isinstance(event.get('action'), str) and _ACTION.fullmatch(event['action']) and
            event.get('previous_hash') == previous and event.get('event_hash') == _hash(event))


def _legacy(org_id):
    path = AUDIT_DIR / f'org-{org_id}.jsonl'
    events = []
    previous = ''
    try:
        with path.open(encoding='utf8') as stream:
            for line in stream:
                event = json.loads(line)
                if not _valid(event, org_id, len(events)+1, previous) or event['schema'] != LEGACY_SCHEMA:
                    return events, False
                _created(event)
                _clean_detail(event.get('detail'))
                events.append(event)
                previous = event['event_hash']
    except FileNotFoundError:
        return [], True
    except (OSError, KeyError, TypeError, ValueError):
        return events, False
    return events, True


def _record(org_id, sequence, event):
    return Record(org_id=org_id, sequence=sequence, actor_user_id=event['actor_user_id'],
                  action=event['action'], created_at=_created(event), payload=_json(event))


async def _lock_head(db, org_id):
    if db.get_bind().dialect.name == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy.dialects.postgresql import insert
    await db.execute(insert(Head).values(org_id=org_id, sequence=0, digest='', legacy_valid=True)
                     .on_conflict_do_nothing(index_elements=[Head.org_id]))
    await db.execute(update(Head).where(Head.org_id == org_id).values(sequence=Head.sequence))
    return await db.get(Head, org_id, populate_existing=True)


async def append(db, org_id, actor_user_id, action, resource_type, resource_id, detail=None):
    """Record a successful admission or mutation in the caller's transaction."""
    if not _ACTION.fullmatch(action):
        raise ValueError('审计 action 无效')
    head = await _lock_head(db, org_id)
    if head.sequence == 0:
        prior, head.legacy_valid = await asyncio.to_thread(_legacy, org_id)
        for sequence, item in enumerate(prior, 1):
            db.add(_record(org_id, sequence, item))
        if prior:
            head.sequence = len(prior)
            head.digest = prior[-1]['event_hash']
    sequence = head.sequence + 1
    event = {'schema': SCHEMA, 'event_id': uuid.uuid4().hex, 'org_id': org_id,
             'sequence': sequence, 'actor_user_id': actor_user_id, 'action': action,
             'resource_type': redact_text(resource_type, limit=60),
             'resource_id': redact_text(str(resource_id), limit=100), 'detail': _clean_detail(detail),
             'created_at': datetime.now(timezone.utc).isoformat(), 'previous_hash': head.digest}
    event['event_hash'] = _hash(event)
    db.add(_record(org_id, sequence, event))
    head.sequence, head.digest = sequence, event['event_hash']
    return event


async def user_event(db, actor, action, resource_type, resource_id, detail=None):
    from .quota import ensure_user_org

    org = await ensure_user_org(db, actor)
    return await append(db, org.id, actor.id, action, resource_type, resource_id, detail)


async def case_event(db, case, actor_user_id, action, resource_type, resource_id, detail=None):
    from .quota import ensure_user_org

    owner = await db.scalar(select(User).join(Workspace).where(Workspace.id == case.workspace_id))
    org = await ensure_user_org(db, owner)
    return await append(db, org.id, actor_user_id, action, resource_type, resource_id,
                        {**(detail or {}), 'case_id': case.id})


async def session_event(db, case_id, actor_user_id, action, resource_type, resource_id, detail=None):
    if case_id is None:
        return await user_event(db, await db.get(User, actor_user_id), action, resource_type, resource_id, detail)
    from ..models import Case
    return await case_event(db, await db.get(Case, case_id), actor_user_id, action, resource_type, resource_id, detail)


async def record_request(case_id, actor_user_id, action, resource_type, resource_id):
    from ..db import SessionLocal

    async with SessionLocal.begin() as db:
        await session_event(db, case_id, actor_user_id, action, resource_type, resource_id)


def _public(event):
    # Legacy documents were sealed before field redaction was introduced.
    return {**event, 'detail': _clean_detail(event.get('detail'))}


async def query(db, org_id, action=None, actor_user_id=None, offset=0, limit=50):
    head = await db.get(Head, org_id)
    if head is None:
        prior, valid = await asyncio.to_thread(_legacy, org_id)
        selected = [item for item in reversed(prior) if (not action or item['action'] == action) and
                    (actor_user_id is None or item['actor_user_id'] == actor_user_id)]
        rows = [_public(item) for item in selected[offset:offset+limit]]
        count = len(selected)
        sequence = len(prior)
        digest = prior[-1]['event_hash'] if prior else ''
    else:
        sequence, digest, valid = head.sequence, head.digest, head.legacy_valid
        previous, ordinal, count = '', 0, 0
        rows = []
        # Scan backwards at a fixed head to verify the chain while returning only this page.
        expected = digest
        records = await db.stream_scalars(select(Record).where(Record.org_id == org_id, Record.sequence <= sequence)
                                          .order_by(Record.sequence.desc()).execution_options(yield_per=128))
        try:
            async for row in records:
                try:
                    event = json.loads(row.payload)
                    previous = event.get('previous_hash')
                    valid = (bool(_valid(event, org_id, row.sequence, previous)) and valid and
                             row.sequence == sequence-ordinal and event['event_hash'] == expected and
                             row.action == event['action'] and row.actor_user_id == event['actor_user_id'] and
                             row.created_at.replace(tzinfo=row.created_at.tzinfo or timezone.utc) == _created(event))
                    expected = previous
                    if ((not action or row.action == action) and
                            (actor_user_id is None or row.actor_user_id == actor_user_id)):
                        if offset <= count < offset+limit:
                            rows.append(_public(event))
                        count += 1
                except (AttributeError, KeyError, TypeError, ValueError):
                    valid = False
                ordinal += 1
        finally:
            await records.close()
        valid = valid and ordinal == sequence and expected == ''
    return {'schema': 'vibeflow.audit.page.v1', 'org_id': org_id, 'chain_valid': valid, 'total': count,
            'offset': offset, 'limit': limit, 'has_more': offset+len(rows) < count,
            'head_sequence': sequence, 'head_hash': digest, 'events': rows}
