"""Read-only native history and snapshot-bound acknowledgement, never process control."""
import base64
from datetime import datetime, timezone
import hashlib
import json
import re
import uuid

from fastapi import HTTPException
from sqlalchemy import and_, inspect, or_, select

from ..db import SessionLocal
from ..models import NativeCliSession, NativeSession, NativeSessionReview, NativeVizSession, User, Workspace, Case
from .async_ops import finish_before_cancel
from .durable_events import json_text, now
from .storage_admission import lock_owner
from .workflow_state import ExecutionConflict

SCHEMA = 'vibeflow.native-session-review.v1'
ACTION = 'retain_for_review'
MODELS = {'terminal': NativeSession, 'cli': NativeCliSession, 'viz': NativeVizSession}


def timestamp(value):
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)).isoformat()


def identifier(kind, value):
    if kind not in MODELS: raise HTTPException(404, '会话类型不存在')
    try:
        if kind == 'viz':
            number = int(value)
            if not 0 < number < 2**63 or str(number) != value: raise ValueError()
            return number
        if str(uuid.UUID(value)) != value: raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(404, '会话不存在') from None


def fingerprint(kind, row):
    values = {column.key: getattr(row, column.key) for column in inspect(type(row)).columns}
    values = {key: timestamp(value) if isinstance(value, datetime) else value for key, value in values.items()}
    return hashlib.sha256(json_text({'kind': kind, 'row': values}).encode('utf8')).hexdigest()


async def authorize(db, kind, target_id, user_id, *, write=False, locked=False):
    from ..routers.workspaces import get_owned_case
    user = await db.get(User, user_id, populate_existing=True)
    if user is None or user.status != 'active': raise HTTPException(403, '账户已不可用')
    row = await db.get(MODELS[kind], identifier(kind, target_id), populate_existing=True,
                       with_for_update=locked)
    if row is None or row.created_by != user_id: raise HTTPException(404, '会话不存在')
    if row.case_id is not None:
        case = await db.get(Case, row.case_id, populate_existing=True)
        if case is not None: await db.get(Workspace, case.workspace_id, populate_existing=True)
        await get_owned_case(row.case_id, user, db, write=write)
    return row


def state(kind, row):
    remote = False
    if kind == 'terminal':
        from .terminal_records import public
        from ..routers.terminal import _sessions
        local = _sessions.get(row.id)
    elif kind == 'cli':
        from .codex_records import public
        from .codex_sessions import SESSIONS
        local = SESSIONS.get(row.id)
    else:
        from ..routers.viz import _out as public, _sessions
        local = _sessions.get(row.id)
    value = public(row)
    if kind == 'viz':
        remote = local is None and value['owner_available']
    cleanup = value['cleanup_verified']
    # Absence of an in-memory owner or lease expiry is never an OS exit proof.
    alive = True if remote else getattr(local, 'alive', None) if local is not None else False if cleanup is True else None
    issues = []
    if value['phase'] == 'evidence_invalid': issues.append('session_evidence_invalid')
    if local is None and not remote and cleanup is not True: issues.append('session_owner_unavailable')
    if cleanup is not True: issues.append('session_cleanup_unverified')
    if row.phase in {'closed', 'failed'} and cleanup is not True: issues.append('terminal_state_not_stop_proof')
    return {'kind': kind, 'id': str(row.id), 'case_id': row.case_id, 'phase': value['phase'],
        'original_phase': row.phase, 'version': row.version, 'created_at': timestamp(row.created_at),
        'ended_at': timestamp(row.ended_at) if row.ended_at else None,
        'runtime': row.shell if kind == 'terminal' else row.runtime if kind == 'cli' else 'visualization',
        'error': row.error, 'cleanup_verified': cleanup, 'owner_available': local is not None or remote,
        'alive': alive, 'issues': issues,
        'conversation_id': row.conversation_id if kind == 'cli' else None}


def event_id(kind, target_id, digest):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'vibeflow.native-manual-review:' + kind + ':' + target_id + ':' + digest))


