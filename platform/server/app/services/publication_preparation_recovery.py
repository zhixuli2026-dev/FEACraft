"""Resume only validated bundle copying; never replay native validation or sync."""
import asyncio
from dataclasses import dataclass
from datetime import datetime,timedelta
import hashlib
import json
import os

from sqlalchemy import select

from ..db import SessionLocal
from .. import storage
from ..models import ArtifactPublication,Case,DurableEvent
from . import job_execution,job_worker_identity,job_recovery_notice as notices
from . import job_drain_recovery as drain,publication_journal as journal
from .async_ops import finish_before_cancel
from .durable_events import json_text,now
from .workflow_state import ExecutionConflict

KEY='bundle_preparation'
SCHEMA='vibeflow.bundle-preparation.v1'
EVENT='attempt.bundle_preparation_ready'
CHECKPOINT='vibeflow.job-write-checkpoint.v3'
CERTIFICATE='vibeflow.bundle-drain-recovery.v1'
DRAIN_EVENT='attempt.bundle_copy_drained'
FIELDS={'schema','bundle_sha256','input_sha256','outputs_sha256','publication_sha256',
    'worker_sha256','process_sha256','children_sha256','stop'}
CERTIFICATE_FIELDS={'schema','intent_sha256','intent_event_id','bundle_sha256','worker_identity_json',
    'owner_observation','observer','observed_at'}


def digest(value):return hashlib.sha256(value.encode('utf8')).hexdigest()


def intent(attempt,bundle_sha256,stop):
    output=json.loads(attempt.output_manifest_json)
    output.pop(KEY,None);output.pop('finalization_intent',None)
    return {'schema':SCHEMA,'bundle_sha256':bundle_sha256,'stop':stop,
        'input_sha256':digest(attempt.input_manifest_json),'outputs_sha256':digest(json_text(output)),
        'publication_sha256':digest(attempt.publication_json),'worker_sha256':digest(attempt.worker_identity_json),
        'process_sha256':digest(attempt.process_identity_json),'children_sha256':digest(attempt.child_processes_json)}


def cleanup(job,attempt,stop):
    from .process_record import for_attempt
    from .native_child_records import document
    drain.complete_stop(stop,for_attempt(json.loads(attempt.process_identity_json),job,attempt))
    if stop['root_exit_code']!=0:raise ValueError('Validated preparation requires successful native exit')
    for child in document(job,attempt)['children'].values():
        if child['phase'] not in {'stopped','failed'}:raise ValueError('Native validator is still open')
        drain.complete_stop(child['stop'],child['identity'])


async def record(owner,prepared,status,code,stop):
    """Original runner has completed hook/evidence/sync/logs and native finalizers."""
    if status!='succeeded' or type(code) is not int or code!=0:return
    # This intent only describes immutable outputs. It does not exclude edits
    # to live case inputs or pretend the still-pending copy has drained.
    bundle=await finish_before_cancel(asyncio.to_thread(journal.record_intent,
        prepared.prefix,prepared.publication,prepared.records,prepared.descriptor))
    async with SessionLocal() as db:
        job,attempt=await job_execution._owned(db,owner)
        if job.status!='running' or attempt.phase!='finalizing':return
        case=await db.get(Case,job.case_id,populate_existing=True)
        row=await db.get(ArtifactPublication,prepared.publication.id,populate_existing=True)
        if row is None or row.status!='prepared' or not prepared.matches(job,attempt,case,row):return
        if attempt.worker_id!=job_execution.WORKER_ID:
            raise ExecutionConflict('state_conflict','Only the original worker may attest native preparation')
        cleanup(job,attempt,stop)
        await drain.history(db,case,job,attempt,pending=True)
        from .execution_output_checkpoint import record as record_output
        await record_output(db,job,attempt,owner)
        proof=intent(attempt,bundle,stop)
        output=json.loads(attempt.output_manifest_json)
        if KEY in output:
            if output[KEY]!=proof:raise ValueError('Bundle preparation was recorded differently')
            return
        output[KEY]=proof;attempt.output_manifest_json=json_text(output)
        await job_execution.attempt_event(db,job,attempt,EVENT,proof)
        await db.commit()
        prepared.output_json=attempt.output_manifest_json


