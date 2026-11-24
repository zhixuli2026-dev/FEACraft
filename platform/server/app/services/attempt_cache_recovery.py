"""Explicit restoration of an attested archived run before full finalization.

No native launch, fresh execution credit, Job terminal transition or publication
authority is created here. The original output and worker history stay intact.
"""
import asyncio,json,os
from sqlalchemy import select
from ..db import SessionLocal
from ..models import DurableEvent
from . import attempt_objects as objects,attempt_output_recovery as outputs,job_execution,request_deadline
from .async_ops import finish_before_cancel
from .durable_events import json_text,now
from .workflow_state import ExecutionConflict

EVENT='attempt.output_cache_restored'
SCHEMA='vibeflow.attempt-output-cache.v1'


async def snapshot(db,job_id,user_id,*,locked=False):
    records=await outputs.snapshot(db,job_id,user_id,locked=locked)
    case,job,attempt,manifest,output,owner,event=records
    if (event is None or (objects.storage._use_s3() and output['object_store']['state']!='complete')
            or job.status!='running' or attempt.phase!='finalizing' or attempt.cancel_requested_at is not None
            or attempt.write_checkpoint_json!='{}' or {'bundle_preparation','finalization_intent'}&set(output)):
        raise ExecutionConflict('state_conflict','原输出尚未归档或收尾状态已变化')
    prepared=output.get(outputs.KEY)
    outputs.validate_preparation(case,job,attempt,output,prepared)
    await outputs.verify_preparation(db,case,job,attempt,output,pending=True)
    outputs.closed(job,attempt,prepared['stop'])
    await outputs.drain.history(db,case,job,attempt,pending=True)
    return records


def binding(records):
    case,job,attempt,_,_,_,event=records
    from .upload_storage import binding as case_binding
    return (case_binding(case),job.id,job.status,attempt.id,attempt.phase,
        attempt.input_manifest_json,attempt.output_manifest_json,attempt.publication_json,
        attempt.worker_identity_json,attempt.process_identity_json,attempt.child_processes_json,
        attempt.write_checkpoint_json,outputs.digest(attempt.claim_token),event.id)


async def restore(job_id,user_id,attempt_id,inventory_sha256):
    from botocore.exceptions import BotoCoreError,ClientError
    async with request_deadline.scope(300):
        try:await asyncio.wait_for(outputs.SLOTS.acquire(),.1)
        except TimeoutError as error:raise ExecutionConflict('output_recovery_busy','输出恢复正在进行，请稍后重试') from error
        try:
            from .finalizing_io import hold
            async with hold(job_id,user_id,attempt_id,inventory_sha256):
                return await _restore(job_id,user_id,attempt_id,inventory_sha256)
        except TimeoutError:
            raise
        except (ValueError,KeyError,TypeError,AttributeError,OSError,BotoCoreError,ClientError) as error:
            raise ExecutionConflict('output_cache_unavailable','原归档或执行缓存无法核验；保留已有数据') from error
        finally:outputs.SLOTS.release()


async def _restore(job_id,user_id,attempt_id,inventory_sha256):
    async with SessionLocal() as db:before=await snapshot(db,job_id,user_id)
    case,job,attempt,manifest,output,_,archive_event=before
    if attempt.id!=attempt_id or outputs.inventory_digest(output)!=inventory_sha256:
        raise ExecutionConflict('state_conflict','请求对应的原尝试或输出清单已变化')
    observed,_,owner_event_id=await outputs.observe_original(before,user_id,persist=True)
    if observed!={'state':'exited','reason':'process_exited'} or owner_event_id is None:
        raise ExecutionConflict('recovery_required','原工作进程的确切退出状态无法核验')
    if not objects.storage._use_s3():
        await finish_before_cancel(asyncio.to_thread(outputs.verify_local,case.s3_prefix,manifest,output))
        return {'state':'cache_ready','job_id':job.id,'attempt_id':attempt.id,'inventory_sha256':inventory_sha256,
            'capture_event_id':archive_event.id,'native_execution_started':False,'finalization_pending':True}
    view=None
    async def prepare():
        nonlocal view
        view=await asyncio.to_thread(objects.prepare_runtime_cache,case.s3_prefix,manifest,output)
    try:
        await finish_before_cancel(prepare())
        from .process_identity_native import observe
        observer=await finish_before_cancel(asyncio.to_thread(observe,os.getpid()))
        async with SessionLocal() as db:
            current=await snapshot(db,job_id,user_id,locked=True)
            if binding(current)!=binding(before):raise ExecutionConflict('state_conflict','下载期间原尝试或输出绑定发生变化')
            request_deadline.current().check()
            await finish_before_cancel(asyncio.to_thread(objects.install_runtime_cache,case.s3_prefix,manifest,output,view))
            events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type==EVENT).limit(2)))
            if len(events)>1:raise ValueError('Runtime cache has duplicate restoration receipts')
            event=events[0] if events else None
            core={'schema':SCHEMA,'job_id':job.id,'attempt_id':attempt.id,'inventory_sha256':inventory_sha256,
                'input_sha256':outputs.digest(attempt.input_manifest_json),'archive_event_id':archive_event.id,
                'owner_exit_event_id':owner_event_id,'native_execution_started':False,'finalization_pending':True}
            if event is not None:
                payload=json.loads(event.payload_json)
                if (event.type!=EVENT or event.case_id!=case.id or event.attempt_id!=attempt.id
                        or set(payload)!=set(core)|{'user_id','observer','observed_at'}
                        or any(payload.get(k)!=v for k,v in core.items())):
                    raise ValueError('Runtime cache receipt no longer matches the original archive')
            else:
                payload={**core,'user_id':user_id,'observer':observer,'observed_at':now().isoformat()}
                event=await job_execution.attempt_event(db,current[1],current[2],EVENT,payload)
                await finish_before_cancel(db.commit())
            return {'state':'cache_ready','job_id':job.id,'attempt_id':attempt.id,
                'inventory_sha256':inventory_sha256,'recovery_event_id':event.id,
                'archive_event_id':archive_event.id,'native_execution_started':False,'finalization_pending':True}
    finally:
        if view is not None:await finish_before_cancel(asyncio.to_thread(view.cleanup))
