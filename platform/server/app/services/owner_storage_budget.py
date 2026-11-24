"""Remote owner credit before SQL business writes, with a permanent CAS journal.

The journal records the last possible in-flight PUT. Closing it fences delayed
journal requests; an unknown data request keeps its own capacity after closure.
"""
import asyncio,hashlib,json,logging,os,threading,uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass,field,replace
from types import SimpleNamespace

from botocore.exceptions import BotoCoreError,ClientError
from fastapi import HTTPException
from sqlalchemy import select
from .. import storage
from ..config import get_settings
from ..db import SessionLocal,defer_after_root_transaction
from ..models import OwnerStorageReservation,User
from .async_ops import finish_before_cancel
from .durable_events import json_text,now
from . import process_identity_native as identity

SCHEMA='vibeflow.owner-capacity.v1'
MAX_RECORD=16*1024
MARGIN=MAX_RECORD+16  # One final journal version and the two permanent probe versions.
RECONCILE_PAGE_SIZE=16
log=logging.getLogger(__name__)
_current=ContextVar('owner_remote_capacity',default=None)


def current():return _current.get()


def binding(user_id):
    settings=get_settings()
    return {'backend':'s3','endpoint':settings.s3_endpoint,'bucket':settings.s3_bucket,'prefix':f'workspaces/{user_id}/'}


def raw_client():
    from .s3_live_store import Client as Logical
    from .remote_write_budget import Client as Metered
    client=storage.s3()
    if isinstance(client,Logical):client=client.raw
    if isinstance(client,Metered):client=client.raw
    retries=getattr(getattr(client.meta,'config',None),'retries',{}) or {}
    if retries.get('total_max_attempts')!=1 and retries.get('max_attempts')!=0:
        raise ValueError('Owner capacity requires one SDK request attempt')
    return client


def key(ticket,name):
    bound=json.loads(ticket['binding_json'])
    return {'Bucket':bound['bucket'],'Key':bound['prefix']+'.storage-capacity/'+ticket['id']+'/'+name}


def read(raw,args,limit):
    try:response=raw.get_object(**args)
    except ClientError as error:
        if error.response.get('ResponseMetadata',{}).get('HTTPStatusCode')==404:return None,None
        raise
    body=response['Body']
    try:
        if response['ContentLength']>limit or not response.get('ETag'):raise ValueError('Invalid capacity object length/version')
        data=body.read(limit+1)
        if len(data)!=response['ContentLength']:raise ValueError('Incomplete capacity object')
        return data,response['ETag']
    finally:body.close()


def verify_backend(raw,ticket):
    """Repeatable, permanently retained probe: at most two 8-byte versions."""
    args=key(ticket,'conditions')
    data,etag=read(raw,args,8)
    if data is None:
        try:raw.put_object(**args,Body=b'original',IfNoneMatch='*')
        except ClientError as error:
            if error.response.get('ResponseMetadata',{}).get('HTTPStatusCode')!=412:raise
        data,etag=read(raw,args,8)
    if data not in {b'original',b'replaced'}:raise ValueError('Capacity probe content changed')
    for condition in ({'IfNoneMatch':'*'},{'IfMatch':'"invalid-capacity-version"'}):
        try:raw.put_object(**args,Body=b'rejected',**condition)
        except ClientError as error:
            if error.response.get('ResponseMetadata',{}).get('HTTPStatusCode')==412:continue
            raise
        raise ValueError('S3 does not enforce capacity journal conditions')
    if data==b'original':
        try:raw.put_object(**args,Body=b'replaced',IfMatch=etag)
        except ClientError as error:
            if error.response.get('ResponseMetadata',{}).get('HTTPStatusCode')!=412:raise
        if read(raw,args,8)[0]!=b'replaced':raise ValueError('Capacity probe update did not persist')


def document(ticket,*,closed,pending,used):
    value={'schema':SCHEMA,'id':ticket['id'],'binding':json.loads(ticket['binding_json']),
        'size':ticket['size'],'nonce':str(uuid.uuid4()),'closed':closed,'pending':pending,'used':used}
    encoded=json_text(value).encode('utf8')
    if len(encoded)>MAX_RECORD:raise ValueError('Capacity journal exceeds its fixed bound')
    return value,encoded


