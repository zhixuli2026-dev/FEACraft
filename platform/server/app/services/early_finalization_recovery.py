"""End a dead, registered private finalizer without claiming publication or cleanup.

Completed publication intents belong to publication_finalizing. Here the owner
died before such an intent existed: keep all private files and original process
history, record interruption, and retain the separate unresolved drain gate.
"""
import json

from ..models import ArtifactPublication
from . import job_execution, job_recovery_notice as notices, native_child_records, process_record
from .workflow_state import ExecutionConflict, transition


async def interrupt(db, records, observation):
    case,job,attempt,head=records
    if (observation!={'state':'exited','reason':'process_exited'} or attempt.phase!='finalizing'
            or job.status!='running' or attempt.write_checkpoint_json!='{}'
            or head is None or head.active_attempt_id!=attempt.id):
        return False
    try:
        output=json.loads(attempt.output_manifest_json)
        if {'finalization_intent','bundle_preparation','alias_plan_sha256','completion'} & set(output):return False
        from .finalizing_stage import read as read_stage
        try:stage=await read_stage(db,case,job,attempt)
        except (ValueError,KeyError,TypeError,AttributeError,ExecutionConflict):stage=None
        if stage is not None and stage['supported']:return False
        # A later explicit recovery can archive and validate these exact stopped
        # bytes. Preserve the original active attempt instead of discarding its
        # publication reservation just because full content drain was not yet
        # recorded. Historical missing/invalid proof keeps the old outcome.
        from .attempt_output_recovery import verify_preparation
        try:await verify_preparation(db,case,job,attempt,output,pending=True)
        except (ValueError,KeyError,TypeError,AttributeError,ExecutionConflict):pass
        else:return False
        from .step_handlers import require_binding
        if await require_binding(db,job,attempt) is None:return False
        process_record.for_attempt(json.loads(attempt.process_identity_json),job,attempt)
        native_child_records.document(job,attempt)
        inputs=json.loads(attempt.input_manifest_json)
        if inputs['execution_spec']['case_prefix']!=case.s3_prefix:return False
        reservation=json.loads(attempt.publication_json)
        row=await db.get(ArtifactPublication,reservation['publication_id'],populate_existing=True)
        if (row is None or row.case_id!=case.id or row.attempt_id!=attempt.id
                or row.status!='prepared' or row.manifest_sha256!='0'*64):return False
    except (ValueError,KeyError,TypeError,AttributeError,ExecutionConflict):
        return False
    from .artifact_publication import finalize
    await finalize(db,job,attempt,'interrupted','recovery_required')
    await transition(db,'attempt',attempt.id,'interrupted',expected_version=attempt.version,
        claim_token=attempt.claim_token,reason_code='recovery_required',
        detail='Original owner exited before publication intent; private results retained without replay')
    await job_execution._job_status(db,job,'interrupted',None,attempt)
    await job_execution.reconcile_workflow(db,attempt)
    await job_execution.attempt_event(db,job,attempt,'attempt.finalization_interrupted',{
        'owner_observation':observation,'publication_id':row.id,'private_results_preserved':True,
        'replayed':False,'stop_evidence_created':False,'content_drained':False})
    await db.flush()
    await notices.record(db,records,'process_cleanup_unverified',observation)
    return True
