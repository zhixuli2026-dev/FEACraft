"""Recover missing drain metadata from existing cleanup receipts and a dead owner.

This never invents process-stop evidence, signals a process, replays work or
changes an existing terminal outcome. Unknown history remains unavailable.
"""
import asyncio
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from ..db import SessionLocal
from ..models import Case, Job, JobAttempt, DurableEvent
from . import job_worker_identity, native_child_records, process_record, process_stop
from . import process_identity_native as identity
from .async_ops import finish_before_cancel
from .durable_events import json_text, lock_case_execution, now
from .workflow_state import TERMINAL

SCHEMA='vibeflow.job-write-checkpoint.v2'
CERTIFICATE='vibeflow.job-drain-recovery.v1'
CERTIFICATE_FIELDS={'schema','worker_identity_json','owner_observation','observer','snapshot_sha256','history_sha256','observed_at'}


def snapshot(case,job,attempt):
    return {'case_id':case.id,'case_prefix':case.s3_prefix,'job_id':job.id,
        'job_case_id':job.case_id,'job_status':job.status,'job_exit_code':job.exit_code,
        'attempt_id':attempt.id,'attempt_job_id':attempt.job_id,'root_job_id':attempt.root_job_id,
        'number':attempt.number,'phase':attempt.phase,'worker_id':attempt.worker_id,
        'claim_sha256':hashlib.sha256(attempt.claim_token.encode()).hexdigest(),
        'worker_identity_json':attempt.worker_identity_json,
        'process_identity_json':attempt.process_identity_json,'child_processes_json':attempt.child_processes_json,
        'input_manifest_json':attempt.input_manifest_json,'output_manifest_json':attempt.output_manifest_json,
        'publication_json':attempt.publication_json}


def fingerprint(value):
    return hashlib.sha256(json_text(value).encode('utf8')).hexdigest()


def complete_stop(stop,process):
    process_stop.validate(stop,process)
    if stop['verified'] is not True:
        raise ValueError('Physical cleanup is not verified')
    if process['host']['platform']=='windows':
        observation=stop.get('scope_observation')
        if not isinstance(observation,dict) or observation.get('complete') is not True:
            raise ValueError('Historical Windows cleanup lacks complete membership evidence')


def eligible(case,job,attempt):
    if job.case_id!=case.id or attempt.job_id!=job.id or job.status not in TERMINAL or attempt.phase!=job.status:
        raise ValueError('Recovery requires a consistent terminal Job and attempt')
    owner=job_worker_identity.document(job,attempt)
    if json.loads(attempt.input_manifest_json)['execution_spec']['case_prefix']!=case.s3_prefix:
        raise ValueError('Historical execution belongs to another case path')
    parent=process_record.for_attempt(json.loads(attempt.process_identity_json),job,attempt)
    stop=json.loads(attempt.output_manifest_json).get('process_stop')
    complete_stop(stop,parent)
    children=native_child_records.document(job,attempt)['children']
    for child in children.values():
        if child['phase'] not in {'stopped','failed'} or child['identity'] is None:
            raise ValueError('Historical child execution has not completed')
        complete_stop(child['stop'],child['identity'])
    from .job_execution import completion_visibility
    visibility=completion_visibility(attempt)
    if visibility in {'pending','invalid'} or (job.status=='succeeded' and attempt.publication_json!='{}' and visibility!='settled'):
        raise ValueError('Historical compatibility writes have not settled')
    return owner,stop


async def history(db,case,job,attempt,*,pending=False):
    """Cross-check root cleanup and every child against durable execution events."""
    from .child_launch_registration import KINDS,verify_history
    kinds={'attempt.process_stopped','attempt.child_reserved','attempt.child_running','attempt.child_stopped'}|KINDS
    events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
        DurableEvent.type.in_(kinds)).order_by(DurableEvent.seq)))
    if any(event.case_id!=case.id for event in events):raise ValueError('Cleanup event crosses case ownership')
    root=[json.loads(event.payload_json) for event in events if event.type=='attempt.process_stopped']
    # Before terminal commit, original cleanup is bound by the write checkpoint
    # and finalization intent; the terminal process_stopped event does not exist.
    expected=[] if pending else [json.loads(attempt.output_manifest_json)['process_stop']]
    if root!=expected:
        raise ValueError('Root cleanup history is missing or changed')
    children=native_child_records.document(job,attempt)['children']
    seen={}
    for event in events:
        if event.type=='attempt.process_stopped':continue
        payload=json.loads(event.payload_json)
        seen.setdefault(payload['child_id'],[]).append((event.type,payload))
    if set(seen)!=set(children):raise ValueError('Child journal omits durable execution history')
    for key,child in children.items():
        verify_history(job,attempt,child,seen[key])
    return fingerprint([(event.id,event.seq,event.type,event.payload_json) for event in events])