def load(raw,ticket):
    encoded,etag=read(raw,key(ticket,'head.json'),MAX_RECORD)
    if encoded is None:return None,None
    value=json.loads(encoded)
    if (not isinstance(value,dict) or value.get('schema')!=SCHEMA or value.get('id')!=ticket['id']
            or value.get('binding')!=json.loads(ticket['binding_json'])
            or value.get('size')!=ticket['size'] or not isinstance(value.get('nonce'),str)
            or str(uuid.UUID(value['nonce']))!=value['nonce']
            or type(value.get('closed')) is not bool or type(value.get('used')) is not int or not 0<=value['used']<=ticket['size']):
        raise ValueError('Capacity journal binding differs')
    request=value['pending'];bound=value['binding']
    if request is not None and (not isinstance(request,dict) or request.get('schema')!='vibeflow.remote-write.v1'
            or request.get('bucket')!=bound['bucket'] or not isinstance(request.get('key'),str)
            or not request['key'].startswith(bound['prefix'])
            or type(request.get('size')) is not int or not 0<=request['size']<=ticket['size']
            or not isinstance(request.get('sha256'),str) or len(request['sha256'])!=64):
        raise ValueError('Capacity pending request differs')
    return value,etag


_OBSERVED=object()
def close_journal(ticket,pending=_OBSERVED):
    raw=raw_client()
    value,etag=load(raw,ticket)
    if value is not None and value['closed']:return value
    if ticket['size']<MARGIN:
        if value is not None:raise ValueError('Undersized credit has a remote journal')
        return document(ticket,closed=True,pending=None,used=0)[0]
    verify_backend(raw,ticket)
    # This caller either drained the live writer or proved that process exited.
    # A racing prior journal PUT can win only once; reread after its CAS wins.
    for _ in range(3):
        value,etag=load(raw,ticket)
        if value is not None and value['closed']:return value
        uncertain=(value['pending'] if value else None) if pending is _OBSERVED else pending
        closed,encoded=document(ticket,closed=True,pending=uncertain,used=value['used'] if value else 16)
        try:
            response=raw.put_object(**key(ticket,'head.json'),Body=encoded,ContentType='application/json',
                **({'IfMatch':etag} if etag else {'IfNoneMatch':'*'}))
        except ClientError as error:
            if error.response.get('ResponseMetadata',{}).get('HTTPStatusCode')==412:continue
            raise
        if not response.get('ETag'):raise ValueError('Capacity closing acknowledgement lacks a version')
        return closed
    raise ValueError('Capacity journal did not converge to a closed version')


def ticket_for(row):
    return {name:getattr(row,name) for name in ('id','user_id','binding_json','writer_json','size')}


async def settle(db,ticket,receipt,*,chunk_plan=None):
    row=await db.get(OwnerStorageReservation,ticket['id'],populate_existing=True)
    if row is None or any(getattr(row,k)!=v for k,v in ticket.items()):raise ValueError('Owner capacity identity changed')
    if row.state=='settled':return
    from .upload_chunk_credit import apply_settlement,settle as settle_chunk
    chunk_settled=(await settle_chunk(db,ticket,receipt) if chunk_plan is None
                   else await apply_settlement(db,ticket,chunk_plan))
    if not chunk_settled:
        row.receipt_json=json_text({**receipt,'upload_recovery_required':True})
        row.updated_at=now()
        return  # Keep the entire grant until the bound upload is verifiable.
    remaining=receipt['pending']['size'] if receipt['pending'] else 0
    if remaining>row.remaining_bytes:raise ValueError('Owner capacity settlement cannot increase credit')
    row.remaining_bytes=remaining;row.state='active' if remaining else 'settled'
    row.receipt_json=json_text(receipt);row.updated_at=now()


async def _observe_reservation(db,ticket):
    """Probe the real writer and remote proof without mutating SQL state."""
    value,_=await finish_before_cancel(asyncio.to_thread(load,raw_client(),ticket))
    if value is None or not value['closed']:
        process=await finish_before_cancel(asyncio.to_thread(identity.probe,json.loads(ticket['writer_json'])))
        if process['state']!='exited':return None
        value=await finish_before_cancel(asyncio.to_thread(close_journal,ticket))
    if value['pending'] is not None:
        from .upload_chunk_credit import completed_pending, completed_metadata
        try:
            upload_proof=await completed_pending(db,ticket,value['pending'])
            if upload_proof is None:
                upload_proof=await completed_metadata(db,ticket,value['pending'])
        except (ValueError,KeyError,TypeError,OSError,BotoCoreError,ClientError):
            upload_proof=None
        if upload_proof is not None:value={**value,'pending':None,'upload_chunk_receipt':upload_proof}
    if value['pending'] is not None:
        from .s3_live_store import split
        from .remote_write_budget import completed_immutable
        request=value['pending'];prefix,_=split(request['key'])
        if prefix is not None:
            bound={**json.loads(ticket['binding_json']),'prefix':prefix}
            try:
                proof=await finish_before_cancel(asyncio.to_thread(completed_immutable,{
                    'request_json':json_text(request),'binding_json':json_text(bound),'size':request['size']}))
            except (ValueError,KeyError,TypeError,OSError,BotoCoreError,ClientError):proof=None
            if proof is not None:value={**value,'pending':None,'immutable_receipt':proof}
    from .upload_chunk_credit import observe_settlement
    return value,await observe_settlement(db,ticket)


