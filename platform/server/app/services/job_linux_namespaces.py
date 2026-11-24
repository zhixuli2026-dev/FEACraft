"""Persist the actual Linux namespace init before releasing business code."""
import asyncio,json,os
from datetime import datetime,timedelta
from sqlalchemy import select
from ..db import SessionLocal
from ..models import Case,DurableEvent,Job,JobAttempt
from . import job_execution,job_worker_identity,process_record,process_identity_native as identity
from .durable_events import json_text,lock_case_execution,now

SCHEMA='vibeflow.job-linux-namespace.v1'
PREPARED='attempt.linux_namespace_prepared'
CERTIFICATE='vibeflow.job-linux-scope-recovery.v1'
STARTUP_CERTIFICATE='vibeflow.job-linux-startup-recovery.v1'
EXIT={'state':'exited','reason':'process_exited'}


def validate(value,job,attempt):
    if (not isinstance(value,dict) or set(value)!={'schema','child_id','parent_nonce','process','init','worker'}
            or value['schema']!=SCHEMA):raise ValueError('Invalid namespace preparation')
    process=process_record.for_attempt(value['process'],job,attempt)
    host=process['host'];init=identity.validate(value['init'])
    if (host['platform']!='linux' or process['native'] is not None or process['runtime']!='windows'
            or init['platform']!='linux' or init['host_id']!=host['host_id'] or init['boot_id']!=host['boot_id']
            or init['pid_namespace']==host['pid_namespace'] or init['uid']!=host['uid']
            or process.get('namespace_init')!=init or value['worker']!=job_worker_identity.document(job,attempt)):
        raise ValueError('Namespace preparation differs from its original owner/host')
    worker=value['worker']['process']
    if worker['platform']!='linux' or any(host[key]!=worker[key] for key in ('host_id','boot_id','uid')):
        raise ValueError('Namespace preparation is not on its worker host')
    if value['child_id'] is None:
        if value['parent_nonce'] is not None:raise ValueError('Root namespace has a parent')
    else:
        from .native_child_records import document
        parent=process_record.for_attempt(json.loads(attempt.process_identity_json),job,attempt)
        child=document(job,attempt)['children'][value['child_id']]
        if value['parent_nonce']!=parent['launch_nonce'] or child['parent_launch_nonce']!=value['parent_nonce']:
            raise ValueError('Namespace preparation crosses its parent')
        if child['identity'] is not None and child['identity']!=process:
            raise ValueError('Namespace preparation differs from its registered child')
    return value


async def prepared(owner,process,init,*,child_id=None,parent_nonce=None):
    async with SessionLocal() as db:
        job,attempt=await job_execution._owned(db,owner)
        if attempt.worker_id!=job_execution.WORKER_ID:
            raise ValueError('Namespace preparation requires the original owner')
        from .job_write_checkpoint import assert_native_open_locked
        await assert_native_open_locked(db,job,attempt)
        worker=job_worker_identity.document(job,attempt)
        if worker['process']!=identity.observe(os.getpid()):
            raise ValueError('Namespace recorder differs from its worker birth')
        bound=process_record.bind(process,job,attempt,attempt.worker_id)
        if child_id is None:
            from .job_launch_registration import intent,match
            match(await intent(db,job,attempt),bound,job,attempt)
            if attempt.phase not in {'starting','cancelling'}:raise ValueError('Root namespace is no longer preparing')
        value=validate({'schema':SCHEMA,'child_id':child_id,'parent_nonce':parent_nonce,
                        'process':bound,'init':init,'worker':worker},job,attempt)
        rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
                                                             DurableEvent.type==PREPARED)))
        if any(json.loads(row.payload_json)['child_id']==child_id for row in rows):
            raise ValueError('Namespace preparation already exists for this launch')
        await job_execution.attempt_event(db,job,attempt,PREPARED,value)
        await db.commit()


