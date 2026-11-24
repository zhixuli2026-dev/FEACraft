"""Committed native turn/event/approval receipts, fenced by conversation owner."""
import hashlib
import json
import re
import uuid
from datetime import timezone

from sqlalchemy import select, update

from ..models import NativeCliApproval, NativeCliEvent, NativeCliSession, NativeCliTurn, ConversationExecution
from . import codex_records, conversation_execution as control
from .codex_approval import request_key
from .durable_events import json_text, now
from .workflow_state import ExecutionConflict

TERMINAL = {'completed', 'failed', 'cancelled', 'interrupted'}
MAX_BYTES = 64 * 1024 * 1024
MAX_EVENTS = 20000


def digest(value):
    return hashlib.sha256(json_text(value).encode('utf8')).hexdigest()


async def current(db, owner, *, launch=True):
    await control.current(db, owner, launch=launch)
    found = await db.execute(update(NativeCliTurn).where(NativeCliTurn.id == owner.resource_id,
        NativeCliTurn.conversation_id == owner.conversation_id, NativeCliTurn.phase.not_in(TERMINAL))
        .values(version=NativeCliTurn.version + 1))
    if found.rowcount != 1:
        raise ExecutionConflict('state_conflict', 'Native turn is no longer active')
    row = await db.get(NativeCliTurn, owner.resource_id, populate_existing=True)
    return row


async def append(db, row, kind, payload, *, terminal=False):
    text = json_text(payload)
    size = len(text.encode('utf8'))
    if size > 1024 * 1024 or row.event_bytes + size > MAX_BYTES - (0 if terminal else 4096) or row.next_seq > MAX_EVENTS - (0 if terminal else 4):
        raise ExecutionConflict('resource_limit', 'Native turn event budget exceeded')
    db.add(NativeCliEvent(turn_id=row.id, seq=row.next_seq, type=kind, payload_json=text))
    row.next_seq += 1
    row.event_bytes += size
    await db.flush()


async def before_attempt(db, owner, message_id):
    row = await current(db, owner)
    attempts = json.loads(row.attempts_json)
    if len(attempts) >= 6:
        raise ExecutionConflict('resource_limit', 'Native capacity retry budget exceeded')
    if attempts and attempts[-1]['status'] not in {'failed'}:
        raise ExecutionConflict('state_conflict', 'Previous native attempt is not a known failure')
    attempts.append({'message_id': message_id, 'turn_id': None, 'status': 'starting'})
    row.attempts_json = json_text(attempts)
    row.phase, row.native_turn_id, row.native_status = 'starting', None, None
    await append(db, row, 'request.started', {'attempt': len(attempts), 'client_message_id': message_id})


async def started(db, owner, thread_id, turn_id, model=None):
    row = await current(db, owner)
    if not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 128 or '\0' in turn_id:
        raise ValueError('Invalid native turn ID')
    attempts = json.loads(row.attempts_json)
    if not attempts or attempts[-1]['turn_id'] is not None or row.phase != 'starting':
        raise ExecutionConflict('state_conflict', 'Native turn was already registered')
    attempts[-1].update(turn_id=turn_id, status='running')
    row.attempts_json = json_text(attempts)
    row.thread_id, row.native_turn_id, row.phase = thread_id, turn_id, 'running'
    await append(db, row, 'turn.started', {'thread_id': thread_id, 'native_turn_id': turn_id, 'model': model})


async def native_terminal(db, owner, thread_id, turn):
    row = await current(db, owner, launch=False)
    if row.thread_id != thread_id or row.native_turn_id != turn.get('id') or turn.get('status') not in {'completed', 'failed', 'interrupted'}:
        raise ValueError('Native terminal does not match its registered attempt')
    attempts = json.loads(row.attempts_json)
    attempts[-1]['status'] = turn['status']
    row.attempts_json = json_text(attempts)
    row.native_status = turn['status']
    row.phase = 'finalizing'
    await db.execute(update(NativeCliApproval).where(NativeCliApproval.turn_id == row.id,
        NativeCliApproval.phase.in_(('pending', 'claimed'))).values(phase='expired'))
    await append(db, row, 'request.completed', {'native_turn_id': row.native_turn_id, 'status': row.native_status})


async def approval(db, owner, event, projected):
    row = await current(db, owner)
    params = event.get('params', {})
    if params.get('threadId') != row.thread_id or params.get('turnId') != row.native_turn_id:
        raise ValueError('Approval does not match the current native turn')
    identifier = str(uuid.uuid4())
    db.add(NativeCliApproval(id=identifier, turn_id=row.id, request_key=str(len(json.loads(row.attempts_json))) + ':' + request_key(event['id']),
        request_id_json=json_text(event['id']), method=event['method'], params_json=json_text(projected),
        request_sha256=digest({'method': event['method'], 'params': params})))
    row.phase = 'awaiting_input'
    await append(db, row, 'approval.requested', {'approval_id': identifier, 'method': event['method'], 'params': projected})
    return identifier


