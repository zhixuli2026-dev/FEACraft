"""Local file write-ahead journal, with a commit witness in the caller's SQL txn.

The caller must hold case_write.coordinate. Recovery takes that same case lock
before consulting SQL. An absent witness means rollback; a committed witness
means retain the new bytes. Unrecognized live bytes are never overwritten.
The local and S3 adapters cover ordinary files and coordinated dictionary
revisions. Unadapted native writers retain their existing storage contracts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from .. import storage
from ..workflow_models import DurableEvent
from .case_write import CaseWriteConflict, record_write
from .durable_events import append_event, json_text
from .fs_directory import move_new

PREFIX = '.vibeflow/file-transactions'
SCHEMA = 'vibeflow.file-transaction.v1'
EVENT = 'files.transaction_applied'
MAX_BYTES = 1024 * 1024 * 1024
MAX_FILES = 256
MAX_LOCAL_TRANSACTION_BYTES = 2 * MAX_BYTES


def limits(scope='files'):
    from .tree_metadata_edits import SCOPES
    if scope in SCOPES:
        from .tree_metadata_native import MAX_BYTES as metadata_bytes
        count, bound, total, manifest = limits(SCOPES[scope])
        return count+1, bound, total+2*metadata_bytes, manifest+4096
    if scope == 'sandbox_recovery':
        from .sandbox_volume import MAX_ENTRIES, MAX_BYTES as volume_bytes
        return MAX_ENTRIES, volume_bytes, 2 * volume_bytes, 8 * 1024 * 1024
    if scope != 'files':raise ValueError('Unknown local file transaction scope')
    return MAX_FILES, MAX_BYTES, MAX_LOCAL_TRANSACTION_BYTES, 1024 * 1024


@dataclass(frozen=True)
class FileSource:
    """A verified staged file within the same case, copied without loading it."""
    relative: str
    expected: dict

    def resolve(self, prefix):
        return _path(storage._fs_path(prefix, '.'), self.relative)

    def changed(self, message, path=None):
        return _conflict(message, path)


@dataclass(frozen=True)
class RecoveryFileSource(FileSource):
    """A manifest member of a completed recovery on this storage node."""
    snapshot_id: str
    case_prefix: str

    def resolve(self, prefix):
        if prefix != self.case_prefix or not re.fullmatch('[0-9a-f]{32}', self.snapshot_id):
            raise self.changed('Recovery source binding differs')
        try:
            root = _path(storage._fs_root().resolve(), f'.terminal-recovery/{self.snapshot_id}/files')
            source = _path(root, self.relative)
            from .attempt_workspace import ordinary
            info = ordinary(source)
            if stat.S_ISREG(info.st_mode) and info.st_size == self.expected['size']:
                return source
        except (OSError, ValueError) as error:
            raise self.changed('Recovery source is unavailable') from error
        raise self.changed('Recovery source size or type differs')

    def changed(self, message, path=None):
        return CaseWriteConflict('recovery_evidence_invalid', '恢复文件未通过快照校验', path=path or self.relative)


def _capture_source(data, prefix, destination=None, *, limit):
    return _capture(data.resolve(prefix), destination, limit=limit)


async def _io(function, *args, **kwargs):
    from .async_ops import finish_before_cancel
    return await finish_before_cancel(asyncio.to_thread(function, *args, **kwargs))


def _conflict(message, path=None):
    return CaseWriteConflict('file_recovery_required', message, path=path)


def check_expected(path, before, expected_files):
    """Compare the captured file with an expected digest, record or absence."""
    if expected_files is None or path not in expected_files:
        return
    expected = expected_files[path]
    observed = before['sha256'] if isinstance(expected, str) and before is not None else before
    if observed != expected:
        raise CaseWriteConflict('file_conflict', '文件内容已变化，请重新读取后再保存', path=path)


def _path(root, relative, *, check=None):
    from .attempt_workspace import canonical_path, ordinary
    if canonical_path(relative) != relative:
        raise ValueError('Journal paths must be canonical')
    current = root
    if check is not None:
        check()
    ordinary(current)
    for part in relative.split('/'):
        if check is not None:
            check()
        current = current / part
        try:
            ordinary(current)
        except FileNotFoundError:
            pass
    if check is not None:
        check()
    resolved = current.resolve()
    if check is not None:
        check()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError('File transaction path escaped its root')
    return current


def _sync(directory):
    # Windows file fsync + atomic directory rename cover process termination.
    # POSIX also flushes directory entries. No claim of arbitrary disk survival.
    if os.name != 'nt':
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_new(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _sync(path.parent)


class CaptureLimitExceeded(ValueError):
    """A file exceeded the byte limit of this snapshot."""


def _capture(path, destination=None, *, limit=None, deadline=None, stream=None):
    """Bounded streaming snapshot; reject links and concurrent modifications."""
    if destination is not None and stream is not None:
        raise ValueError('A file snapshot has only one destination')
    if limit is None:
        limit = MAX_BYTES
    limit_message='File transaction exceeds 1 GiB' if limit==MAX_BYTES else 'File transaction exceeds its admitted size'
    from .attempt_workspace import ordinary
    try:
        before = ordinary(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise ValueError('File transactions require regular files')
    if before.st_size > limit:
        raise CaptureLimitExceeded(limit_message)
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    digest, size = hashlib.sha256(), 0
    output = stream
    try:
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            output = destination.open('xb')
        with path.open('rb') as stream:
            if identity(os.fstat(stream.fileno())) != identity(before):
                raise _conflict('File changed while preparing its journal')
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                if deadline is not None:
                    deadline.check()
                size += len(chunk)
                if size > limit:
                    raise CaptureLimitExceeded(limit_message)
                digest.update(chunk)
                if output is not None:
                    output.write(chunk)
            if identity(os.fstat(stream.fileno())) != identity(before):
                raise _conflict('File changed while preparing its journal')
        if identity(ordinary(path)) != identity(before) or size != before.st_size:
            raise _conflict('File changed while preparing its journal')
        if destination is not None:
            output.flush()
            os.fsync(output.fileno())
    finally:
        if destination is not None and output is not None:
            output.close()
    return {'size': size, 'sha256': digest.hexdigest()}


def _binding(case):
    return {'case_id': case.id, 'prefix': case.s3_prefix,
            'backend': 'fs', 'root': str(storage._fs_path(case.s3_prefix, '.'))}


def _base(case):
    root = storage._fs_path(case.s3_prefix, '.')
    return _path(root, PREFIX)


def _blob(journal, index, side):
    return _path(journal, f'{side}/{index}')


def _load(case, pending, *, deadline=None, capture=None):
    if capture is None:
        capture = _capture
    manifest = _path(pending, 'manifest.json')
    with manifest.open('rb') as stream:
        encoded = stream.read(8 * 1024 * 1024 + 4096 + 1)
    if len(encoded) > 8 * 1024 * 1024 + 4096:
        raise ValueError('Oversized file journal')
    document = json.loads(encoded)
    plan = document['plan']
    max_files, max_bytes, max_total, max_manifest = limits(plan.get('scope', 'files'))
    if len(encoded) > max_manifest:raise ValueError('File journal exceeds its scope')
    if plan['schema'] != SCHEMA or plan['binding'] != _binding(case):
        raise ValueError('File journal storage binding differs')
    if str(uuid.UUID(plan['id'])) != plan['id'] or str(uuid.UUID(document['event_id'])) != document['event_id']:
        raise ValueError('Invalid journal identity')
    if hashlib.sha256(json_text(plan).encode()).hexdigest() != document['plan_sha256']:
        raise ValueError('File journal manifest changed')
    records = plan['files']
    if not isinstance(records, list) or not 0 < len(records) <= max_files:
        raise ValueError('Invalid journal file count')
    paths, total = set(), 0
    from .attempt_workspace import assert_mutable_path
    from .tree_metadata_edits import internal, check_records
    check_records(plan.get('scope', 'files'), records)
    for index, entry in enumerate(records):
        path = entry['path']
        if not internal(plan.get('scope', 'files'), path):
            assert_mutable_path(case.s3_prefix, path)
        _path(storage._fs_path(case.s3_prefix, '.'), path)
        if path.casefold() in paths or any(path.casefold().startswith(other + '/') or
                other.startswith(path.casefold() + '/') for other in paths):
            raise ValueError('Conflicting journal paths')
        paths.add(path.casefold())
        for side in ('before', 'after'):
            expected = entry[side]
            mode = entry.get(side+'_mode')
            if mode is not None and (expected is None or type(mode) is not int or not 0 <= mode <= 0o7777):
                raise ValueError('Invalid journal file mode')
            if expected is not None:
                if capture(_blob(pending, index, side), limit=max_bytes, deadline=deadline) != expected:
                    raise ValueError('File journal backup is missing or corrupt')
                total += expected['size']
    if total > max_total:
        raise ValueError('File transaction exceeds 2 GiB including backups')
    return document


def _apply(case, journal, index, entry, side, expected):
    root = storage._fs_path(case.s3_prefix, '.')
    path = _path(root, entry['path'])
    if _capture(path) != expected:
        raise _conflict('File changed outside the pending transaction; preserve it for review', entry['path'])
    target = entry[side]
    if target is None:
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            actual = _capture(_blob(journal, index, side), temporary, limit=target['size'])
            if actual != target:
                raise _conflict('File journal backup changed; preserve it for review', entry['path'])
            if _capture(_path(root, entry['path'])) != expected:
                raise _conflict('File changed before replacement; preserve it for review', entry['path'])
            if os.name != 'nt' and entry.get(side+'_mode') is not None:
                os.chmod(temporary, entry[side+'_mode'])
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    if path.parent.exists():
        _sync(path.parent)


async def write(db, case, changes, *, expected_files=None, content_types=None, scope='files'):
    """Prepare one multi-file mutation. SQL commit remains caller-owned."""
    if storage._use_s3():
        from . import s3_file_transactions
        return await s3_file_transactions.write(db, case, changes, expected_files=expected_files,
                                                 content_types=content_types, scope=scope)
    max_files, max_bytes, max_total, max_manifest = limits(scope)
    from .case_write import _writes
    from .attempt_workspace import assert_mutable_path
    current = _writes.get()
    if current is None or current[0] != case.s3_prefix:
        raise ValueError('Local file journal requires the case write coordinator')
    from .tree_metadata_edits import expand, internal, check_records
    changes, expected_files, scope = await _io(expand, case.s3_prefix, changes, expected_files, scope)
    max_files, max_bytes, max_total, max_manifest = limits(scope)
    if not isinstance(changes, dict) or not 0 < len(changes) <= max_files:
        raise ValueError('Invalid file transaction size')
    changes=dict(changes)  # Freeze entries before async planning/admission.
    if expected_files is not None and (not isinstance(expected_files, dict) or set(expected_files) - changes.keys()):
        raise ValueError('File transaction preconditions must refer to changed paths')
    root = storage._fs_path(case.s3_prefix, '.')
    names = set()
    for relative, data in changes.items():
        if not internal(scope, relative):assert_mutable_path(case.s3_prefix, relative)
        _path(root, relative)
        key = relative.casefold()
        if (key in names or any(key.startswith(other + '/') or other.startswith(key + '/') for other in names)
                or data is not None and not isinstance(data, (bytes, FileSource))):
            raise ValueError('Conflicting file transaction paths or invalid data')
        names.add(key)
    base = _base(case)
    pending = _path(root, PREFIX + '/pending')
    if pending.exists():
        raise _conflict('A file transaction is already pending for this case')
    identifier = str(uuid.uuid4())
    stage = _path(root, PREFIX + '/preparing-' + identifier)
    records, total = [], 0
    for index, (relative, data) in enumerate(changes.items()):
        if not internal(scope, relative):assert_mutable_path(case.s3_prefix, relative)
        path = _path(root, relative)
        before = await _io(_capture, path, limit=max_bytes)
        check_expected(relative, before, expected_files)
        after = None
        if isinstance(data, FileSource):
            after = await _io(_capture_source, data, case.s3_prefix, limit=max_bytes)
            if after != data.expected or after is None:
                raise data.changed('Staged file changed while preparing the transaction', relative)
        elif data is not None:
            after = {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            if after['size'] > max_bytes:
                raise ValueError('File transaction object exceeds 1 GiB')
        total += sum(item['size'] for item in (before, after) if item is not None)
        if total > max_total:
            raise ValueError('File transaction exceeds 2 GiB including backups')
        entry = {'path': relative, 'before': before, 'after': after}
        if os.name != 'nt' and before is not None:
            mode = stat.S_IMODE(path.stat().st_mode)
            entry['before_mode'] = mode
            if after is not None:entry['after_mode'] = mode
        records.append(entry)
    check_records(scope, records)
    plan = {'schema': SCHEMA, 'id': identifier, 'binding': _binding(case), 'files': records}
    if scope != 'files':plan['scope'] = scope
    plan_hash = hashlib.sha256(json_text(plan).encode()).hexdigest()
    event_id=str(uuid.uuid4())
    document = {'plan': plan, 'event_id': event_id, 'plan_sha256': plan_hash}
    encoded=json_text(document).encode()
    if len(encoded)>max_manifest:raise ValueError('Oversized file journal')
    from .local_file_budget import allocation,replacement_peak
    receipt={'transaction_id':identifier,'decision':'rolled_back','event_id':event_id,'plan_sha256':plan_hash}
    peak=total+len(encoded)+replacement_peak(((r['before'],r['after']) for r in records),len(json_text(receipt).encode()))
    async with allocation(db,case,peak):
        stage.mkdir(parents=True)
        for index,((relative,data),entry) in enumerate(zip(changes.items(),records)):
            before=entry['before']
            if before is not None:
                captured=await _io(_capture,_path(root,relative),_blob(stage,index,'before'),limit=before['size'])
                if captured!=before:raise _conflict('File changed after journal admission',relative)
            if isinstance(data,FileSource):
                captured=await _io(_capture_source,data,case.s3_prefix,_blob(stage,index,'after'),limit=entry['after']['size'])
                if captured!=entry['after']:raise data.changed('Staged file changed after journal admission',relative)
            elif isinstance(data,bytes):await _io(_write_new,_blob(stage,index,'after'),data)
        async with db.begin_nested():
            await append_event(db, case.id, EVENT, {'transaction_id': identifier, 'plan_sha256': plan_hash},event_id=event_id)
            await _io(_publish_prepared, stage, pending, root, base, encoded)
            db.info.setdefault('local_file_transactions', {})[case.id] = (db.sync_session.get_transaction(), identifier)
            for index, entry in enumerate(records):
                await _io(_apply, SimpleNamespace(id=case.id, s3_prefix=case.s3_prefix),
                          pending, index, entry, 'after', entry['before'])
                record_write(case.s3_prefix, entry['path'])
    return identifier


def _publish_prepared(stage, pending, root, base, encoded):
    _write_new(stage / 'manifest.json', encoded)
    for child in stage.iterdir():
        if child.is_dir():
            _sync(child)
    _sync(stage)
    move_new(stage, pending, root=root)
    _sync(base)


async def finish_committed_edit(db, case, owner_id):
    """Settle a public file edit after its caller commits the business transaction."""
    if not storage._use_s3():
        # The business transaction has committed. Take owner credit before the
        # case lock so a concurrent saver can finish without a lock inversion.
        from fastapi import HTTPException
        from .storage_admission import lock_owner
        try:
            await lock_owner(db,owner_id,wait=True,settlement=True)
        except HTTPException as error:
            if error.status_code!=409 or not isinstance(error.detail,dict) or error.detail.get('code')!='storage_quota_busy':
                raise
            raise HTTPException(409,{'code':'file_cleanup_pending','committed':True,'case_id':case.id,
                'message':'写入已提交，事务清理仍在等待。请刷新文件版本。'}) from error
    await finish_committed(db,case)


async def finish_committed(db, case):
    """Clear a known committed journal before returning a successful write."""
    from sqlalchemy import select
    from . import indexsvc
    from .case_write import bounded_case_read, coordinate
    from .request_deadline import scope
    pending = db.info.get('file_index_events', {}).pop(case.id, ())
    # A cooperating delete can win after our SQL commit. It already recovered
    # this journal before moving the case; don't turn that committed save into
    # a false case_unavailable response during housekeeping.
    async with coordinate(db, case, content_write=False, allow_deleted=True):
        if pending and indexsvc.enabled(case.id):
            events = await db.scalars(select(DurableEvent.payload_json).where(
                DurableEvent.case_id==case.id, DurableEvent.id.in_(pending), DurableEvent.type=='files.changed'))
            # An event ID can survive a rollback in session.info. Only a
            # durable event from the previous transaction authorizes a refresh.
            paths = dict.fromkeys(path for payload in events for path in json.loads(payload)['paths'])
            if paths:
                try:
                    async with scope(3):
                        await bounded_case_read(indexsvc.refresh_paths, case.id, case.s3_prefix, tuple(paths)[:256],
                                                code='file_index', label='文件索引')
                except Exception as error:
                    # The committed file is authoritative. The original outbox
                    # event stays queued if its derived index could not update.
                    print('[WARN] File index refresh deferred: '+type(error).__name__)
    await db.commit()


async def finish_confirmed_stage(case_id):
    """Settle post-commit housekeeping without asking clients to reconfirm.

    Call only after the confirmation transaction commits and all its owner
    scopes close. Each known-busy retry owns a fresh transaction; the stage
    mutation and native dispatch are never repeated here.
    """
    from fastapi import HTTPException
    from ..db import SessionLocal
    from ..models import Case
    from .case_write import CaseWriteConflict
    from .execution_storage_admission import admit
    async def settle():
        async with SessionLocal() as db:
            case=await db.get(Case,case_id)
            # A cooperating deletion recovers the journal before removing the
            # case. It cannot turn an already committed confirmation back.
            if case is not None:await finish_committed(db,case)
    try:await admit(settle,case_contention=True)
    except (HTTPException,CaseWriteConflict) as error:
        known=(isinstance(error,HTTPException) and error.status_code==409
            and isinstance(error.detail,dict) and error.detail.get('code')=='storage_quota_busy')
        known=known or (isinstance(error,CaseWriteConflict) and error.code=='validation_busy')
        if not known:raise
        raise HTTPException(409,{'code':'confirmation_cleanup_pending','message':'阶段确认已提交，后续清理仍在等待；请读取当前状态，不要重复确认',
            'committed':True,'case_id':case_id}) from error


async def _witness(db, case, document):
    event = await db.get(DurableEvent, document['event_id'], populate_existing=True)
    if event is not None and (event.case_id != case.id or event.type != EVENT or
            json.loads(event.payload_json) != {'transaction_id': document['plan']['id'],
                                             'plan_sha256': document['plan_sha256']}):
        raise ValueError('File transaction commit witness differs')
    return event


PREPARED_RESOLUTION = 'commit-ready.json'


async def recover_locked(db, case):
    """Called before any coordinated read/write, after the case SQL lock."""
    if storage._use_s3():
        from .s3_file_transactions import recover_locked as recover_remote
        return await recover_remote(db, case)
    try:
        from .tree_transactions import recover_locked as recover_tree
        tree = await recover_tree(db, case)
        if tree is not None:return tree
        if not storage._fs_path(case.s3_prefix, '.').exists():
            return None
        base = _base(case)
        pending = _path(base, 'pending') if base.exists() else None
        if pending is None or not pending.exists():
            return None
        reference = SimpleNamespace(id=case.id, s3_prefix=case.s3_prefix)
        installed_by_case = db.info.get('installed_local_file_edits', {})
        installed = installed_by_case.get(case.id)
        if installed is not None and await _io(installed.matches, pending):
            document = installed.document
        else:
            installed = None
            document = await _io(_load, reference, pending)
        plan = document['plan']
        event = await _witness(db, case, document)
        owner = db.info.get('local_file_transactions', {}).get(case.id)
        if event is not None and owner == (db.sync_session.get_transaction(), plan['id']):
            # Our own uncommitted marker is not proof of a durable SQL commit.
            return {'transaction_id': plan['id'], 'decision': 'in_transaction'}
        installed_by_case.pop(case.id, None)
        from .case_validation import assert_writable_locked
        await assert_writable_locked(db, case.id)
        if installed is not None and event is not None:
            await _io(installed.verify, reference)
            receipt = {'transaction_id': plan['id'], 'decision': 'committed',
                       'event_id': document['event_id'], 'plan_sha256': document['plan_sha256']}
            await _io(_finish_local, reference, base, pending, plan, receipt, True)
            return receipt
        if installed is not None:
            document = await _io(_load, reference, pending)
            plan = document['plan']
            event = await _witness(db, case, document)
        observed = []
        for entry in plan['files']:
            live = await _io(_capture, _path(storage._fs_path(case.s3_prefix, '.'), entry['path']))
            allowed = (entry['after'],) if event is not None else (entry['before'], entry['after'])
            if live not in allowed:
                raise _conflict('Pending file transaction conflicts with current bytes; nothing was overwritten', entry['path'])
            observed.append(live)
        decision = 'committed' if event is not None else 'rolled_back'
        receipt = {'transaction_id': plan['id'], 'decision': decision,
                   'event_id': document['event_id'], 'plan_sha256': document['plan_sha256']}
        from .local_file_budget import allocation,replacement_peak
        replacements=[(live,entry['before']) for entry,live in zip(plan['files'],observed)
                      if event is None and live!=entry['before']]
        peak=replacement_peak(replacements,len(json_text(receipt).encode()))
        async with allocation(db,case,peak):
            if event is None:
                for index, (entry, live) in enumerate(zip(plan['files'], observed)):
                    if live != entry['before']:
                        await _io(_apply, reference, pending, index, entry, 'before', live)
            await _io(_finish_local, reference, base, pending, plan, receipt)
        return receipt
    except CaseWriteConflict:
        raise
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
        raise _conflict('File transaction journal cannot be verified; preserve it for review') from error


def _prepared_resolution(pending, receipt):
    path = _path(pending, PREPARED_RESOLUTION)
    encoded = json_text(receipt).encode()
    try:
        with path.open('rb') as stream:
            return stream.read(len(encoded)+1) == encoded
    except FileNotFoundError:
        return False


def _finish_local(case, base, pending, plan, receipt, prepaid_only=False):
    resolved = _path(pending, 'resolution.json')
    ready = _path(pending, PREPARED_RESOLUTION)
    # The candidate is not evidence of commit. Only a verified receipt
    # supplied by the caller may publish it, with no additional allocation.
    if _prepared_resolution(pending, receipt):
        os.replace(ready, resolved)
    elif prepaid_only:
        raise _conflict('Prepaid file resolution changed before settlement')
    else:
        storage._fs_put(case.s3_prefix, PREFIX + '/pending/resolution.json', json_text(receipt).encode())
        ready.unlink(missing_ok=True)
    _sync(pending)
    history = _path(base, 'history')
    history.mkdir(exist_ok=True)
    move_new(pending, history / plan['id'], root=storage._fs_path(case.s3_prefix, '.'))
    _sync(base)
    _sync(history)
