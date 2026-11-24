"""Commit legacy S3 staging through the existing SQL-witnessed file journal."""
from dataclasses import dataclass
import json
import mimetypes
from os.path import lexists, samestat
import re
import stat

from .. import storage
from . import attempt_workspace as workspace, case_copy_io, file_transactions as journal, request_deadline
from . import staging_preparation
from . import s3_file_transactions as remote
from .s3_streams import snapshot
from .storage_listing_budget import ListingBudget


@dataclass(frozen=True)
class StagingFile:
    stage: str
    path: str
    expected: dict
    source_prefix: str

    @property
    def relative(self):
        return self.path

    def resolve(self, prefix):
        if prefix != self.source_prefix or not re.fullmatch(r'\.staging/job-[0-9]+-[0-9a-f]{32}', self.stage):
            raise ValueError('Staged file belongs to a different case')
        if staging_preparation.registered(self.stage) or lexists(storage._terminal_cleanup_marker(self.stage)):
            raise storage.StagingConflict('Staging entered cleanup during synchronization')
        root = journal._path(storage._fs_root(), self.stage)
        return journal._path(root, self.path)


def load(stage, prefix):
    if not re.fullmatch(r'\.staging/job-[0-9]+-[0-9a-f]{32}', stage):
        raise ValueError('Invalid staging identity')
    if staging_preparation.registered(stage) or lexists(storage._terminal_cleanup_marker(stage)):
        raise storage.StagingConflict('终端副本已进入清理，不能重新同步')
    root = journal._path(storage._fs_root(), stage)
    path = journal._path(storage._fs_root(), stage + '.manifest.json')
    if path.stat().st_size > remote.MAX_MANIFEST:
        raise ValueError('Oversized staging manifest')
    manifest = json.loads(path.read_bytes())
    if manifest.get('version') != 1 or manifest.get('source_prefix') != prefix:
        raise ValueError('Staging source binding changed')
    if 'storage' in manifest:
        from .attempt_objects import binding
        if manifest['storage'] != binding(prefix):
            raise storage.StagingConflict('暂存文件的对象存储地址或 bucket 已变化')
    if staging_preparation.registered(stage) or lexists(storage._terminal_cleanup_marker(stage)):
        raise storage.StagingConflict('终端副本已进入清理，不能重新同步')
    return root, manifest


def collect(stage, prefix, excluded=()):
    deadline = request_deadline.current()
    if deadline is not None:
        deadline.check()
    root, manifest = load(stage, prefix)
    initial = manifest['files']
    excluded = set(excluded)
    changes, expected, types = {}, {}, {}
    budget = ListingBudget()

    def directory_info():
        try:
            info = workspace.ordinary(root)
        except FileNotFoundError as error:
            raise storage.StagingConflict('暂存工作目录不存在，已保留清单') from error
        if not stat.S_ISDIR(info.st_mode):
            raise storage.StagingConflict('暂存工作目录不是普通目录')
        return info

    def eligible(path):
        if path in excluded or workspace._reserved(path):
            return False
        if workspace.canonical_path(path) != path:
            raise ValueError('Invalid staging path')
        if deadline is not None:
            deadline.check()
        return True

    def compare(path, before, after):
        if after is not None and before is not None and after['sha256'] == before['sha256']:
            return
        current, _, _ = snapshot(prefix, path, limit=remote.STAGING_MAX_BYTES)
        if deadline is not None:
            deadline.check()
        if current == after:
            return  # Includes a previously committed sync whose response was lost.
        if before is None:
            if current is not None:
                raise storage.StagingConflict('远端文件已存在：' + path)
        elif current is None or current['sha256'] != before['sha256']:
            raise storage.StagingConflict('远端文件已变化：' + path)
        # Rollback can restore the same bytes under a different ETag (e.g. a
        # multipart object's original tag). The journal captures the current
        # version and CAS-checks it at mutation; source content is the precondition.
        changes[path] = StagingFile(stage, path, after, prefix) if after is not None else None
        expected[path] = current
        types[path] = mimetypes.guess_type(path)[0] or 'application/octet-stream'

    original_directory = directory_info()
    for path, _ in storage._fs_entries(stage, budget=budget):
        with budget.processing_entry():
            if not eligible(path):
                continue
            before = initial.pop(path, None)
            after = journal._capture(journal._path(root, path), limit=remote.STAGING_MAX_BYTES, deadline=deadline)
            if after is None:
                raise storage.StagingConflict('扫描期间暂存文件消失：' + path)
            compare(path, before, after)

    if not samestat(original_directory, directory_info()):
        raise storage.StagingConflict('扫描期间暂存工作目录已替换，已保留清单')
    for path, before in initial.items():
        if eligible(path):
            compare(path, before, None)
    return changes, expected, types


