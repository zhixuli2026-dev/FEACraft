"""Durable user capacity for immutable copies, including their journal receipts.

An active claim reserves the entire estimate until its writer drains. Existing
physical bytes remain charged independently. A crashed writer is never replaced
on a timer, nor can two recovery processes spend one copy allowance concurrently.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import os
import uuid
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select
from .. import storage
from ..db import SessionLocal
from ..models import ArtifactPublication,Case,User,Workspace,JobAttempt,PublicationStorageReservation
from . import publication_journal as journal,process_identity_native as identity
from .async_ops import finish_before_cancel
from .durable_events import json_text,now
from .storage_admission import lock_owner
from .upload_storage import binding


@dataclass(frozen=True)
class Estimate:
    digest: str
    size: int


def estimate(prefix,row,records,descriptor):
    from .publication_objects import validate_records,encoded
    validate_records(records)
    manifest=journal.bundle_manifest(row,records,descriptor,prefix=prefix)
    data=encoded(manifest)
    digest=hashlib.sha256(data).hexdigest()
    intent=encoded({'schema':journal.SCHEMA,'manifest_sha256':digest,'manifest':manifest})
    ready=encoded({'schema':journal.SCHEMA,'manifest_sha256':digest,'state':'bundle_ready'})
    # Intent/receipt temporary files are renamed, never copied, into place.
    # There is one admitted writer per publication. A retained interrupted stage
    # is measured as actual occupancy before admitting its replacement copy.
    return Estimate(digest,sum(v['size'] for v in records.values())+len(data)+len(intent)+len(ready)
        +(0 if storage._use_s3() else 1))


def ready(prefix,row,digest):
    """Require both the complete immutable bundle AND its matching ready receipt."""
    if storage._use_s3():
        from .publication_objects import journal_path
        from .attempt_objects import read
        try:value=json.loads(read(prefix,journal_path(row,'ready.json')))
        except FileNotFoundError:return False
    else:
        from .attempt_workspace import checked_path
        root=storage._fs_path(prefix,'.')
        try:path=checked_path(root,journal.DIRECTORY+'/'+row.id+'/ready.json')
        except FileNotFoundError:return False
        try:value=journal._read(path)
        except FileNotFoundError:return False
    expected={'schema':journal.SCHEMA,'manifest_sha256':digest,'state':'bundle_ready'}
    if value!=expected:raise ValueError('Publication readiness differs from reserved capacity')
    diagnostic_row=SimpleNamespace(**{key:getattr(row,key) for key in (
        'id','case_id','attempt_id','kind','generation','immutable_path','manifest_sha256')},
        status=getattr(row,'status','prepared'))
    if journal.inspect(prefix,diagnostic_row)!={'state':'bundle_verified','manifest_sha256':digest}:
        raise ValueError('Publication readiness has no matching verified bundle')
    return True


@dataclass
class Ticket:
    publication_id: str
    user_id: int
    case_id: int
    binding_json: str
    estimate: Estimate
    token: str
    ready: bool=False


async def _admit(ticket,publication,prefix):
    writer=await finish_before_cancel(asyncio.to_thread(identity.observe,os.getpid()))
    # Reading a valid ready receipt grants no publication/head authority; it
    # merely proves that this immutable copy needs no additional allocation.
    complete=await finish_before_cancel(asyncio.to_thread(ready,prefix,publication,ticket.estimate.digest))
    async with SessionLocal() as db:
        await lock_owner(db,ticket.user_id)
        case=await db.get(Case,ticket.case_id)
        workspace=await db.get(Workspace,case.workspace_id) if case else None
        current=await db.get(ArtifactPublication,ticket.publication_id)
        if (workspace is None or workspace.user_id!=ticket.user_id or binding(case)!=ticket.binding_json
                or current is None or current.status!='prepared' or not publication.matches(current)):
            raise ValueError('Publication capacity no longer belongs to its case and reservation')
        row=await db.get(PublicationStorageReservation,ticket.publication_id,populate_existing=True)
        if row is not None:
            if (row.user_id,row.case_id,row.binding_json,row.manifest_sha256,row.size)!=(
                    ticket.user_id,ticket.case_id,ticket.binding_json,ticket.estimate.digest,ticket.estimate.size):
                raise ValueError('Publication capacity binding changed')
            if row.state=='active':
                observation=await finish_before_cancel(asyncio.to_thread(identity.probe,json.loads(row.writer_json)))
                if observation!={'state':'exited','reason':'process_exited'}:
                    raise HTTPException(409,{'code':'publication_copy_busy','message':'成果复制进程仍在运行或无法确认退出'})
            elif row.state=='materialized' and not complete:
                raise ValueError('Materialized publication lost its readiness evidence')
        if not complete:
            from .quota import check_storage_quota
            user=await db.get(User,ticket.user_id)
            await check_storage_quota(db,user,0 if row is not None and row.state=='active' else ticket.estimate.size)
        if row is None:
            row=PublicationStorageReservation(publication_id=ticket.publication_id,user_id=ticket.user_id,
                case_id=ticket.case_id,binding_json=ticket.binding_json,manifest_sha256=ticket.estimate.digest,
                size=ticket.estimate.size)
            db.add(row)
        row.writer_json=json_text(writer);row.token=ticket.token;row.updated_at=now()
        row.state='materialized' if complete else 'active'
        row.remaining_bytes=0 if complete else row.size
        await db.commit()
        ticket.ready=complete


async def _settle(ticket,state):
    async with SessionLocal() as db:
        # No case lock is held or acquired here. A concurrent upload must not
        # turn a drained copy into an abandoned live-writer quota claim.
        await lock_owner(db,ticket.user_id,wait=True,settlement=True)
        row=await db.get(PublicationStorageReservation,ticket.publication_id,populate_existing=True)
        # Admission may have failed before commit, or a later process may own it.
        if row is None or row.token!=ticket.token or row.state!='active':return
        if (row.binding_json,row.manifest_sha256)!=(ticket.binding_json,ticket.estimate.digest):
            raise ValueError('Publication settlement binding changed')
        row.state=state;row.remaining_bytes=0;row.updated_at=now()
        await db.commit()


@asynccontextmanager
async def node_capacity(ticket):
    from .node_storage_budget import allocation
    from .case_write import CaseWriteConflict
    try:
        if storage._use_s3() or ticket.ready:
            yield
        else:
            async with allocation(ticket.estimate.size):yield
    except CaseWriteConflict as error:
        if error.code in {'node_storage_full','node_storage_unavailable'}:
            from .execution_budget import current
            budget=current()
            if budget is not None:budget.trip('disk_limit' if error.code=='node_storage_full' else 'disk_unverified','storage')
        raise


@asynccontextmanager
async def capacity(case_id,prefix,publication,records,descriptor):
    async with SessionLocal() as db:
        case=await db.get(Case,case_id)
        workspace=await db.get(Workspace,case.workspace_id) if case else None
        if workspace is None or case.s3_prefix!=prefix:raise ValueError('Publication storage owner is missing')
        ticket=Ticket(publication.id,workspace.user_id,case.id,binding(case),
            estimate(prefix,publication,records,descriptor),str(uuid.uuid4()))
    state='released'
    remote_started=False
    try:
        from .execution_storage_admission import admit as wait_for_admission
        await wait_for_admission(lambda:finish_before_cancel(_admit(ticket,publication,prefix)))
        remote_started=storage._use_s3() and not ticket.ready
        async with node_capacity(ticket):yield ticket
        if not await finish_before_cancel(asyncio.to_thread(ready,prefix,publication,ticket.estimate.digest)):
            raise ValueError('Publication copy returned without a verified readiness receipt')
        state='materialized'
    finally:
        # All caller I/O must use finish_before_cancel. Local errors release only
        # after drain; an uncertain remote request retains its whole allowance.
        # Process death skips this finally and preserves the active SQL claim.
        if state=='materialized' or not remote_started:
            await finish_before_cancel(_settle(ticket,state))


def quota_failure(owner,error):
    if error.status_code!=429:return
    from .execution_budget import current
    budget=current(owner)
    if budget is not None:budget.trip('storage_limit','publication')


async def release_failed(db,publication):
    """Settle exited local writers in the SAME transaction as terminal failure.

    A live/unknown copier keeps its allowance. The case owner uses fail-fast
    owner admission here, so an inverted lock cannot deadlock terminal commit.
    """
    # A dead client does not prove that an accepted S3 request stopped. Only a
    # complete immutable bundle+ready receipt may settle an uncertain S3 copy.
    if publication.status!='failed' or storage._use_s3():return
    row=await db.get(PublicationStorageReservation,publication.id)
    if row is None or row.state!='active':return
    await lock_owner(db,row.user_id)
    await db.refresh(row)
    if row.state!='active':return
    observation=await finish_before_cancel(asyncio.to_thread(identity.probe,json.loads(row.writer_json)))
    if observation!={'state':'exited','reason':'process_exited'}:return
    row.state='released';row.remaining_bytes=0;row.updated_at=now()


async def reconcile(job_id):
    """Read-only remote verification can settle a copy without publishing it."""
    from botocore.exceptions import BotoCoreError,ClientError
    try:return await _reconcile(job_id)
    except (OSError,ValueError,TypeError,KeyError,BotoCoreError,ClientError):return 'publication_capacity_unverified'


async def _reconcile(job_id):
    async with SessionLocal() as db:
        row=await db.scalar(select(PublicationStorageReservation).join(ArtifactPublication,
            ArtifactPublication.id==PublicationStorageReservation.publication_id).join(JobAttempt,
                JobAttempt.id==ArtifactPublication.attempt_id).where(JobAttempt.job_id==job_id,
                    PublicationStorageReservation.state=='active'))
        if row is None:return
        case=await db.get(Case,row.case_id);workspace=await db.get(Workspace,case.workspace_id)
        publication=await db.get(ArtifactPublication,row.publication_id)
        if workspace.user_id!=row.user_id or binding(case)!=row.binding_json:raise ValueError('Publication capacity storage changed')
        prefix=case.s3_prefix
        ticket=Ticket(row.publication_id,row.user_id,row.case_id,row.binding_json,Estimate(row.manifest_sha256,row.size),row.token)
    if not await finish_before_cancel(asyncio.to_thread(ready,prefix,publication,ticket.estimate.digest)):return
    # Conditional immutable writes cannot allocate another version of these
    # already verified keys, even if a prior client still waits for its response.
    await finish_before_cancel(_settle(ticket,'materialized'))
