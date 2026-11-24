"""Atomically transfer future payload; reconcile only from bound remote proof."""
import asyncio
from dataclasses import dataclass
import re
from sqlalchemy import select
from fastapi import HTTPException
from botocore.exceptions import BotoCoreError,ClientError
from ..models import Case,UploadStorageReservation,UploadChunkCredit
from .. import storage
from . import upload_storage,s3_uploads
from .uploads import UploadError,MAX_CHUNK_BYTES
from .async_ops import finish_before_cancel
from .durable_events import json_text,now,lock_case_execution


BOUNDED_RECEIPT = json_text({'schema': 'vibeflow.bounded-upload-chunk.v1'})


async def _case_and_row(db,user,request,*,locked):
    from .owner_storage_budget import owner_for_case
    from .case_write import assert_case_available
    identifier=request['upload_id'];offset=request['offset'];size=request['size']
    if type(offset) is not int or offset<0 or type(size) is not int or not 1<=size<=MAX_CHUNK_BYTES:
        raise UploadError('非法分块范围',413 if size>MAX_CHUNK_BYTES else 400)
    case=await db.get(Case,request['case_id'],populate_existing=True)
    if case is None:raise UploadError('上传会话不存在',404)
    if locked:
        await lock_case_execution(db,case.id)
        await db.refresh(case)
    if (await owner_for_case(db,case)).id!=user.id:
        raise UploadError('上传会话不存在',404)
    assert_case_available(case)
    if locked:
        from .storage_operations import admit_locked

        await admit_locked(db, case.id)
    if await db.scalar(select(UploadChunkCredit.id).where(
            UploadChunkCredit.upload_id==identifier,UploadChunkCredit.state=='active')) is not None:
        raise HTTPException(409,{'code':'upload_quota_recovery_required','message':'上次分块额度尚未核验，请保留会话重试'})
    row=await upload_storage.row_for(db,case,user,identifier)
    if row is not None and row.state!='active':raise UploadError('上传额度已结算，不能继续写入',409)
    return case,row


@dataclass(frozen=True)
class Observation:
    prefix: str
    binding_json: str
    status: dict
    meta_etag: str
    data_path: str
    data_etag: str|None

    def still_current(self):
        from .s3_streams import snapshot_head

        def version(path,limit):
            head=snapshot_head(self.prefix,path,limit)
            return None if head is None else head['ETag']

        return (version(s3_uploads.directory(self.status['id'])+'/meta.json',65536)==self.meta_etag
                and version(self.data_path,MAX_CHUNK_BYTES)==self.data_etag)


def _observe(prefix,bound,request):
    from .s3_streams import snapshot
    identifier=request['upload_id'];offset=request['offset'];size=request['size']
    meta,etag=s3_uploads._meta(prefix,identifier)
    if meta['status']=='cancelled':raise UploadError('上传已取消',404)
    status=s3_uploads._status(meta)
    if status['offset']!=offset or offset+size>status['size']:
        raise UploadError('分块偏移与远端进度不一致',409)
    path=s3_uploads.directory(identifier)+f'/chunks/{offset:010d}/data'
    try:observed,data_etag,_=snapshot(prefix,path,limit=size)
    except ValueError as error:
        raise UploadError('已存在的远端分块与本次重试不一致，请保留会话检查',409) from error
    expected={'size':size,'sha256':request['sha256']}
    if observed is not None and observed!=expected:
        raise UploadError('已存在的远端分块与本次重试不一致，请保留会话检查',409)
    return Observation(prefix,bound,status,etag,path,data_etag)


async def observe(user_id,request):
    from ..db import SessionLocal
    from ..models import User
    async with SessionLocal() as db:
        user=await db.get(User,user_id)
        if user is None:raise ValueError('Capacity owner is missing')
        case,_=await _case_and_row(db,user,request,locked=False)
        prefix,bound=case.s3_prefix,upload_storage.binding(case)
    return await finish_before_cancel(asyncio.to_thread(_observe,prefix,bound,request))


async def prepare(db,user,request,observation,inventory):
    """Apply the observed transfer after the owner epoch has been locked."""
    from .quota import check_locked_storage_quota
    from .storage_admission import InventoryOutdated
    identifier=request['upload_id'];offset=request['offset'];size=request['size']
    case,row=await _case_and_row(db,user,request,locked=True)
    if upload_storage.binding(case)!=observation.binding_json:raise InventoryOutdated()
    status=observation.status
    if row is None:
        # Adoption still requires all unmaterialized bytes to fit before
        # installing the legacy upload's first durable reservation.
        await check_locked_storage_quota(db,user,status['size']-offset,inventory=inventory)
        row=await upload_storage.register(db,case,user,status)
    else:
        await upload_storage.progress(db,row,status)
    # An earlier data PUT may have completed before metadata advanced. Its
    # exact immutable bytes were observed before inventory and are already
    # charged as physical usage. A later arrival retains its pending credit.
    materialized=observation.data_etag is not None
    row.remaining_bytes=status['size']-offset-(size if materialized else 0)
    await db.flush()
    await check_locked_storage_quota(db,user,0,inventory=inventory)
    return row,dict(upload_id=identifier,binding_json=observation.binding_json,offset=offset,size=size,
        amount=0 if materialized else size,sha256=request['sha256'],key=storage._s3_key(case.s3_prefix,observation.data_path))


