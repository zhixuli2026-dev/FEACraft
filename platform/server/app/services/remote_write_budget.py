"""Persist capacity before each physical S3 PUT in a durable storage operation.

The raw client sends one request per claim. Successful or explicitly rejected
requests settle; unknown outcomes retain their whole allowance across rollback,
thread cancellation and process exit. PUTs run after admission commits. Physical
inventory runs before the transaction and is checked against the owner epoch.
"""
import asyncio,hashlib,json,re,uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import func,select,update
from ..db import SessionLocal
from ..models import Case,Workspace,User,StorageWriteReservation
from ..storage_models import StorageQuotaEpoch
from .async_ops import finish_before_cancel
from .durable_events import json_text,now
from .storage_admission import lock_owner
from .upload_storage import binding

_scope=ContextVar('remote_write_capacity',default=None)
RECONCILE_PAGE_SIZE=16


class BoundedBody:
    """A seekable view whose Content-Length cannot grow after admission."""
    def __init__(self,source):
        self.source=source;self.start=source.tell()
        source.seek(0,2);self.size=source.tell()-self.start;source.seek(self.start)
        if not 0<=self.size<2**63:raise ValueError('Invalid remote body length')
    def tell(self):return self.source.tell()-self.start
    def seek(self,offset,whence=0):
        position=offset if whence==0 else self.tell()+offset if whence==1 else self.size+offset if whence==2 else -1
        if not 0<=position<=self.size:raise ValueError('Remote body seek exceeds admitted bytes')
        self.source.seek(self.start+position);return position
    def read(self,size=-1):
        remaining=self.size-self.tell()
        count=remaining if size is None or size<0 else min(size,remaining)
        value=self.source.read(count)
        if not isinstance(value,bytes) or len(value)>count:raise ValueError('Invalid remote body stream')
        return value
    def __len__(self):return self.size


def request(args,scope):
    from ..config import get_settings
    from .attempt_workspace import canonical_path
    from .request_deadline import current
    from .s3_streams import _digest_body
    deadline=current()
    if deadline is not None:deadline.check()
    prefix=scope.lease.case.s3_prefix.rstrip('/')+'/'
    key=args.get('Key')
    if args.get('Bucket')!=get_settings().s3_bucket or not isinstance(key,str) or not key.startswith(prefix):
        raise ValueError('Remote write crosses its reserved storage operation')
    path=key[len(prefix):]
    if canonical_path(path)!=path:raise ValueError('Remote write has a noncanonical key')
    if args.get('IfNoneMatch')!='*' and not isinstance(args.get('IfMatch'),str):
        raise ValueError('Reserved S3 writes require an explicit condition')
    body=args.get('Body',b'')
    if isinstance(body,(bytes,bytearray,memoryview)):
        body=bytes(body);size=len(body);digest=hashlib.sha256(body).hexdigest()
    else:
        body=BoundedBody(body);size=body.size
        try:
            digest=_digest_body(body,size,deadline=deadline)['sha256']
        finally:body.seek(0)
    if deadline is not None:deadline.check()
    if 'ContentLength' in args and args['ContentLength']!=size:raise ValueError('Remote body length differs')
    record={'schema':'vibeflow.remote-write.v1','bucket':args['Bucket'],'key':key,'size':size,
        'sha256':digest,'if_match':args.get('IfMatch'),'if_none_match':args.get('IfNoneMatch')}
    return {**args,'Body':body,'ContentLength':size},record


@dataclass
class Scope:
    lease: object
    loop: object
    closed: bool=False
    scan_limit: tuple[int,int]|None=None

    def call(self,operation):
        if self.closed:
            operation.close();raise ValueError('Storage operation budget already closed')
        try:running=asyncio.get_running_loop()
        except RuntimeError:running=None
        from .storage_operations import inside_control
        if running is self.loop or inside_control():
            operation.close();raise ValueError('Remote capacity must be requested outside SQL and the event-loop thread')
        return asyncio.run_coroutine_threadsafe(operation,self.loop).result()


async def admit(scope,record):
    from ..config import get_settings
    from .request_deadline import scope as deadline_scope
    # Retry only the uncommitted admission, after releasing its case lock.
    # No physical PUT has been sent here; unknown write outcomes are not retried.
    async with deadline_scope(get_settings().storage_measurement_timeout_seconds):
        while True:
            try:return await _admit_once(scope,record)
            except HTTPException as error:
                if (error.status_code!=409 or not isinstance(error.detail,dict)
                        or error.detail.get('code')!='storage_quota_busy'):raise
                scope.scan_limit=None  # Refresh a possibly stale lower-bound hint.
            await asyncio.sleep(.05)