async def snapshot(db,job_id,*,locked=False):
    case,job,attempt,head=await notices.rows(db,job_id,lock=locked)
    if job is None or attempt is None or job.status!='running' or attempt.phase!='finalizing':return None
    if head is None or head.active_attempt_id!=attempt.id or attempt.cancel_requested_at is not None:
        raise ValueError('Bundle preparation lineage changed or was cancelled')
    proof=json.loads(attempt.output_manifest_json)[KEY]
    if (not isinstance(proof,dict) or set(proof)!=FIELDS
            or proof!=intent(attempt,proof['bundle_sha256'],proof['stop'])):
        raise ValueError('Bundle preparation no longer matches the original inputs and outputs')
    cleanup(job,attempt,proof['stop'])
    events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
        DurableEvent.type==EVENT)))
    if len(events)!=1 or events[0].case_id!=case.id or json.loads(events[0].payload_json)!=proof:
        raise ValueError('Bundle preparation lacks its original durable event')
    history=await drain.history(db,case,job,attempt,pending=True)
    reservation=json.loads(attempt.publication_json)
    row=await db.get(ArtifactPublication,reservation['publication_id'],populate_existing=True)
    if row is None or row.status!='prepared' or row.case_id!=case.id or row.attempt_id!=attempt.id:
        raise ValueError('Bundle reservation no longer matches')
    from .artifact_publication import PublicationIdentity
    binding={'execution':drain.snapshot(case,job,attempt),'checkpoint':attempt.write_checkpoint_json,
        'publication':vars(PublicationIdentity.capture(row)),'history':history,
        'intent_event':(events[0].id,events[0].seq,events[0].payload_json)}
    from .execution_output_checkpoint import read as read_output
    binding['output_budget']=await read_output(db,job,attempt)
    if attempt.write_checkpoint_json!='{}':
        from .job_write_checkpoint import validate
        checkpoint=validate(case,job,attempt)
        await verify_certificate_history(db,case,job,attempt,checkpoint)
    return case,job,attempt,row,job_worker_identity.document(job,attempt),binding,proof


def resume_copy(records):
    from .artifact_publication import PreparedValidation,PublicationIdentity,prepare_bundle
    from . import attempt_workspace as workspace
    case,job,attempt,row,_,_,proof=records
    diagnostic=journal.inspect(case.s3_prefix,row)
    if (diagnostic.get('state') not in {'copy_incomplete','bundle_verified'}
            or diagnostic.get('manifest_sha256')!=proof['bundle_sha256']):
        raise ValueError('Validated copy plan or existing bundle changed')
    manifest=journal.read_intent(case.s3_prefix,row)['manifest']
    if digest(json_text(manifest))!=proof['bundle_sha256']:
        raise ValueError('Copy journal changed after diagnosis')
    run=None
    if diagnostic['state']=='copy_incomplete':
        _,run=workspace.verify(case.s3_prefix,json.loads(attempt.input_manifest_json)['workspace'])
        for path,expected in manifest['files'].items():
            if workspace.digest_file(workspace.checked_path(run,path))!={k:expected[k] for k in ('size','sha256')}:
                raise ValueError('Validated original output changed before copy recovery')
    identity=PublicationIdentity.capture(row)
    # A fully verified immutable bundle no longer needs the old node's run
    # directory. Recheck it and settle readiness without copying source files.
    bundle=prepare_bundle(case.s3_prefix,identity,run,manifest['files'],manifest['descriptor'])
    if bundle!=proof['bundle_sha256']:raise ValueError('Recovered copy differs from original intent')
    return PreparedValidation(case.id,job.id,attempt.id,digest(attempt.claim_token),case.s3_prefix,
        attempt.input_manifest_json,attempt.output_manifest_json,attempt.publication_json,row.kind,
        manifest['descriptor'],identity,records=manifest['files'],bundle_sha256=bundle)


