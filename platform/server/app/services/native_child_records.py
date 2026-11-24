"""Durable child launch/stop records, separate from immutable artifact snapshots."""
import asyncio
from datetime import datetime,timedelta,timezone
import hashlib
import json
import re
import uuid

from . import process_record,process_stop,process_identity_native,owned_process
from .durable_events import json_text,now
from .workflow_state import ExecutionConflict

SCHEMA='vibeflow.child-executions.v1'
MAX_CHILDREN=128
FIELDS={'id','purpose','caller','phase','parent_launch_nonce','request_sha256','identity','stop','error','created_at','ended_at'}
WSL_PURPOSES={'latex_probe','latex_compile','mpi_run','udf_run','flotherm_library','moose_solve','code_aster_solve'}
HOST_PURPOSES={'pandoc_parse','pandoc_compile','pandoc_version','calculix_solve','su2_solve','elmer_solve','paraview_run',
               'mpi_run_linux','udf_run_linux','latex_probe_linux','latex_compile_linux','flotherm_library_linux',
               'moose_solve_linux','code_aster_solve_linux'}


def document(job,attempt):
    value=json.loads(attempt.child_processes_json)
    if value=={}:return {'schema':SCHEMA,'children':{}}
    if not isinstance(value,dict) or set(value)!={'schema','children'} or value['schema']!=SCHEMA:
        raise ValueError('Invalid child execution journal')
    rows=value['children']
    if not isinstance(rows,dict) or len(rows)>MAX_CHILDREN:
        raise ValueError('Invalid child execution count')
    parent=process_record.for_attempt(json.loads(attempt.process_identity_json),job,attempt)
    for key,row in rows.items():
        if not isinstance(row,dict) or set(row)!=FIELDS or str(uuid.UUID(key))!=key or row['id']!=key:
            raise ValueError('Invalid child execution entry')
        if row['purpose'] not in WSL_PURPOSES|HOST_PURPOSES or row['caller'] not in {'worker','finalizer'}:
            raise ValueError('Invalid child execution purpose')
        if row['phase'] not in {'preparing','running','stopped','failed'} or row['parent_launch_nonce']!=parent['launch_nonce']:
            raise ValueError('Child belongs to another parent launch')
        if not isinstance(row['request_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',row['request_sha256']):
            raise ValueError('Invalid child request digest')
        if row['error'] is not None and (not isinstance(row['error'],str) or not re.fullmatch('[A-Za-z][A-Za-z0-9_]{0,79}',row['error'])):
            raise ValueError('Invalid child error category')
        terminal=row['phase'] in {'stopped','failed'}
        for name in ('created_at','ended_at'):
            stamp=row[name]
            if name=='ended_at' and not terminal:
                if stamp is not None:raise ValueError('Active child already has a completion timestamp')
            elif not isinstance(stamp,str) or len(stamp)>64 or datetime.fromisoformat(stamp).utcoffset()!=timedelta(0):
                raise ValueError('Child timestamps require UTC')
        if not terminal and (row['stop'] is not None or row['error'] is not None):
            raise ValueError('Active child already has a cleanup result')
        if row['phase']=='preparing' and row['identity'] is not None:
            raise ValueError('Preparing child already has a registered identity')
        if row['identity'] is not None:
            process_record.for_attempt(row['identity'],job,attempt)
            native=row['purpose'] in WSL_PURPOSES
            if row['identity']['runtime']!=('wsl' if native else 'windows') or (row['identity']['native'] is not None)!=native:
                raise ValueError('Child runtime does not match its registered purpose')
        if row['stop'] is not None:
            if row['identity'] is None:raise ValueError('Child cleanup lacks its launch identity')
            process_stop.validate(row['stop'],row['identity'])
        if row['phase']=='running' and row['identity'] is None:
            raise ValueError('Running child has no registered identity')
        if row['phase']=='stopped' and (row['stop'] is None or not row['stop']['verified'] or row['error'] is not None):
            raise ValueError('Stopped child lacks verified cleanup')
    return value


def public(job,attempt):
    try:return document(job,attempt)
    except (ValueError,TypeError,KeyError,AttributeError):return None


def require_stopped(job,attempt):
    value=document(job,attempt)
    if any(row['phase']!='stopped' for row in value['children'].values()):
        raise ValueError('An attempt child has not completed verified cleanup')


async def _current(db,owner,parent_nonce,*,launch=False):
    from .job_execution import _owned,WORKER_ID
    job,attempt=await _owned(db,owner)
    parent=process_record.for_attempt(json.loads(attempt.process_identity_json),job,attempt)
    if parent['launch_nonce']!=parent_nonce or attempt.worker_id!=WORKER_ID:
        raise ExecutionConflict('state_conflict','Child executor no longer owns this parent launch')
    if launch and (job.status!='running' or attempt.phase not in {'running','finalizing'}):
        raise ExecutionConflict('state_conflict','Parent no longer permits native launches')
    if launch:
        from .job_write_checkpoint import assert_native_open_locked
        await assert_native_open_locked(db,job,attempt)
    return job,attempt


async def reserve(owner,parent_nonce,identifier,purpose,caller,request):
    from ..db import SessionLocal
    from .job_execution import attempt_event
    async with SessionLocal() as db:
        job,attempt=await _current(db,owner,parent_nonce,launch=True)
        value=document(job,attempt)
        if len(value['children'])>=MAX_CHILDREN or identifier in value['children']:
            raise ExecutionConflict('resource_limit','Child execution journal capacity exceeded')
        value['children'][identifier]={'id':identifier,'purpose':purpose,'caller':caller,'phase':'preparing',
            'parent_launch_nonce':parent_nonce,'request_sha256':hashlib.sha256(json_text(request).encode()).hexdigest(),
            'identity':None,'stop':None,'error':None,'created_at':now().isoformat(),'ended_at':None}
        attempt.child_processes_json=json_text(value)
        document(job,attempt)
        from .child_handoff_recovery import contract
        await attempt_event(db,job,attempt,'attempt.child_reserved',{'child_id':identifier,'purpose':purpose,'caller':caller,
            'preparation_contract':contract()})
        await db.commit()


async def register(owner,parent_nonce,identifier,identity,distribution):
    from ..db import SessionLocal
    from .job_execution import attempt_event,WORKER_ID
    async with SessionLocal() as db:
        job,attempt=await _current(db,owner,parent_nonce,launch=True)
        value=document(job,attempt);row=value['children'][identifier]
        bound=process_record.bind(identity,job,attempt,WORKER_ID)
        from .child_launch_registration import require_prepared
        await require_prepared(db,job,attempt,row,bound)
        native=row['purpose'] in WSL_PURPOSES
        if (row['phase']!='preparing' or bound['runtime']!=('wsl' if native else 'windows')
                or (bound['native'] is not None)!=native
                or (native and bound['native']['distribution']!=distribution) or (not native and distribution is not None)
                or bound['gate_sha256']!=hashlib.sha256(owned_process.GATE.read_bytes()).hexdigest()
                or bound['identity_probe_sha256']!=hashlib.sha256(owned_process.PROBE.read_bytes()).hexdigest()):
            raise ExecutionConflict('evidence_invalid','Child launch differs from its registered profile')
        if (await asyncio.to_thread(process_identity_native.probe,bound['host']))['state']!='match':
            raise ExecutionConflict('evidence_invalid','Child host identity no longer matches')
        if native and (await owned_process.probe_native(distribution,bound['native']['process']))['state']!='match':
            raise ExecutionConflict('evidence_invalid','Child native identity no longer matches')
        row.update(phase='running',identity=bound)
        attempt.child_processes_json=json_text(value)
        await attempt_event(db,job,attempt,'attempt.child_running',{'child_id':identifier,'identity':bound})
        await db.commit()


async def release(owner,parent_nonce,identifier,process,send):
    """Order the last child release against the parent's durable cancellation."""
    from ..db import SessionLocal
    from .job_execution import WORKER_ID
    async with SessionLocal() as db:
        job,attempt=await _current(db,owner,parent_nonce,launch=True)
        expires=attempt.lease_expires_at
        if (attempt.cancel_requested_at is not None or expires is None
                or (expires if expires.tzinfo else expires.replace(tzinfo=timezone.utc))<=now()):
            raise ExecutionConflict('state_conflict','Parent no longer permits native release')
        row=document(job,attempt)['children'][identifier]
        if row['phase']!='running' or row['identity']!=process_record.bind(process.record,job,attempt,WORKER_ID):
            raise ExecutionConflict('evidence_invalid','Child registration changed before release')
        await asyncio.wait_for(send(),timeout=5)
        await db.commit()


async def finish(owner,parent_nonce,identifier,identity,stop,error):
    from ..db import SessionLocal
    from .job_execution import attempt_event,WORKER_ID
    async with SessionLocal() as db:
        job,attempt=await _current(db,owner,parent_nonce)
        value=document(job,attempt);row=value['children'][identifier]
        if row['phase'] in {'stopped','failed'}:
            if row['stop']!=stop or row['error']!=error:
                raise ExecutionConflict('state_conflict','Child cleanup is already recorded differently')
            return
        if identity is not None:
            bound=process_record.bind(identity,job,attempt,WORKER_ID)
            if row['identity'] is not None and row['identity']!=bound:
                raise ExecutionConflict('evidence_invalid','Child cleanup changed its launch identity')
            row['identity']=bound
        if stop is not None:
            if row['identity'] is None:raise ValueError('Child cleanup has no identity')
            process_stop.validate(stop,row['identity'])
        from .child_launch_registration import aborted_locked
        await aborted_locked(db,job,attempt,row,row['identity'],stop)
        row.update(phase='stopped' if stop is not None and stop['verified'] and error is None else 'failed',
            stop=stop,error=error,ended_at=now().isoformat())
        attempt.child_processes_json=json_text(value)
        document(job,attempt)
        await attempt_event(db,job,attempt,'attempt.child_stopped',{'child_id':identifier,'phase':row['phase'],'stop':stop,'error':error})
        await db.commit()
