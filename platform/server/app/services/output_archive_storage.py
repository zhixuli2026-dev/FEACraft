"""Reserve immutable S3 output archives independently of API process lifetime.

An interrupted/uncertain remote write retains its whole allowance. Only verified
remote completion settles such a claim; process exit alone is not a receipt from
the object store. Reconciliation is read-only and grants no execution authority.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib,json,os,uuid

from fastapi import HTTPException
from sqlalchemy import select
from ..db import SessionLocal
from ..models import Case,Workspace,User,Job,JobAttempt,JobExecutionHead,OutputArchiveReservation
from . import attempt_objects as objects,process_identity_native as identity
from .async_ops import finish_before_cancel
from .durable_events import json_text,now
from .storage_admission import lock_owner
from .upload_storage import binding


def digest(value):return hashlib.sha256(value.encode('utf8')).hexdigest()


@dataclass
class Ticket:
    attempt_id: str
    case_id: int
    user_id: int
    binding_json: str
    input_sha256: str
    manifest_sha256: str
    size: int
    token: str
    receipt: dict
    ready: bool=False
    started: bool=False


def plan(prefix,input_json,output_json):
    manifest=json.loads(input_json)['workspace'];output=json.loads(output_json)
    if (output.get('input_attempt_id')!=manifest['attempt_id'] or output.get('run_path')!=manifest['run_path']
            or output.get('object_store',{}).get('storage')!=objects.binding(prefix)):
        raise ValueError('Archive plan no longer matches its captured output')
    encoded,receipt=objects.output_plan(prefix,manifest,output['files'],output['directories'])
    return manifest,output,receipt,sum(row['size'] for row in output['files'].values())+len(encoded)


def ready(prefix,input_json,output_json):
    manifest,output,receipt,_=plan(prefix,input_json,output_json)
    try:objects.read(prefix,receipt['manifest_path'])
    except FileNotFoundError:return False
    objects.verify_outputs(prefix,manifest,{**output,'object_store':{'state':'complete',**receipt}})
    return True


def require_binding(row,ticket):
    for field in ('attempt_id','case_id','user_id','binding_json','input_sha256','manifest_sha256','size'):
        if getattr(row,field)!=getattr(ticket,field):raise ValueError('Output archive reservation binding changed')


async def admit(owner,ticket,prefix,input_json,output_json,*,recover_terminal=False):
    writer=await finish_before_cancel(asyncio.to_thread(identity.observe,os.getpid()))
    complete=await finish_before_cancel(asyncio.to_thread(ready,prefix,input_json,output_json))
    async with SessionLocal() as db:
        await lock_owner(db,ticket.user_id)
        case=await db.get(Case,ticket.case_id)
        workspace=await db.get(Workspace,case.workspace_id) if case else None
        attempt=await db.get(JobAttempt,ticket.attempt_id)
        job=await db.get(Job,owner.job_id);head=await db.get(JobExecutionHead,owner.root_job_id)
        finalizing=(job is not None and attempt is not None and job.status=='running'
            and attempt.phase=='finalizing' and attempt.cancel_requested_at is None)
        cancelling=(job is not None and attempt is not None and job.status=='cancelled'
            and attempt.phase=='cancelling' and attempt.cancel_requested_at is not None)
        terminal_recovery=(recover_terminal and job is not None and attempt is not None
            and job.status in {'failed','interrupted'} and attempt.phase==job.status
            and attempt.cancel_requested_at is None)
        if (workspace is None or workspace.user_id!=ticket.user_id or binding(case)!=ticket.binding_json
                or attempt is None or job is None or job.case_id!=case.id
                or attempt.job_id!=job.id or attempt.root_job_id!=owner.root_job_id or attempt.claim_token!=owner.token
                or head is None or head.active_attempt_id!=attempt.id
                or (attempt.write_checkpoint_json!='{}' and not terminal_recovery)
                or not (finalizing or cancelling or terminal_recovery)
                or attempt.input_manifest_json!=input_json or attempt.output_manifest_json!=output_json):
            raise ValueError('Output archive ownership or captured inventory changed')
        if terminal_recovery:
            # Only the explicit recovery caller may copy a terminal job's
            # original bytes. Reuse its complete stop/drain/history checks and
            # durable original-owner exit witness; no fresh execution grant.
            from . import attempt_output_recovery,job_worker_identity
            from .job_write_checkpoint import validate as validate_checkpoint
            output=json.loads(output_json)
            validate_checkpoint(case,job,attempt)
            await attempt_output_recovery.verify_preparation(db,case,job,attempt,output,pending=False)
            records=(case,job,attempt,None,output,job_worker_identity.document(job,attempt))
            if await attempt_output_recovery.read_owner_exit(db,records) is None:
                raise ValueError('Terminal output recovery has no verified original-owner exit')
        if cancelling:
            # Archive the original runner's already stopped output even after
            # cancellation. This grants neither native execution nor success;
            # unknown writers and unbound inventories still cannot upload.
            from . import attempt_output_recovery,job_worker_identity
            output=json.loads(output_json)
            prepared=attempt_output_recovery.validate_preparation(case,job,attempt,output,
                output.get(attempt_output_recovery.KEY))
            attempt_output_recovery.closed(job,attempt,prepared['stop'])
            if job_worker_identity.document(job,attempt)['process']!=writer:
                raise ValueError('Cancelled output archive belongs to another original worker')
        row=await db.get(OutputArchiveReservation,ticket.attempt_id,populate_existing=True)
        if row is not None:
            require_binding(row,ticket)
            if row.state=='active' and not complete:
                observation=await finish_before_cancel(asyncio.to_thread(identity.probe,json.loads(row.writer_json)))
                if observation!={'state':'exited','reason':'process_exited'}:
                    from .attempt_output_recovery import writer_drained
                    if not await writer_drained(db,row):
                        raise HTTPException(409,{'code':'output_archive_busy','message':'输出归档仍在写入或其状态无法核验'})
            elif row.state=='materialized' and not complete:
                raise ValueError('Completed output archive no longer verifies')
        if not complete:
            from .quota import check_storage_quota
            user=await db.get(User,ticket.user_id)
            await check_storage_quota(db,user,0 if row is not None and row.state=='active' else ticket.size)
        if row is None:
            row=OutputArchiveReservation(**{key:getattr(ticket,key) for key in (
                'attempt_id','case_id','user_id','binding_json','input_sha256','manifest_sha256','size')})
            db.add(row)
        row.writer_json=json_text(writer);row.token=ticket.token;row.updated_at=now()
        row.state='materialized' if complete else 'active';row.remaining_bytes=0 if complete else row.size
        await db.commit();ticket.ready=complete


async def settle(ticket,state):
    async with SessionLocal() as db:
        await lock_owner(db,ticket.user_id,wait=True,settlement=True)
        row=await db.get(OutputArchiveReservation,ticket.attempt_id,populate_existing=True)
        if row is None or row.token!=ticket.token or row.state!='active':return
        require_binding(row,ticket)
        if state=='materialized':
            case=await db.get(Case,row.case_id)
            workspace=await db.get(Workspace,case.workspace_id) if case else None
            attempt=await db.get(JobAttempt,row.attempt_id)
            if (workspace is None or workspace.user_id!=row.user_id or binding(case)!=row.binding_json
                    or attempt is None or digest(attempt.input_manifest_json)!=row.input_sha256):
                raise ValueError('Archive settlement ownership changed')
            _,_,receipt,size=plan(case.s3_prefix,attempt.input_manifest_json,attempt.output_manifest_json)
            if receipt['sha256']!=row.manifest_sha256 or size!=row.size:raise ValueError('Archive settlement inventory changed')
        row.state=state;row.remaining_bytes=0;row.updated_at=now()
        await db.commit()


@asynccontextmanager
async def capacity(owner,case_id,prefix,input_json,output_json,*,recover_terminal=False):
    _,_,receipt,size=plan(prefix,input_json,output_json)
    async with SessionLocal() as db:
        case=await db.get(Case,case_id)
        workspace=await db.get(Workspace,case.workspace_id) if case else None
        if workspace is None or case.s3_prefix!=prefix:raise ValueError('Output archive owner is missing')
        ticket=Ticket(owner.attempt_id,case.id,workspace.user_id,binding(case),digest(input_json),
            receipt['sha256'],size,str(uuid.uuid4()),receipt)
    completed=False
    try:
        from .execution_storage_admission import admit as wait_for_admission
        await wait_for_admission(lambda:finish_before_cancel(admit(owner,ticket,prefix,input_json,output_json,recover_terminal=recover_terminal)))
        yield ticket
        if not await finish_before_cancel(asyncio.to_thread(ready,prefix,input_json,output_json)):
            raise ValueError('Output archive has no verified completion manifest')
        completed=True
    except HTTPException as error:
        if error.status_code==429:
            from .execution_budget import current
            budget=current(owner)
            if budget is not None:budget.trip('storage_limit','archive')
        raise
    finally:
        # A timed-out request may still commit remotely. Never return its unused
        # estimate to the user merely because the local thread/API has stopped.
        if completed or not ticket.started:
            await finish_before_cancel(settle(ticket,'materialized' if completed else 'released'))


async def reconcile(job_id):
    """Settle only fully materialized immutable objects, regardless of job state."""
    from botocore.exceptions import BotoCoreError,ClientError
    try:return await _reconcile(job_id)
    except (OSError,ValueError,TypeError,KeyError,BotoCoreError,ClientError):
        # Quota remains reserved. Unknown archives must not disable unrelated
        # execution recovery or pretend that an incomplete remote write drained.
        return 'archive_unverified'


async def _reconcile(job_id):
    async with SessionLocal() as db:
        row=await db.scalar(select(OutputArchiveReservation).join(JobAttempt,
            JobAttempt.id==OutputArchiveReservation.attempt_id).where(JobAttempt.job_id==job_id,
                OutputArchiveReservation.state=='active'))
        if row is None:return
        attempt=await db.get(JobAttempt,row.attempt_id);case=await db.get(Case,row.case_id)
        workspace=await db.get(Workspace,case.workspace_id)
        if workspace.user_id!=row.user_id or binding(case)!=row.binding_json:raise ValueError('Archive recovery storage changed')
        input_json=attempt.input_manifest_json;output_json=attempt.output_manifest_json;prefix=case.s3_prefix
        _,_,receipt,size=plan(prefix,input_json,output_json)
        ticket=Ticket(row.attempt_id,case.id,row.user_id,binding(case),digest(input_json),receipt['sha256'],size,row.token,receipt)
        require_binding(row,ticket);writer=json.loads(row.writer_json)
    identity.validate(writer)
    # Complete matching objects need no future capacity: even a late request
    # from a live/unknown writer uses If-None-Match on these same immutable keys.
    # Incomplete objects keep the claim even when the original process exited.
    if not await finish_before_cancel(asyncio.to_thread(ready,prefix,input_json,output_json)):return
    await finish_before_cancel(settle(ticket,'materialized'))
