"""Remaining upload payload reconciled under owner + case transaction locks.

No TTL releases quota. Physical retained bytes remain charged by inventory after
completion/cancellation; this ledger reserves only payload not yet materialized.
"""
import asyncio
import re
from pathlib import Path
from ..models import UploadStorageReservation
from ..config import get_settings
from .async_ops import finish_before_cancel
from .durable_events import json_text,now
from .storage_admission import lock_owner
from .uploads import UploadError,session_status


def binding(case):
    settings=get_settings()
    location=({'root':str(Path(settings.storage_fs_root).resolve())} if settings.storage_backend=='fs'
        else {'endpoint':settings.s3_endpoint,'bucket':settings.s3_bucket})
    return json_text({'case_id':case.id,'prefix':case.s3_prefix,'backend':settings.storage_backend,**location})


async def row_for(db,case,user,upload_id):
    if not re.fullmatch('[0-9a-f]{32}',upload_id):raise UploadError('上传会话不存在',404)
    from .manual_upload_recovery import require_open
    await require_open(db,case,upload_id)
    row=await db.get(UploadStorageReservation,upload_id,populate_existing=True)
    if row is not None and row.case_id!=case.id:
        raise UploadError('上传会话不存在',404)
    if row is not None and row.binding_json!=binding(case):
        raise UploadError('上传额度归属与存储现场不一致，请保留现场检查',409)
    if row is not None and row.user_id!=user.id:
        from .case_write import CaseWriteConflict
        raise CaseWriteConflict('upload_owner_reconciliation_required',
            '旧上传额度仍属于协作者，请由项目所有者发起上传归属修复后继续')
    return row


async def register(db,case,user,status):
    # Caller checked the whole declared increment while holding owner admission.
    row=UploadStorageReservation(id=status['id'],user_id=user.id,case_id=case.id,
        binding_json=binding(case),path=status['path'],size=status['size'],
        remaining_bytes=status['size']-status['offset'],state='active')
    db.add(row);await db.flush();return row


async def reconcile(db,case,user,upload_id):
    await lock_owner(db,user.id)
    row=await row_for(db,case,user,upload_id)
    if row is not None and row.state!='active':raise UploadError('上传额度已结算，不能继续写入',409)
    status=await finish_before_cancel(asyncio.to_thread(session_status,case.s3_prefix,upload_id))
    if row is None:
        # Legacy sessions are adopted only after their remaining payload fits.
        # Already materialized bytes are counted by the physical inventory.
        from .quota import check_storage_quota
        await check_storage_quota(db,user,status['size']-status['offset'])
        row=await register(db,case,user,status)
    else:await progress(db,row,status)
    return row,status


async def progress(db,row,status):
    if (status['id'],status['path'],status['size'])!=(row.id,row.path,row.size):
        raise UploadError('上传额度与声明内容不一致',409)
    offset=status['offset']
    if type(offset) is not int or not 0<=offset<=row.size:
        raise UploadError('上传进度无法核验',409)
    row.remaining_bytes=row.size-offset;row.updated_at=now();await db.flush()


async def settle(db,case,user,upload_id,state):
    if state not in {'completed','cancelled'}:raise ValueError('Invalid upload settlement')
    row=await row_for(db,case,user,upload_id)
    if row is None:return  # Completed/cancelled legacy session has no claim.
    if row.state not in {'active',state}:raise UploadError('上传额度结算状态冲突',409)
    row.state=state;row.remaining_bytes=0;row.updated_at=now();await db.flush()
