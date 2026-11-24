"""One live detector per case and API process, with a shared durable observation."""
import asyncio
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import stat
from types import SimpleNamespace

from .. import storage
from ..config import get_settings
from ..db import SessionLocal
from ..models import Case, CaseFileObservation
from .async_ops import finish_before_cancel
from .case_read_capacity import admitted, file_read_deadline, read_ready, reading
from .case_write import assert_case_available, bounded_case_read
from .durable_events import append_event, ensure_parent_transaction, json_text, lock_case_execution
from .event_replay import latest_sequence
from .storage_listing_budget import ListingBudget
from .storage_usage import _ordinary_info


POLL_SECONDS = 2
_MAX_PATH_BYTES = 1024 * 1024
_EVENT_PATH_BYTES = 8192
_EVENT_MAX_PATHS = 256
_MAX_NATIVE_MONITORS = 16
_log = logging.getLogger(__name__)


def native_enabled():
    return get_settings().file_watch_native and not storage._use_s3()


def _private(name):
    return name.rstrip(' .').casefold() == '.vibeflow'


def _entries(prefix):
    from ..storage_paths import is_file_stage

    if storage._use_s3():
        for entry in storage.list_tree(prefix):
            if not _private(entry['path'].split('/', 1)[0]):
                yield entry['path'], ('file', entry['size'], entry['modified'], entry['etag'])
        return
    root = storage._fs_path(prefix, '.')
    budget = ListingBudget()
    pending = [root]
    while pending:
        budget.check()
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                budget.visit()
                if is_file_stage(entry.name) or (directory == root and _private(entry.name)):
                    continue
                path = Path(entry.path)
                info = _ordinary_info(entry.stat(follow_symlinks=False))
                relative = path.relative_to(root).as_posix()
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                    yield relative, ('directory',)
                else:
                    # Persist metadata shared across replicas, including mounts
                    # whose device/inode numbers are specific to the API host.
                    yield relative, ('file', info.st_size, info.st_mtime_ns)


def snapshot(prefix):
    entries = sorted(_entries(prefix))
    digest = hashlib.sha256()
    compact = {}
    size = 0
    for path, identity in entries:
        value = json_text(identity)
        record = json_text((path, identity)).encode('utf-8')
        digest.update(record+b'\n')
        size += len(record)+1
        if compact is not None:
            if size <= _MAX_PATH_BYTES:
                compact[path] = value
            else:
                compact = None
    return digest.hexdigest(), compact


def _changes(before, after):
    changes = []
    if before is not None and after is not None:
        remaining = _EVENT_PATH_BYTES
        for path in sorted(before.keys() | after.keys()):
            if before.get(path) == after.get(path):
                continue
            size = len(path.encode('utf-8'))
            if len(changes) == _EVENT_MAX_PATHS or size > remaining:
                return []  # Tell consumers to reconcile the full tree.
            remaining -= size
            kind = 'created' if path not in before else 'deleted' if path not in after else 'modified'
            changes.append({'path': path, 'kind': kind})
    return changes


def _candidate(source):
    fingerprint, entries = snapshot(source.reference.s3_prefix)
    if fingerprint == source.fingerprint:
        return None
    entries_json = json_text(entries) if entries is not None else None
    if entries_json is not None and len(entries_json.encode('utf-8')) > _MAX_PATH_BYTES:
        entries_json = None
    prior = json.loads(source.entries_json) if source.entries_json is not None else None
    return fingerprint, entries_json, _changes(prior, entries)


async def _storage_marker(prefix):
    from .case_tree import listing_marker

    return await bounded_case_read(listing_marker, prefix, code='file_watch', label='文件监听')


async def _source(db, case):
    baseline = await db.get(CaseFileObservation, case.id)
    sequence = await latest_sequence(db, case.id)
    marker = await _storage_marker(case.s3_prefix)
    return SimpleNamespace(reference=SimpleNamespace(id=case.id, workspace_id=case.workspace_id,
                                                     s3_prefix=case.s3_prefix),
                           sequence=sequence, marker=marker,
                           fingerprint=baseline.fingerprint if baseline is not None else None,
                           entries_json=baseline.entries_json if baseline is not None else None)


