"""Case control-lock coordinator and optimistic file/revision preconditions.

Lock order: case control row -> refreshed Case -> file preconditions -> file
 publication/Case mirror -> durable events -> caller's commit. Derived indexes
 are updated only from committed events or coordinated current-state reads.
 The outer transaction
retains the DB lock after this context exits. Pending local ordinary-file journals
are recovered under this lock; other writers retain their own publication rules.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from collections import deque
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import PurePosixPath, PureWindowsPath
import re
import stat
from threading import BoundedSemaphore

from fastapi import HTTPException
from .. import storage
from ..config import get_settings
from .async_ops import finish_before_cancel
from .durable_events import ensure_parent_transaction, lock_case_execution
from .request_deadline import current as current_deadline, scope as deadline_scope

_writes = ContextVar('case_publication_alias_writes', default=None)
_digest_workers = 4
_digest_slots = BoundedSemaphore(_digest_workers)
_digest_pool = ThreadPoolExecutor(max_workers=_digest_workers, thread_name_prefix='case-digest')


def record_write(prefix, path):
    current = _writes.get()
    if current is not None and current[0].rstrip('/') == prefix.rstrip('/'):
        normalized = normalize_path(path)
        if not storage._use_s3():
            normalized = storage._fs_path(prefix, path).relative_to(storage._fs_path(prefix, '.')).as_posix()
        current[1].add(normalized)


class CaseWriteConflict(ValueError):
    def __init__(self, code, message, *, path=None, current_revision=None):
        self.code, self.path, self.current_revision = code, path, current_revision
        super().__init__(message)

    def detail(self):
        return {"code":self.code, "message":str(self), "path":self.path,
                "current_revision":self.current_revision}


def normalize_path(path):
    if not isinstance(path, str) or not path or "\x00" in path or ":" in path:
        raise ValueError("Invalid relative case path")
    path = path.replace("\\", "/")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or PureWindowsPath(path).drive or ".." in parsed.parts or str(parsed) == ".":
        raise ValueError("Case path must stay inside the case directory")
    return parsed.as_posix()


def file_record(prefix, path):
    path = normalize_path(path)
    if storage._use_s3():
        from .s3_streams import file_record as remote_record
        return remote_record(prefix, path)
    target = storage._fs_path(prefix, path)
    try:
        mode = target.stat().st_mode
        if stat.S_ISDIR(mode):
            raise ValueError("目标路径是目录，请指定文件路径")
        if not stat.S_ISREG(mode):
            raise ValueError("目标路径不是文件")
        stream = target.open("rb")
    except FileNotFoundError:
        return None
    digest = hashlib.sha256()
    size = 0
    deadline = current_deadline()
    with stream:
        before = os.fstat(stream.fileno())
        for chunk in iter(lambda: stream.read(1024*1024), b""):
            if deadline is not None:
                deadline.check()
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(stream.fileno())
    if size != before.st_size or size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise CaseWriteConflict("file_conflict", "读取时文件已变化，请重新读取", path=path)
    return {"size": size, "sha256": digest.hexdigest()}


def file_hash(prefix, path):
    record = file_record(prefix, path)
    return record["sha256"] if record is not None else None


async def read_file_record(prefix, path):
    """Keep the caller's case lock and the reader slot until file I/O drains."""
    return await bounded_case_read(file_record, prefix, path)


@contextmanager
def case_read_slot(code, label):
    if not _digest_slots.acquire(blocking=False):
        raise HTTPException(429, {"code": code + "_busy", "message": label + "繁忙，请稍后重试"},
                            headers={"Retry-After": "1"})
    try:
        yield
    finally:
        _digest_slots.release()


@asynccontextmanager
async def _read_operation_slot(code, label, wait_for_capacity):
    if not wait_for_capacity:
        with case_read_slot(code, label):
            yield
        return
    while not _digest_slots.acquire(blocking=False):
        await asyncio.sleep(.005)
    try:
        yield
    finally:
        _digest_slots.release()


async def bounded_case_read(operation, *args, code='file_version', label='文件版本读取', seconds=None,
                            wait_for_capacity=False):
    try:
        async with deadline_scope(get_settings().storage_measurement_timeout_seconds if seconds is None else seconds) as deadline:
            async with _read_operation_slot(code, label, wait_for_capacity):
                deadline.check()
                future = asyncio.get_running_loop().run_in_executor(_digest_pool, copy_context().run, operation, *args)
                try:
                    record = await asyncio.shield(future)
                except asyncio.CancelledError:
                    deadline.cancel()
                    await finish_before_cancel(asyncio.gather(future, return_exceptions=True))
                    raise
                deadline.check()
                return record
    except TimeoutError as error:
        raise HTTPException(504, {"code": code + "_timeout", "message": label + "超时，请稍后重试"}) from error


