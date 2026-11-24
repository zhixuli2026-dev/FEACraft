"""Finish committed publication compatibility writes after exact owner exit."""
import asyncio
from contextlib import AsyncExitStack
import json

from .. import storage
from ..db import SessionLocal
from ..models import ArtifactPublication,ArtifactPublicationHead
from . import job_execution,job_worker_identity,job_recovery_notice as notices
from . import job_drain_recovery as drain
from .async_ops import finish_before_cancel
from .durable_events import json_text,now


async def _snapshot(db,job_id,*,locked=False):
    case,job,attempt,head=await notices.rows(db,job_id,lock=locked)
    if job is None or attempt is None or job.status!='succeeded' or job_execution.completion_visibility(attempt)!='pending':
        return None
    if attempt.phase!='succeeded' or head is None or head.active_attempt_id!=attempt.id:
        raise ValueError('Publication completion does not match active execution lineage')
    from .job_write_checkpoint import validate
    checkpoint=validate(case,job,attempt)
    from .publication_preparation_recovery import verify_certificate_history
    preparation_history=await verify_certificate_history(db,case,job,attempt,checkpoint)
    recovered_exit=None
    if checkpoint['schema']=='vibeflow.job-write-checkpoint.v5':
        from .job_output_finalizing import verify_history
        from .attempt_output_recovery import read_owner_exit
        preparation_history=await verify_history(db,case,job,attempt,checkpoint)
        certificate=await read_owner_exit(db,(case,job,attempt,None,None,job_worker_identity.document(job,attempt),None))
        recovered_exit=json.loads(certificate.payload_json)['owner_exit']
    if checkpoint['state']!='stopped':raise ValueError('Publication execution cleanup is missing')
    from .process_record import for_attempt
    drain.complete_stop(json.loads(attempt.output_manifest_json)['process_stop'],
        for_attempt(json.loads(attempt.process_identity_json),job,attempt))
    history=await drain.history(db,case,job,attempt)
    owner=job_worker_identity.document(job,attempt)
    reservation=json.loads(attempt.publication_json)
    row=await db.get(ArtifactPublication,reservation['publication_id'],populate_existing=True)
    output=json.loads(attempt.output_manifest_json)
    digest=output.get('alias_plan_sha256')
    if (row is None or row.case_id!=case.id or row.attempt_id!=attempt.id or row.status not in {'committed','superseded'}
            or (row.status=='committed' and (not isinstance(digest,str) or len(digest)!=64))
            or output.get('publication',{}).get('id')!=row.id):
        raise ValueError('Committed publication lacks a bound compatibility plan')
    publication_head=await db.get(ArtifactPublicationHead,(case.id,row.kind),populate_existing=True)
    binding={'execution':drain.snapshot(case,job,attempt),'checkpoint':attempt.write_checkpoint_json,'history':history,
        'publication':{k:getattr(row,k) for k in ('id','attempt_id','case_id','kind','generation','manifest_sha256','status','immutable_path')},
        'head':(publication_head.active_publication_id,publication_head.version) if publication_head else None,
        'preparation_history':preparation_history}
    if recovered_exit is not None:binding['original_owner_exit']=recovered_exit
    return case,job,attempt,row,owner,binding,digest


async def reconcile(job_id):
    try:
        async with SessionLocal() as db:
            before=await _snapshot(db,job_id)
    except (ValueError,TypeError,KeyError,AttributeError):
        return 'publication_compatibility_unverified'
    if before is None:return None
    observed=before[5].get('original_owner_exit') or await drain.observe_owner(before[4])
    if observed=={'state':'match'} or observed.get('state')=='match':return 'owner_live'
    if observed!={'state':'exited','reason':'process_exited'}:return 'owner_unverified'
    try:
        async with AsyncExitStack() as scopes:
            prepared=None
            remote_applied=False
            case,_,_,row,_,binding,digest=before
            if row.status=='committed' and binding['head'] is not None and binding['head'][0]==row.id:
                if storage._use_s3():
                    from .publication_alias_objects import stage_application
                    remote_applied=await scopes.enter_async_context(stage_application(case.s3_prefix,row,digest))
                    if not remote_applied:
                        return 'publication_compatibility_changed'
                else:
                    from .publication_alias_local import stage
                    prepared=await scopes.enter_async_context(stage(case.s3_prefix,row,digest))
            db=await scopes.enter_async_context(SessionLocal())
            current=await _snapshot(db,job_id,locked=True)
            if current is None:return 'publication_compatibility_already_settled'
            if before[5]!=current[5]:return 'publication_compatibility_changed'
            case,job,attempt,row,owner,binding,digest=current
            from .case_write import coordinate
            from .artifact_publication import _write_aliases
            alias_error=None
            async with coordinate(db,case):
                if row.status=='superseded' or binding['head'] is None or binding['head'][0]!=row.id:
                    # Explicit edits or a newer result have superseded the alias
                    # destination. Retain history without restoring stale bytes.
                    alias_error='SupersededPublication'
                elif prepared is not None:
                    await prepared.publish(db,case,row)
                elif not remote_applied:
                    row.alias_plan_sha256=digest
                    await finish_before_cancel(asyncio.to_thread(_write_aliases,case.s3_prefix,row))
                output=json.loads(attempt.output_manifest_json)
                output['completion'].update(state='settled',alias_error=alias_error,settled_at=now().isoformat())
                attempt.output_manifest_json=json_text(output)
                await job_execution.attempt_event(db,job,attempt,'attempt.compatibility_recovered',{
                    'publication_id':row.id,'alias_plan_sha256':digest,'owner_observation':observed,
                    'replayed':False,'alias_error':alias_error})
                await job_execution.attempt_event(db,job,attempt,'attempt.completion_visible',{'alias_error':alias_error})
            await db.commit()
            return 'publication_compatibility_recovered'
    except (OSError,ValueError,TypeError,KeyError,AttributeError):
        # A file error does not grant permission to replace edited destinations
        # or invent completion. The durable plan remains available to retry.
        async with SessionLocal() as db:
            records=await notices.rows(db,job_id,lock=True)
            _,job,attempt,_=records
            if job is not None and attempt is not None and job_execution.completion_visibility(attempt)=='pending':
                await notices.record(db,records,'publication_recovery_required',observed)
                await db.commit()
        return 'publication_compatibility_requires_attention'