def validate_certificate(case,job,attempt,value):
    """A recovered copy drain is distinct from original v1 and terminal v2."""
    proof=json.loads(attempt.output_manifest_json)[KEY];certificate=value['recovery']
    owner=job_worker_identity.document(job,attempt)
    if (not isinstance(proof,dict) or set(proof)!=FIELDS or proof.get('schema')!=SCHEMA
            or not isinstance(certificate,dict) or set(certificate)!=CERTIFICATE_FIELDS
            or certificate['schema']!=CERTIFICATE or certificate['intent_sha256']!=digest(json_text(proof))
            or certificate['bundle_sha256']!=proof['bundle_sha256'] or value['stop']!=proof['stop']
            or certificate['worker_identity_json']!=attempt.worker_identity_json
            or certificate['owner_observation']!={'state':'exited','reason':'process_exited'}
            or not isinstance(certificate['intent_event_id'],str) or not certificate['intent_event_id']):
        raise ValueError('Recovered copy drain no longer matches original preparation')
    from . import process_identity_native as identity
    observer=identity.validate(certificate['observer'])
    if (observer['platform']!=owner['process']['platform'] or observer['host_id']!=owner['process']['host_id']
            or observer==owner['process']):
        raise ValueError('Copy drain observer cannot establish original worker exit')
    stamp=datetime.fromisoformat(certificate['observed_at'])
    if stamp.utcoffset()!=timedelta(0) or stamp>datetime.fromisoformat(value['recorded_at']):
        raise ValueError('Invalid copy recovery observation time')


async def verify_certificate_history(db,case,job,attempt,checkpoint):
    if checkpoint['schema']!=CHECKPOINT:return None
    proof=json.loads(attempt.output_manifest_json)[KEY]
    events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,
        DurableEvent.type.in_((EVENT,DRAIN_EVENT))).order_by(DurableEvent.seq)))
    if (len(events)!=2 or any(e.case_id!=case.id for e in events)
            or events[0].id!=checkpoint['recovery']['intent_event_id']
            or [(e.type,json.loads(e.payload_json)) for e in events]!=[(EVENT,proof),
                (DRAIN_EVENT,{'checkpoint_sha256':digest(attempt.write_checkpoint_json)})]):
        raise ValueError('Recovered copy checkpoint lost its original preparation/drain history')
    return digest(json_text([(e.id,e.seq,e.type,e.payload_json) for e in events]))


async def checkpoint(records,prepared,observation,observer,observed_at):
    from .job_write_checkpoint import validate,claim_digest
    async with SessionLocal() as db:
        current=await snapshot(db,records[1].id,locked=True)
        if current is None:return False
        case,job,attempt,row,_,binding,proof=current
        if binding!=records[5]:return False
        if attempt.write_checkpoint_json=='{}':
            certificate={'schema':CERTIFICATE,'intent_sha256':digest(json_text(proof)),
                'intent_event_id':binding['intent_event'][0],'bundle_sha256':prepared.bundle_sha256,
                'worker_identity_json':attempt.worker_identity_json,'owner_observation':observation,
                'observer':observer,'observed_at':observed_at}
            value={'schema':CHECKPOINT,'case_id':case.id,'job_id':job.id,'attempt_id':attempt.id,
                'case_prefix':case.s3_prefix,'claim_sha256':claim_digest(attempt),'state':'stopped',
                'process_identity_json':attempt.process_identity_json,'child_processes_json':attempt.child_processes_json,
                'stop':proof['stop'],'recorded_at':now().isoformat(),'recovery':certificate}
            attempt.write_checkpoint_json=json_text(value)
            validate(case,job,attempt)
            await job_execution.attempt_event(db,job,attempt,DRAIN_EVENT,{'checkpoint_sha256':digest(attempt.write_checkpoint_json)})
            await db.commit()
        return True