async def completed_pending(db,ticket,request):
    """Both immutable objects of an admitted chunk remain occupied after closure."""
    from .attempt_objects import digest
    from .s3_streams import snapshot
    credit=await db.get(UploadChunkCredit,ticket['id'])
    if credit is None:return None
    upload=await db.get(UploadStorageReservation,credit.upload_id)
    if upload is None or upload.user_id!=ticket['user_id'] or upload.binding_json!=credit.binding_json:return None
    case=await db.get(Case,upload.case_id)
    if case is None or upload_storage.binding(case)!=credit.binding_json:return None
    base=s3_uploads.directory(upload.id)+f'/chunks/{credit.offset:010d}/'
    expected={'size':credit.size,'sha256':credit.sha256}
    path=base+'data'
    if request.get('key')==storage._s3_key(case.s3_prefix,base+'record.json'):
        path=base+'record.json'
        expected=digest(json_text({'offset':credit.offset,**expected}).encode())
    if (request.get('key'),request.get('size'),request.get('sha256'),request.get('if_none_match'),request.get('if_match'))!=(
            storage._s3_key(case.s3_prefix,path),expected['size'],expected['sha256'],'*',None):return None
    observed,etag,_=await finish_before_cancel(asyncio.to_thread(snapshot,case.s3_prefix,path,limit=expected['size']))
    if observed is None:return None
    if observed!=expected:
        if observed['size']!=0 or upload.state not in {'completed','cancelled'}:return None
    return {'schema':'vibeflow.upload-chunk-proof.v1','etag':etag,
            'sha256':observed['sha256'],'closed':observed['size']==0}


async def completed_metadata(db, ticket, request):
    """Prove a bound conditional creation or exact terminal replacement."""
    from .s3_live_store import split
    from .attempt_objects import digest
    from .s3_file_transactions import _read
    prefix, path = split(request['key'])
    match = re.fullmatch(r'\.vibeflow/uploads/([0-9a-f]{32})/meta\.json', path or '')
    old_etag = request.get('if_match')
    creating = request.get('if_none_match') == '*' and old_etag is None
    replacing = isinstance(old_etag, str) and re.fullmatch(r'"[^"*,]+"', old_etag) and request.get('if_none_match') is None
    if not match or not (creating or replacing):
        return None
    identifier = match[1]
    upload = await db.get(UploadStorageReservation, identifier)
    if upload is None or upload.user_id != ticket['user_id']:
        return None
    case = await db.get(Case, upload.case_id)
    if case is None or case.s3_prefix.rstrip('/') != prefix or upload_storage.binding(case) != upload.binding_json:
        return None
    data, etag, _ = await finish_before_cancel(asyncio.to_thread(_read, case.s3_prefix, path, 65536))
    if creating:
        from . import upload_creation
        import uuid
        row = await upload_creation.event(db, case, uuid.UUID(hex=identifier))
        if row is None or row.type != upload_creation.PREPARED or data is None:
            return None
        import json
        initial = upload_creation.remote_meta(case.s3_prefix, json.loads(row.payload_json)['initial'])
        expected = digest(json_text(initial).encode())
        if expected != {k:request[k] for k in ('size','sha256')}:
            return None
        if data == b'':
            from .uploads import completion_record
            if upload.state != 'completed' or await completion_record(db,case,identifier) is None:
                return None
        else:
            meta = s3_uploads._decode_meta(case.s3_prefix,identifier,data)
            if any(meta.get(k) != v for k,v in initial.items() if k not in {'offset','chunk_count','status','updated_at'}):
                return None
        return {'schema':'vibeflow.upload-create-proof.v1','etag':etag,'sha256':expected['sha256']}
    if data is None or etag == old_etag or digest(data) != {k:request[k] for k in ('size', 'sha256')}:
        return None
    meta = s3_uploads._decode_meta(case.s3_prefix, identifier, data)
    if (meta['status'], meta['path'], meta['size']) != ('cancelled', upload.path, upload.size):
        return None
    return {'schema':'vibeflow.upload-cancel-proof.v1', 'etag':etag, 'sha256':request['sha256']}


async def registered_offsets(db, upload_id):
    return list(await db.scalars(select(UploadChunkCredit.offset).where(
        UploadChunkCredit.upload_id == upload_id).distinct()))


