"""Resume prepared publication commits from an original owner's durable intent.

Recovery never launches a compiler, solver, staging sync or artifact builder.
The original owner records validated bundle identity after all content writers
drain. Recovery repeats immutable/source verification and the existing commit.
"""
import asyncio
from dataclasses import dataclass, replace
import hashlib
import json

from sqlalchemy import select

from ..db import SessionLocal
from ..models import ArtifactPublication, Case, DurableEvent
from . import job_execution, job_recovery_notice as notices, job_drain_recovery as drain
from .async_ops import finish_before_cancel
from .durable_events import json_text
from .workflow_state import ExecutionConflict

SCHEMA='vibeflow.publication-finalization.v1'
KEY='finalization_intent'
EVENT='attempt.finalization_prepared'
FIELDS={'schema','status','exit_code','reason','bundle_sha256','input_sha256','outputs_sha256','reservation_sha256','checkpoint_sha256'}
_tasks={}


def digest(value):return hashlib.sha256(value.encode('utf8')).hexdigest()


def _intent(attempt,bundle_sha256):
    output=json.loads(attempt.output_manifest_json);output.pop(KEY,None)
    return {'schema':SCHEMA,'status':'succeeded','exit_code':0,'reason':'',
        'bundle_sha256':bundle_sha256,'input_sha256':digest(attempt.input_manifest_json),
        'outputs_sha256':digest(json_text(output)),'reservation_sha256':digest(attempt.publication_json),
        'checkpoint_sha256':digest(attempt.write_checkpoint_json)}


async def record(owner,prepared,status,code,process_stop):
    """Persist success intent separately from the final result, never infer success."""
    if status!='succeeded' or type(code) is not int or code!=0 or prepared is None or prepared.error is not None:return
    async with SessionLocal() as db:
        job,attempt=await job_execution._owned(db,owner)
        if job.status!='running' or attempt.phase!='finalizing':return
        from .artifact_publication import PreparedValidation
        from .job_write_checkpoint import validate
        case=await db.get(Case,job.case_id,populate_existing=True)
        row=await db.get(ArtifactPublication,prepared.publication.id,populate_existing=True)
        if (not isinstance(prepared,PreparedValidation) or row is None or row.status!='prepared'
                or not prepared.matches(job,attempt,case,row)):
            # Preserve the existing owned finalizer's stale/binding failure;
            # no recovery authority may be recorded for this preparation.
            return
        # A failed/absent drain checkpoint must retain the original failure path.
        if attempt.write_checkpoint_json=='{}':return
        checkpoint=validate(case,job,attempt)
        if checkpoint['state']!='stopped' or checkpoint['stop']!=process_stop:return
        if not isinstance(prepared.bundle_sha256,str) or len(prepared.bundle_sha256)!=64:
            return
        from .execution_output_checkpoint import record as record_output
        await record_output(db,job,attempt,owner)
        receipt=_intent(attempt,prepared.bundle_sha256)
        output=json.loads(attempt.output_manifest_json)
        if KEY in output:
            if output[KEY]!=receipt:raise ValueError('Finalization was already prepared differently')
            return
        output[KEY]=receipt;attempt.output_manifest_json=json_text(output)
        await job_execution.attempt_event(db,job,attempt,EVENT,receipt)
        await db.commit()
        prepared.output_json=attempt.output_manifest_json


