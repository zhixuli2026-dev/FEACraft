"""Explicit history links; never starts, resumes, stops or inherits native execution."""
import hashlib
import json
import re
import uuid

from sqlalchemy import or_, select

from ..db import SessionLocal
from ..models import NativeSessionLink
from . import native_session_review as review
from .async_ops import finish_before_cancel
from .durable_events import json_text, now
from .storage_admission import lock_owner
from .workflow_state import ExecutionConflict

SCHEMA = 'vibeflow.native-session-link.v1'
ACTION = 'link_existing_successor'


def identifier(kind, successor_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'vibeflow.native-session-successor:' + kind + ':' + successor_id))


def identity_digest(kind, row):
    # Heartbeats and completion may advance while an operator reads the form.
    # The selected identity is immutable; the full current row is also captured
    # in the audit at commit. Nothing is inherited from either snapshot.
    value = {'kind': kind, 'id': str(row.id), 'case_id': row.case_id, 'created_by': row.created_by,
        'created_at': review.timestamp(row.created_at), 'owner_id': row.owner_id,
        'owner_identity_json': row.owner_identity_json, 'launch_nonce': row.launch_nonce,
        'profile_json': row.profile_json}
    if kind == 'terminal': value['shell'] = row.shell
    if kind == 'cli': value.update(runtime=row.runtime, conversation_id=row.conversation_id)
    return hashlib.sha256(json_text(value).encode('utf8')).hexdigest()


def check_pair(kind, source, successor):
    if source.id == successor.id or source.case_id != successor.case_id or source.created_by != successor.created_by:
        raise ExecutionConflict('session_lineage_conflict', '只能关联同一创建者、同一算例或同一未绑定范围的不同会话')
    if (review.timestamp(source.created_at), source.id) >= (review.timestamp(successor.created_at), successor.id):
        raise ExecutionConflict('session_lineage_conflict', '后续会话必须晚于原会话，不能建立循环关联')
    if review.state(kind, successor)['phase'] == 'evidence_invalid':
        raise ExecutionConflict('session_lineage_unverified', '后续会话的原登记无法核验，请先核对其记录')


def receipt(row, kind, user_id):
    try:
        value = json.loads(row.payload_json)
        if (not isinstance(value, dict) or set(value) != {'schema', 'action', 'kind', 'source_id', 'successor_id',
                'case_id', 'actor_user_id', 'source_snapshot_sha256', 'successor_snapshot_sha256',
                'successor_identity_sha256', 'source_version', 'successor_version', 'note',
                'execution_authorized', 'stop_evidence_created', 'context_inherited'}
                or row.kind != kind or row.actor_user_id != user_id or row.source_id == row.successor_id
                or value['schema'] != SCHEMA or value['action'] != ACTION or value['kind'] != kind
                or value['source_id'] != row.source_id or value['successor_id'] != row.successor_id
                or value['case_id'] != row.case_id or value['actor_user_id'] != user_id
                or not isinstance(value['note'], str) or len(value['note']) > 2000 or '\0' in value['note']
                or any(not isinstance(value[key], str) or re.fullmatch('[0-9a-f]{64}', value[key]) is None
                    for key in ('source_snapshot_sha256', 'successor_snapshot_sha256', 'successor_identity_sha256'))
                or any(type(value[key]) is not int or value[key] < 0 for key in ('source_version', 'successor_version'))
                or any(value[key] is not False for key in ('execution_authorized', 'stop_evidence_created', 'context_inherited'))
                or row.id != identifier(kind, row.successor_id)):
            raise ValueError()
        return {'id': row.id, 'state': 'existing_successor_linked', 'kind': kind,
            'source_id': row.source_id, 'successor_id': row.successor_id, 'case_id': row.case_id,
            'recorded_at': review.timestamp(row.created_at), 'note': value['note'],
            'source_snapshot_sha256': value['source_snapshot_sha256'],
            'successor_identity_sha256': value['successor_identity_sha256'],
            'execution_authorized': False, 'stop_evidence_created': False, 'context_inherited': False}
    except (ValueError, TypeError, KeyError):
        raise ExecutionConflict('session_lineage_unverified', '原会话关联凭证无法完整核验，请保留记录') from None