def chunk_paths(upload_id, offsets):
    base=s3_uploads.directory(upload_id)+'/chunks/'
    return (f'{base}{offset:010d}/{name}' for offset in offsets for name in ('data','record.json'))


async def close_pending_paths(case,paths):
    """Close the captured keys after a caller releases its SQL control lock."""
    from .s3_file_transactions import _conditional
    from .s3_streams import snapshot
    from .request_deadline import current

    deadline = current()
    for path in paths:
        for _ in range(3):
            if deadline is not None:
                deadline.check()
            observed,etag,_=await finish_before_cancel(asyncio.to_thread(snapshot,case.s3_prefix,path,limit=MAX_CHUNK_BYTES))
            if observed is not None and observed['size']==0:break
            try:
                await finish_before_cancel(asyncio.to_thread(_conditional,case.s3_prefix,path,b'',etag,
                    {'ContentType':'application/vnd.vibeflow.closed-upload-chunk'}))
                break
            except Exception as error:
                from .case_write import CaseWriteConflict
                if not isinstance(error,CaseWriteConflict):raise
        else:raise UploadError('上传关闭标记写入冲突，请重试取消',409)


async def install(db,ticket,row,credit):
    row.remaining_bytes-=credit['amount'];row.updated_at=now()
    db.add(UploadChunkCredit(id=ticket['id'],state='active',receipt_json=BOUNDED_RECEIPT,
        **{k:v for k,v in credit.items() if k!='key'}))
    await db.flush()


async def _settlement_rows(db,ticket):
    credit=await db.get(UploadChunkCredit,ticket['id'],populate_existing=True)
    if credit is None or credit.state=='settled':return credit,None,None
    upload=await db.get(UploadStorageReservation,credit.upload_id,populate_existing=True)
    if upload is None or upload.user_id!=ticket['user_id'] or upload.binding_json!=credit.binding_json:
        return credit,upload,None
    case=await db.get(Case,upload.case_id,populate_existing=True)
    return credit,upload,case


def _settlement_basis(credit,upload,case):
    def record(row,fields):return None if row is None else tuple(getattr(row,key) for key in fields.split())
    return (record(credit,'id upload_id binding_json offset size amount sha256 state receipt_json'),
            record(upload,'id user_id case_id binding_json path size remaining_bytes state updated_at'),
            upload_storage.binding(case) if case is not None else None)


async def observe_settlement(db,ticket):
    """Read a bound outcome without changing the upload credit or reservation."""
    credit,upload,case=await _settlement_rows(db,ticket)
    basis=_settlement_basis(credit,upload,case)
    if credit is None or credit.state=='settled':return basis,{}
    if case is None or upload_storage.binding(case)!=credit.binding_json:return basis,None
    if upload.state in {'completed','cancelled'}:
        return basis,{'state':upload.state,'remaining_bytes':0}
    try:
        meta,_=await finish_before_cancel(asyncio.to_thread(s3_uploads._meta,case.s3_prefix,upload.id))
        if (meta['path'],meta['size'])!=(upload.path,upload.size) or meta['offset']<credit.offset:
            return basis,None
        if meta['status']=='cancelled':
            # Only the cancellation transaction knows both chunk keys are
            # fenced. Keep the upload promise until that proof is durable.
            return basis,{'state':'cancel_cleanup_pending','offset':meta['offset']}
        remaining=upload.size-meta['offset']
        if meta['offset']==credit.offset:
            from .s3_streams import snapshot
            path=s3_uploads.directory(upload.id)+f'/chunks/{credit.offset:010d}/data'
            observed,_,_=await finish_before_cancel(asyncio.to_thread(snapshot,case.s3_prefix,path,limit=credit.size))
            if observed is not None:
                if observed!={'size':credit.size,'sha256':credit.sha256}:return basis,None
                remaining-=credit.size
        return basis,{'state':meta['status'],'offset':meta['offset'],'remaining_bytes':remaining}
    except (UploadError,ValueError,KeyError,TypeError,OSError,BotoCoreError,ClientError):
        return basis,None


async def apply_settlement(db,ticket,plan):
    """Revalidate SQL identity before releasing bytes; this does no storage I/O."""
    basis,outcome=plan
    credit,upload,case=await _settlement_rows(db,ticket)
    if outcome is None or basis!=_settlement_basis(credit,upload,case):return False
    if not outcome:return True
    if outcome['state']!='cancel_cleanup_pending':
        upload.remaining_bytes=outcome['remaining_bytes'];upload.updated_at=now()
    credit.state='settled';credit.receipt_json=json_text(outcome);credit.updated_at=now()
    return True


async def settle(db,ticket,receipt):
    """False retains the whole pool if a proof or its bound state is unverifiable."""
    return await apply_settlement(db,ticket,await observe_settlement(db,ticket))