async def _recovery_candidates(db,user_id):
    records=select(OwnerStorageReservation).where(OwnerStorageReservation.user_id==user_id,
        OwnerStorageReservation.state=='active',OwnerStorageReservation.binding_json==json_text(binding(user_id)))
    page=records.order_by(OwnerStorageReservation.updated_at,OwnerStorageReservation.id).limit(RECONCILE_PAGE_SIZE)
    blocking=OwnerStorageReservation.receipt_json=='{}'
    # Capture both pages before reconciliation, so a just-closed row is not
    # observed again at the expense of a historical candidate in this pass.
    open_page=(await db.scalars(page.where(blocking))).all()
    retained_page=(await db.scalars(page.where(~blocking))).all()
    return (*open_page,*retained_page)


def _open_grants(user_id):
    from ..models import UploadChunkCredit
    from .upload_chunk_credit import BOUNDED_RECEIPT

    bounded_chunk = select(UploadChunkCredit.id).where(
        UploadChunkCredit.id == OwnerStorageReservation.id,
        UploadChunkCredit.state == 'active',
        UploadChunkCredit.receipt_json == BOUNDED_RECEIPT,
    ).exists()
    return select(OwnerStorageReservation.id).where(OwnerStorageReservation.user_id==user_id,
        OwnerStorageReservation.state=='active',OwnerStorageReservation.receipt_json=='{}',
        ~bounded_chunk).limit(1)


def _open_grant_busy():
    return HTTPException(409,{'code':'storage_quota_busy','message':'账户的远端写入尚未结算，请稍后重试'})


async def _persist_grant(db,user_id,owner,available,inventory,*,chunk=None,on_commit=None):
    if available>=2**63:raise ValueError('Owner capacity exceeds database byte range')
    row=OwnerStorageReservation(id=str(uuid.uuid4()),user_id=user_id,binding_json=json_text(binding(user_id)),
        writer_json=json_text(owner),size=available,remaining_bytes=available,state='active',receipt_json='{}')
    db.add(row);ticket=ticket_for(row)
    if chunk is not None:
        upload,credit=chunk
        await db.flush()
        from .upload_chunk_credit import install
        await install(db,ticket,upload,credit)
    async def commit_grant():
        await db.commit()
        if on_commit is not None:on_commit(ticket,chunk[1] if chunk is not None else None,inventory)

    # Cancellation before dispatch may roll SQL back. Once dispatched, the
    # caller must receive ownership even if it is already being cancelled.
    if asyncio.current_task().cancelling():raise asyncio.CancelledError
    await finish_before_cancel(commit_grant())
    return ticket


