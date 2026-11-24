"""Settle a stopped CLI's retained S3 stage, then release its execution."""
import asyncio
import json
import os
import re

from .. import storage
from ..config import get_settings
from ..db import SessionLocal
from . import codex_records, codex_sessions, codex_turn_recovery as recovery
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict
from .codex_permissions import READ_ONLY
from .request_deadline import scope, STAGING_CLEANUP_SECONDS
from .staging_cleanup import intent
from .workflow_state import ExecutionConflict


def stage_paths(selected):
    stage = selected.get('staging_prefix')
    if not isinstance(stage, str) or not re.fullmatch(r'\.staging/job-[0-9]+-[0-9a-f]{32}', stage):
        raise ExecutionConflict('evidence_invalid', 'CLI 暂存目录绑定无效')
    root = storage._fs_path(stage, '.').resolve()
    if str(root) != selected['cwd']:
        raise ExecutionConflict('evidence_invalid', 'CLI 暂存工作目录已变化')
    return root, storage._staging_manifest_path(stage), storage._terminal_cleanup_marker(stage)


async def reconcile(identifier, user_id):
    async with scope(STAGING_CLEANUP_SECONDS):
        # Reject a changed object-store destination before scope publication
        # invokes any case/file-journal reconciliation against that destination.
        async with SessionLocal() as db:
            prior_turn, prior, _, receipt, released = await recovery.authorized(db, identifier, user_id)
            original = json.loads(prior.profile_json)
            # A validated receipt already proves the old scope stopped. Reuse
            # it while settling retained storage work; republishing under the
            # case fence would reject the very storage journal we must drain.
            result = await recovery.output(db, prior_turn, receipt, released) if receipt is not None else None
        if (original.get('staging_prefix') is not None and not released
                and original['permission_mode'] not in READ_ONLY):
            original_paths = stage_paths(original)
            if any(os.path.lexists(p) for p in original_paths) and not os.path.lexists(original_paths[2]):
                await source_bound(original)
        if result is None:
            result = await recovery.recover(identifier, user_id)
        async with SessionLocal() as db:
            row, session, _, receipt, released = await recovery.authorized(db, identifier, user_id)
            selected = json.loads(session.profile_json)
            if selected.get('staging_prefix') is None:
                return result
            paths = stage_paths(selected)
            if released:
                if any(os.path.lexists(p) for p in paths):
                    raise ExecutionConflict('evidence_invalid', '已释放会话仍有暂存文件，请保留现场')
                return {**result, 'staging_reconciled': True}
            binding = recovery.turns.digest(receipt)
            case_id = row.case_id
        if get_settings().storage_backend != 's3':
            raise ExecutionConflict('input_stale', 'CLI 原对象存储运行环境已变化')

        async def authorized(db):
            row, session, _, current, released = await recovery.authorized(db, identifier, user_id)
            if released or current is None or recovery.turns.digest(current) != binding:
                raise ExecutionConflict('state_conflict', 'CLI 恢复归属已变化，请刷新后重试')
            if json.loads(session.profile_json) != selected or codex_records.public(session)['cleanup_verified'] is not True:
                raise ExecutionConflict('evidence_invalid', 'CLI 完整停止及目录绑定尚未核验')
            case, _ = await codex_sessions.authorize(db, case_id, row.conversation_id, user_id, expected=selected)
            stage_paths(selected)
            return case

        async def cleanup(lease, *, synchronized=True):
            await lease.checkpoint()
            from ..routers.terminal import _cleanup_staging
            await finish_before_cancel(asyncio.to_thread(_cleanup_staging, selected['staging_prefix'], synchronized=synchronized))

        marker = paths[2]
        read_only = selected['permission_mode'] in READ_ONLY
        if read_only or os.path.lexists(marker) or not any(os.path.lexists(p) for p in paths):
            # Partial cleanup is never a new remote deletion snapshot.
            from . import storage_operations
            from .durable_events import lock_case_execution
            async def checked(db):
                await lock_case_execution(db, case_id)
                return await authorized(db)
            async with storage_operations.hold(checked, purpose='staging') as lease:
                from .s3_storage_recovery import recover_reserved
                await recover_reserved(lease)
                if os.path.lexists(marker):
                    from .sandbox_staging_cleanup import read
                    value = json.loads(await asyncio.to_thread(read, marker, 4096))
                    synchronized = value == intent(selected['staging_prefix'], synchronized=True)
                    if not synchronized and not (read_only and value == intent(selected['staging_prefix'], synchronized=False)):
                        raise ExecutionConflict('evidence_invalid', 'CLI 暂存清理记录未证明同步完成')
                    await cleanup(lease, synchronized=synchronized)
                elif read_only:
                    await cleanup(lease, synchronized=False)
                elif any(os.path.lexists(p) for p in paths):
                    raise ExecutionConflict('state_conflict', 'CLI 暂存文件在恢复期间发生变化')
        else:
            from .staging_sync import sync_authorized
            try:
                await source_bound(selected)
                await sync_authorized(selected['staging_prefix'], selected['source_prefix'], case_id,
                                      authorized, after_sync=cleanup)
            except storage.StagingConflict as error:
                raise CaseWriteConflict('staging_conflict', str(error)) from error
        result = await recovery.recover(identifier, user_id)
        return {**result, 'staging_reconciled': not any(os.path.lexists(p) for p in paths)}


async def source_bound(selected):
    from .staging_sync import load
    try:
        _, manifest = await finish_before_cancel(asyncio.to_thread(load, selected['staging_prefix'], selected['source_prefix']))
    except storage.StagingConflict as error:
        raise CaseWriteConflict('staging_conflict', str(error)) from error
    if 'storage' not in manifest:
        raise ExecutionConflict('recovery_required', '旧暂存副本缺少对象存储地址记录，请保留文件核对原存储')
