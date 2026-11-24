"""Admit immutable remote Job logs; unknown writes retain durable capacity.

The SQL receipt binds the exact log snapshot to its execution and storage owner.
Recovery only verifies that snapshot; it never uploads or grants Job authority.
"""
import asyncio
from dataclasses import dataclass
import hashlib
import json,os,uuid

from fastapi import HTTPException
from ..db import SessionLocal
from ..models import Case,Workspace,User,Job,JobAttempt,JobExecutionHead,JobLogReservation
from . import attempt_objects as objects,process_identity_native as identity
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict
from .durable_events import json_text,now
from .file_transactions import _capture
from .request_deadline import current as deadline_now
from .storage_admission import lock_owner
from .upload_storage import binding


def log_key(job_id):
    if type(job_id) is not int or job_id<=0:raise ValueError('Invalid log Job identity')
    return f'.vibeflow/job-logs/job-{job_id}.log'


@dataclass
class Ticket:
    job_id: int
    attempt_id: str
    case_id: int
    user_id: int
    binding_json: str
    sha256: str
    size: int
    token: str
    ready: bool=False


def _verify_stream(body,ticket):
    deadline=deadline_now()
    digest=hashlib.sha256()
    size=0
    while True:
        if deadline is not None:
            deadline.check()
        chunk=body.read(64*1024)
        if not chunk:
            break
        size+=len(chunk)
        if size>ticket.size:
            raise ValueError('S3 response exceeds its admitted size')
        digest.update(chunk)
    if size!=ticket.size or digest.hexdigest()!=ticket.sha256:
        raise CaseWriteConflict('input_stale','Immutable S3 object content differs from its manifest')


def ready(prefix,ticket):
    try:
        with objects._selected_body(prefix,log_key(ticket.job_id),None,ticket.size) as (body,_):
            _verify_stream(body,ticket)
    except FileNotFoundError:
        return False
    return True


def put_new(prefix,ticket,data):
    # This capability owns only the canonical Job log key. The namespace is
    # protected from file edits, staging sync and live-file manifest rewrites.
    if isinstance(data,(bytes,bytearray)):
        objects.checked(data,{'size':ticket.size,'sha256':ticket.sha256})
    else:
        data.seek(0)
        _verify_stream(data,ticket)
        data.seek(0)
    objects._create_immutable(prefix,log_key(ticket.job_id),data)
    if not ready(prefix,ticket):raise ValueError('Remote log has no completion evidence')


def require_binding(row,ticket):
    for field in ('job_id','attempt_id','case_id','user_id','binding_json','sha256','size'):
        if getattr(row,field)!=getattr(ticket,field):raise ValueError('Log reservation binding changed')


async def check_binding(db,ticket,*,allow_legacy=False):
    case=await db.get(Case,ticket.case_id)
    workspace=await db.get(Workspace,case.workspace_id) if case else None
    job=await db.get(Job,ticket.job_id);attempt=await db.get(JobAttempt,ticket.attempt_id)
    allowed={log_key(ticket.job_id)}
    if allow_legacy:allowed.add(f'.vibeflow/jobs/job-{ticket.job_id}.log')
    if (job is not None and attempt is not None and case is not None and job.status==attempt.phase=='cancelled'
            and job.log_s3_key not in allowed):
        from . import job_finalizing_source as source,job_write_checkpoint
        output=json.loads(attempt.output_manifest_json)
        checkpoint=job_write_checkpoint.validate(case,job,attempt)
        original=source.validate(case,job,attempt,output,output.get(source.KEY))
        if (checkpoint['schema']=='vibeflow.job-write-checkpoint.v5' and job.log_s3_key==output['run_path']+'/'+source.LOG
                and original['log']=={'size':ticket.size,'sha256':ticket.sha256}):allowed.add(job.log_s3_key)
    if (workspace is None or workspace.user_id!=ticket.user_id or binding(case)!=ticket.binding_json
            or job is None or job.case_id!=case.id or job.log_s3_key not in allowed
            or attempt is None or attempt.job_id!=job.id):
        raise ValueError('Log owner, execution or storage changed')
    return case,attempt


