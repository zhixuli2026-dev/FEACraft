"""Explicit SQL-only reconciliation of legacy collaborator upload promises.

No remote write pool is opened: acquire both quota owners and case control,
validate retained storage, then move the promise and audit in one SQL commit.
Unresolved writers and changed bindings are never silently reassigned.
"""
import asyncio,hashlib,json,re,uuid
from fastapi import HTTPException
from sqlalchemy import select
from ..models import (Workspace,UploadStorageReservation,OwnerStorageReservation,
    StorageWriteReservation,UploadChunkCredit,DurableEvent)
from . import upload_storage,uploads
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict,assert_case_available
from .durable_events import append_event,lock_case_execution,now
from .storage_admission import lock_owner

SCHEMA='vibeflow.upload-owner-reconciliation.v1'
EVENT='files.upload_owner_reconciled'


def conflict(message):return CaseWriteConflict('upload_owner_recovery_required',message)


async def owned(db,case,user):
    workspace=await db.get(Workspace,case.workspace_id,populate_existing=True)
    if workspace is None or workspace.user_id!=user.id:
        raise HTTPException(404,'上传归属修复仅由项目所有者发起')
    # A moved/rebound physical namespace needs its separate ownership proof.
    if not case.s3_prefix.startswith(f'workspaces/{user.id}/'):
        raise conflict('项目目录与当前所有者不一致，请先核对存储归属')
    assert_case_available(case)


async def reconcile(db,case,user,upload_id):
    try:
        if not re.fullmatch('[0-9a-f]{32}',upload_id):raise uploads.UploadError('上传会话不存在',404)
        await owned(db,case,user)
        await lock_owner(db,user.id)
        row=await db.get(UploadStorageReservation,upload_id,populate_existing=True)
        if row is None or row.case_id!=case.id:raise uploads.UploadError('上传会话不存在',404)
        old_user_id=row.user_id
        if old_user_id!=user.id:
            # PostgreSQL try-lock avoids waiting behind a reverse owner/case
            # order; SQLite already serializes this writer transaction.
            await lock_owner(db,old_user_id)
        await lock_case_execution(db,case.id)
        await db.refresh(case);await owned(db,case,user)
        row=await db.get(UploadStorageReservation,upload_id,populate_existing=True)
        if row is None or row.case_id!=case.id:raise uploads.UploadError('上传会话不存在',404)
        if row.binding_json!=upload_storage.binding(case):
            raise conflict('上传记录与当前存储绑定不同，拒绝转移额度')
        identifier=str(uuid.uuid5(uuid.NAMESPACE_URL,f'vibeflow:upload-owner:{case.id}:{upload_id}'))
        previous=await db.get(DurableEvent,identifier,populate_existing=True)
        if row.user_id==user.id:
            if previous is None:
                result={'id':upload_id,'case_id':case.id,'owner_id':user.id,'reconciled':False,'replayed':True}
            else:
                value=json.loads(previous.payload_json)
                if previous.case_id!=case.id or previous.type!=EVENT or value.get('schema')!=SCHEMA or value.get('upload_id')!=upload_id or value.get('new_user_id')!=user.id:
                    raise conflict('上传归属审计与当前记录不匹配，请保留现场检查')
                result={'id':upload_id,'case_id':case.id,'owner_id':user.id,'event_id':previous.id,'reconciled':True,'replayed':True}
            await finish_before_cancel(db.commit());return result
        if row.user_id!=old_user_id or previous is not None:
            raise conflict('上传归属在核验期间变化，请保留现场重试')
        for query in [
            select(OwnerStorageReservation.id).where(OwnerStorageReservation.user_id==old_user_id,OwnerStorageReservation.state=='active'),
            select(StorageWriteReservation.id).where(StorageWriteReservation.user_id==old_user_id,StorageWriteReservation.case_id==case.id,StorageWriteReservation.state=='active'),
            select(UploadChunkCredit.id).where(UploadChunkCredit.upload_id==upload_id,UploadChunkCredit.state=='active')]:
            if await db.scalar(query.limit(1)) is not None:
                raise conflict('旧上传仍有活动或未核验的写入额度，请先结算或恢复原写入者')
        remaining=0
        if row.state=='active':
            status=await finish_before_cancel(asyncio.to_thread(uploads.session_status,case.s3_prefix,upload_id))
            if (status['id'],status['path'],status['size'])!=(row.id,row.path,row.size):
                raise conflict('上传实际元数据与额度记录不一致，拒绝转移')
            offset=status['offset']
            if type(offset) is not int or not 0<=offset<=row.size:
                raise conflict('上传实际进度无法核验，拒绝转移')
            remaining=row.size-offset
        elif row.state not in {'completed','cancelled'} or row.remaining_bytes!=0:
            raise conflict('上传结算记录无法核验，拒绝转移')
        if remaining:
            from .quota import check_storage_quota
            # Old promise remains charged to the old owner until this succeeds.
            # Physical bytes are already included in the current owner's usage.
            await check_storage_quota(db,user,remaining)
        document={'schema':SCHEMA,'case_id':case.id,'upload_id':upload_id,
            'old_user_id':old_user_id,'new_user_id':user.id,
            'old_remaining_bytes':row.remaining_bytes,'remaining_bytes':remaining,
            'state':row.state,'binding_sha256':hashlib.sha256(row.binding_json.encode('utf8')).hexdigest()}
        row.user_id=user.id;row.remaining_bytes=remaining;row.updated_at=now()
        await append_event(db,case.id,EVENT,document,event_id=identifier)
        await finish_before_cancel(db.commit())
        return {'id':upload_id,'case_id':case.id,'owner_id':user.id,'event_id':identifier,'reconciled':True,'replayed':False}
    except BaseException:
        await finish_before_cancel(db.rollback());raise