def validate_certificate(case,job,attempt,value):
    owner,_=eligible(case,job,attempt)
    proof=value['recovery']
    if (not isinstance(proof,dict) or set(proof)!=CERTIFICATE_FIELDS or proof['schema']!=CERTIFICATE
            or proof['worker_identity_json']!=attempt.worker_identity_json
            or proof['owner_observation']!={'state':'exited','reason':'process_exited'}
            or not isinstance(proof['history_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',proof['history_sha256'])
            or proof['snapshot_sha256']!=fingerprint(snapshot(case,job,attempt))):
        raise ValueError('Recovered drain proof no longer matches its historical evidence')
    observer=identity.validate(proof['observer'])
    if (observer['platform']!=owner['process']['platform'] or observer['host_id']!=owner['process']['host_id']
            or observer==owner['process']):
        raise ValueError('Recovery observer cannot establish this historical owner exit')
    stamp=datetime.fromisoformat(proof['observed_at'])
    if stamp.utcoffset()!=timedelta(0) or stamp>datetime.fromisoformat(value['recorded_at']):
        raise ValueError('Recovery observation timestamp is invalid')


async def observe_owner(owner):
    """No open case transaction and no process signalling during observation."""
    return await finish_before_cancel(asyncio.to_thread(identity.probe,owner['process']))


@dataclass(frozen=True)
class DrainedCancellation:
    binding: str
    observation: dict


def drained_cancellation_candidates():
    from .job_write_checkpoint import SCHEMA as owner_checkpoint
    return ((Job.status == 'cancelled') & (JobAttempt.phase == 'cancelling')
            & JobAttempt.write_checkpoint_json.contains(f'"schema":"{owner_checkpoint}"'))


async def cancellation_binding(db, case, job, attempt):
    from .job_write_checkpoint import SCHEMA as owner_checkpoint, validate

    if (case is None or attempt is None or job.status != 'cancelled' or attempt.phase != 'cancelling'
            or attempt.cancel_requested_at is None):
        return None
    checkpoint = validate(case, job, attempt)
    if checkpoint['schema'] != owner_checkpoint or checkpoint['state'] != 'stopped':
        return None
    parent = process_record.for_attempt(json.loads(attempt.process_identity_json), job, attempt)
    complete_stop(checkpoint['stop'], parent)
    for child in native_child_records.document(job, attempt)['children'].values():
        complete_stop(child['stop'], child['identity'])
    events = (await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id == attempt.id,
        DurableEvent.type == 'attempt.content_drained').limit(2))).all()
    if (len(events) != 1 or events[0].case_id != case.id
            or json.loads(events[0].payload_json) != {'state':'stopped'}):
        raise ValueError('Cancelled attempt is missing its original write drain event')
    owner = job_worker_identity.document(job, attempt)
    native_history = await history(db, case, job, attempt, pending=True)
    binding = fingerprint([snapshot(case, job, attempt), attempt.write_checkpoint_json,
                           attempt.cancel_requested_at.isoformat(), events[0].id, native_history])
    return owner, checkpoint['stop'], binding


async def require_drained_cancellation(db, job, attempt, proof, status, reason):
    if status != 'cancelled' or reason != 'cancel_requested' or proof.observation != {
            'state':'exited','reason':'process_exited'}:
        raise ValueError('Drained recovery authorizes only the original cancellation after owner exit')
    case = await db.get(Case, job.case_id, populate_existing=True)
    current = await cancellation_binding(db, case, job, attempt)
    if current is None or current[2] != proof.binding:
        raise ValueError('Original cancellation changed before recovery committed')
    from .job_execution import attempt_event
    await attempt_event(db, job, attempt, 'attempt.cancel_finalization_recovered',
        {'snapshot_sha256':proof.binding,'owner_observation':proof.observation,'replayed':False})


async def reconcile_drained_cancellation(job_id):
    from . import job_execution
    from .publication_source_validation import for_finish

    async with SessionLocal() as db:
        case, job, attempt = await rows(db, job_id)
        try:
            bound = await cancellation_binding(db, case, job, attempt)
        except (ValueError, TypeError, KeyError, AttributeError):
            return False
        if bound is None:
            return False
        worker, stop, binding = bound
        owner = job_execution.Claim(job.id, attempt.id, attempt.root_job_id, attempt.number, attempt.claim_token)
        code = job.exit_code
    observation = await observe_owner(worker)
    if observation != {'state':'exited','reason':'process_exited'}:
        return False
    try:
        async with for_finish(owner, 'cancelled', None) as (validation, prepared):
            await job_execution._finish_owned(owner, 'cancelled', code, reason='cancel_requested',
                process_stop=stop, return_result=False, check_success=None, prepared=prepared,
                validation=validation, recovery=DrainedCancellation(binding, observation))
    except (ValueError, job_execution.ExecutionConflict):
        return False
    return True


async def rows(db,job_id):
    job=await db.get(Job,job_id)
    if job is None:return None,None,None
    await lock_case_execution(db,job.case_id)
    await db.refresh(job)
    attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id)
        .order_by(JobAttempt.number.desc()).limit(1).execution_options(populate_existing=True))
    case=await db.get(Case,job.case_id,populate_existing=True)
    return case,job,attempt


def existing(case,job,attempt):
    if attempt is None:return 'legacy_unverified'
    if attempt.write_checkpoint_json=='{}':return None
    from .job_write_checkpoint import validate
    try:validate(case,job,attempt)
    except (ValueError,TypeError,KeyError,AttributeError):return 'drain_proof_unverified'
    return 'content_drained'


