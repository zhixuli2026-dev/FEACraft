"""Explicit recovery of real upload sessions with immutable source selection."""
import asyncio,json,re,uuid
from fastapi import HTTPException
from sqlalchemy import select
from .. import storage
from ..db import SessionLocal
from ..models import DurableEvent,UploadStorageReservation
from . import upload_recovery_source as source,upload_storage,uploads,manual_file_recovery as manual
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict,coordinate
from .durable_events import append_event,now
from . import owner_storage_budget,case_read_capacity

SCHEMA='vibeflow.upload-recovery.v1'
REQUESTED='files.upload_recovery.requested'
COMMITTED='files.upload_recovery.committed'
GUARD='files.upload_recovery.source_selected'
NS=uuid.UUID('77da5861-01c5-4dcd-bdb7-dc8906bf942b')


def eid(ticket,kind):return str(uuid.uuid5(NS,ticket+':'+kind))
def gid(case_id,identifier):return str(uuid.uuid5(NS,str(case_id)+':source:'+identifier))
async def io(fn,*args):return await finish_before_cancel(asyncio.to_thread(fn,*args))


async def event(db,case_id,identifier,kind):
    row=await db.get(DurableEvent,identifier,populate_existing=True)
    if row is None:return None
    value=json.loads(row.payload_json)
    if row.case_id!=case_id or row.type!=kind or value['schema']!=SCHEMA or value['case_id']!=case_id:
        raise CaseWriteConflict('upload_recovery_required','上传恢复记录无法核验，请保留现场')
    return value


async def guard(db,case,identifier):
    value=await event(db,case.id,gid(case.id,identifier),GUARD)
    if value is None:return None
    manifest=value['manifest']
    if value['binding']!=upload_storage.binding(case) or manifest['id']!=identifier:
        raise CaseWriteConflict('upload_recovery_required','所选文件与当前上传存储不一致')
    source.validate_blocks(manifest['blocks'],manifest['size'])
    return manifest


async def listing(case_id,user_id,cursor='',limit=50):
    if cursor:source.check_id(cursor)
    async with SessionLocal() as db:
        case=await manual.authorized(db,case_id,user_id)
        async with case_read_capacity.reading(db,case):
            rows=list(await db.scalars(select(UploadStorageReservation).where(UploadStorageReservation.case_id==case.id,
                UploadStorageReservation.id>cursor).order_by(UploadStorageReservation.id).limit(limit+1)))
            ids=sorted({row.id for row in rows}|set(await io(source.identifiers,case.s3_prefix,cursor,limit+1)))
            page=ids[:limit];entries=[]
            for identifier in page:
                row=await db.get(UploadStorageReservation,identifier,populate_existing=True)
                if row is not None and row.case_id!=case_id:continue
                if row is not None and row.state!='active':continue
                from .manual_upload_recovery import closed
                if await closed(db,case,identifier) is not None:continue
                if await uploads.completion_record(db,case,identifier) is not None:continue
                item={'id':identifier,'path':row.path if row else None,'size':row.size if row else None,
                    'offset':None,'status':'unavailable','legacy_without_reservation':row is None}
                try:
                    if row is not None and row.binding_json!=upload_storage.binding(case):
                        raise uploads.UploadError('上传存储位置已变化，请保留现场检查',409)
                    meta,offset,_=await io(source.metadata,case.s3_prefix,identifier)
                    item.update(path=meta['path'],size=meta['size'],offset=offset,status='uploading',
                        owner_reconciliation_required=row is not None and row.user_id!=(await owner_storage_budget.owner_for_case(db,case)).id)
                except (ValueError,OSError,KeyError,TypeError):
                    item['message']='上传元数据或存储位置无法核验，请保留现场；不会自动续传或清理'
                    item['manual_source']='uploads/'+identifier
                entries.append(item)
            return {'schema':SCHEMA,'entries':entries,'has_more':len(ids)>limit,
                'next_cursor':page[-1] if len(ids)>limit and page else None}


async def observed(db,case,identifier):
    row=await db.get(UploadStorageReservation,identifier,populate_existing=True)
    if row is not None:
        if row.case_id!=case.id:raise HTTPException(404,'上传会话不存在')
        if row.binding_json!=upload_storage.binding(case):raise uploads.UploadError('上传存储位置已变化，请保留现场',409)
        if row.state!='active':raise uploads.UploadError('上传已经结算，请查询原结果',409)
    if await uploads.completion_record(db,case,identifier) is not None:raise uploads.UploadError('上传已经完成',409)
    return await io(source.snapshot,manual.reference(case),identifier)