async def _publish(source, fingerprint, entries_json, changes):
    from sqlalchemy.orm import load_only
    from .case_journal_recovery import require_settled

    reference = source.reference
    async with SessionLocal() as db:
        case = await db.get(Case, reference.id)
        if case is None:
            return
        remote = storage._use_s3()
        async with (admitted(db, case, polling=True) if remote else nullcontext()):
            try:
                await ensure_parent_transaction(db)
                await lock_case_execution(db, reference.id)
                await db.refresh(case)
                assert_case_available(case)
                if (case.workspace_id, case.s3_prefix) != (reference.workspace_id, reference.s3_prefix):
                    return
                await read_ready(db, case)
                if (await latest_sequence(db, case.id) != source.sequence
                        or await _storage_marker(case.s3_prefix) != source.marker):
                    # A cooperating write, rollback, or newer observer intervened.
                    return
                observation = await db.get(CaseFileObservation, case.id,
                                           options=(load_only(CaseFileObservation.fingerprint),))
                if (observation.fingerprint if observation is not None else None) != source.fingerprint:
                    return
                # A remote write can race even after reading HEAD. Fence using
                # the exact catalog observed by the scan, with owner admission.
                await require_settled(case, catalog_etag=source.marker if remote else None)
                initial = observation is None
                if initial:
                    observation = CaseFileObservation(case_id=case.id)
                    db.add(observation)
                observation.fingerprint, observation.entries_json = fingerprint, entries_json
                await append_event(db, case.id, 'files.changed', {'source': 'file_watch', 'rescan': True, 'initial': initial,
                    'paths_complete': bool(changes), 'changes': changes, 'paths': [item['path'] for item in changes]})
                await finish_before_cancel(db.commit())
            finally:
                await finish_before_cancel(db.rollback())


async def observe(case_id):
    from .case_journal_recovery import DeferredFileRecovery, recover_bound_case, reject_pending

    async with file_read_deadline('file_watch', '文件监听'):
        try:
            async with SessionLocal() as db:
                case = await db.get(Case, case_id)
                if case is None:
                    return
                assert_case_available(case)
                await read_ready(db, case)
                await reject_pending(case)
                source = await _source(db, case)
                if storage._use_s3() and source.marker is None:
                    # Bootstrap an old namespace under the original control.
                    async with reading(db, case, defer_journal=True):
                        source = await _source(db, case)
                await reject_pending(case)
            candidate = await bounded_case_read(_candidate, source, code='file_watch', label='文件监听')
            if candidate is not None:
                await _publish(source, *candidate)
        except DeferredFileRecovery as pending:
            await recover_bound_case(pending.reference, SessionLocal)


async def _native_wake(prefix, wake, stop):
    from watchfiles import awatch

    root = storage._fs_path(prefix, '.').resolve()

    def included(_change, path):
        try:
            parts = Path(path).relative_to(root).parts
        except ValueError:
            return False
        return bool(parts) and not _private(parts[0])

    try:
        async for _ in awatch(root, watch_filter=included, stop_event=stop, debounce=250,
                              step=80, rust_timeout=300):
            wake.set()
    except (OSError, RuntimeError) as error:
        _log.info('Case file watch falls back to polling: %s', type(error).__name__)


@dataclass
class _Monitor:
    task: asyncio.Task
    native: bool
    clients: int = 1


_monitors: dict[int, _Monitor] = {}


async def _monitor(case_id, prefix, native):
    wake, stop = asyncio.Event(), asyncio.Event()
    native_task = asyncio.create_task(_native_wake(prefix, wake, stop)) if native else None
    last_problem = None
    try:
        while True:
            wake.clear()
            try:
                await observe(case_id)
                last_problem = None
            except Exception as error:
                problem = type(error).__name__
                if problem != last_problem:
                    _log.info('Case file observation deferred for %s: %s', case_id, problem)
                last_problem = problem
            try:
                await asyncio.wait_for(wake.wait(), POLL_SECONDS)
            except TimeoutError:
                pass
    finally:
        if native_task is not None:
            stop.set()
            await asyncio.gather(native_task, return_exceptions=True)


@asynccontextmanager
async def monitor_case(case_id, prefix):
    monitor = _monitors.get(case_id)
    if monitor is None:
        native = native_enabled() and sum(m.native for m in _monitors.values()) < _MAX_NATIVE_MONITORS
        monitor = _Monitor(asyncio.create_task(_monitor(case_id, prefix, native)), native)
        _monitors[case_id] = monitor
    else:
        monitor.clients += 1
    try:
        yield monitor.native
    finally:
        monitor.clients -= 1
        if monitor.clients == 0:
            _monitors.pop(case_id, None)
            monitor.task.cancel()
            await asyncio.gather(monitor.task, return_exceptions=True)