def is_case_dictionary(prefix, path):
    """Recognize the protected dictionary, including local filesystem aliases."""
    import os
    normalized = normalize_path(path)
    canonical = "0-caseDict/caseDict"
    if normalized == canonical:
        return True
    if storage._use_s3():
        return False
    if os.name == "nt" and "/".join(part.rstrip(" .").casefold() for part in normalized.split("/")) == canonical.casefold():
        return True
    try:
        return storage._fs_path(prefix, normalized).samefile(storage._fs_path(prefix, canonical))
    except FileNotFoundError:
        return False


@dataclass(frozen=True)
class CaseWriteSnapshot:
    case_id: int
    dict_revision: int
    hashes: dict[str, str | None]


@asynccontextmanager
async def coordinate(db, case, *, expected_revision=None, expected_files=None, allow_deleted=False,
                     content_write=True, prepared_aliases=None, defer_journal=False):
    """Authorize before entry; all cooperating writers must hold this lock.

    Omitted file key means no hash condition. A present key with None requires
    that the file does not exist. Never interpret missing and unconstrained as
    the same precondition. This context does not commit or automatically retry.
    content_write=False is only for audited read-only callers. It retains normal
    case serialization but does not conflict with a read-validation reservation.
    """
    if expected_revision is not None and (isinstance(expected_revision, bool) or
            not isinstance(expected_revision, int) or expected_revision < 0):
        raise ValueError("Revision must be a nonnegative integer")
    conditions = {}
    for path, expected in (expected_files or {}).items():
        normalized = normalize_path(path)
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected)):
            raise ValueError("File precondition must be a SHA-256 or explicit absence")
        if normalized in conditions and conditions[normalized] != expected:
            raise ValueError("Conflicting file preconditions")
        conditions[normalized] = expected
    await ensure_parent_transaction(db)
    async with db.begin_nested():
        await lock_case_execution(db, case.id)
        # Never validate against the object loaded before a competing writer's commit.
        await db.refresh(case)
        if not allow_deleted:
            assert_case_available(case)
        from .storage_operations import admit_locked
        storage_owned = await admit_locked(db, case.id)
        if not storage_owned:
            if defer_journal:
                from .case_journal_recovery import require_settled
                await require_settled(case)
            else:
                from .file_transactions import recover_locked
                await recover_locked(db, case)
        if content_write:
            from .case_validation import assert_writable_locked
            await assert_writable_locked(db, case.id)
        if expected_revision is not None and expected_revision != case.dict_revision:
            raise CaseWriteConflict("revision_conflict", "caseDict 已被其他操作修改，请刷新后重试",
                                    current_revision=case.dict_revision)
        hashes = {}
        for path, expected in conditions.items():
            record = await read_file_record(case.s3_prefix, path)
            current = record['sha256'] if record is not None else None
            hashes[path] = current
            if current != expected:
                raise CaseWriteConflict("file_conflict", "文件内容已变化，请重新读取后再保存",
                                        path=path, current_revision=case.dict_revision)
        changes = set()
        marker = _writes.set((case.s3_prefix, changes))
        try:
            yield CaseWriteSnapshot(case.id, case.dict_revision, hashes)
            if changes:
                from .artifact_publication import invalidate_aliases
                await invalidate_aliases(db, case, changes, prepared=prepared_aliases)
                from .durable_events import append_event
                # File-tree consumers reload current state; the bounded path hint
                # is not a replayable write operation or a filesystem journal.
                hints = []
                budget = 8192
                for path in sorted(changes):
                    size = len(path.encode('utf8'))
                    if len(hints) == 256 or size > budget:
                        break
                    hints.append(path)
                    budget -= size
                event = await append_event(db, case.id, 'files.changed',
                    {'paths': hints, 'changes': [], 'rescan': True, 'paths_complete': len(hints) == len(changes)})
                if hints:
                    db.info.setdefault('file_index_events', {}).setdefault(case.id, deque(maxlen=256)).append(event.id)
            await db.flush()
        finally:
            _writes.reset(marker)


def assert_case_available(case):
    """Recheck only after taking the case lock; pre-lock authorization can be stale."""
    from .trash import case_unavailable
    if case is None:
        raise CaseWriteConflict("case_unavailable", "算例不存在，请刷新后重试")
    if case_unavailable(case.id, case.workspace_id):
        raise CaseWriteConflict("case_unavailable", "算例已在回收站，请先恢复", current_revision=case.dict_revision)
