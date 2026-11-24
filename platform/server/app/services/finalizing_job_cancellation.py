"""Settle an original cancelled Job after its independent validator stops.

No solver/validator is started here. Historical missing request/stop evidence
keeps its recovery refusal, and no old child stop is synthesized.
"""
import json
from ..db import SessionLocal
from ..models import NativeRequest
from . import job_output_finalizing as finalizing,finalizing_native_scope as scopes
from . import finalizing_request_recovery as recovery,execution_budget,job_execution
from .async_ops import finish_before_cancel
from .durable_events import lock_case_execution,append_event,json_text
from .workflow_state import ExecutionConflict


async def settle_if_requested(job_id,user_id,attempt_id,inventory_sha256):
    from .job_recovery_notice import rows
    async with SessionLocal() as db:
        _,job,attempt,_=await rows(db,job_id)
        if job is None or attempt is None or job.status!='cancelled' or attempt.phase!='cancelling':return None
    async with SessionLocal() as db:before=await finalizing.snapshot(db,job_id,user_id,allow_cancellation=True,allow_pending_log=True)
    case,job,attempt,manifest,output,*_=before
    if attempt.id!=attempt_id or finalizing.outputs.inventory_digest(output)!=inventory_sha256:
        raise ExecutionConflict('state_conflict','取消收尾对应的原尝试或清单已变化')
    if before[6] is None:
        from .cancelled_archive_recovery import settle
        return await settle(before,user_id)
    pointer=output.get(finalizing.KEY)
    observed,_,exit_event=await finalizing.outputs.observe_original(before,user_id,persist=True,allow_cancelling=True)
    if observed!={'state':'exited','reason':'process_exited'} or exit_event is None:
        raise ExecutionConflict('recovery_required','取消收尾尚未核验原worker实际退出')
    if pointer is None:
        from .finalizing_io import require_returned_cancel,legacy_attention
        try:guard=require_returned_cancel()
        except ExecutionConflict as error:
            if error.code=='legacy_finalizing_io_unverified':await legacy_attention(job_id)
            raise
        claim=job_execution.Claim(job.id,attempt.id,attempt.root_job_id,attempt.number,attempt.claim_token)
        budget=await execution_budget.load(claim);budget.restore(output[finalizing.outputs.KEY]['output_usage'])
        profile=finalizing.profile_for(before)
        async with SessionLocal() as db:
            await lock_case_execution(db,case.id)
            current=await finalizing.snapshot(db,job_id,user_id,allow_cancellation=True,allow_pending_log=True)
            if finalizing.KEY in current[4] or finalizing.profile_for(current)!=profile:raise ExecutionConflict('state_conflict','取消期间原存储收尾绑定已变化')
            request=await finalizing.requests.create(case.id,user_id,finalizing.KIND,profile,db=db)
            from .finalizing_native_usage import initialize
            with budget.activate():await initialize(db,request,profile,budget)
            current[4][finalizing.KEY]={'schema':finalizing.CERTIFICATE,'request_id':request.id}
            current[2].output_manifest_json=json_text(current[4])
            await append_event(db,case.id,'job.finalizing_io_cancelled',{'schema':'vibeflow.finalizing-io-cancel.v1',
                'operation_id':guard.lease.id,'request_id':request.id,'binding_sha256':finalizing.digest(guard.started),
                'actual_prework_returned':True,'native_execution_started':False},attempt_id=attempt.id)
            await finish_before_cancel(db.commit())
        # This records the actual cancellation-only operation. No native
        # command or validator ran; prepared remains absent and usage is spent
        # original credit. It is never presented as a successful validation.
        result={'schema':finalizing.CERTIFICATE,'profile_sha256':finalizing.digest(profile),'usage':budget.usage(),
            'prepared':None,'error':'OriginalJobCancelled'}
        await finish_before_cancel(finalizing.requests.complete(request,'OriginalJobCancelled',result=result))
        async with SessionLocal() as db:before=await finalizing.snapshot(db,job_id,user_id,allow_cancellation=True,allow_pending_log=True)
        pointer=before[4][finalizing.KEY]
    await recovery.retire(before,user_id,allow_cancellation=True)
    async with SessionLocal() as db:
        current=await finalizing.snapshot(db,job_id,user_id,allow_cancellation=True,allow_pending_log=True)
        if current[4].get(finalizing.KEY)!=pointer:raise ExecutionConflict('state_conflict','取消期间独立校验请求已变化')
        row=await db.get(NativeRequest,pointer['request_id'])
        if row is None or json.loads(row.profile_json)!=finalizing.profile_for(current):raise ValueError('Cancelled validator belongs to a different original attempt')
    if row.phase in {'failed','succeeded'} and row.error!='OwnerExited':await scopes.recover(row,terminal=True)
    async with SessionLocal() as db:
        current=await finalizing.snapshot(db,job_id,user_id,allow_cancellation=True,allow_pending_log=True)
        row,result=await finalizing.request_receipt(db,current)
    claim=job_execution.Claim(job.id,attempt.id,attempt.root_job_id,attempt.number,attempt.claim_token)
    budget=await execution_budget.load(claim);budget.restore(result['usage'])
    # The already committed archive contains this exact original root log.
    # Cancellation performs no cache reconstruction, upload or native work.
    async with SessionLocal() as db:
        await lock_case_execution(db,case.id)
        selected=await finalizing.snapshot(db,job_id,user_id,allow_cancellation=True,allow_pending_log=True)
        if selected[4].get(finalizing.KEY)!=pointer or finalizing.frozen_binding(*selected[:3])!=finalizing.frozen_binding(*current[:3]):
            raise ExecutionConflict('state_conflict','取消收尾的原始绑定已变化')
        selected[1].log_s3_key=manifest['run_path']+'/'+finalizing.source.LOG
        await finish_before_cancel(db.commit())
    with budget.activate():return await finalizing.complete(selected,claim,budget,row,result,exit_event,user_id)