def startup_process(job,attempt,registration):
    from . import job_launch_registration as launches
    from .process_preparation import chain,matches_record
    if not isinstance(registration,dict):raise ValueError('Invalid startup registration')
    fields={'original_phase','intent','prepared','observations'}
    if registration.get('prepared') is None:fields.add('namespace')
    cancelling=registration.get('original_phase')=='cancelling'
    if cancelling:fields.add('cancel_requested_at')
    if (set(registration)!=fields or registration['original_phase'] not in {'starting','cancelling'}
            or attempt.process_identity_json!='{}'
            or attempt.child_processes_json!='{}'):
        raise ValueError('Startup recovery cannot replace original running or child registration')
    cancelled_at=attempt.cancel_requested_at
    if cancelling:
        if cancelled_at is None or registration['cancel_requested_at']!=cancelled_at.isoformat():
            raise ValueError('Startup cancellation differs from the original request')
    elif cancelled_at is not None:raise ValueError('Startup recovery omitted the original cancellation')
    intended=launches.validate_intent(registration['intent'],job,attempt)
    prepared=registration['prepared']
    if prepared is None:
        namespace=validate(registration['namespace'],job,attempt)
        if namespace['child_id'] is not None:raise ValueError('Startup recovery requires the root namespace')
        root=namespace['process']
    elif not isinstance(prepared,dict) or set(prepared)!={'identity'}:
        raise ValueError('Startup recovery requires the complete original prepared identity')
    else:root=process_record.for_attempt(prepared['identity'],job,attempt)
    launches.match(intended,root,job,attempt)
    observed=chain(registration['observations'],intended)
    matches_record(observed,root)
    if root['host']['platform']!='linux' or root['native'] is not None or 'namespace_init' not in root:
        raise ValueError('Startup recovery requires an originally observed Linux namespace')
    return root


async def startup_registration(db,job,attempt):
    from . import job_launch_registration as launches
    rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
        DurableEvent.type==launches.OBSERVED).order_by(DurableEvent.seq)))
    if any(row.case_id!=job.case_id for row in rows):raise ValueError('Startup observations cross case ownership')
    value={'original_phase':attempt.phase,'intent':await launches.intent(db,job,attempt),
           'prepared':await launches.event(db,job,attempt,launches.PREPARED),
           'observations':[json.loads(row.payload_json) for row in rows]}
    if value['prepared'] is None:
        namespaces=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
            DurableEvent.type==PREPARED)))
        if len(namespaces)!=1 or namespaces[0].case_id!=job.case_id:
            raise ValueError('Startup recovery lacks an original root namespace')
        value['namespace']=json.loads(namespaces[0].payload_json)
    if attempt.phase=='cancelling':
        value['cancel_requested_at']=attempt.cancel_requested_at.isoformat() if attempt.cancel_requested_at else None
    startup_process(job,attempt,value)
    return value


def processes(job,attempt,registration=None):
    from .native_child_records import document
    if registration is not None:return {None:startup_process(job,attempt,registration)}
    root=process_record.for_attempt(json.loads(attempt.process_identity_json),job,attempt)
    children=document(job,attempt)['children']
    result={None:root}
    for key,child in children.items():
        if child['identity'] is None:raise ValueError('Child namespace was not fully registered')
        result[key]=child['identity']
    return result