def receipt(row, kind, target_id, user_id, case_id):
    try:
        value = json.loads(row.payload_json)
        if (not isinstance(value, dict) or set(value) != {'schema', 'action', 'kind', 'session_id', 'case_id',
                'actor_user_id', 'snapshot_sha256', 'original_phase', 'original_version', 'issues', 'note',
                'execution_authorized', 'stop_evidence_created'}
                or not isinstance(value['note'], str) or len(value['note']) > 2000 or '\0' in value['note']
                or not isinstance(value['issues'], list) or not value['issues']
                or any(not isinstance(issue, str) or issue not in {'session_evidence_invalid',
                    'session_owner_unavailable', 'session_cleanup_unverified', 'terminal_state_not_stop_proof'} for issue in value['issues'])
                or len(set(value['issues'])) != len(value['issues'])
                or value['original_phase'] not in {'starting', 'active', 'closed', 'failed'}
                or type(value['original_version']) is not int or value['original_version'] < 0
                or not isinstance(row.snapshot_sha256, str) or re.fullmatch('[0-9a-f]{64}', row.snapshot_sha256) is None
                or row.target_kind != kind or row.target_id != target_id or row.actor_user_id != user_id
                or row.case_id != case_id or value['schema'] != SCHEMA or value['action'] != ACTION
                or value['kind'] != kind or value['session_id'] != target_id or value['actor_user_id'] != user_id
                or value['case_id'] != case_id or value['snapshot_sha256'] != row.snapshot_sha256
                or value['execution_authorized'] is not False or value['stop_evidence_created'] is not False
                or row.id != event_id(kind, target_id, row.snapshot_sha256)):
            raise ValueError()
        return {'id': row.id, 'state': 'retained_for_manual_review', 'snapshot_sha256': row.snapshot_sha256,
            'recorded_at': timestamp(row.created_at), 'note': value['note'], 'issues': value['issues'],
            'original_phase': value['original_phase'], 'stop_evidence_created': False,
            'execution_authorized': False}
    except (ValueError, KeyError, TypeError):
        raise ExecutionConflict('session_review_unverified', '人工核查记录无法完整核验，请保留记录') from None


async def describe(db, kind, row, user_id):
    value = state(kind, row)
    digest = fingerprint(kind, row)
    reviews = (await db.scalars(select(NativeSessionReview).where(
        NativeSessionReview.target_kind == kind, NativeSessionReview.target_id == str(row.id),
        NativeSessionReview.actor_user_id == user_id).order_by(NativeSessionReview.created_at.desc(), NativeSessionReview.id.desc()).limit(20))).all()
    current = await db.get(NativeSessionReview, event_id(kind, str(row.id), digest))
    result = {**value, 'snapshot_sha256': digest, 'schema': SCHEMA,
        'available_actions': [ACTION] if value['issues'] and not value['owner_available'] else [],
        'review': receipt(current, kind, str(row.id), user_id, row.case_id) if current else None,
        'reviews': [receipt(item, kind, str(row.id), user_id, row.case_id) for item in reviews],
        'execution_authorized': False, 'automatic_replay': False}
    from .native_session_lineage import links
    result['links'] = await links(db, kind, row, user_id)
    # Existing recovery executors perform their own physical observations. This
    # query only offers potentially applicable actions, not a claim of safety.
    if kind == 'cli' and value['phase'] != 'evidence_invalid' and not value['owner_available']:
        journal = json.loads(row.process_json)
        if isinstance(journal, dict) and journal.get('management', {}).get('retirement') is None and 'management' in journal:
            result['available_actions'].append('reconcile_scope')
        from ..models import CliInitializationReservation
        credit = await db.get(CliInitializationReservation, row.id)
        if credit is not None and credit.state == 'active':
            from .cli_initialization_recovery import mode
            try:
                mode(credit, row)
            except (ExecutionConflict, ValueError, KeyError, TypeError): pass
            else: result['available_actions'].append('reconcile_initialization')
    return result


