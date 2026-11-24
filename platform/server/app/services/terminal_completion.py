"""Retain actual terminal completion across SQL failure and API process exit.

The private encrypted witness contains the original owner's fenced capability
and exact completion, never a reconstructed stop. Recovery commits that witness
only after the original API birth is confirmed exited; it never starts or stops
a process. Keep the configured JWT secret stable while witnesses are pending.
"""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

from cryptography.fernet import Fernet
from ..config import get_settings
from ..db import SessionLocal
from ..models import NativeSession
from . import process_identity_native as identity, terminal_records as records
from .async_ops import finish_before_cancel
from .native_request_records import Owner
from .workflow_state import ExecutionConflict
from .publication_journal import _flush_directory

SCHEMA = 'vibeflow.terminal-completion.v1'
MAX_BYTES = 4 * 1024 * 1024


class CompletionBlocked(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _path(sid, *, create=False):
    if str(uuid.UUID(sid)) != sid:
        raise ValueError('Invalid terminal completion identity')
    root = Path(os.getenv('VIBEFLOW_AUDIT_DIR', Path(__file__).resolve().parents[2] / 'data/audit')) / 'terminal-completions'
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise ValueError('Terminal completion directory is redirected')
    path = root / (sid + '.enc')
    if path.is_symlink() or path.resolve().parent != root.resolve():
        raise ValueError('Terminal completion witness is redirected')
    return path


def available(sid):
    try:
        return _path(sid).is_file()
    except (ValueError, OSError):
        return False


def _cipher():
    secret = ('vibeflow.terminal-completion.v1:' + get_settings().jwt_secret).encode('utf8')
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret).digest()))


def _binding(row):
    return {key: getattr(row, key) for key in ('id', 'case_id', 'created_by', 'owner_id',
        'owner_token_sha256', 'owner_identity_json', 'launch_nonce', 'profile_json', 'retirement_event_id')}


def _read(sid):
    path = _path(sid)
    with path.open('rb') as stream:
        encrypted = stream.read(MAX_BYTES + 1)
    if len(encrypted) > MAX_BYTES:
        raise ValueError('Terminal completion witness exceeds its bound')
    value = json.loads(_cipher().decrypt(encrypted))
    if (not isinstance(value, dict) or set(value) != {'schema', 'binding', 'owner', 'payload'}
            or value['schema'] != SCHEMA or value['binding']['id'] != sid
            or set(value['owner']) != {'id', 'token', 'worker_id', 'nonce'}
            or value['owner']['id'] != sid
            or set(value['payload']) != {'root', 'stop', 'tail', 'exit_code', 'error', 'export'}):
        raise ValueError('Invalid terminal completion witness')
    owner = Owner(**value['owner'])
    if (owner.worker_id != value['binding']['owner_id'] or owner.nonce != value['binding']['launch_nonce']
            or hashlib.sha256(owner.token.encode()).hexdigest() != value['binding']['owner_token_sha256']):
        raise ValueError('Completion capability differs from its binding')
    return value


def _write(sid, value):
    path = _path(sid, create=True)
    if path.exists():
        if _read(sid) != value:
            raise ValueError('Existing terminal completion witness differs')
        return
    encrypted = _cipher().encrypt(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf8'))
    if len(encrypted) > MAX_BYTES:
        raise ValueError('Terminal completion witness exceeds its bound')
    descriptor, temporary = tempfile.mkstemp(prefix='.' + sid + '-', dir=path.parent)
    staged = Path(temporary)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(encrypted)
            stream.flush()
            os.fsync(stream.fileno())
        # One retained completion belongs to one fenced session owner. Parallel
        # callers coalesce on its close task; an existing witness is immutable.
        os.replace(staged, path)
        _flush_directory(path.parent)
    finally:
        staged.unlink(missing_ok=True)


def _discard(sid, expected):
    path = _path(sid)
    if not path.exists():
        return
    if _read(sid) != expected:
        raise ValueError('Terminal completion changed before acknowledgement')
    path.unlink()
    _flush_directory(path.parent)


async def retain(owner, payload):
    async with SessionLocal() as db:
        row = await db.get(NativeSession, owner.id)
        if (row is None or row.owner_id != owner.worker_id or row.launch_nonce != owner.nonce
                or row.owner_token_sha256 != hashlib.sha256(owner.token.encode()).hexdigest()):
            raise ExecutionConflict('state_conflict', 'Terminal completion ownership changed')
        value = {'schema': SCHEMA, 'binding': _binding(row),
                 'owner': {key: getattr(owner, key) for key in ('id', 'token', 'worker_id', 'nonce')},
                 'payload': payload}
    await finish_before_cancel(asyncio.to_thread(_write, owner.id, value))
    return value


def _equivalent(row, value):
    payload = value['payload']
    if _binding(row) != value['binding']:
        return False
    code = payload['exit_code']
    if code is not None and code >= 2**31:
        code -= 2**32
    journal = json.loads(row.process_json)
    return (row.phase == ('failed' if payload['error'] else 'closed')
        and row.error == payload['error'] and row.exit_code == code
        and json.loads(row.stop_json) == (payload['stop'] or {})
        and journal['root'] == payload['root']
        and journal.get('export') == payload['export']
        and row.tail_text == payload['tail'].encode('utf8')[-records.TAIL_BYTES:].decode('utf8', 'ignore'))


async def commit(owner, payload):
    """Try SQL even if witness storage fails; never lose a valid DB completion."""
    retained = None
    try:
        retained = await retain(owner, payload)
    except Exception as failure:
        print('[WARN] terminal completion retention unavailable: ' + type(failure).__name__)
    try:
        await records.complete(owner, **payload)
    except ExecutionConflict:
        if retained is None:
            raise
        async with SessionLocal() as db:
            row = await db.get(NativeSession, owner.id)
            if row is None or not _equivalent(row, retained):
                raise
    if retained is not None:
        try:
            await finish_before_cancel(asyncio.to_thread(_discard, owner.id, retained))
        except Exception as failure:
            # A committed exact completion remains authoritative. Retain an
            # unremoved witness for idempotent acknowledgement, not reexecution.
            print('[WARN] terminal completion acknowledgement deferred: ' + type(failure).__name__)


async def recover(row):
    try:
        value = await finish_before_cancel(asyncio.to_thread(_read, row.id))
    except FileNotFoundError:
        raise CompletionBlocked('completion_witness_missing') from None
    except Exception:
        raise CompletionBlocked('completion_witness_unverifiable') from None
    if _binding(row) != value['binding']:
        raise CompletionBlocked('completion_binding_changed')
    if not _equivalent(row, value):
        observed = await asyncio.to_thread(identity.probe, json.loads(row.owner_identity_json))
        if observed['state'] != 'exited':
            raise CompletionBlocked('completion_owner_alive' if observed['state'] == 'match' else 'completion_owner_exit_unverified')
        owner = Owner(**value['owner'])
        try:
            # This submits the original signed capability and exact physical
            # stop; it does not claim ownership, launch, signal or infer a stop.
            await records.complete(owner, **value['payload'])
        except ExecutionConflict:
            async with SessionLocal() as db:
                current = await db.get(NativeSession, row.id)
                if current is None or not _equivalent(current, value):
                    raise
    try:
        await finish_before_cancel(asyncio.to_thread(_discard, row.id, value))
    except Exception as failure:
        # SQL already contains the exact completion. An acknowledgement error
        # must not report that the physical stop or committed recovery failed.
        print('[WARN] terminal completion acknowledgement deferred: ' + type(failure).__name__)
    return True