async def claim_approval(db, owner, identifier, response_public):
    row = await current(db, owner)
    found = await db.execute(update(NativeCliApproval).where(NativeCliApproval.id == identifier,
        NativeCliApproval.turn_id == row.id, NativeCliApproval.phase == 'pending').values(
            phase='claimed', response_json=json_text(response_public)))
    if found.rowcount != 1:
        raise ExecutionConflict('state_conflict', 'Approval was already answered or expired')
    await append(db, row, 'approval.claimed', {'approval_id': identifier})
    return await db.get(NativeCliApproval, identifier, populate_existing=True)


async def approval_sent(db, owner, identifier):
    row = await current(db, owner, launch=False)
    await db.execute(update(NativeCliApproval).where(NativeCliApproval.id == identifier,
        NativeCliApproval.turn_id == row.id, NativeCliApproval.phase == 'claimed').values(phase='sent'))
    pending = await db.scalar(select(NativeCliApproval.id).where(NativeCliApproval.turn_id == row.id, NativeCliApproval.phase == 'pending').limit(1))
    if pending is None and row.phase == 'awaiting_input':
        row.phase = 'running'
    await append(db, row, 'approval.sent', {'approval_id': identifier})


async def finish(db, owner, status, error=None):
    row = await current(db, owner, launch=False)
    if status not in TERMINAL or (error is not None and not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,79}', error)):
        raise ValueError('Invalid native turn completion')
    session = await db.get(NativeCliSession, row.session_id)
    value = codex_records.public(session)
    if status == 'completed' and (row.native_status != 'completed' or value['phase'] != 'closed' or value['cleanup_verified'] is not True):
        raise ExecutionConflict('evidence_invalid', 'Native turn completion lacks delivered scope evidence')
    row.phase, row.error, row.ended_at = status, error, now()
    await db.execute(update(NativeCliApproval).where(NativeCliApproval.turn_id == row.id,
        NativeCliApproval.phase.in_(('pending', 'claimed'))).values(phase='expired'))
    await append(db, row, 'stream.done', {'status': 'waiting_continue' if status == 'completed' and row.mode == 'goal' else status,
        'native_status': row.native_status, 'error': error, 'cleanup_verified': value['cleanup_verified']}, terminal=True)
    return row


async def public(db, row, *, refresh=True):
    from .codex_usage import latest as latest_usage
    session = await db.get(NativeCliSession, row.session_id, populate_existing=True)
    valid = (session is not None and session.case_id == row.case_id and session.conversation_id == row.conversation_id
             and session.created_by == row.created_by)
    value = codex_records.public(session) if session is not None else None
    phase = row.phase
    try:
        attempts = json.loads(row.attempts_json)
        if not isinstance(attempts, list) or len(attempts) > 6:
            valid = False
        elif any(not isinstance(item, dict) or set(item) != {'message_id', 'turn_id', 'status'}
                or not isinstance(item['message_id'], str) or not item['message_id']
                or item['status'] not in {'starting', 'running', 'completed', 'failed', 'interrupted'}
                or (item['turn_id'] is not None and not isinstance(item['turn_id'], str)) for item in attempts):
            valid = False
        elif attempts and row.native_turn_id != attempts[-1]['turn_id']:
            valid = False
    except (ValueError, TypeError):
        attempts, valid = [], False
    if phase == 'completed' and (not value or value['phase'] != 'closed' or value['cleanup_verified'] is not True or row.native_status != 'completed'):
        valid = False
    if not valid or (value and value['phase'] == 'evidence_invalid'):
        phase = 'evidence_invalid'
    elif phase not in TERMINAL:
        execution = await db.get(ConversationExecution, row.conversation_id)
        if (execution is None or execution.phase != 'active' or execution.resource_id != row.id
                or execution.lease_expires_at.replace(tzinfo=timezone.utc) <= now()):
            # Under READ COMMITTED a terminal commit can land between reading
            # this turn and its conversation fence. The commit changes both
            # atomically; refresh the older turn before declaring recovery.
            if refresh and (execution is None or execution.phase == 'idle' or execution.resource_id != row.id):
                latest = await db.get(NativeCliTurn, row.id, populate_existing=True)
                if latest is not None:
                    return await public(db, latest, refresh=False)
            phase = 'recovery_required'
        else:
            from . import process_identity_native as identity
            try:
                observed = identity.probe(json.loads(execution.owner_identity_json))
                if observed['state'] != 'match':
                    phase = 'recovery_required'
            except (ValueError, TypeError, KeyError, OSError):
                phase = 'evidence_invalid'
    return {'id': row.id, 'session_id': row.session_id, 'case_id': row.case_id, 'conversation_id': row.conversation_id,
            'client_message_id': row.client_message_id, 'mode': row.mode, 'phase': phase,
            'status': 'waiting_continue' if phase == 'completed' and row.mode == 'goal' else phase,
            'thread_id': row.thread_id, 'native_turn_id': row.native_turn_id, 'native_status': row.native_status,
            'last_seq': row.next_seq - 1, 'error': row.error, 'cleanup_verified': value['cleanup_verified'] if value else None,
            'attempts': attempts, 'usage': await latest_usage(db, row), 'created_at': row.created_at.isoformat(),
            'ended_at': row.ended_at.isoformat() if row.ended_at is not None else None}