def validate_certificate(case,job,attempt,value):
    from .job_scope_recovery import SCHEMA as CHECKPOINT,FIELDS,digest,snapshot
    from .job_write_checkpoint import claim_digest
    from .attempt_workspace import require_sealed
    from .step_handlers import validate_binding
    from .job_scope_guardians import sealed_windows_shell
    require_sealed(job,attempt)
    if validate_binding(job,attempt) is None and not sealed_windows_shell(job,attempt):
        raise ValueError('Linux scope recovery requires sealed private execution')
    if (not isinstance(value,dict) or set(value)!=FIELDS or value['schema']!=CHECKPOINT
            or type(value['case_id']) is not int or type(value['job_id']) is not int
            or value['case_id']!=case.id or value['job_id']!=job.id or value['attempt_id']!=attempt.id
            or job.case_id!=case.id or attempt.job_id!=job.id or value['case_prefix']!=case.s3_prefix
            or value['claim_sha256']!=claim_digest(attempt) or value['state']!='recovered_stopped'
            or value['process_identity_json']!=attempt.process_identity_json
            or value['child_processes_json']!=attempt.child_processes_json or value['stop'] is not None
            or datetime.fromisoformat(value['recorded_at']).utcoffset()!=timedelta(0)):
        raise ValueError('Linux recovered checkpoint changed its original binding')
    proof=value['recovery']
    startup=isinstance(proof,dict) and proof.get('schema')==STARTUP_CERTIFICATE
    cancelled=(startup and isinstance(proof.get('registration'),dict) and proof['registration'].get('original_phase')=='cancelling') or (not startup and attempt.phase=='cancelled')
    phase,reason=('cancelled','cancel_requested') if cancelled else ('interrupted','recovery_required')
    fields={'schema','snapshot_sha256','observer','worker','worker_exit',
            'namespaces','original_output_manifest_json','original_root_stop_missing'}
    if startup:fields.add('registration')
    if cancelled and not startup:
        fields.add('cancel_requested_at')
        if attempt.cancel_requested_at is None or proof.get('cancel_requested_at')!=attempt.cancel_requested_at.isoformat():
            raise ValueError('Linux recovery changed the original cancellation request')
    if (not isinstance(proof,dict) or set(proof)!=fields
            or proof['schema'] not in {CERTIFICATE,STARTUP_CERTIFICATE} or proof['snapshot_sha256']!=digest(snapshot(case,job,attempt))
            or proof['worker']!=job_worker_identity.document(job,attempt) or proof['worker_exit']!=EXIT
            or json.loads(proof['original_output_manifest_json'])!={} or proof['original_root_stop_missing'] is not True
            or job.status!=attempt.phase or attempt.phase!=phase or attempt.reason_code!=reason
            or json.loads(attempt.output_manifest_json).get('process_stop') is not None):
        raise ValueError('Linux recovery changed its original interrupted state')
    observer=identity.validate(proof['observer']);worker=proof['worker']['process']
    if (observer['platform']!='linux' or worker['platform']!='linux' or observer==worker
            or observer['host_id']!=worker['host_id'] or observer['boot_id']!=worker['boot_id']):
        raise ValueError('Linux recovery requires an independent observer on the execution host')
    if startup and proof['registration'] is None:raise ValueError('Startup recovery lacks its original preparation')
    expected=processes(job,attempt,proof['registration'] if startup else None);seen=set()
    for row in proof['namespaces']:
        if not isinstance(row,dict) or set(row)!={'event_id','event_seq','prepared','host_exit','init_exit'}:
            raise ValueError('Invalid Linux namespace recovery witness')
        prepared=validate(row['prepared'],job,attempt);key=prepared['child_id']
        if (key in seen or key not in expected or prepared['process']!=expected[key]
                or not isinstance(row['event_id'],str) or not row['event_id']
                or type(row['event_seq']) is not int or row['event_seq']<=0
                or row['host_exit']!=EXIT or row['init_exit']!=EXIT):
            raise ValueError('Linux recovery omitted or changed a namespace')
        seen.add(key)
    if seen!=set(expected):raise ValueError('Linux recovery lacks a complete namespace inventory')
    return value