async def recover_owner(user_id,*,wait=False,require_available=False):
    """Read old reservations and S3 proofs before taking the owner write lock."""
    from .request_deadline import scope as deadline_scope
    from .storage_admission import InventoryOutdated,lock_owner,quota_revision

    async with deadline_scope(get_settings().storage_measurement_timeout_seconds) as deadline:
        while True:
            async with SessionLocal() as read:
                revision=await quota_revision(read,user_id)
                active=await read.scalar(select(OwnerStorageReservation.id).where(
                    OwnerStorageReservation.user_id==user_id,OwnerStorageReservation.state=='active').limit(1))
                if active is None:return revision
                selected=[(ticket_for(row),row.remaining_bytes,row.receipt_json)
                          for row in await _recovery_candidates(read,user_id)]
                if not selected:
                    if require_available and await read.scalar(_open_grants(user_id)) is not None:
                        raise _open_grant_busy()
                    return revision
            outcomes=[]
            for ticket,remaining,prior in selected:
                deadline.check()
                try:
                    async with SessionLocal() as read:
                        proof=await _observe_reservation(read,ticket)
                except (ValueError,KeyError,TypeError,OSError,BotoCoreError,ClientError) as error:
                    proof=None
                    log.warning('Owner capacity retained: owner=%s reservation=%s reason=%s',
                                user_id,ticket['id'],type(error).__name__)
                outcomes.append((ticket,remaining,prior,proof))
            deadline.check()
            try:
                async with SessionLocal() as db:
                    await lock_owner(db,user_id,wait=wait,inventory_revision=revision)
                    for ticket,remaining,prior,proof in outcomes:
                        row=await db.get(OwnerStorageReservation,ticket['id'],populate_existing=True)
                        if (row is None or row.state!='active' or ticket_for(row)!=ticket
                                or (row.remaining_bytes,row.receipt_json)!=(remaining,prior)):
                            continue
                        # Advance the bounded review page even for incomplete proofs.
                        row.updated_at=now()
                        if proof is None:continue
                        value,chunk_plan=proof
                        try:
                            async with db.begin_nested():
                                await settle(db,ticket,value,chunk_plan=chunk_plan)
                        except (ValueError,KeyError,TypeError) as error:
                            log.warning('Owner capacity retained: owner=%s reservation=%s reason=%s',
                                        user_id,ticket['id'],type(error).__name__)
                    outstanding=await db.scalar(_open_grants(user_id)) if require_available else None
                    revision=await quota_revision(db,user_id)
                    await finish_before_cancel(db.commit())
            except InventoryOutdated:
                await asyncio.sleep(.05)
                continue
            if outstanding is not None:raise _open_grant_busy()
            return revision


async def _acquire_pool(user_id,owner,*,chunk=None,on_commit=None):
    from .storage_admission import lock_owner,reserved_bytes
    from .quota import quotas_for,_storage_limit,prepare_storage_inventory
    from .request_deadline import scope as deadline_scope
    from .upload_chunk_credit import observe,prepare
    from .uploads import MAX_METADATA_BYTES
    async with deadline_scope(get_settings().storage_measurement_timeout_seconds):
        while True:
            revision=await recover_owner(user_id,wait=True,require_available=True)
            observation=await observe(user_id,chunk) if chunk is not None else None
            inventory=await prepare_storage_inventory(user_id,revision)
            if observation is not None and not await finish_before_cancel(asyncio.to_thread(observation.still_current)):
                await asyncio.sleep(.05)
                continue
            async with SessionLocal() as db:
                try:await lock_owner(db,user_id,wait=True,inventory_revision=inventory.revision)
                except HTTPException as error:
                    if (error.status_code!=409 or not isinstance(error.detail,dict)
                            or error.detail.get('code')!='storage_quota_busy'):raise
                else:
                    user=await db.get(User,user_id)
                    if user is None:raise ValueError('Capacity owner is missing')
                    if await db.scalar(_open_grants(user_id)) is not None:raise _open_grant_busy()
                    transfer=await prepare(db,user,chunk,observation,inventory) if chunk is not None else None
                    limit=_storage_limit(await quotas_for(db,user))
                    available=max(0,limit-inventory.bytes-await reserved_bytes(db,user_id))
                    if transfer is not None:
                        if available<MARGIN:
                            raise HTTPException(429,'存储额度不足以保存分块元数据及恢复记录')
                        # The body comes from the upload's existing promise.
                        # Bound overhead to data/record/meta journals, their
                        # final closure, and the record plus metadata objects.
                        available = min(available, MARGIN + 3 * MAX_RECORD + 1024 + MAX_METADATA_BYTES)
                        available += transfer[1]['amount']
                    # Installing the SQL grant advances the epoch without adding
                    # physical bytes. Its holder can reuse the measured snapshot
                    # if that new epoch still matches when taking the write lock.
                    admitted_inventory=replace(inventory,revision=inventory.revision+1)
                    return await _persist_grant(db,user_id,owner,available,admitted_inventory,chunk=transfer,on_commit=on_commit)
            # A competing reservation or settlement made this scan obsolete.
            # Only retry before granting bytes; never replay a physical write.
            await asyncio.sleep(.05)


async def acquire(user_id,*,chunk=None,on_commit=None):
    from .request_deadline import current as request_deadline
    owner=await finish_before_cancel(asyncio.to_thread(identity.observe,os.getpid()))
    parent=request_deadline()
    try:return await _acquire_pool(user_id,owner,chunk=chunk,on_commit=on_commit)
    except TimeoutError as error:
        if parent is not None and parent.expired:raise
        raise HTTPException(503,'存储用量暂时无法核验，请稍后重试') from error


