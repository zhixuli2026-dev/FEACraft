"""Test-only timings around actual input preparation, sealing and launch.

Every wrapper calls the original function. No source, model output, native
result, limit or exception is substituted; records contain labels and timings.
"""
import functools,json,os,threading,time,uuid
from contextvars import ContextVar
from pathlib import Path


def install(root,role):
    from app.services import (attempt_objects,attempt_workspace,case_read_view,
        jobrunner,mpi_handler,private_cache,quota,seal_storage_budget,step_handlers,
        workflow_mpi_handler,workflow_scheduler)
    target=Path(root)/'mpi-stage-timings.jsonl';lock=threading.Lock()
    parent=ContextVar('mpi_observed_span',default=None)
    def emit(value):
        value.update(role=role,pid=os.getpid(),wall_time=time.time())
        with lock,target.open('a',encoding='utf8') as stream:
            stream.write(json.dumps(value)+'\n')
    def begin(label, reader=None):
        identifier=uuid.uuid4().hex;ancestor=parent.get();token=parent.set(identifier)
        emit({'event':'begin','span':identifier,'parent':ancestor,'label':label,
              **({'reader':reader} if reader else {})})
        return identifier,token,time.monotonic()
    def end(span,error):
        identifier,token,started=span
        emit({'event':'end','span':identifier,'seconds':round(time.monotonic()-started,6),'error':error})
        parent.reset(token)
    # Join a refused quota check to the actual outstanding grant and the
    # operation that acquired it. Only log numerical counters and source names.
    from app.services import owner_storage_budget as owner_budget
    from app.models import OwnerStorageReservation
    from sqlalchemy import select
    from contextlib import asynccontextmanager
    import traceback
    acquire, hold, finish = owner_budget.acquire, owner_budget.hold, owner_budget.finish
    over = quota._storage_over
    origin = ContextVar('mpi_grant_caller',default=())
    @asynccontextmanager
    async def observed_hold(*args,**kwargs):
        frames = [Path(f.filename).stem+'.'+f.name for f in traceback.extract_stack()
                  if '/app/' in f.filename.replace('\\','/')]
        token = origin.set(frames[-7:])
        try:
            async with hold(*args,**kwargs) as scope:yield scope
        finally:origin.reset(token)
    async def observed_acquire(*args,**kwargs):
        ticket=await acquire(*args,**kwargs)
        emit({'event':'grant.acquired','grant':ticket['id'],'origin':origin.get()})
        return ticket
    async def observed_finish(scope):
        await finish(scope)
        emit({'event':'grant.finished','grant':scope.ticket['id'],'session_in_transaction':scope.db.in_transaction()})
    async def observed_over(db,user,quotas):
        rows=(await db.execute(select(OwnerStorageReservation.id,OwnerStorageReservation.remaining_bytes)
              .where(OwnerStorageReservation.user_id==user.id,OwnerStorageReservation.state=='active',
                     OwnerStorageReservation.receipt_json=='{}'))).all()
        emit({'event':'storage.over','excluded':owner_budget.exclusion(db,user.id),'grants':[list(r) for r in rows]})
        return await over(db,user,quotas)
    owner_budget.acquire,owner_budget.hold,owner_budget.finish = observed_acquire,observed_hold,observed_finish
    quota._storage_over = observed_over
    def observe(owner,name,label,asynchronous):
        original=getattr(owner,name)
        if getattr(original,'_mpi_timing_observer',False):return
        if asynchronous:
            @functools.wraps(original)
            async def wrapped(*args,**kwargs):
                span=begin(label);error=None
                try:return await original(*args,**kwargs)
                except BaseException as failure:error=type(failure).__name__;raise
                finally:end(span,error)
        else:
            @functools.wraps(original)
            def wrapped(*args,**kwargs):
                reader = None
                if owner is case_read_view and len(args)>1:
                    reader = args[1].__module__+'.'+getattr(args[1],'__qualname__',type(args[1]).__name__)
                span=begin(label,reader);error=None
                try:return original(*args,**kwargs)
                except BaseException as failure:error=type(failure).__name__;raise
                finally:end(span,error)
        wrapped._mpi_timing_observer=True
        setattr(owner,name,wrapped)
    for owner,name,label in [
        (workflow_mpi_handler.WorkflowMpiHandler,'prepare','workflow_MPI.prepare'),
        (mpi_handler.MpiHandler,'prepare','native_MPI.prepare'),
        (attempt_workspace,'seal','attempt.seal'),
        (workflow_scheduler,'queue_step','workflow.queue_step'),
        (step_handlers,'launch','handler.launch'),
        (attempt_workspace,'runtime_workspace','attempt.runtime_workspace'),
        (jobrunner,'start_job','job.start'),
        (quota,'check_storage_quota','storage.quota_check')]:observe(owner,name,label,True)
    for owner,name,label in [
        (case_read_view,'inspect','case_input.inspect'),
        (attempt_objects.Snapshot,'inspect','S3_snapshot.inspect'),
        (attempt_objects.Snapshot,'materialize','S3_snapshot.materialize'),
        (attempt_objects.Snapshot,'prepare','S3_snapshot.prepare'),
        (attempt_workspace.Snapshot,'prepare','local_snapshot.prepare'),
        (attempt_objects,'put_input_members','S3_snapshot.upload_members'),
        (attempt_objects,'ensure_inputs','S3_snapshot.verify_inputs'),
        (attempt_objects,'verify_members','S3_snapshot.verify_members'),
        (private_cache.Cache,'fetch_many','case_input.fetch_many'),
        (seal_storage_budget,'estimate','storage.seal_estimate')]:observe(owner,name,label,False)