async def bind_archived_output(db,case,job,attempt,before,previous_checkpoint,archive_event):
    """Rebind only a verified archive receipt after a valid terminal snapshot.

    Preserve the exact original physical stop, owner exit and cleanup history.
    The archive event and the old/new digests make this metadata transition
    explicit; it is not a new process stop or permission to replay execution.
    """
    from .job_write_checkpoint import validate
    previous=json.loads(previous_checkpoint)
    if previous.get('schema')!=SCHEMA:
        validate(case,job,attempt)
        return
    after=snapshot(case,job,attempt)
    old_output=json.loads(before['output_manifest_json'])
    new_output=json.loads(after['output_manifest_json'])
    if ({k:v for k,v in before.items() if k!='output_manifest_json'}!=
            {k:v for k,v in after.items() if k!='output_manifest_json'}
            or {k:v for k,v in old_output.items() if k!='object_store'}!=
               {k:v for k,v in new_output.items() if k!='object_store'}
            or previous['recovery']['snapshot_sha256']!=fingerprint(before)
            or new_output.get('object_store',{}).get('state')!='complete'
            or archive_event.type!='attempt.outputs_recovered'
            or archive_event.case_id!=case.id or archive_event.attempt_id!=attempt.id):
        raise ValueError('Archive transition changed original terminal execution evidence')
    payload=json.loads(archive_event.payload_json)
    if payload.get('archive')!=new_output['object_store'] or payload.get('native_execution_started') is not False:
        raise ValueError('Archive transition has no exact immutable completion receipt')
    eligible(case,job,attempt)
    if await history(db,case,job,attempt)!=previous['recovery']['history_sha256']:
        raise ValueError('Original cleanup history changed during archive completion')
    value=json.loads(previous_checkpoint)
    value['recorded_at']=now().isoformat()
    value['recovery']['snapshot_sha256']=fingerprint(after)
    attempt.write_checkpoint_json=json_text(value)
    validate(case,job,attempt)
    from .job_execution import attempt_event
    await attempt_event(db,job,attempt,'attempt.archive_drain_rebound',{
        'archive_event_id':archive_event.id,'previous_checkpoint_sha256':fingerprint(previous),
        'checkpoint_sha256':fingerprint(value),'previous_snapshot_sha256':fingerprint(before),
        'snapshot_sha256':fingerprint(after),'original_process_stop_preserved':True,'replayed':False})


async def reconcile(job_id):
    async with SessionLocal() as db:
        case,job,attempt=await rows(db,job_id)
        if job is None:return 'removed'
        present=existing(case,job,attempt)
        if present:return present
        try:
            owner,stop=eligible(case,job,attempt)
            before=snapshot(case,job,attempt)
            history_sha256=await history(db,case,job,attempt)
        except (ValueError,TypeError,KeyError,AttributeError):return 'drain_history_unverified'
    observation=await observe_owner(owner)
    if observation['state']=='match':return 'owner_live'
    if observation!={'state':'exited','reason':'process_exited'}:return 'owner_unverified'
    observed_at=now().isoformat()
    observer=await finish_before_cancel(asyncio.to_thread(identity.observe,os.getpid()))
    async with SessionLocal() as db:
        case,job,attempt=await rows(db,job_id)
        if job is None:return 'removed'
        present=existing(case,job,attempt)
        if present:return present
        try:
            eligible(case,job,attempt)
            if snapshot(case,job,attempt)!=before:return 'drain_recovery_changed'
            if await history(db,case,job,attempt)!=history_sha256:return 'drain_recovery_changed'
        except (ValueError,TypeError,KeyError,AttributeError):return 'drain_recovery_changed'
        from .case_validation import assert_writable_locked
        from .case_write import CaseWriteConflict
        try:await assert_writable_locked(db,case.id)
        except CaseWriteConflict:return 'validation_busy'
        from .job_write_checkpoint import claim_digest,validate
        value={'schema':SCHEMA,'case_id':case.id,'job_id':job.id,'attempt_id':attempt.id,
            'case_prefix':case.s3_prefix,'claim_sha256':claim_digest(attempt),'state':'stopped',
            'process_identity_json':attempt.process_identity_json,'child_processes_json':attempt.child_processes_json,
            'stop':stop,'recorded_at':now().isoformat(),
            'recovery':{'schema':CERTIFICATE,'worker_identity_json':attempt.worker_identity_json,
                'owner_observation':observation,'observer':observer,'snapshot_sha256':fingerprint(before),
                'history_sha256':history_sha256,'observed_at':observed_at}}
        attempt.write_checkpoint_json=json_text(value)
        validate(case,job,attempt)
        from .job_execution import attempt_event
        await attempt_event(db,job,attempt,'attempt.content_drain_recovered',{
            'schema':CERTIFICATE,'snapshot_sha256':fingerprint(before),'observer':observer,
            'original_process_stop_preserved':True,'replayed':False})
        await db.commit()
        return 'content_drain_recovered'
