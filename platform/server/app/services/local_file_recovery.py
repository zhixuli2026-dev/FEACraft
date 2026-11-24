"""Prepare cold local journal recovery without holding SQL or case locks."""
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select

from .. import storage
from ..db import SessionLocal
from ..models import Case, Workspace
from . import file_transactions as files
from .async_ops import finish_before_cancel
from .attempt_workspace import ordinary
from .case_write import CaseWriteConflict, bounded_case_read
from .durable_events import json_text
from .file_read import _identity
from .file_stage_locations import MOUNTED_SCHEMA, batches, locate
from .local_file_budget import check_quota, lock_for_prepared_edit, replacement_peak
from .owned_staging import recover_abandoned
from .request_deadline import current


STAGE_SCHEMA = 'vibeflow.local-file-recovery-staging.v1'
CODE = 'file_recovery'
LABEL = '文件事务恢复'


def _pending(case):
    if not storage._fs_path(case.s3_prefix, '.').exists():
        return None
    base = files._base(case)
    pending = files._path(base, 'pending') if base.exists() else None
    return pending if pending is not None and pending.exists() else None


def pending_reference(case):
    try:
        if _pending(case) is not None:
            return SimpleNamespace(id=case.id, s3_prefix=case.s3_prefix, kind='files')
    except (ValueError, OSError) as error:
        raise files._conflict('File transaction cannot be verified; preserve it for review') from error
    return None


def _version(path):
    try:
        return _identity(ordinary(path))
    except FileNotFoundError:
        return None


@dataclass(frozen=True)
class Recovery:
    case: object
    pending: Path
    document: dict
    versions: dict
    live: list

    def verify(self, case):
        if (self.document['plan']['binding'] != files._binding(case)
                or _pending(case) != self.pending):
            raise files._conflict('Local file journal or live files changed during recovery')
        deadline = current()
        for path, token in self.versions.items():
            if deadline is not None:
                deadline.check()
            if _version(path) != token:
                raise files._conflict('Local file journal or live files changed during recovery')

    def decision(self, committed):
        plan = self.document['plan']
        changes = []
        for index, (entry, observed) in enumerate(zip(plan['files'], self.live)):
            allowed = (entry['after'],) if committed else (entry['before'], entry['after'])
            if observed not in allowed:
                raise files._conflict('Pending file transaction conflicts with current bytes; nothing was overwritten', entry['path'])
            if not committed and observed != entry['before']:
                changes.append((index, entry, observed))
        receipt = {'transaction_id': plan['id'], 'decision': 'committed' if committed else 'rolled_back',
                   'event_id': self.document['event_id'], 'plan_sha256': self.document['plan_sha256']}
        return receipt, changes


def _read(case):
    pending = _pending(case)
    if pending is None:
        return None
    manifest = files._path(pending, 'manifest.json')
    versions = {manifest: _version(manifest)}
    deadline = current()

    def capture(path, **kwargs):
        version = _version(path)
        result = files._capture(path, **kwargs)
        if _version(path) != version:
            raise files._conflict('File changed while validating a pending journal')
        versions[path] = version
        return result

    root = storage._fs_path(case.s3_prefix, '.')
    document = files._load(case, pending, deadline=deadline, capture=capture)
    live = [capture(files._path(root, entry['path']), deadline=deadline) for entry in document['plan']['files']]
    recovery = Recovery(case, pending, document, versions, live)
    recovery.verify(case)
    return recovery


def _locations(recovery):
    root = storage._fs_path(recovery.case.s3_prefix, '.')
    default = storage._fs_path('.', '.file-recovery-staging')
    journal = locate(root, files.PREFIX+'/pending', default)
    destinations = [locate(root, entry['path'], default, shared=journal)
                    for entry in recovery.document['plan']['files']]
    for location in {journal, *destinations}:
        if location.local:
            recover_abandoned(location.scratch, MOUNTED_SCHEMA)
    return journal, destinations