async def _snapshot(db,job_id,*,locked=False):
    case,job,attempt,head=await notices.rows(db,job_id,lock=locked)
    if job is None or attempt is None or job.status!='running' or attempt.phase!='finalizing':return None
    if head is None or head.active_attempt_id!=attempt.id or attempt.cancel_requested_at is not None:
        raise ValueError('Finalization has lost active lineage or was cancelled')
    from .job_write_checkpoint import validate
    from .job_worker_identity import document
    from .process_record import for_attempt
    from .native_child_records import document as children_document
    checkpoint=validate(case,job,attempt)
    from .publication_preparation_recovery import verify_certificate_history
    preparation_history=await verify_certificate_history(db,case,job,attempt,checkpoint)
    if checkpoint['state']!='stopped' or checkpoint['stop']['root_exit_code']!=0:
        raise ValueError('Finalization has no successful drained process')
    drain.complete_stop(checkpoint['stop'],for_attempt(json.loads(attempt.process_identity_json),job,attempt))
    for child in children_document(job,attempt)['children'].values():
        drain.complete_stop(child['stop'],child['identity'])
    output=json.loads(attempt.output_manifest_json);receipt=output[KEY]
    if not isinstance(receipt,dict) or set(receipt)!=FIELDS or receipt!=_intent(attempt,receipt['bundle_sha256']):
        raise ValueError('Finalization intent no longer matches its checkpoint and inputs')
    if not isinstance(receipt['bundle_sha256'],str) or len(receipt['bundle_sha256'])!=64:
        raise ValueError('Invalid prepared bundle identity')
    recovered_copy=checkpoint['schema']=='vibeflow.job-write-checkpoint.v3'
    drain_event=('attempt.bundle_copy_drained',{'checkpoint_sha256':digest(attempt.write_checkpoint_json)}) if recovered_copy else ('attempt.content_drained',{'state':'stopped'})
    events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
        DurableEvent.type.in_((EVENT,'attempt.content_drained','attempt.bundle_copy_drained'))).order_by(DurableEvent.seq)))
    if (len(events)!=2 or any(e.case_id!=case.id for e in events)
            or [(e.type,json.loads(e.payload_json)) for e in events]!=[
                drain_event,(EVENT,receipt)]):
        raise ValueError('Finalization intent lacks original ordered drain history')
    history=await drain.history(db,case,job,attempt,pending=True)
    reservation=json.loads(attempt.publication_json)
    row=await db.get(ArtifactPublication,reservation['publication_id'],populate_existing=True)
    if row is None or row.status!='prepared' or row.case_id!=case.id or row.attempt_id!=attempt.id:
        raise ValueError('Prepared publication reservation changed')
    from .artifact_publication import PublicationIdentity
    binding={'execution':drain.snapshot(case,job,attempt),'checkpoint':attempt.write_checkpoint_json,
        'publication':vars(PublicationIdentity.capture(row)),'history':history,
        'intent_events':[(e.id,e.seq,e.payload_json) for e in events],'preparation_history':preparation_history}
    from .execution_output_checkpoint import read as read_output
    binding['output_budget']=await read_output(db,job,attempt)
    return case,job,attempt,row,document(job,attempt),binding,receipt,checkpoint


def _prepared(records):
    from .artifact_publication import PreparedValidation, PublicationIdentity, manifest_for_alias_paths
    from . import publication_journal
    case,job,attempt,row,_,_,receipt,_=records
    diagnostic=publication_journal.inspect(case.s3_prefix,row)
    if diagnostic!={'state':'bundle_verified','manifest_sha256':receipt['bundle_sha256']}:
        raise ValueError('Prepared publication journal is incomplete or changed')
    identity=PublicationIdentity.capture(row)
    manifest=manifest_for_alias_paths(case.s3_prefix,replace(identity,manifest_sha256=receipt['bundle_sha256']))
    return PreparedValidation(case.id,job.id,attempt.id,digest(attempt.claim_token),case.s3_prefix,
        attempt.input_manifest_json,attempt.output_manifest_json,attempt.publication_json,row.kind,
        manifest['descriptor'],identity,records=manifest['files'],bundle_sha256=receipt['bundle_sha256'])


@dataclass(frozen=True)
class Recovery:
    binding: dict
    observation: dict


async def require_recovery(db,job,attempt,proof,status,reason):
    from .job_drain_recovery import DrainedCancellation,require_drained_cancellation
    if isinstance(proof,DrainedCancellation):
        return await require_drained_cancellation(db,job,attempt,proof,status,reason)
    from .cancelled_archive_recovery import Recovery as CancelledArchive,require_recovery as require_cancelled
    if isinstance(proof,CancelledArchive):return await require_cancelled(db,job,attempt,proof,status,reason)
    from .job_output_finalizing import Recovery as OutputRecovery,require_recovery as require_output
    if isinstance(proof,OutputRecovery):return await require_output(db,job,attempt,proof,status,reason)
    from .publication_preparation_recovery import ExpiredPreparation,require_expired
    if isinstance(proof,ExpiredPreparation):
        return await require_expired(db,job,attempt,proof,status,reason)
    if not isinstance(proof,Recovery) or proof.observation!={'state':'exited','reason':'process_exited'}:
        raise ValueError('Finalization recovery has no exact owner-exit observation')
    current=await _snapshot(db,job.id,locked=True)
    if current is None or current[5]!=proof.binding:
        raise ExecutionConflict('recovery_required','Prepared finalization changed before commit')
    await job_execution.attempt_event(db,job,attempt,'attempt.finalization_recovered',{
        'bundle_sha256':current[6]['bundle_sha256'],'owner_observation':proof.observation,
        'replayed':False,'native_execution_started':False})