async def options(kind, target_id, user_id):
    identifier(kind, target_id)
    async with SessionLocal() as db:
        return await describe(db, kind, await authorize(db, kind, target_id, user_id), user_id)


async def retain(kind, target_id, user_id, digest, note):
    identifier(kind, target_id)
    async with SessionLocal() as db:
        await lock_owner(db, user_id)
        row = await authorize(db, kind, target_id, user_id, write=True, locked=True)
        if fingerprint(kind, row) != digest:
            raise ExecutionConflict('state_conflict', '原会话记录已变化，请重新读取核查方案')
        plan = await describe(db, kind, row, user_id)
        existing = await db.get(NativeSessionReview, event_id(kind, target_id, digest))
        if existing is not None:
            value = receipt(existing, kind, target_id, user_id, row.case_id)
            if value['note'] != note: raise ExecutionConflict('state_conflict', '本快照已有不同的核查说明，请保留原记录')
            return value
        if ACTION not in plan['available_actions']:
            raise ExecutionConflict('state_conflict', '会话由当前实例管理或已有完整停止证明，请刷新方案')
        at = now()
        payload = {'schema': SCHEMA, 'action': ACTION, 'kind': kind, 'session_id': target_id,
            'case_id': row.case_id, 'actor_user_id': user_id, 'snapshot_sha256': digest,
            'original_phase': row.phase, 'original_version': row.version, 'issues': plan['issues'],
            'note': note, 'stop_evidence_created': False, 'execution_authorized': False}
        audit = NativeSessionReview(id=event_id(kind, target_id, digest), target_kind=kind,
            target_id=target_id, case_id=row.case_id, actor_user_id=user_id,
            snapshot_sha256=digest, payload_json=json_text(payload), created_at=at)
        db.add(audit)
        await finish_before_cancel(db.commit())
        return receipt(audit, kind, target_id, user_id, row.case_id)


def decode_cursor(kind, cursor):
    try:
        value = json.loads(base64.urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
        at = datetime.fromisoformat(value['at'])
        if value['kind'] != kind or at.tzinfo is None: raise ValueError()
        return at, identifier(kind, value['id'])
    except (ValueError, KeyError, TypeError, HTTPException):
        raise HTTPException(400, '历史游标无效，请重新读取会话') from None


async def listing(kind, user_id, case_id=None, cursor=None, limit=50):
    if kind not in MODELS: raise HTTPException(404, '会话类型不存在')
    from ..routers.workspaces import get_owned_case
    async with SessionLocal() as db:
        user = await db.get(User, user_id)
        if user is None or user.status != 'active': raise HTTPException(403, '账户已不可用')
        model = MODELS[kind]
        query = select(model).where(model.created_by == user_id)
        if case_id is not None:
            await get_owned_case(case_id, user, db, write=False)
            query = query.where(model.case_id == case_id)
        if cursor:
            at, before_id = decode_cursor(kind, cursor)
            query = query.where(or_(model.created_at < at, and_(model.created_at == at, model.id < before_id)))
        rows = (await db.scalars(query.order_by(model.created_at.desc(), model.id.desc()).limit(limit + 1))).all()
        selected = rows[:limit]; result = []
        for row in selected:
            if case_id is None and row.case_id is not None:
                try: await get_owned_case(row.case_id, user, db, write=False)
                except HTTPException as error:
                    if error.status_code in {403, 404}: continue
                    raise
            result.append(state(kind, row))
        next_cursor = None
        if len(rows) > limit:
            last = selected[-1]
            at = last.created_at.replace(tzinfo=timezone.utc) if last.created_at.tzinfo is None else last.created_at
            next_cursor = base64.urlsafe_b64encode(json_text({'kind': kind, 'at': at.isoformat(), 'id': str(last.id)}).encode()).decode().rstrip('=')
        return {'data': result, 'next_cursor': next_cursor}