async def admit(owner,ticket,prefix):
    writer=await finish_before_cancel(asyncio.to_thread(identity.observe,os.getpid()))
    complete=await finish_before_cancel(asyncio.to_thread(ready,prefix,ticket))
    async with SessionLocal() as db:
        await lock_owner(db,ticket.user_id)
        _,attempt=await check_binding(db,ticket,allow_legacy=True)
        head=await db.get(JobExecutionHead,owner.root_job_id)
        if (attempt.id!=owner.attempt_id or attempt.job_id!=owner.job_id
                or attempt.root_job_id!=owner.root_job_id or attempt.claim_token!=owner.token
                or head is None or head.active_attempt_id!=attempt.id or attempt.write_checkpoint_json!='{}'):
            raise ValueError('Log writer no longer owns this execution')
        row=await db.get(JobLogReservation,ticket.job_id,populate_existing=True)
        if row is not None:
            require_binding(row,ticket)
            if row.state=='active' and not complete:
                observation=await finish_before_cancel(asyncio.to_thread(identity.probe,json.loads(row.writer_json)))
                from .finalizing_io import log_writer_drained
                if observation!={'state':'exited','reason':'process_exited'} and not await log_writer_drained(db,row):
                    raise HTTPException(409,{'code':'job_log_busy','message':'任务日志仍在写入或其状态无法核验'})
            elif row.state=='materialized' and not complete:raise ValueError('Completed remote log is missing')
        if not complete:
            from .quota import check_storage_quota
            await check_storage_quota(db,await db.get(User,ticket.user_id),
                0 if row is not None and row.state=='active' else ticket.size)
        if row is None:
            row=JobLogReservation(**{key:getattr(ticket,key) for key in (
                'job_id','attempt_id','case_id','user_id','binding_json','sha256','size')})
            db.add(row)
        row.writer_json=json_text(writer);row.token=ticket.token;row.updated_at=now()
        row.state='materialized' if complete else 'active';row.remaining_bytes=0 if complete else row.size
        # Old logical logs remain untouched and charged by physical inventory.
        # The receipt and reader pointer switch atomically before any new PUT.
        job=await db.get(Job,ticket.job_id);job.log_s3_key=log_key(job.id)
        await db.commit();ticket.ready=complete


async def settle(ticket,state):
    async with SessionLocal() as db:
        await lock_owner(db,ticket.user_id,wait=True,settlement=True)
        row=await db.get(JobLogReservation,ticket.job_id,populate_existing=True)
        if row is None or row.token!=ticket.token or row.state!='active':return
        require_binding(row,ticket)
        await check_binding(db,ticket)
        row.state=state;row.remaining_bytes=0;row.updated_at=now()
        await db.commit()


def _snapshot(path):
    size=objects.workspace.ordinary(path).st_size
    snapshot=_capture(path,limit=size,deadline=deadline_now())
    if snapshot is None:
        raise FileNotFoundError(path)
    return snapshot


async def upload(owner,prefix,path):
    # The runner flushes and stops appending before taking this snapshot. Later
    # failure diagnostics stay local; they cannot mutate an admitted remote log.
    snapshot=await finish_before_cancel(asyncio.to_thread(_snapshot,path))
    async with SessionLocal() as db:
        job=await db.get(Job,owner.job_id)
        case=await db.get(Case,job.case_id) if job else None
        workspace=await db.get(Workspace,case.workspace_id) if case else None
        if workspace is None or case.s3_prefix!=prefix:raise ValueError('Log owner is missing')
        ticket=Ticket(job.id,owner.attempt_id,case.id,workspace.user_id,binding(case),
            snapshot['sha256'],snapshot['size'],str(uuid.uuid4()))
    started=completed=False

    def write():
        nonlocal started
        with objects._frozen_member_body(path,log_key(ticket.job_id),snapshot,kind='log') as body:
            started=True
            put_new(prefix,ticket,body)

    try:
        from .execution_storage_admission import admit as wait_for_admission
        await wait_for_admission(lambda:finish_before_cancel(admit(owner,ticket,prefix)))
        if not ticket.ready:
            await finish_before_cancel(asyncio.to_thread(write))
        completed=True
    except HTTPException as error:
        if error.status_code==429:
            from .execution_budget import current
            budget=current(owner)
            if budget is not None:budget.trip('storage_limit','log')
        raise
    finally:
        if completed or not started:
            await finish_before_cancel(settle(ticket,'materialized' if completed else 'released'))


async def reconcile(job_id):
    from botocore.exceptions import BotoCoreError,ClientError
    try:return await _reconcile(job_id)
    except (OSError,ValueError,TypeError,KeyError,BotoCoreError,ClientError):return 'log_unverified'


async def _reconcile(job_id):
    async with SessionLocal() as db:
        row=await db.get(JobLogReservation,job_id)
        if row is None or row.state!='active':return
        ticket=Ticket(**{key:getattr(row,key) for key in (
            'job_id','attempt_id','case_id','user_id','binding_json','sha256','size','token')})
        case,_=await check_binding(db,ticket);prefix=case.s3_prefix
        writer=json.loads(row.writer_json)
    identity.validate(writer)
    # A complete immutable object cannot grow through a late conditional PUT,
    # including while the writer is still alive. Absence proves no such thing.
    if await finish_before_cancel(asyncio.to_thread(ready,prefix,ticket)):
        await finish_before_cancel(settle(ticket,'materialized'))