async def links(db, kind, row, user_id):
    values = (await db.scalars(select(NativeSessionLink).where(
        NativeSessionLink.kind == kind, NativeSessionLink.actor_user_id == user_id,
        or_(NativeSessionLink.source_id == str(row.id), NativeSessionLink.successor_id == str(row.id)))
        .order_by(NativeSessionLink.created_at.desc(), NativeSessionLink.id.desc()).limit(20))).all()
    return [receipt(value, kind, user_id) for value in values]


async def plan(kind, source_id, successor_id, user_id):
    review.identifier(kind, source_id); review.identifier(kind, successor_id)
    async with SessionLocal() as db:
        source = await review.authorize(db, kind, source_id, user_id)
        successor = await review.authorize(db, kind, successor_id, user_id)
        check_pair(kind, source, successor)
        existing = await db.get(NativeSessionLink, identifier(kind, successor_id))
        if existing is not None and existing.source_id != source_id:
            raise ExecutionConflict('session_lineage_conflict', '该后续会话已登记其他来源，请保留原关联')
        return {'source': review.state(kind, source), 'successor': review.state(kind, successor),
            'source_snapshot_sha256': review.fingerprint(kind, source),
            'successor_identity_sha256': identity_digest(kind, successor),
            'existing': receipt(existing, kind, user_id) if existing else None,
            'execution_authorized': False, 'context_inherited': False}


async def link(kind, source_id, successor_id, user_id, source_sha, successor_sha, note):
    review.identifier(kind, source_id); review.identifier(kind, successor_id)
    async with SessionLocal() as db:
        await lock_owner(db, user_id)
        rows = {}
        for target_id in sorted({source_id, successor_id}, key=lambda value: review.identifier(kind, value)):
            rows[target_id] = await review.authorize(db, kind, target_id, user_id, write=True, locked=True)
        source, successor = rows[source_id], rows[successor_id]
        if source.case_id != successor.case_id:
            raise ExecutionConflict('session_lineage_conflict', '不能关联其他算例的会话')
        existing = await db.get(NativeSessionLink, identifier(kind, successor_id))
        if existing is not None:
            result = receipt(existing, kind, user_id)
            if (result['source_id'] != source_id or result['source_snapshot_sha256'] != source_sha
                    or result['successor_identity_sha256'] != successor_sha or result['note'] != note):
                raise ExecutionConflict('session_lineage_conflict', '该后续会话已有不同的确认记录，请读取原凭证')
            return result
        check_pair(kind, source, successor)
        if review.fingerprint(kind, source) != source_sha or identity_digest(kind, successor) != successor_sha:
            raise ExecutionConflict('state_conflict', '原会话或后续会话登记已变化，请重新核验关联方案')
        payload = {'schema': SCHEMA, 'action': ACTION, 'kind': kind, 'source_id': source_id,
            'successor_id': successor_id, 'case_id': source.case_id, 'actor_user_id': user_id,
            'source_snapshot_sha256': source_sha, 'successor_snapshot_sha256': review.fingerprint(kind, successor),
            'successor_identity_sha256': successor_sha, 'source_version': source.version,
            'successor_version': successor.version, 'note': note, 'execution_authorized': False,
            'stop_evidence_created': False, 'context_inherited': False}
        value = NativeSessionLink(id=identifier(kind, successor_id), kind=kind, source_id=source_id,
            successor_id=successor_id, case_id=source.case_id, actor_user_id=user_id,
            payload_json=json_text(payload), created_at=now())
        db.add(value); await finish_before_cancel(db.commit())
        return receipt(value, kind, user_id)