@dataclass
class Scope:
    db: object
    ticket: dict
    inventory: object
    used: int=0
    etag: str|None=None
    pending: object=None
    initialized: bool=False
    started: bool=False
    broken: bool=False
    closed: bool=False
    lock: object=field(default_factory=threading.Lock)
    future_bytes: int=0
    chunk_credit: dict|None=None
    payload_spent: int=0

    def reserve_future(self,amount):
        """Keep a new upload's promised payload out of metadata spending.

        The existing durable pool already reserves these bytes. A successful
        caller installs the upload ledger before closing the pool; a failed
        caller has made no durable promise to accept future payload.
        """
        if type(amount) is not int or amount<0:
            raise ValueError('Future capacity must be non-negative integer bytes')
        if self.initialized or self.used or self.future_bytes or self.closed:
            raise ValueError('Reserve future payload before any remote writes')
        if amount+MARGIN>self.ticket['size']:
            raise HTTPException(429,'存储额度不足以保留上传内容及恢复记录')
        self.future_bytes=amount

    def put(self,raw,args):
        if not self.lock.acquire(blocking=False):raise ValueError('Owner capacity requires serial physical requests')
        try:
            if self.closed or self.broken:raise ValueError('Owner capacity writer already stopped')
            retries=getattr(getattr(raw.meta,'config',None),'retries',{}) or {}
            if retries.get('total_max_attempts')!=1 and retries.get('max_attempts')!=0:
                raise ValueError('Owner capacity requires one SDK request attempt')
            from .remote_write_budget import request
            proxy=SimpleNamespace(lease=SimpleNamespace(case=SimpleNamespace(s3_prefix=f"workspaces/{self.ticket['user_id']}")))
            args,record=request(args,proxy)
            credit=self.chunk_credit
            payload=0
            protected=self.future_bytes
            if credit is not None:
                if record['key']==credit['key']:
                    if (record['size'],record['sha256'],record['if_none_match'])!=(credit['size'],credit['sha256'],'*'):
                        raise ValueError('Upload payload differs from its durable transfer')
                    if not self.payload_spent:payload=credit['amount']
                protected+=credit['amount']-self.payload_spent-payload
            if not self.initialized:
                if self.ticket['size']<MARGIN+record['size']+protected:raise HTTPException(429,'存储额度不足以保存远端写入及恢复记录')
                self.broken=True
                verify_backend(raw,self.ticket)
                self.initialized=True;self.used=16;self.broken=False
            # Store the total debit in the next journal. Its decimal width may
            # affect its own encoded length, so calculate the fixed point.
            debit=self.used+record['size']
            for _ in range(8):
                value,encoded=document(self.ticket,closed=False,pending=record,used=debit)
                updated=self.used+len(encoded)+record['size']
                if updated==debit:break
                debit=updated
            else:raise ValueError('Capacity encoding failed to stabilize')
            if debit+MAX_RECORD+protected>self.ticket['size']:raise HTTPException(429,'存储额度不足以保存远端写入及恢复记录')
            self.broken=True
            response=raw.put_object(**key(self.ticket,'head.json'),Body=encoded,ContentType='application/json',
                **({'IfMatch':self.etag} if self.etag else {'IfNoneMatch':'*'}))
            if not response.get('ETag'):raise ValueError('Capacity journal acknowledgement lacks a version')
            self.etag=response['ETag'];self.used=debit;self.pending=record
            self.payload_spent+=payload
            try:response=raw.put_object(**args)
            except ClientError as error:
                if error.response.get('ResponseMetadata',{}).get('HTTPStatusCode')==412:
                    self.pending=None;self.broken=False
                raise
            if not response.get('ETag'):raise ValueError('Physical write acknowledgement lacks a version')
            self.pending=None;self.broken=False
            return response
        finally:self.lock.release()


def exclusion(db,user_id):
    scope=current()
    return scope.ticket['id'] if scope is not None and not scope.closed and scope.db is db and scope.ticket['user_id']==user_id else None


async def owner_for_case(db,case):
    """Resolve billing ownership after the caller has authorized access."""
    from ..models import Workspace,User
    workspace=await db.get(Workspace,case.workspace_id,populate_existing=True)
    if workspace is None:raise ValueError('Case storage owner disappeared')
    owner=await db.get(User,workspace.user_id)
    if owner is None:raise ValueError('Case storage owner disappeared')
    return owner