async def reconcile(job_id,*,starting=False):
    from .job_scope_recovery import SCHEMA as CHECKPOINT,EVENT,digest,snapshot
    from .job_write_checkpoint import claim_digest
    from .async_ops import finish_before_cancel
    async with SessionLocal() as db:
        job=await db.get(Job,job_id)
        if job is None:return False
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id).order_by(JobAttempt.number.desc()).limit(1))
        if attempt is None or attempt.write_checkpoint_json!='{}':return False
        if attempt.output_manifest_json!='{}':return False
        cancelling=not starting and job.status=='cancelled' and attempt.phase=='cancelling' and attempt.cancel_requested_at is not None
        if starting:
            if attempt.phase=='cancelling':
                if job.status!='cancelled' or attempt.cancel_requested_at is None:return False
            elif job.status not in {'queued','running'} or attempt.phase!='starting' or attempt.cancel_requested_at is not None:return False
        elif not cancelling and (job.status!=attempt.phase or attempt.phase!='interrupted' or attempt.reason_code!='recovery_required'):return False
        case=await db.get(Case,job.case_id)
        try:
            worker=job_worker_identity.document(job,attempt)
            if worker['process']['platform']!='linux':return False
            registration=await startup_registration(db,job,attempt) if starting else None
            expected=processes(job,attempt,registration)
        except (ValueError,KeyError,TypeError):return False
        before=snapshot(case,job,attempt)
        cancelled_at=attempt.cancel_requested_at.isoformat() if attempt.cancel_requested_at else None
        rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
            DurableEvent.type==PREPARED).order_by(DurableEvent.seq)))
        witnesses=[];seen=set()
        for row in rows:
            prepared=validate(json.loads(row.payload_json),job,attempt);key=prepared['child_id']
            if key in seen or key not in expected or prepared['process']!=expected[key] or row.case_id!=case.id:return False
            seen.add(key);witnesses.append({'event_id':row.id,'event_seq':row.seq,'prepared':prepared})
        if seen!=set(expected):return False
    if await finish_before_cancel(asyncio.to_thread(identity.probe,worker['process']))!=EXIT:return False
    observer=await finish_before_cancel(asyncio.to_thread(identity.observe,os.getpid()))
    for witness in witnesses:
        prepared=witness['prepared']
        witness['host_exit']=await finish_before_cancel(asyncio.to_thread(identity.probe,prepared['process']['host']))
        witness['init_exit']=await finish_before_cancel(asyncio.to_thread(identity.probe,prepared['init']))
        if witness['host_exit']!=EXIT or witness['init_exit']!=EXIT:return False
    async with SessionLocal() as db:
        await lock_case_execution(db,case.id)
        case=await db.get(Case,case.id,populate_existing=True)
        job=await db.get(Job,job_id,populate_existing=True);attempt=await db.get(JobAttempt,attempt.id,populate_existing=True)
        if snapshot(case,job,attempt)!=before or attempt.write_checkpoint_json!='{}':return False
        if (attempt.cancel_requested_at.isoformat() if attempt.cancel_requested_at else None)!=cancelled_at:return False
        if starting:
            if await startup_registration(db,job,attempt)!=registration:return False
        current=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
            DurableEvent.type==PREPARED).order_by(DurableEvent.seq)))
        if [(r.id,r.seq,r.payload_json) for r in current]!=[(r['event_id'],r['event_seq'],json_text(r['prepared'])) for r in witnesses]:return False
        if starting or cancelling:
            from .workflow_state import transition
            cancelled=cancelling or registration['original_phase']=='cancelling'
            target,reason=('cancelled','cancel_requested') if cancelled else ('interrupted','recovery_required')
            await transition(db,'attempt',attempt.id,target,expected_version=attempt.version,
                claim_token=attempt.claim_token,reason_code=reason,
                detail='Original worker and namespaces exited; original cancellation preserved; execution not replayed' if cancelling else
                    'Original worker and prepared namespace exited before running registration; execution not replayed')
            await job_execution._job_status(db,job,target,None,attempt)
        from .artifact_publication import finalize
        await finalize(db,job,attempt,attempt.phase,attempt.reason_code)
        schema=STARTUP_CERTIFICATE if starting else CERTIFICATE
        proof={'schema':schema,'snapshot_sha256':digest(snapshot(case,job,attempt)),
               'observer':observer,'worker':worker,'worker_exit':EXIT,'namespaces':witnesses,
               'original_output_manifest_json':before['output_manifest_json'],'original_root_stop_missing':True}
        if starting:proof['registration']=registration
        if cancelling:proof['cancel_requested_at']=cancelled_at
        value={'schema':CHECKPOINT,'case_id':case.id,'job_id':job.id,'attempt_id':attempt.id,'case_prefix':case.s3_prefix,
               'claim_sha256':claim_digest(attempt),'state':'recovered_stopped','process_identity_json':attempt.process_identity_json,
               'child_processes_json':attempt.child_processes_json,'stop':None,'recorded_at':now().isoformat(),'recovery':proof}
        validate_certificate(case,job,attempt,value)
        attempt.write_checkpoint_json=json_text(value)
        await job_execution.attempt_event(db,job,attempt,EVENT,{'schema':schema,'checkpoint_sha256':digest(value),
            'original_root_stop_missing':True,'original_child_stops_preserved':True,'replayed':False})
        await db.commit();return True