def _stage(recovery, changes, objects, directories):
    deadline = current()
    staged = {}
    for index, entry, _ in changes:
        expected = entry['before']
        if expected is None:
            continue
        source = files._blob(recovery.pending, index, 'before')
        target = directories[objects[index][0]]/str(index)
        if files._capture(source, target, limit=expected['size'], deadline=deadline) != expected:
            raise files._conflict('File journal backup changed; preserve it for review', entry['path'])
        if os.name != 'nt' and entry.get('before_mode') is not None:
            os.chmod(target, entry['before_mode'])
        staged[index] = (target, _version(target))
    return staged


def _install(recovery, changes, staged, receipt):
    recovery.verify(recovery.case)
    root = storage._fs_path(recovery.case.s3_prefix, '.')
    deadline = current()
    for index, entry, _ in changes:
        if deadline is not None:
            deadline.check()
        path = files._path(root, entry['path'])
        if _version(path) != recovery.versions[path]:
            raise files._conflict('File changed before rollback', entry['path'])
        if entry['before'] is None:
            path.unlink(missing_ok=True)
        else:
            source, version = staged[index]
            source = files._path(source.parent, str(index))
            if _version(source) != version:
                raise files._conflict('Prepared rollback copy changed', entry['path'])
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, path)
        if path.parent.exists():
            files._sync(path.parent)
    if deadline is not None:
        deadline.check()
    files._finish_local(recovery.case, recovery.pending.parent, recovery.pending, recovery.document['plan'], receipt)


async def _check_locked(db, case, recovery):
    from .case_validation import assert_writable_locked
    from .storage_operations import admit_locked

    await admit_locked(db, case.id)
    await assert_writable_locked(db, case.id)
    await files._io(recovery.verify, case)
    return await files._witness(db, case, recovery.document) is not None


async def recover(checked, reference, *, session_factory=SessionLocal):
    try:
        snapshot = await bounded_case_read(_read, reference, code=CODE, label=LABEL)
        if snapshot is None:
            return
        journal, destinations = await files._io(_locations, snapshot)
        async with session_factory() as read:
            owner_id = await read.scalar(select(Workspace.user_id).select_from(Case)
                .join(Workspace, Case.workspace_id==Workspace.id).where(Case.id==reference.id))
        if owner_id is None:
            raise CaseWriteConflict('case_source_changed','文件事务关联的存储账户不存在')
        async with session_factory() as db:
            inventory = await lock_for_prepared_edit(db,owner_id,wait=True,session_factory=session_factory)
            case = await checked(db)
            if _pending(case) is None:
                return
            committed = await _check_locked(db, case, snapshot)
            receipt, changes = snapshot.decision(committed)
            tail = len(json_text(receipt).encode())
            peak = replacement_peak(((old, entry['before']) for _, entry, old in changes), tail)
            await check_quota(db, case, peak, inventory=inventory)
        objects = {index: (destinations[index], entry['before']['size'])
                   for index, entry, _ in changes if entry['before'] is not None}
        staged_local = sum(size for location, size in objects.values() if location.local)
        async with batches(objects, STAGE_SCHEMA, default=journal, tail=tail) as directories:
            staged = await bounded_case_read(_stage, snapshot, changes, objects, directories, code=CODE, label=LABEL)
            async with session_factory() as db:
                inventory = await lock_for_prepared_edit(db,owner_id,wait=True,session_factory=session_factory)
                case = await checked(db)
                if _pending(case) is None:
                    return
                if await _check_locked(db, case, snapshot) != committed:
                    raise files._conflict('File transaction SQL witness changed during recovery')
                await check_quota(db, case, max(0, peak-staged_local), inventory=inventory)
                deadline = current()
                if deadline is not None:
                    deadline.check()
                await files._io(_install, snapshot, changes, staged, receipt)
                await finish_before_cancel(db.commit())
    except (TimeoutError, CaseWriteConflict):
        raise
    except FileNotFoundError as error:
        if pending_reference(reference) is not None:
            raise files._conflict('File transaction journal cannot be verified; preserve it for review') from error
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as error:
        raise files._conflict('File transaction journal cannot be verified; preserve it for review') from error