async def _admit_once(scope,record):
    lease=scope.lease
    from .storage_operations import require_context
    require_context(lease)
    from .quota import _storage_limit,check_storage_quota,prepare_storage_inventory,quotas_for
    from .storage_admission import busy
    # Fetch the epoch in one short read. The prior limit is only a hint for
    # cutting off an over-limit scan; it never authorizes a physical write.
    async with SessionLocal() as db:
        observed=(await db.execute(select(Workspace.user_id,func.coalesce(StorageQuotaEpoch.revision,0))
            .select_from(Case).join(Workspace,Case.workspace_id==Workspace.id)
            .outerjoin(StorageQuotaEpoch,StorageQuotaEpoch.user_id==Workspace.user_id)
            .where(Case.id==lease.case.id))).one_or_none()
        if observed is None:raise busy()
        owner_id,revision=observed
        if scope.scan_limit is None or scope.scan_limit[0]!=owner_id:
            scope.scan_limit=(owner_id,_storage_limit(await quotas_for(db,await db.get(User,owner_id))))
        allowance=max(0,scope.scan_limit[1]-record['size'])
    inventory=await prepare_storage_inventory(owner_id,revision,stop_after=allowance)
    identifier=str(uuid.uuid4())
    try:
        async with lease.control() as db:
            case=await db.get(Case,lease.case.id)
            workspace=await db.get(Workspace,case.workspace_id)
            await check_storage_quota(db,await db.get(User,workspace.user_id),record['size'],inventory=inventory)
            row=StorageWriteReservation(id=identifier,operation_id=lease.id,user_id=workspace.user_id,case_id=case.id,
                binding_json=binding(case),request_json=json_text(record),writer_json=lease.owner_json,
                size=record['size'],remaining_bytes=record['size'],state='active',receipt_json='{}')
            db.add(row)
            ticket={key:getattr(row,key) for key in ('id','operation_id','user_id','case_id','binding_json','request_json','writer_json','size')}
        return ticket
    except HTTPException as error:
        if error.status_code==429:
            from .execution_budget import current
            budget=current()
            if budget is not None:budget.trip('storage_limit','staging')
        raise


async def settle(ticket,receipt):
    async with SessionLocal() as db:
        await lock_owner(db,ticket['user_id'],wait=True,settlement=True)
        row=await db.get(StorageWriteReservation,ticket['id'],populate_existing=True)
        if row is None or any(getattr(row,key)!=value for key,value in ticket.items()):
            raise ValueError('Remote write reservation changed')
        if row.state!='active':return
        case=await db.get(Case,row.case_id)
        workspace=await db.get(Workspace,case.workspace_id) if case else None
        if workspace is None or workspace.user_id!=row.user_id or binding(case)!=row.binding_json:
            raise ValueError('Remote write owner or storage changed')
        row.state='settled';row.remaining_bytes=0;row.receipt_json=json_text(receipt);row.updated_at=now()
        await finish_before_cancel(db.commit())


class Client:
    """Proxy only physical PUTs; logical writes can allocate several raw objects."""
    _remote_capacity_client=True
    def __init__(self,raw):self.raw=raw
    def __getattr__(self,name):return getattr(self.raw,name)
    def put_object(self,**args):
        from .owner_storage_budget import current as owner_capacity
        owner=owner_capacity()
        if owner is not None:return owner.put(self.raw,args)
        scope=_scope.get()
        if scope is None:return self.raw.put_object(**args)
        retries=getattr(getattr(self.raw.meta,'config',None),'retries',{}) or {}
        if retries.get('total_max_attempts')!=1 and retries.get('max_attempts')!=0:
            raise ValueError('One remote capacity claim requires exactly one SDK request attempt')
        args,record=request(args,scope)
        ticket=scope.call(admit(scope,record))
        from botocore.exceptions import ClientError
        try:response=self.raw.put_object(**args)
        except ClientError as error:
            if error.response.get('ResponseMetadata',{}).get('HTTPStatusCode')==412:
                scope.call(settle(ticket,{'schema':'vibeflow.remote-write-receipt.v1','status':412}))
            raise
        # An acknowledged single request has no remaining network retry. The
        # actual object and any retained versions are charged by usage scans.
        if not response.get('ETag'):raise ValueError('Remote write acknowledgement has no version')
        scope.call(settle(ticket,{'schema':'vibeflow.remote-write-receipt.v1','status':200,
            'etag':response['ETag'],'version_id':response.get('VersionId')}))
        return response


def wrap(raw):return raw if isinstance(raw,Client) else Client(raw)