async def plan(case_id,user_id,identifier):
    source.check_id(identifier)
    async with SessionLocal() as db:
        case=await manual.authorized(db,case_id,user_id)
        async with case_read_capacity.reading(db,case):
            value=await observed(db,case,identifier);readonly=await manual.write_access(db,case_id,user_id)
            blocked=readonly
            if blocked is None:
                try:
                    await manual.drained(db,case)
                    await upload_storage.row_for(db,case,await owner_storage_budget.owner_for_case(db,case),identifier)
                except CaseWriteConflict as error:blocked={'code':error.code,'message':str(error)}
            meta=value['metadata'];actions=[] if readonly else ['retain_for_review']
            if blocked is None:
                actions.append('cancel_upload')
                if value['target_matches']:actions.append('resume_upload')
            return {'schema':SCHEMA,'id':identifier,'path':meta['path'],'size':meta['size'],
                'snapshot_sha256':source.digest(value),'offset':value['content']['offset'],
                'prefix_blocks':value['content']['blocks'],'declared_sha256':meta.get('sha256'),
                'target':{'path':meta['path'],'exists':value['target'] is not None,
                    'size':value['target']['size'] if value['target'] else 0,
                    'content_sha256':meta['target_sha256'],'protected':False},
                'target_matches':value['target_matches'],'available_actions':actions,'blocked':blocked,
                'original_file_identity_known':meta.get('sha256') is not None}


async def receipt(db,case_id,ticket):
    value=await event(db,case_id,eid(ticket,COMMITTED),COMMITTED)
    if value is not None:return value['receipt']
    requested=await event(db,case_id,eid(ticket,REQUESTED),REQUESTED)
    if requested is None:raise HTTPException(404,'上传恢复记录不存在')
    return {'schema':SCHEMA,'id':ticket,'case_id':case_id,'upload_id':requested['body']['upload_id'],
        'action':requested['body']['action'],'state':'pending','note':requested['body']['note'],
        'recorded_at':requested['recorded_at'],'result':None,'stop_evidence_created':False}


async def commit(db,case,requested,result):
    ticket=requested['id'];body=requested['body']
    value={'schema':SCHEMA,'id':ticket,'case_id':case.id,'upload_id':body['upload_id'],'action':body['action'],
        'state':'completed','note':body['note'],'recorded_at':requested['recorded_at'],'result':result,'stop_evidence_created':False}
    await append_event(db,case.id,COMMITTED,{'schema':SCHEMA,'case_id':case.id,'receipt':value},event_id=eid(ticket,COMMITTED))
    return value


async def reconcile_owner(case_id,user_id):
    if not storage._use_s3():return
    async with SessionLocal() as db:
        case=await manual.authorized(db,case_id,user_id,write=True)
        owner_id=(await owner_storage_budget.owner_for_case(db,case)).id
    await owner_storage_budget.recover_owner(owner_id)