async def sync(db, case, stage):
    """Compatibility entry retaining the caller's SQL transaction through I/O.

    Product lifetimes use sync_job/sync_authorized. This entry remains for
    transaction protocol fixtures and requires coordinate(db, case).
    """
    changes, expected, types = await journal._io(collect, stage, case.s3_prefix)
    if changes:
        await remote.write(db, case, changes, expected_files=expected, content_types=types, scope='staging')
    return {'uploaded': sum(value is not None for value in changes.values()),
            'deleted': sum(value is None for value in changes.values())}


async def sync_job(owner, stage, prefix):
    from ..models import Case
    from .job_execution import _owned
    from .case_write import assert_case_available

    async def checked(db):
        job, attempt = await _owned(db, owner)
        case = await db.get(Case, job.case_id, populate_existing=True)
        from .workflow_state import TERMINAL
        assert_case_available(case)
        if job.status in TERMINAL or attempt.phase not in {'finalizing', 'cancelling'}:
            raise storage.StagingConflict('作业已不处于可同步的收尾阶段')
        if case.s3_prefix != prefix:
            raise storage.StagingConflict('作业算例来源已变化')
        return case

    # The Job logger owns this snapshot through a separate immutable receipt.
    # Never copy the same log again through the mutable-file journal.
    return await sync_checked(stage, prefix, checked,
        excluded=(f'.vibeflow/jobs/job-{owner.job_id}.log',))


async def sync_authorized(stage, prefix, case_id, authorize, *, excluded=(), after_sync=None):
    """Fresh authorization and native-owner checks under case control each time."""
    from .case_write import assert_case_available, CaseWriteConflict
    from .durable_events import lock_case_execution

    async def checked(db):
        await lock_case_execution(db, case_id)
        case = await authorize(db)
        assert_case_available(case)
        if case.id != case_id or case.s3_prefix != prefix:
            raise CaseWriteConflict('input_stale', '会话算例来源已变化，已保留本次副本')
        return case

    return await sync_checked(stage, prefix, checked, excluded=excluded, after_sync=after_sync)


async def sync_checked(stage, prefix, checked, *, excluded=(), after_sync=None):
    """checked(db) takes case control and validates the caller's current rights.

    No outer SQL session may span this operation. Every bulk I/O boundary is
    outside these dedicated transactions; callers clean up only after success.
    """
    from . import s3_staging_transactions as transfer

    from . import storage_operations as operations
    from .s3_storage_recovery import recover_reserved
    async with request_deadline.scope(request_deadline.STAGING_CLEANUP_SECONDS) as deadline:
        with case_copy_io.admit(code='staging_sync_busy', label='暂存同步'):
            async with operations.hold(checked, purpose='staging') as lease:
                await recover_reserved(lease)
                deadline.check()
                changes, expected, types = await remote._io(collect, stage, prefix, excluded)
                deadline.check()
                if changes:
                    await transfer.write_reserved(lease, changes, expected, types)
                if not changes or after_sync is not None:
                    deadline.check()
                    await lease.checkpoint()
                if after_sync is not None:
                    deadline.check()
                    await after_sync(lease)
            deadline.check()
            return {'uploaded': sum(value is not None for value in changes.values()),
                    'deleted': sum(value is None for value in changes.values())}