async def reconcile(job_id):
    try:
        from .publication_preparation_recovery import KEY as preparation_key,recover
        async with SessionLocal() as db:
            _,job,attempt,_=await notices.rows(db,job_id)
            output=json.loads(attempt.output_manifest_json) if attempt is not None else {}
            resume=job is not None and job.status=='running' and attempt.phase=='finalizing' and KEY not in output and preparation_key in output
        if resume:
            result=await recover(job_id)
            if result is not None:return result
        async with SessionLocal() as db:before=await _snapshot(db,job_id)
        if before is None:return 'not_finalizing'
        observed=await drain.observe_owner(before[4])
        if observed.get('state')=='match':return 'owner_live'
        if observed!={'state':'exited','reason':'process_exited'}:return 'owner_unverified'
        _,job,attempt,_,_,_,_,_=before
        owner=job_execution.Claim(job.id,attempt.id,attempt.root_job_id,attempt.number,attempt.claim_token)
        from . import execution_budget
        budget=await execution_budget.load(owner,recovery=True)
        with budget.activate():
            from .execution_storage import recovery as monitor_storage
            async with monitor_storage(budget,before[0],attempt):
                return await _complete(before,observed,owner,budget)
    except (OSError,ValueError,KeyError,TypeError,AttributeError,ExecutionConflict):
        async with SessionLocal() as db:
            records=await notices.rows(db,job_id,lock=True)
            _,job,attempt,_=records
            if job is not None and attempt is not None and job.status=='running' and attempt.phase=='finalizing':
                await notices.record(db,records,'publication_recovery_required')
                await db.commit()
        return 'finalization_requires_attention'


async def _complete(before,observed,owner,budget):
        from . import execution_budget
        from .publication_source_validation import for_finish
        status,code,reason=execution_budget.failure(owner,'succeeded',0,'')
        prepared=None if status=='failed' else await finish_before_cancel(asyncio.to_thread(_prepared,before))
        async with SessionLocal() as db:
            current=await _snapshot(db,owner.job_id,locked=True)
            if current is None or current[5]!=before[5]:return 'finalization_changed'
        _,job,attempt,_,_,binding,receipt,checkpoint=current
        status,code,reason=execution_budget.failure(owner,status,code,reason)
        try:
            async with for_finish(owner,status,prepared) as (validation,prepared):
                if status=='succeeded' and validation is None:return 'finalization_changed'
                result=await job_execution._finish_owned(owner,status,code,reason=reason,process_stop=checkpoint['stop'],
                    return_result=False,check_success=None,prepared=prepared,validation=validation,
                    recovery=Recovery(binding,observed))
        except execution_budget.BudgetExceeded:
            async with for_finish(owner,'failed',None) as (validation,unused):
                result=await job_execution._finish_owned(owner,'failed',-1,reason='resource_limit',process_stop=checkpoint['stop'],
                return_result=False,check_success=None,prepared=prepared,validation=validation,
                recovery=Recovery(binding,observed))
        return 'finalization_recovered_'+result


async def schedule(job_id):
    """Schedule recovery after startup; large verification must not block readiness."""
    async with SessionLocal() as db:
        _,job,attempt,_=await notices.rows(db,job_id)
        if (job is None or attempt is None or job.status!='running' or attempt.phase!='finalizing'
                or not {KEY,'bundle_preparation'} & set(json.loads(attempt.output_manifest_json))):
            return None
    if job_id not in _tasks:
        task=asyncio.create_task(reconcile(job_id),name='publication-finalizing-'+str(job_id))
        _tasks[job_id]=task
        def finished(done):
            if _tasks.get(job_id) is done:_tasks.pop(job_id,None)
            if not done.cancelled() and done.exception() is not None:
                print('[WARN] Publication recovery failed: '+type(done.exception()).__name__)
        task.add_done_callback(finished)
    return 'finalization_recovery_scheduled'


async def shutdown():
    tasks=list(_tasks.values())
    for task in tasks:task.cancel()
    if tasks:await asyncio.gather(*tasks,return_exceptions=True)
