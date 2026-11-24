"""Confirmed, restartable removal of a retired sandbox's original S3 cache.

The retained snapshot and retirement witness authorize the operation. A committed
intent binds the exact local inventory before any unlink. Partial deletion is
retryable; unknown or changed entries are never recursively removed. This does
not remove the ext4 image, export publication staging, or retained snapshots.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

from .. import storage
from ..db import SessionLocal
from ..models import DurableEvent, NativeRequest
from . import sandbox_retirement as retirement, sandbox_recovery_execution as recovery
from .async_ops import finish_before_cancel
from .attempt_workspace import ordinary, checked_path, canonical_path
from .durable_events import append_event, json_text, lock_case_execution
from .node_storage_budget import reserve
from . import request_deadline

SCHEMA = 'vibeflow.sandbox-staging-cleanup.v1'
INTENT = 'terminal.staging_cleanup_started'
DONE = 'terminal.staging_cleanup_completed'
DIRECTORY = '.terminal-cleanup'
MAX_PLAN = 64 * 1024 * 1024
MAX_ENTRIES = 200000


def check_deadline():
    deadline=request_deadline.current()
    if deadline is not None:deadline.check()


def event_id(sid, kind):
    return str(uuid.uuid5(uuid.UUID(sid), SCHEMA + ':' + kind))


def paths(stage):
    if not isinstance(stage, str) or not re.fullmatch(r'\.staging/job-[0-9]+-[0-9a-f]{32}', stage):
        raise ValueError('Noncanonical original staging prefix')
    root = storage._fs_root().resolve()
    ordinary(root)
    parent = root / '.staging'
    ordinary(parent)
    return parent / stage.split('/')[1], storage._staging_manifest_path(stage), storage._terminal_cleanup_marker(stage)


def identity(path):
    info = ordinary(path)
    return {'device': info.st_dev, 'inode': info.st_ino, 'kind': 'directory' if stat.S_ISDIR(info.st_mode) else 'file'}


def exists(path):
    # lexists also detects dangling symlinks, which must block completion.
    return os.path.lexists(path)


def read(path, maximum=MAX_PLAN):
    check_deadline()
    info = ordinary(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        raise ValueError('Invalid cleanup evidence file')
    with path.open('rb') as stream:
        raw = stream.read(maximum + 1)
    check_deadline()
    if len(raw) > maximum:
        raise ValueError('Cleanup evidence exceeds its bound')
    return raw


def digest(path, expected):
    check_deadline()
    info = ordinary(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size != expected['size']:
        raise ValueError('Staging contents changed')
    total = 0
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            check_deadline()
            total += len(chunk)
            if total > expected['size']:
                raise ValueError('Staging contents grew during verification')
            value.update(chunk)
    check_deadline()
    if total != expected['size'] or value.hexdigest() != expected['sha256']:
        raise ValueError('Staging contents differ from the downloaded baseline')


def inventory(root):
    check_deadline()
    entries = {}
    if not exists(root):
        return entries
    if identity(root)['kind'] != 'directory':
        raise ValueError('Staging root is not a directory')
    pending = [root]
    while pending:
        check_deadline()
        parent = pending.pop()
        # Stream each directory rather than materializing/sorting it before
        # enforcing either the inventory bound or the request deadline.
        with os.scandir(parent) as children:
            for child in children:
                check_deadline()
                if len(entries)>=MAX_ENTRIES:raise ValueError('Staging inventory exceeds its bound')
                path=Path(child.path)
                name = path.relative_to(root).as_posix()
                if canonical_path(name) != name:
                    raise ValueError('Noncanonical staging entry')
                entries[name] = identity(path)
                if entries[name]['kind'] == 'directory':
                    pending.append(path)
    check_deadline()
    return entries


def plan_path(sid):
    if str(uuid.UUID(sid)) != sid:
        raise ValueError('Invalid cleanup session')
    root = storage._fs_root().resolve()
    ordinary(root)
    parent = root / DIRECTORY
    parent.mkdir(exist_ok=True)
    ordinary(parent)
    return parent / (sid + '.json')


def build_plan(binding):
    check_deadline()
    root, manifest, marker = paths(binding['stage'])
    if exists(marker):
        raise ValueError('Another staging cleanup already owns this cache')
    raw = read(manifest)
    baseline = json.loads(raw)
    if baseline['version'] != 1 or baseline['source_prefix'] != binding['source_prefix']:
        raise ValueError('Staging manifest belongs to another source')
    files = baseline['files']
    entries = inventory(root)
    if not exists(root) or set(files) != {name for name, entry in entries.items() if entry['kind'] == 'file'}:
        raise ValueError('Staging contains unknown or missing files')
    directories = set()
    for name, record in files.items():
        check_deadline()
        if (canonical_path(name) != name or type(record['size']) is not int or record['size'] < 0
                or not re.fullmatch('[0-9a-f]{64}', record['sha256'])):
            raise ValueError('Invalid baseline entry')
        parts = name.split('/')
        directories.update('/'.join(parts[:i]) for i in range(1, len(parts)))
        digest(checked_path(root, name), record)
    if directories != {name for name, entry in entries.items() if entry['kind'] == 'directory'}:
        raise ValueError('Staging contains unknown directories')
    return {**binding, 'schema': SCHEMA, 'root_identity': identity(root), 'entries': entries,
            'baseline': baseline, 'manifest_sha256': hashlib.sha256(raw).hexdigest(),
            'manifest_identity': identity(manifest)}


def encode_plan(plan):
    check_deadline()
    encoded = json_text(plan).encode()
    check_deadline()
    if len(encoded) > MAX_PLAN:
        raise ValueError('Cleanup plan is too large')
    return encoded


def write_expected(path, encoded, maximum):
    """Finish only an exact prefix of bytes authorized by a committed intent.

    Never truncate or replace existing metadata. A crash during append leaves
    another verifiable prefix. Case control excludes other cleanup writers.
    """
    from .file_transactions import _sync
    check_deadline()
    raw = b''
    if exists(path):
        raw = read(path, maximum)
        if raw == encoded:
            return
        if not encoded.startswith(raw):
            raise ValueError('Cleanup metadata contains unrecognized bytes')
    expected = identity(path) if exists(path) else None
    with reserve(len(encoded) - len(raw)), path.open('r+b' if expected else 'xb') as stream:
        info = os.fstat(stream.fileno())
        if expected and (info.st_dev != expected['device'] or info.st_ino != expected['inode']
                or stream.read(maximum + 1) != raw):
            raise ValueError('Cleanup metadata changed while opening')
        check_deadline()
        stream.write(encoded[len(raw):])
        stream.flush()
        os.fsync(stream.fileno())
    _sync(path.parent)
    check_deadline()


def ensure_plan(document):
    path = plan_path(document['session_id'])
    if exists(path) and hashlib.sha256(read(path)).hexdigest() == document['plan_sha256']:
        return load_plan(document)
    # The intent is already committed, but the plan may have been only partly
    # written. Reconstruct it ONLY from a complete, unchanged original stage.
    # If deletion has begun, missing/corrupt evidence remains a hard failure.
    binding = {key: document[key] for key in ('session_id', 'case_id', 'created_by', 'stage',
        'source_prefix', 'retirement_event_id', 'recovery_id')}
    plan = build_plan(binding)
    encoded = encode_plan(plan)
    if hashlib.sha256(encoded).hexdigest() != document['plan_sha256']:
        raise ValueError('Cannot reconstruct the committed cleanup inventory')
    write_expected(path, encoded, MAX_PLAN)
    return load_plan(document)


def load_plan(document):
    raw = read(plan_path(document['session_id']))
    if hashlib.sha256(raw).hexdigest() != document['plan_sha256']:
        raise ValueError('Cleanup intent inventory changed')
    plan = json.loads(raw)
    if any(plan.get(key) != document[key] for key in ('schema', 'session_id', 'case_id', 'created_by',
            'stage', 'source_prefix', 'retirement_event_id', 'recovery_id')):
        raise ValueError('Cleanup inventory belongs to another intent')
    return plan


def check_remaining(plan):
    root, manifest, marker = paths(plan['stage'])
    if exists(root) and identity(root) != plan['root_identity']:
        raise ValueError('Staging directory identity changed')
    current = inventory(root)
    for name, entry in current.items():
        check_deadline()
        if plan['entries'].get(name) != entry:
            raise ValueError('Staging contains an unknown or replaced entry')
        if entry['kind'] == 'file':
            digest(checked_path(root, name), plan['baseline']['files'][name])
    if exists(manifest) and (identity(manifest) != plan['manifest_identity']
            or hashlib.sha256(read(manifest)).hexdigest() != plan['manifest_sha256']):
        raise ValueError('Staging baseline changed')
    return current


def marker_bytes(document):
    return json_text({'schema': SCHEMA, 'intent_id': document['intent_id'],
        'stage_prefix': document['stage'], 'plan_sha256': document['plan_sha256'],
        'intent': 'cleanup_only', 'source_synchronized': False}).encode()


def remove_entry(root, name, entry, files):
    check_deadline()
    path = checked_path(root, name)
    if identity(path) != entry:
        raise ValueError('Staging identity changed during cleanup')
    if entry['kind'] == 'file':
        digest(path, files[name])
        check_deadline()
        path.unlink()
    else:
        check_deadline()
        path.rmdir()
    check_deadline()


def remove_remaining(document):
    plan = load_plan(document)
    root, manifest, marker = paths(plan['stage'])
    current = check_remaining(plan)  # Validate ALL survivors before removing any.
    encoded = marker_bytes(document)
    write_expected(marker, encoded, 4096)
    # No recursive deletion: only the exact committed, still-matching entries.
    for name in sorted(current, key=lambda value: (-value.count('/'), value)):
        check_deadline()
        remove_entry(root, name, plan['entries'][name], plan['baseline']['files'])
    if exists(root):
        if identity(root) != plan['root_identity']:
            raise ValueError('Staging root changed during cleanup')
        check_deadline()
        root.rmdir()
    if exists(manifest):
        if identity(manifest) != plan['manifest_identity'] or hashlib.sha256(read(manifest)).hexdigest() != plan['manifest_sha256']:
            raise ValueError('Staging manifest changed during cleanup')
        check_deadline()
        manifest.unlink()
    if read(marker, 4096) != encoded:
        raise ValueError('Staging marker changed during cleanup')
    check_deadline()
    marker.unlink()
    check_deadline()


async def intent(db, row, witness):
    value = await db.get(DurableEvent, event_id(row.id, 'intent'), populate_existing=True)
    if value is None:
        if await db.get(DurableEvent, event_id(row.id, 'done'), populate_existing=True) is not None:
            raise ValueError('Staging completion has lost its original intent')
        return None
    document = json.loads(value.payload_json)
    selected = json.loads(row.profile_json)
    expected = {'schema': SCHEMA, 'intent_id': value.id, 'session_id': row.id, 'case_id': row.case_id,
        'created_by': row.created_by, 'stage': selected['staging_prefix'], 'source_prefix': witness['source_prefix'],
        'retirement_event_id': row.retirement_event_id, 'recovery_id': witness['recovery_id']}
    if value.type != INTENT or value.case_id != row.case_id or any(document.get(k) != v for k, v in expected.items()):
        raise ValueError('Invalid staging cleanup intent')
    return document


async def completed(db, row, document):
    value = await db.get(DurableEvent, event_id(row.id, 'done'), populate_existing=True)
    if value is None:
        return False
    if value.type != DONE or value.case_id != row.case_id or json.loads(value.payload_json) != document:
        raise ValueError('Invalid staging cleanup completion')
    await finish_before_cancel(asyncio.to_thread(load_plan, document))
    if any(exists(path) for path in paths(document['stage'])):
        raise ValueError('Staging reappeared after cleanup completion')
    return True


async def require_finished(db, row, witness):
    """Case admin holds its control lock; absence alone is not a completion."""
    document = await intent(db, row, witness)
    if document is not None and not await completed(db, row, document):
        raise ValueError('Staging cleanup still requires reconciliation')


def public(document, finished):
    return {'status': 'completed' if finished else 'pending', 'session_id': document['session_id'],
        'intent_id': document['intent_id'], 'recovery_id': document['recovery_id'],
        'original_staging_removed': finished, 'source_synchronized': False,
        'image_removed': False, 'snapshot_retained': True}


async def status(sid, user_id):
    async with SessionLocal() as db:
        row, _ = await retirement.authorized(db, sid, user_id)
        witness = await retirement.verified(db, row)
        if witness is None:
            return {'status': 'not_retired', 'session_id': sid}
        if json.loads(row.profile_json)['staging_prefix'] is None:
            return {'status': 'not_applicable', 'session_id': sid}
        document = await intent(db, row, witness)
        return public(document, await completed(db, row, document)) if document else {'status': 'not_started', 'session_id': sid}


async def selection(db, sid, recovery_id, user_id):
    row, _ = await retirement.authorized(db, sid, user_id)
    witness = await retirement.verified(db, row)
    if witness is None or witness['recovery_id'] != recovery_id:
        raise ValueError('Cleanup requires the committed retirement snapshot')
    stage = json.loads(row.profile_json)['staging_prefix']
    if stage is None:raise ValueError('Sandbox has no original S3 staging')
    document = await intent(db, row, witness)
    recovered = await db.get(NativeRequest, recovery_id, populate_existing=True)
    selected = recovery.snapshot_profile(json.loads(recovered.profile_json))
    binding = {'session_id': sid, 'case_id': row.case_id, 'created_by': user_id, 'stage': stage,
        'source_prefix': witness['source_prefix'], 'retirement_event_id': row.retirement_event_id,
        'recovery_id': recovery_id}
    return row, witness, selected, binding, document


async def clean(sid, recovery_id, user_id):
    async with request_deadline.scope(request_deadline.STAGING_CLEANUP_SECONDS) as deadline:
        return await _clean(sid, recovery_id, user_id, deadline)


async def _clean(sid, recovery_id, user_id, deadline):
    from .case_validation import assert_writable_locked
    async with SessionLocal() as db:
        _, witness, selected, binding, original_document = await selection(db,sid,recovery_id,user_id)
    case_id=binding['case_id']
    def archive():
        with recovery.download(selected,witness['receipt']):pass
    def prepare():
        return hashlib.sha256(encode_plan(build_plan(binding))).hexdigest()
    def unchanged(current):
        if current[1:4]!=(witness,selected,binding):
            raise ValueError('Staging cleanup binding changed during unlocked verification')
    # Read-only archive and first inventory do not own the case write lock.
    # The committed intent and locked survivor checks remain authoritative.
    deadline.check()
    await finish_before_cancel(asyncio.to_thread(archive))
    deadline.check()
    sha=None
    if original_document is None:
        sha=await finish_before_cancel(asyncio.to_thread(prepare))
        deadline.check()
    async with SessionLocal() as db:
        await lock_case_execution(db,case_id);await assert_writable_locked(db,case_id)
        current=await selection(db,sid,recovery_id,user_id);unchanged(current)
        row,_,_,_,document=current
        if original_document is not None and document!=original_document:
            raise ValueError('Staging cleanup intent changed during unlocked verification')
        if document and await completed(db,row,document):return public(document,True)
        deadline.check()
        if document is None:
            document={**binding,'schema':SCHEMA,'intent_id':event_id(sid,'intent'),'plan_sha256':sha}
            await append_event(db,case_id,INTENT,document,event_id=document['intent_id'])
            deadline.check()
            await finish_before_cancel(db.commit())
    deadline.check()
    await finish_before_cancel(asyncio.to_thread(archive))
    deadline.check()
    # Commit the authority BEFORE filesystem mutation; a fresh locked transaction
    # serializes retries and prevents case moves while the filesystem drains.
    async with SessionLocal() as db:
        await lock_case_execution(db, case_id)
        await assert_writable_locked(db, case_id)
        current=await selection(db,sid,recovery_id,user_id);unchanged(current)
        row,_,_,_,locked_document=current
        if locked_document!=document:
            raise ValueError('Cleanup intent changed before filesystem mutation')
        if await completed(db, row, document):
            return public(document, True)
        deadline.check()
        await finish_before_cancel(asyncio.to_thread(ensure_plan, document))
        deadline.check()
        await finish_before_cancel(asyncio.to_thread(remove_remaining, document))
        deadline.check()
        await append_event(db, case_id, DONE, document, event_id=event_id(sid, 'done'))
        deadline.check()
        await finish_before_cancel(db.commit())
        deadline.check()
        return public(document, True)