async def recover(job_id):
    """Caller provides the shared background recovery/error-notice lifecycle."""
    async with SessionLocal() as db:before=await snapshot(db,job_id)
    if before is None:return None
    observation=await drain.observe_owner(before[4])
    if observation.get('state')=='match':return 'owner_live'
    if observation!={'state':'exited','reason':'process_exited'}:return 'owner_unverified'
    _,job,attempt,_,_,_,proof=before
    owner=job_execution.Claim(job.id,attempt.id,attempt.root_job_id,attempt.number,attempt.claim_token)
    from . import execution_budget
    budget=await execution_budget.load(owner,recovery=True)
    with budget.activate():
        from .execution_storage import recovery as monitor_storage
        async with monitor_storage(budget,before[0],attempt):
            try:budget.check()
            except execution_budget.BudgetExceeded:
                return await fail_budget(before,observation,owner)
            return await _resume(before,observation,owner)


async def fail_budget(before,observation,owner):
    # No copy began under this observer, and no storage reclamation is implied.
    from .publication_source_validation import for_finish
    async with for_finish(owner,'failed',None) as (validation,prepared):
        await job_execution._finish_owned(owner,'failed',-1,reason='resource_limit',process_stop=before[6]['stop'],
            return_result=False,check_success=None,prepared=None,validation=validation,
            recovery=ExpiredPreparation(before[5],observation))
    return 'preparation_recovered_failed'


@dataclass(frozen=True)
class ExpiredPreparation:
    binding: dict
    observation: dict


async def require_expired(db,job,attempt,proof,status,reason):
    from . import execution_budget
    budget=execution_budget.current()
    if (status!='failed' or reason!='resource_limit' or budget is None or budget.attempt_id!=attempt.id
            or proof.observation!={'state':'exited','reason':'process_exited'}):
        raise ValueError('Invalid expired preparation recovery')
    try:budget.check()
    except execution_budget.BudgetExceeded:pass
    else:raise ValueError('Preparation has not exhausted its budget')
    current=await snapshot(db,job.id,locked=True)
    if current is None or current[5]!=proof.binding:
        raise ExecutionConflict('recovery_required','Expired preparation changed before failure commit')
    await job_execution.attempt_event(db,job,attempt,'attempt.bundle_preparation_expired',{
        'owner_observation':proof.observation,'replayed':False,'copy_resumed':False,'storage_reclaimed':False})


async def _resume(before,observation,owner):
    from . import process_identity_native as identity
    observer=await finish_before_cancel(asyncio.to_thread(identity.observe,os.getpid()))
    observed_at=now().isoformat()
    from .publication_storage import capacity,quota_failure
    from .artifact_publication import PublicationIdentity
    from fastapi import HTTPException
    from .case_write import CaseWriteConflict
    case,_,_,row=before[:4]
    manifest=(await finish_before_cancel(asyncio.to_thread(journal.read_intent,case.s3_prefix,row)))['manifest']
    try:
        async with capacity(case.id,case.s3_prefix,PublicationIdentity.capture(row),manifest['files'],manifest['descriptor']):
            prepared=await finish_before_cancel(asyncio.to_thread(resume_copy,before))
    except HTTPException as error:
        quota_failure(owner,error)
        if error.status_code==429:return await fail_budget(before,observation,owner)
        raise
    except CaseWriteConflict as error:
        if error.code in {'node_storage_full','node_storage_unavailable'}:
            return await fail_budget(before,observation,owner)
        raise
    from .execution_budget import current
    budget=current(owner)
    if budget.storage_watch is not None:await budget.storage_watch.sample()
    # Even if limits expired while copying, attest the copy that actually
    # drained. The same budget then forces failure in the terminal transaction.
    if not await checkpoint(before,prepared,observation,observer,observed_at):return 'preparation_changed'
    _,job,attempt,_,_,_,proof=before
    from .publication_finalizing import record as record_finalization
    await record_finalization(owner,prepared,'succeeded',0,proof['stop'])
    # Preserve this budget and any in-flight violation across copy/commit.
    from .publication_finalizing import _snapshot,_complete
    async with SessionLocal() as db:after=await _snapshot(db,job.id)
    if after is None:return 'finalization_changed'
    return await _complete(after,observation,owner,budget)