async def perform(case_id,user_id,body,key):
    if not body['note'].strip():raise HTTPException(422,'请填写本次处理原因')
    if body['action']!='resume_upload' and (body.get('source_blocks') is not None or body.get('prefix_blocks') is not None):
        raise HTTPException(422,'本次操作不接受续传文件摘要')
    if not isinstance(key,str) or re.fullmatch('[A-Za-z0-9_-]{8,128}',key) is None:
        raise HTTPException(422,'请提供固定的上传恢复确认标识')
    identifier=source.check_id(body['upload_id']);ticket=str(uuid.uuid5(NS,str(case_id)+':'+str(user_id)+':'+key))
    async with SessionLocal() as db:
        await manual.authorized(db,case_id,user_id,write=True)
        old=await event(db,case_id,eid(ticket,REQUESTED),REQUESTED)
        if old is not None:
            if old['body']!=body or old['actor_user_id']!=user_id:raise CaseWriteConflict('idempotency_conflict','同一标识已绑定另一份上传确认')
            finished=await event(db,case_id,eid(ticket,COMMITTED),COMMITTED)
            if finished is not None:return finished['receipt']
    if old is not None and body['action']=='cancel_upload':
        return await finish_cancel(case_id,user_id,identifier,ticket,body,old)
    await reconcile_owner(case_id,user_id)
    async with SessionLocal() as db:
        case=await manual.authorized(db,case_id,user_id,write=True)
        async with owner_storage_budget.hold_case(db,case) as owner:
            async with coordinate(db,case,content_write=False):
                await manual.authorized(db,case_id,user_id,write=True)
                requested=await event(db,case_id,eid(ticket,REQUESTED),REQUESTED)
                if requested is not None:
                    if requested['body']!=body:raise CaseWriteConflict('idempotency_conflict','同一标识已绑定另一份上传确认')
                    finished=await event(db,case_id,eid(ticket,COMMITTED),COMMITTED)
                    if finished is not None:return finished['receipt']
                else:
                    value=await observed(db,case,identifier)
                    if source.digest(value)!=body['snapshot_sha256']:raise CaseWriteConflict('input_stale','上传来源或目标已经变化，请重新核对')
                    requested={'schema':SCHEMA,'id':ticket,'case_id':case_id,'actor_user_id':user_id,
                        'recorded_at':now().isoformat(),'body':body,'source':value}
                    await append_event(db,case_id,REQUESTED,requested,event_id=eid(ticket,REQUESTED))
                if body['action']=='retain_for_review':
                    result=await commit(db,case,requested,{'original_bytes_retained':True,'write_limits_relaxed':False})
                elif body['action']=='resume_upload':
                    await manual.drained(db,case)
                    value=await observed(db,case,identifier)
                    if source.digest(value)!=body['snapshot_sha256'] or not value['target_matches']:
                        raise CaseWriteConflict('input_stale','上传来源或目标已经变化，请重新核对')
                    meta=value['metadata'];blocks=body.get('source_blocks');source.validate_blocks(blocks,meta['size'])
                    prefix=body.get('prefix_blocks')
                    if prefix!=value['content']['blocks']:raise uploads.UploadError('所选文件与已上传部分不同',409)
                    for index,record in enumerate(prefix):
                        if record['size']==source.BLOCK or value['content']['offset']==meta['size']:
                            if record['sha256']!=blocks[index]:raise uploads.UploadError('所选文件与已上传部分不同',409)
                    manifest={'id':identifier,'path':meta['path'],'size':meta['size'],'blocks':blocks}
                    previous=await guard(db,case,identifier)
                    if previous is not None and previous!=manifest:
                        raise uploads.UploadError('原上传已绑定另一份文件，请保留或取消后重新上传',409)
                    await upload_storage.reconcile(db,case,owner,identifier)
                    if previous is None:
                        await append_event(db,case_id,GUARD,{'schema':SCHEMA,'case_id':case_id,'binding':upload_storage.binding(case),
                            'manifest':manifest,'confirmation_id':ticket},event_id=gid(case_id,identifier))
                    result=await commit(db,case,requested,{'upload':uploads._status(meta,value['content']['offset']),
                        'source_selected':True,'target_file_written':False,'manifest_sha256':source.digest(manifest)})
                else:result=None
            await finish_before_cancel(db.commit())
    if result is not None:return result
    return await finish_cancel(case_id,user_id,identifier,ticket,body,requested)


async def finish_cancel(case_id,user_id,identifier,ticket,body,requested):
    # Cancellation uses the normal physical cleanup, quota settlement and late
    # chunk fences. Only the immutable requested event is committed beforehand.
    async def validate(db,case,row):
        await manual.authorized(db,case_id,user_id,write=True);await manual.drained(db,case)
        if upload_storage.binding(case)!=requested['source']['binding']:
            raise CaseWriteConflict('input_stale','上传存储位置已变化，请保留现场')
        if storage._use_s3():return  # The case lease guards the remote comparison below.
        if row is not None and row.state=='cancelled':return
        if row is not None and not uploads._session_dir(case.s3_prefix,identifier).exists():return
        value=await observed(db,case,identifier)
        if source.digest(value)!=body['snapshot_sha256']:raise CaseWriteConflict('input_stale','上传内容已变化，本次未取消')

    async def verify(case, already_cancelled):
        if already_cancelled:
            return
        from .s3_uploads import _meta

        meta, _ = await io(_meta, case.s3_prefix, identifier)
        original = requested['source']['metadata']
        mutable = {'status', 'updated_at'}
        if (meta.get('status') == 'cancelled'
                and {k:v for k,v in meta.items() if k not in mutable}
                == {k:v for k,v in original.items() if k not in mutable}):
            return
        value = await io(source.snapshot, case, identifier)
        if source.digest(value) != body['snapshot_sha256']:
            raise CaseWriteConflict('input_stale', '上传内容已变化，本次未取消')

    async def record(db,case):
        finished=await event(db,case_id,eid(ticket,COMMITTED),COMMITTED)
        if finished is None:await commit(db,case,requested,{'cancelled':True,'target_file_written':False})
    async with SessionLocal() as db:
        case=await manual.authorized(db,case_id,user_id,write=True)
        await uploads.cancel_with_ledger(db,case,identifier,user_id=user_id,validate=validate,verify=verify,record=record)
        return await receipt(db,case_id,ticket)


async def history(case_id,user_id,after=0,limit=50):
    async with SessionLocal() as db:
        await manual.authorized(db,case_id,user_id)
        rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.case_id==case_id,DurableEvent.type==REQUESTED,
            DurableEvent.seq>after).order_by(DurableEvent.seq).limit(limit+1)))
        return {'entries':[await receipt(db,case_id,json.loads(row.payload_json)['id']) for row in rows[:limit]],
            'has_more':len(rows)>limit,'next_cursor':rows[limit-1].seq if len(rows)>limit else None}