def completed_immutable(ticket):
    """Only permanent private objects prove that a late conditional PUT is fenced."""
    from .. import storage
    from .s3_live_store import Client as LogicalClient
    record=json.loads(ticket['request_json']);bound=json.loads(ticket['binding_json'])
    prefix=bound['prefix'].rstrip('/')+'/'
    if (not isinstance(record,dict) or record.get('schema')!='vibeflow.remote-write.v1' or record.get('size')!=ticket['size']
            or record.get('bucket')!=bound.get('bucket') or not isinstance(record.get('key'),str)
            or not record['key'].startswith(prefix) or not isinstance(record.get('sha256'),str)
            or not re.fullmatch('[0-9a-f]{64}',record['sha256'])):return None
    relative=record['key'][len(prefix):]
    client=storage.s3();raw=client.raw if isinstance(client,LogicalClient) else client
    closed_chunk = re.fullmatch(r'\.vibeflow/uploads/[0-9a-f]{32}/chunks/[0-9]{10}/(data|record\.json)', relative)
    if closed_chunk and record['size'] == 0 and record['sha256'] == hashlib.sha256(b'').hexdigest():
        previous = record.get('if_match')
        creating = previous is None and record.get('if_none_match') == '*'
        replacing = isinstance(previous, str) and re.fullmatch(r'"[^"*,]+"', previous) and record.get('if_none_match') is None
        if not (creating or replacing):
            return None
        head = raw.head_object(Bucket=record['bucket'], Key=record['key'])
        etag = head.get('ETag')
        if (head.get('ContentLength') != 0 or not etag or etag == previous
                or head.get('ContentType') != 'application/vnd.vibeflow.closed-upload-chunk'):
            return None
        return {'schema':'vibeflow.remote-write-receipt.v1','status':'verified_closed_upload_chunk',
                'etag':etag,'sha256':record['sha256']}
    if record.get('if_none_match') != '*' or record.get('if_match') is not None:
        return None
    match=re.fullmatch(r'\.vibeflow/(?:file-transactions|live-store)/objects/([0-9a-f-]{36})/.+',relative)
    if match is None or str(uuid.UUID(match[1]))!=match[1]:return None
    response=raw.get_object(Bucket=record['bucket'],Key=record['key'])
    body=response['Body'];total=0;digest=hashlib.sha256()
    try:
        if response.get('ContentLength')!=ticket['size'] or not response.get('ETag'):return None
        while block:=body.read(min(1024*1024,ticket['size']-total+1)):
            total+=len(block)
            if total>ticket['size']:return None
            digest.update(block)
        if total!=ticket['size'] or digest.hexdigest()!=record['sha256']:return None
    finally:body.close()
    return {'schema':'vibeflow.remote-write-receipt.v1','status':'verified_immutable',
        'etag':response['ETag'],'sha256':record['sha256']}


async def reconcile(lease):
    """Bounded read-only reconciliation; mutable heads/probes stay reserved."""
    from botocore.exceptions import BotoCoreError,ClientError
    from .request_deadline import current as request_deadline

    async with SessionLocal() as db:
        case=await db.get(Case,lease.case.id)
        if case is None or case.s3_prefix!=lease.case.s3_prefix:raise ValueError('Storage recovery binding changed')
        workspace=await db.get(Workspace,case.workspace_id)
        owner_id, storage_binding=workspace.user_id,binding(case)
        conditions=(StorageWriteReservation.case_id==case.id,StorageWriteReservation.state=='active',
                    StorageWriteReservation.user_id==owner_id,StorageWriteReservation.binding_json==storage_binding)
        rows=await db.scalars(select(StorageWriteReservation).where(*conditions)
            .order_by(StorageWriteReservation.updated_at,StorageWriteReservation.id).limit(RECONCILE_PAGE_SIZE))
        tickets=[{key:getattr(row,key) for key in ('id','operation_id','user_id','case_id','binding_json','request_json','writer_json','size')} for row in rows]
    if not tickets:return

    # Record the bounded page selected for review while the lease still owns
    # the case. Slow/missing objects cannot pin the next page to these IDs.
    # No network IO runs in this transaction, and only a proof can settle.
    async with lease.control() as db:
        case=await db.get(Case,lease.case.id)
        workspace=await db.get(Workspace,case.workspace_id)
        if workspace is None or workspace.user_id!=owner_id or binding(case)!=storage_binding:
            raise ValueError('Storage recovery owner or binding changed')
        await db.execute(update(StorageWriteReservation).where(*conditions,
            StorageWriteReservation.id.in_([ticket['id'] for ticket in tickets])).values(updated_at=now()))

    deadline=request_deadline()
    for ticket in tickets:
        if deadline is not None:deadline.check()
        try:
            receipt=await finish_before_cancel(asyncio.to_thread(completed_immutable,ticket))
            if receipt is not None:await finish_before_cancel(settle(ticket,receipt))
        except (ValueError,KeyError,TypeError,OSError,BotoCoreError,ClientError):
            continue  # Unknown evidence preserves capacity; it grants no write authority.


@asynccontextmanager
async def capacity(lease):
    from .. import storage
    client=storage.s3()
    if not isinstance(client,Client) and not isinstance(getattr(client,'raw',None),Client):
        raise ValueError('Storage client does not support durable remote capacity')
    current=_scope.get()
    if current is not None:raise ValueError('Remote capacity scopes cannot be nested')
    scope=Scope(lease,asyncio.get_running_loop());token=_scope.set(scope)
    try:
        await reconcile(lease)
        yield scope
    finally:scope.closed=True;_scope.reset(token)