@asynccontextmanager
async def hold_case(db,case,*,chunk=None):
    """Authorized collaborators write against the storage owner's credit."""
    owner=await owner_for_case(db,case)
    async with hold(db,owner.id,chunk=chunk):yield owner


@asynccontextmanager
async def waiting(db,user_id):
    """Wait for admission before private startup writes, never replay writes."""
    from .request_deadline import scope as deadline_scope
    async with deadline_scope(get_settings().storage_measurement_timeout_seconds):
        while True:
            context=hold(db,user_id)
            try:
                scope=await context.__aenter__()
                break
            except HTTPException as error:
                if error.status_code!=409 or not isinstance(error.detail,dict) or error.detail.get('code')!='storage_quota_busy':raise
                await asyncio.sleep(.05)
    try:yield scope
    except BaseException as error:
        if not await context.__aexit__(type(error),error,error.__traceback__):raise
    else:await context.__aexit__(None,None,None)


@asynccontextmanager
async def hold(db,user_id,*,chunk=None):
    if not storage._use_s3():
        yield
        return
    existing=current()
    if existing is not None:
        if chunk is not None:raise ValueError('Upload transfer requires its own owner scope')
        if existing.db is not db or existing.ticket['user_id']!=user_id or existing.closed:raise ValueError('Nested owner capacity differs')
        yield existing
        return
    if db.new or db.dirty or db.deleted:raise ValueError('Reserve owner capacity before changing business state')
    if db.get_bind().dialect.name=='sqlite':
        connection=await db.connection();raw=await connection.get_raw_connection()
        if raw.driver_connection.in_transaction:raise ValueError('Reserve owner capacity before SQLite business writes')
    scope=None;token=None
    def admitted(ticket,credit,inventory):
        nonlocal scope
        scope=Scope(db,ticket,inventory,chunk_credit=credit)
    try:
        # Observation and admission can now stop without creating a grant.
        # Only commit + handoff are shielded inside acquire().
        await acquire(user_id,chunk=chunk,on_commit=admitted)
        if scope is None:raise ValueError('Committed owner capacity was not handed off')
        token=_current.set(scope)
        scope.started=True
        try:yield scope
        except BaseException:
            await finish_before_cancel(db.rollback())
            raise
    finally:
        if scope is not None:
            scope.closed=True
            try:await finish_before_cancel(finish(scope))
            finally:
                if token is not None:_current.reset(token)


async def finish(scope):
    # IO callers drain their threads before scope exit. No quota lock is
    # released while a known local thread can send another request.
    if not scope.lock.acquire(blocking=False):raise ValueError('Owner capacity exited before writer drain')
    scope.lock.release()
    if scope.db.in_transaction() and scope.started:
        # The caller owns commit/rollback. Keep the entire durable credit
        # while its transaction is live, and close only after SQL releases it.
        defer_after_root_transaction(scope.db,lambda: _finish_after_business(scope))
        return
    await _finish_drained(scope)


async def _finish_after_business(scope):
    try:
        await _finish_drained(scope)
    except Exception as error:
        # SQL has already ended. An uncertain remote close retains the grant;
        # it must not make a known business commit look like a rollback.
        log.warning('Owner capacity close retained after business transaction: owner=%s reservation=%s reason=%s',
                    scope.ticket['user_id'],scope.ticket['id'],type(error).__name__)


async def _finish_drained(scope):
    user_id=scope.ticket['user_id']
    receipt=(document(scope.ticket,closed=True,pending=None,used=0)[0] if not scope.initialized and not scope.broken
             else await finish_before_cancel(asyncio.to_thread(close_journal,scope.ticket,scope.pending)))
    from .storage_admission import lock_owner
    chunk_plan=None
    if scope.chunk_credit is not None:
        from .request_deadline import scope as deadline_scope
        from .upload_chunk_credit import observe_settlement
        try:
            # Even a cancelled request must settle its already-owned credit.
            async with deadline_scope(get_settings().storage_measurement_timeout_seconds,inherit=False):
                async with SessionLocal() as read:
                    chunk_plan=await observe_settlement(read,scope.ticket)
        except TimeoutError:
            chunk_plan=(None,None)  # Keep the entire grant for later recovery.
    async with SessionLocal() as finishing:
        await lock_owner(finishing,user_id,wait=True,settlement=True)
        await settle(finishing,scope.ticket,receipt,chunk_plan=chunk_plan)
        await finish_before_cancel(finishing.commit())
