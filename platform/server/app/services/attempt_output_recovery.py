"""Resume an attested output upload; no native execution or terminal success grant."""
import asyncio,hashlib,json,os,uuid
from collections import OrderedDict
from datetime import datetime,timedelta,timezone
from sqlalchemy import select
from fastapi import HTTPException
from .. import storage
from ..db import SessionLocal
from ..models import DurableEvent,Job,JobAttempt,OutputArchiveReservation,User
from . import attempt_objects as objects,attempt_workspace as workspace,job_execution,job_worker_identity
from . import job_drain_recovery as drain,native_child_records,process_record,request_deadline
from .async_ops import finish_before_cancel
from .durable_events import json_text,now
from .upload_storage import binding
from .workflow_state import ExecutionConflict,TERMINAL

KEY='output_preparation'
LEGACY_SCHEMA='vibeflow.attempt-output-preparation.v1'
SCHEMA='vibeflow.attempt-output-preparation.v2'
RECOVERED_SCHEMA='vibeflow.attempt-output-preparation.v3'
IO_PROTOCOL='vibeflow.finalizing-io.v1'
PREPARED='attempt.output_preparation_ready'
RECOVERED='attempt.outputs_recovered'
LEGACY_RECOVERY='vibeflow.attempt-output-recovery.v2'
RECOVERY='vibeflow.attempt-output-recovery.v3'
DRAINED='attempt.output_copy_drained'
COPY_DRAIN='vibeflow.output-copy-drained.v1'
COPY_FIELDS={'schema','job_id','attempt_id','claim_sha256','writer','binding_sha256',
    'input_sha256','manifest_sha256','size','outcome','io_drained','observed_at'}
SLOTS=asyncio.Semaphore(2)
EXIT_OBSERVATIONS=OrderedDict()
OWNER_EXIT='attempt.output_owner_exited'
OWNER_SCHEMA='vibeflow.output-owner-exit.v1'
OWNER_FIELDS={'schema','job_id','attempt_id','original_owner','owner_exit','observer','observed_at'}
RECOVERY_FIELDS={'schema','job_id','attempt_id','inventory_sha256','input_sha256','case_binding_sha256',
    'archive','user_id','original_owner','owner_exit','observer','observed_at','native_execution_started'}

async def owner_observation(owner):
    """A witnessed exit stays true for this exact immutable process birth.

    Cache only positive exits, never live, mismatched or unavailable identities.
    This avoids a later PID reuse/query failure invalidating a witnessed exit;
    every action still revalidates its Job, native stop and inventory bindings.
    """
    key=digest(json_text(owner['process']))
    cached=EXIT_OBSERVATIONS.get(key)
    if cached is not None:
        EXIT_OBSERVATIONS.move_to_end(key);return cached
    observed=await drain.observe_owner(owner);value=(observed,now().isoformat())
    if observed=={'state':'exited','reason':'process_exited'}:
        EXIT_OBSERVATIONS[key]=value
        while len(EXIT_OBSERVATIONS)>256:EXIT_OBSERVATIONS.popitem(last=False)
    return value

def owner_event_id(attempt_id,owner):
    return str(uuid.uuid5(uuid.NAMESPACE_URL,'vibeflow.output-owner-exit:'+attempt_id+':'+digest(json_text(owner['process']))))

async def read_owner_exit(db,records):
    event=await db.get(DurableEvent,owner_event_id(records[2].id,records[5]))
    if event is None:return None
    value=json.loads(event.payload_json);original=records[5]['process']
    if (event.type!=OWNER_EXIT or event.case_id!=records[0].id or event.attempt_id!=records[2].id
            or set(value)!=OWNER_FIELDS or value['schema']!=OWNER_SCHEMA
            or value['job_id']!=records[1].id or value['attempt_id']!=records[2].id
            or value['original_owner']!=original or value['owner_exit']!={'state':'exited','reason':'process_exited'}):
        raise ValueError('Owner exit receipt belongs to a different process birth or attempt')
    from .process_identity_native import validate
    observer=validate(value['observer']);stamp=datetime.fromisoformat(value['observed_at'])
    recorded=event.created_at.replace(tzinfo=timezone.utc) if event.created_at.tzinfo is None else event.created_at
    if (observer['platform']!=original['platform'] or observer['host_id']!=original['host_id'] or observer==original
            or stamp.utcoffset()!=timedelta(0) or stamp>recorded):
        raise ValueError('Owner exit witness or observation time is invalid')
    return event

async def observe_original(records,user_id,*,persist=False,allow_cancelling=False):
    async with SessionLocal() as db:existing=await read_owner_exit(db,records)
    if existing is not None:
        value=json.loads(existing.payload_json);return value['owner_exit'],value['observed_at'],existing.id
    observed,at=await owner_observation(records[5])
    if not persist or observed!={'state':'exited','reason':'process_exited'}:return observed,at,None
    from .durable_events import append_event
    from .process_identity_native import observe
    observer=await finish_before_cancel(asyncio.to_thread(observe,os.getpid()))
    async with SessionLocal() as db:
        current=await snapshot(db,records[1].id,user_id,locked=True,allow_cancelling=allow_cancelling)
        if current[2].id!=records[2].id or current[5]!=records[5]:raise ExecutionConflict('state_conflict','原工作归属在退出核验期间发生变化')
        event=await read_owner_exit(db,current)
        if event is None:
            payload={'schema':OWNER_SCHEMA,'job_id':current[1].id,'attempt_id':current[2].id,
                'original_owner':current[5]['process'],'owner_exit':observed,'observer':observer,'observed_at':at}
            event=await append_event(db,current[0].id,OWNER_EXIT,payload,attempt_id=current[2].id,event_id=owner_event_id(current[2].id,current[5]))
            await finish_before_cancel(db.commit())
        value=json.loads(event.payload_json);return value['owner_exit'],value['observed_at'],event.id
LEGACY_FIELDS={'schema','status','exit_code','case_id','job_id','attempt_id','case_binding_sha256',
    'input_sha256','inventory_sha256','worker_sha256','process_sha256','children_sha256','stop','output_usage'}
FIELDS=LEGACY_FIELDS|{'recovery_protocol'}
RECOVERED_FIELDS=FIELDS|{'stage_sha256'}

def digest(value):return hashlib.sha256(value.encode('utf8')).hexdigest()

def inventory(output):
    return {key:output[key] for key in ('schema','run_path','files','directories','input_attempt_id','publications','dependency_error') if key in output}

def inventory_digest(output):return digest(json_text(inventory(output)))

def copy_event_id(attempt_id,token):
    return str(uuid.uuid5(uuid.NAMESPACE_URL,'vibeflow.output-copy:'+attempt_id+':'+digest(token)))

async def writer_drained(db,row):
    """An exact completed request can retire its live API's immutable write fence.

    The allowance remains active. Reuse authorizes only this same manifest's
    conditional keys, including a late response from the drained SDK request.
    """
    event=await db.get(DurableEvent,copy_event_id(row.attempt_id,row.token))
    if event is None:return False
    value=json.loads(event.payload_json)
    if (event.type!=DRAINED or event.attempt_id!=row.attempt_id or event.case_id!=row.case_id
            or not isinstance(value,dict) or set(value)!=COPY_FIELDS or value['schema']!=COPY_DRAIN
            or value['attempt_id']!=row.attempt_id or value['claim_sha256']!=digest(row.token)
            or value['writer']!=json.loads(row.writer_json) or value['binding_sha256']!=digest(row.binding_json)
            or value['input_sha256']!=row.input_sha256 or value['manifest_sha256']!=row.manifest_sha256
            or value['size']!=row.size or value['io_drained'] is not True
            or value['outcome'] not in {'returned','failed','cancelled'}):
        raise ValueError('Output copy drain receipt differs from the reserved writer')
    attempt=await db.get(JobAttempt,row.attempt_id)
    if attempt is None or value['job_id']!=attempt.job_id:raise ValueError('Output copy drain belongs to another Job')
    from .process_identity_native import validate
    validate(value['writer'])
    stamp=datetime.fromisoformat(value['observed_at'])
    recorded=event.created_at.replace(tzinfo=timezone.utc) if event.created_at.tzinfo is None else event.created_at
    if stamp.utcoffset()!=timedelta(0) or stamp>recorded:raise ValueError('Invalid output copy drain time')
    return True

async def record_copy_drained(ticket,outcome):
    """Called only after the protected thread has actually returned or raised."""
    from .storage_admission import lock_owner
    from .durable_events import append_event
    async with SessionLocal() as db:
        # Cleanup of admitted work may outlive its request, but still uses the
        # same finite settlement lock budget. It grants no new output bytes.
        await lock_owner(db,ticket.user_id,wait=True,settlement=True)
        row=await db.get(OutputArchiveReservation,ticket.attempt_id,populate_existing=True)
        if row is None or row.token!=ticket.token or row.state!='active':return
        from .output_archive_storage import require_binding
        require_binding(row,ticket)
        if await writer_drained(db,row):return
        attempt=await db.get(JobAttempt,row.attempt_id)
        job=await db.get(Job,attempt.job_id) if attempt else None
        if job is None or job.case_id!=row.case_id:raise ValueError('Output copy drain lost its original Job')
        payload={'schema':COPY_DRAIN,'job_id':job.id,'attempt_id':row.attempt_id,'claim_sha256':digest(row.token),
            'writer':json.loads(row.writer_json),'binding_sha256':digest(row.binding_json),
            'input_sha256':row.input_sha256,'manifest_sha256':row.manifest_sha256,'size':row.size,
            'outcome':outcome,'io_drained':True,'observed_at':now().isoformat()}
        await append_event(db,row.case_id,DRAINED,payload,attempt_id=row.attempt_id,event_id=copy_event_id(row.attempt_id,row.token))
        await finish_before_cancel(db.commit())

def proof(case,job,attempt,output,status,code,stop,usage,*,schema=SCHEMA):
    if schema not in {SCHEMA,LEGACY_SCHEMA,RECOVERED_SCHEMA}:raise ValueError('Unsupported output preparation protocol')
    value={'schema':schema,'status':status,'exit_code':code,'case_id':case.id,'job_id':job.id,'attempt_id':attempt.id,
        'case_binding_sha256':digest(binding(case)),'input_sha256':digest(attempt.input_manifest_json),
        'inventory_sha256':inventory_digest(output),'worker_sha256':digest(attempt.worker_identity_json),
        'process_sha256':digest(attempt.process_identity_json),'children_sha256':digest(attempt.child_processes_json),
        'stop':stop,'output_usage':usage}
    if schema in {SCHEMA,RECOVERED_SCHEMA}:value['recovery_protocol']=IO_PROTOCOL
    if schema==RECOVERED_SCHEMA:value['stage_sha256']=digest(json_text(output['finalizing_stage']))
    return value


def current_contract(value):
    return isinstance(value,dict) and value.get('schema') in {SCHEMA,RECOVERED_SCHEMA} and value.get('recovery_protocol')==IO_PROTOCOL


def validate_preparation(case,job,attempt,output,prepared):
    schema=prepared.get('schema') if isinstance(prepared,dict) else None
    fields=RECOVERED_FIELDS if schema==RECOVERED_SCHEMA else FIELDS if schema==SCHEMA else LEGACY_FIELDS
    if (schema not in {SCHEMA,LEGACY_SCHEMA,RECOVERED_SCHEMA} or set(prepared)!=fields
            or prepared.get('status') not in TERMINAL or type(prepared.get('exit_code')) is not int
            or prepared!=proof(case,job,attempt,output,prepared['status'],prepared['exit_code'],prepared['stop'],prepared['output_usage'],schema=schema)):
        raise ExecutionConflict('output_recovery_unavailable','原尝试缺少匹配的输出准备及停止凭据')
    return prepared

def closed(job,attempt,stop):
    drain.complete_stop(stop,process_record.for_attempt(json.loads(attempt.process_identity_json),job,attempt))
    for child in native_child_records.document(job,attempt)['children'].values():
        if child['phase'] not in {'stopped','failed'}:raise ValueError('Output preparation still has a native writer')
        drain.complete_stop(child['stop'],child['identity'])

async def record_capture(db,case,job,attempt,owner,output,completion):
    """Original runner attests the already-observed stop before its remote upload.

    This is not a complete content/output drain: later professional validators,
    log upload and publication preparation still have their original budgets.
    """
    from .execution_budget import current
    from .execution_output_checkpoint import validate_usage
    if completion is None or current(owner) is None:return
    status,code,stop=completion
    if status not in TERMINAL or type(code) is not int or stop is None:return
    if attempt.worker_id!=job_execution.WORKER_ID:raise ExecutionConflict('state_conflict','Only the original worker may attest captured output')
    closed(job,attempt,stop)
    if status=='succeeded' and (code!=0 or stop['root_exit_code']!=0):raise ValueError('Successful captured output lacks successful native exit')
    usage=validate_usage(current(owner).usage(),json.loads(attempt.input_manifest_json)['execution_spec']['max_log_bytes'])
    output[KEY]=proof(case,job,attempt,output,status,code,stop,usage)
    await job_execution.attempt_event(db,job,attempt,PREPARED,output[KEY])

async def snapshot(db,job_id,user_id,*,locked=False,allow_cancelling=False):
    from .job_recovery_notice import rows
    from ..routers.workspaces import get_owned_case
    user=await db.get(User,user_id,populate_existing=True)
    if user is None or user.status!='active':raise HTTPException(403,'账户已不可用')
    case,job,attempt,head=await rows(db,job_id,lock=locked)
    if job is None:raise HTTPException(404,'作业不存在')
    await get_owned_case(job.case_id,user,db)
    if (attempt is None or head is None or head.active_attempt_id!=attempt.id
            or attempt.job_id!=job.id or attempt.phase not in TERMINAL|{'finalizing'}|({'cancelling'} if allow_cancelling else set())):
        raise ExecutionConflict('state_conflict','原尝试已变化或尚未进入收尾')
    local=not storage._use_s3()
    output=json.loads(attempt.output_manifest_json);manifest=json.loads(attempt.input_manifest_json).get('workspace')
    if (not isinstance(manifest,dict) or output.get('schema')!='vibeflow.attempt-outputs.v1'
            or output.get('input_attempt_id')!=attempt.id or output.get('run_path')!=manifest.get('run_path')
            or (not local and output.get('object_store',{}).get('state') not in {'pending','complete'})
            or (local and (output.get(KEY,{}).get('schema')!=RECOVERED_SCHEMA
                or output.get('local_store')!={'schema':'vibeflow.finalizing-stage-recovery.v1','stage_sha256':output[KEY].get('stage_sha256')}))):
        raise ExecutionConflict('output_recovery_unavailable','原尝试没有完整的输出清单')
    if not local:
        from .output_archive_storage import plan
        plan(case.s3_prefix,attempt.input_manifest_json,attempt.output_manifest_json)
    owner=job_worker_identity.document(job,attempt)
    if local:
        await verify_preparation(db,case,job,attempt,output,pending=attempt.phase not in TERMINAL)
        from .finalizing_stage_recovery import verify_capture
        event=await verify_capture(db,case,job,attempt,output)
        return case,job,attempt,manifest,output,owner,event
    saved=(await db.execute(select(DurableEvent,JobAttempt).join(JobAttempt,JobAttempt.id==DurableEvent.attempt_id)
        .where(JobAttempt.id==attempt.id,DurableEvent.type==RECOVERED)
        .order_by(DurableEvent.seq).limit(2).execution_options(populate_existing=True))).all()
    events=[row[0] for row in saved]
    if events:
        # A peer may have committed after this reader first loaded the attempt.
        # Its receipt and newly completed archive must come from one statement.
        output=json.loads(attempt.output_manifest_json)
        manifest=json.loads(attempt.input_manifest_json)['workspace']
        if len(events)!=1 or events[0].case_id!=case.id:raise ValueError('Duplicate or rebound output recovery receipt')
        payload=json.loads(events[0].payload_json)
        schema=payload.get('schema')
        fields=RECOVERY_FIELDS|{'owner_observed_at','owner_exit_event_id'} if schema in {RECOVERY,LEGACY_RECOVERY} else RECOVERY_FIELDS
        if schema==RECOVERY:fields|={'preparation_sha256','recovery_protocol'}
        if (set(payload)!=fields or schema not in {RECOVERY,LEGACY_RECOVERY,'vibeflow.attempt-output-recovery.v1'}
                or payload.get('job_id')!=job.id or payload.get('attempt_id')!=attempt.id
                or payload.get('inventory_sha256')!=inventory_digest(output)
                or payload.get('input_sha256')!=digest(attempt.input_manifest_json)
                or payload.get('case_binding_sha256')!=digest(binding(case))
                or payload.get('original_owner')!=owner['process']
                or payload.get('owner_exit')!={'state':'exited','reason':'process_exited'}
                or type(payload.get('user_id')) is not int or payload['user_id']<=0
                or output['object_store']['state']!='complete'
                or payload.get('archive')!=output['object_store']
                or payload.get('native_execution_started') is not False):
            raise ValueError('Output recovery receipt no longer matches its immutable archive')
        from .process_identity_native import validate
        observer=validate(payload['observer'])
        if (observer['platform']!=owner['process']['platform'] or observer['host_id']!=owner['process']['host_id']
                or observer==owner['process'] or datetime.fromisoformat(payload['observed_at']).utcoffset()!=timedelta(0)):
            raise ValueError('Output recovery observer or timestamp is invalid')
        prepared=await verify_preparation(db,case,job,attempt,output,pending=attempt.phase not in TERMINAL)
        if schema==RECOVERY:
            if (not current_contract(prepared) or payload['recovery_protocol']!=IO_PROTOCOL
                    or payload['preparation_sha256']!=digest(json_text(prepared))):
                raise ValueError('Versioned archive lost its original IO exclusion contract')
        elif current_contract(prepared):raise ValueError('Modern preparation cannot use an unguarded legacy archive receipt')
        if schema in {RECOVERY,LEGACY_RECOVERY}:
            stamp=datetime.fromisoformat(payload['owner_observed_at'])
            if stamp.utcoffset()!=timedelta(0) or stamp>datetime.fromisoformat(payload['observed_at']):raise ValueError('Owner exit observation time is invalid')
            certificate=await read_owner_exit(db,(case,job,attempt,manifest,output,owner,None))
            if (certificate is None or payload['owner_exit_event_id']!=certificate.id
                    or payload['owner_observed_at']!=json.loads(certificate.payload_json)['observed_at']):
                raise ValueError('Output recovery lost its bound owner exit certificate')
        return case,job,attempt,manifest,output,owner,events[0]
    await verify_preparation(db,case,job,attempt,output,pending=attempt.phase not in TERMINAL)
    return case,job,attempt,manifest,output,owner,None


async def verify_preparation(db,case,job,attempt,output,*,pending=True):
    """Original early stop/usage history, without inventing full content drain."""
    prepared=output.get(KEY)
    validate_preparation(case,job,attempt,output,prepared)
    from .execution_output_checkpoint import validate_usage
    validate_usage(prepared['output_usage'],json.loads(attempt.input_manifest_json)['execution_spec']['max_log_bytes'])
    closed(job,attempt,prepared['stop'])
    if prepared['status']=='succeeded' and (prepared['exit_code']!=0 or prepared['stop']['root_exit_code']!=0):raise ValueError('Prepared native success is inconsistent')
    if prepared['schema']==RECOVERED_SCHEMA:
        from .finalizing_stage_recovery import verify_capture
        await verify_capture(db,case,job,attempt,output)
    else:
        original=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type==PREPARED).limit(2)))
        if len(original)!=1 or original[0].case_id!=case.id or json.loads(original[0].payload_json)!=prepared:raise ValueError('Output preparation has no unique durable history')
    await drain.history(db,case,job,attempt,pending=pending)
    return prepared

def check_deadline():
    value=request_deadline.current()
    if value is not None:value.check()

def resume_objects(prefix,manifest,output):
    """Reuse matching remote members, then copy only missing captured bytes."""
    from botocore.exceptions import ClientError
    encoded,receipt=objects.output_plan(prefix,manifest,output['files'],output['directories'])
    run=storage._fs_path(prefix,manifest['run_path'])
    for name,expected in output['files'].items():
        check_deadline()
        try:objects.verify_members(prefix,manifest['run_path'],{name:expected})
        except (FileNotFoundError,ClientError) as error:
            if isinstance(error,ClientError) and error.response['ResponseMetadata']['HTTPStatusCode']!=404:raise
            path=workspace.checked_path(run,name)
            objects.put_output_member(prefix,manifest['run_path'],name,path,expected)
        check_deadline()
    objects.put_new(prefix,receipt['manifest_path'],encoded)
    objects.verify_outputs(prefix,manifest,{**output,'object_store':{'state':'complete',**receipt}})
    check_deadline()
    return receipt

def response(records,event):
    return {'state':'archived' if storage._use_s3() else 'captured','job_id':records[1].id,'attempt_id':records[2].id,
        'inventory_sha256':inventory_digest(records[4]),'recovery_event_id':event.id,
        'native_execution_started':False,'finalization_pending':records[2].phase not in TERMINAL or job_execution.completion_visibility(records[2]) in {'pending','invalid'}}


def select_archived_failure_log(case,job,attempt,manifest,output):
    """An expired execution may have settled before its log could be uploaded.

    Explicit output recovery has already verified the immutable archive. Point
    failed/interrupted jobs at that same captured log without new execution or
    a second log upload. Keep canonical log receipts and all success paths.
    """
    from . import job_finalizing_source as source
    value=output.get(source.KEY)
    if (job.status not in {'failed','interrupted'} or not isinstance(value,dict)
            or job.log_s3_key!=value.get('source_log_key')
            or output.get('object_store',{}).get('state')!='complete'):
        return
    source.validate(case,job,attempt,output,value)
    job.log_s3_key=manifest['run_path']+'/'+source.LOG

async def options(job_id,user_id):
    try:
        async with SessionLocal() as db:
            records=await snapshot(db,job_id,user_id,allow_cancelling=True)
            from .job_recovery_notice import public
            notice=await public(db,records[1]) if records[1].status=='cancelled' and records[2].phase=='cancelling' else None
    except (ValueError,KeyError,TypeError,AttributeError) as error:
        raise ExecutionConflict('output_recovery_unavailable','输出准备、归属或停止凭据无法核验') from error
    if records[6] is not None:
        value=response(records,records[6])
        if records[1].status=='cancelled' and records[2].phase=='cancelling':
            value['cancellation_recovery']={'preparation_schema':records[4][KEY]['schema'],
                'archive_schema':json.loads(records[6].payload_json)['schema'],
                'authority':'versioned_io_protocol' if current_contract(records[4][KEY]) else 'recorded_io_stop_required',
                'automatic_replay':False,'notice':notice}
        return value
    if records[1].status=='cancelled' and records[2].phase=='cancelled':
        from .cancelled_archive_recovery import verify_history
        async with SessionLocal() as db:
            await verify_history(db,*records[:3],json.loads(records[2].write_checkpoint_json))
        return {'state':'cancelled','job_id':records[1].id,'attempt_id':records[2].id,
            'inventory_sha256':inventory_digest(records[4]),'native_execution_started':False,
            'automatic_replay':False,'finalization_pending':False,'archive_pending':True,'next_step':'retain_pending_archive'}
    observed,_,_=await observe_original(records,user_id)
    if records[1].status=='cancelled' and records[2].phase=='cancelling':
        return {'state':'requires_attention','reason':'cancelled_output_archive_pending','next_step':'review_pending_output_copy',
            'job_id':records[1].id,'attempt_id':records[2].id,'inventory_sha256':inventory_digest(records[4]),
            'owner_observation':observed,'native_execution_started':False,'automatic_replay':False,
            'finalization_pending':True,'archive_pending':True}
    return {'state':'recoverable' if observed=={'state':'exited','reason':'process_exited'} else 'owner_unavailable_for_recovery',
        'job_id':records[1].id,'attempt_id':records[2].id,'inventory_sha256':inventory_digest(records[4]),
        'owner_observation':observed,'native_execution_started':False,'finalization_pending':True}

async def recover(job_id,user_id,attempt_id,inventory_sha256):
    from botocore.exceptions import BotoCoreError,ClientError
    async with request_deadline.scope(300):
        try:await asyncio.wait_for(SLOTS.acquire(),.1)
        except TimeoutError as error:raise ExecutionConflict('output_recovery_busy','输出补传核验正在进行，请稍后重试') from error
        try:return await _recover(job_id,user_id,attempt_id,inventory_sha256)
        except (ValueError,KeyError,TypeError,AttributeError,OSError,BotoCoreError,ClientError) as error:
            reply=await archived_reply(job_id,user_id,attempt_id,inventory_sha256)
            if reply is not None:return reply
            raise ExecutionConflict('output_recovery_unavailable','输出副本、绑定或恢复凭据无法核验；保留现有数据') from error
        except (HTTPException,ExecutionConflict):
            reply=await archived_reply(job_id,user_id,attempt_id,inventory_sha256)
            if reply is not None:return reply
            raise
        finally:SLOTS.release()

async def archived_reply(job_id,user_id,attempt_id,inventory_sha256):
    """A peer may have committed while an older request was checking admission."""
    from botocore.exceptions import BotoCoreError,ClientError
    try:
        async with SessionLocal() as db:current=await snapshot(db,job_id,user_id)
        if current[6] is None or current[2].id!=attempt_id or inventory_digest(current[4])!=inventory_sha256:return None
        await finish_before_cancel(asyncio.to_thread(objects.verify_outputs if storage._use_s3() else verify_local,current[0].s3_prefix,current[3],current[4]))
        return response(current,current[6])
    except (ValueError,KeyError,TypeError,AttributeError,OSError,BotoCoreError,ClientError,HTTPException,ExecutionConflict):return None

async def _recover(job_id,user_id,attempt_id,inventory_sha256):
    async with SessionLocal() as db:before=await snapshot(db,job_id,user_id)
    case,job,attempt,manifest,output,owner,event=before
    if attempt.id!=attempt_id or inventory_digest(output)!=inventory_sha256:raise ExecutionConflict('state_conflict','请求对应的原尝试或输出清单已变化')
    if event is not None:
        await finish_before_cancel(asyncio.to_thread(objects.verify_outputs if storage._use_s3() else verify_local,case.s3_prefix,manifest,output))
        async with SessionLocal() as db:
            current=await snapshot(db,job_id,user_id,locked=True)
            if current[2].id!=attempt_id or inventory_digest(current[4])!=inventory_sha256:
                raise ExecutionConflict('state_conflict','归档日志核对期间原尝试或输出清单已变化')
            select_archived_failure_log(*current[:5])
            await finish_before_cancel(db.commit())
            return response(current,current[6])
    if job.status=='cancelled' or attempt.cancel_requested_at is not None:
        raise ExecutionConflict('cancelled_archive_pending','原任务已取消；保留待归档数据，禁止新补传或改写归档回执')
    observed,owner_observed_at,owner_exit_event_id=await observe_original(before,user_id,persist=True)
    if observed!={'state':'exited','reason':'process_exited'}:
        raise HTTPException(409,{'code':'recovery_required','message':'原工作进程仍存活或其身份无法核验','owner_observation':observed})
    from . import process_identity_native as identity
    observer=await finish_before_cancel(asyncio.to_thread(identity.observe,os.getpid()))
    from .output_archive_storage import capacity
    claim=job_execution.Claim(job.id,attempt.id,attempt.root_job_id,attempt.number,attempt.claim_token)
    input_json=attempt.input_manifest_json;output_json=attempt.output_manifest_json
    async with capacity(claim,case.id,case.s3_prefix,input_json,output_json,
            recover_terminal=job.status in {'failed','interrupted'}) as ticket:
        if ticket.ready:archive=ticket.receipt
        else:
            ticket.started=True
            outcome='failed'
            try:
                archive=await finish_before_cancel(asyncio.to_thread(resume_objects,case.s3_prefix,manifest,output));outcome='returned'
            except asyncio.CancelledError:outcome='cancelled';raise
            finally:await finish_before_cancel(record_copy_drained(ticket,outcome))
    async with SessionLocal() as db:
        current=await snapshot(db,job_id,user_id,locked=True)
        if current[6] is not None:return response(current,current[6])
        case,job,attempt,_,current_output,_,_=current
        if job.status=='cancelled' or attempt.cancel_requested_at is not None:
            raise ExecutionConflict('cancelled_archive_pending','补传期间原任务已取消；保留实际写入和容量，不提交成功归档回执')
        if attempt.id!=attempt_id or attempt.input_manifest_json!=input_json or attempt.output_manifest_json!=output_json:
            raise ExecutionConflict('state_conflict','补传期间原尝试或输出绑定发生变化')
        terminal_snapshot=None
        if job.status in {'failed','interrupted'}:
            from .job_write_checkpoint import validate as validate_checkpoint
            validate_checkpoint(case,job,attempt)
            terminal_snapshot=(drain.snapshot(case,job,attempt),attempt.write_checkpoint_json)
        current_output['object_store']={'state':'complete',**archive}
        select_archived_failure_log(case,job,attempt,manifest,current_output)
        attempt.output_manifest_json=json_text(current_output);attempt.version+=1
        modern=current_contract(current_output.get(KEY))
        payload={'schema':RECOVERY if modern else LEGACY_RECOVERY,'job_id':job.id,'attempt_id':attempt.id,
            'inventory_sha256':inventory_sha256,'input_sha256':digest(input_json),
            'case_binding_sha256':digest(binding(case)),'archive':current_output['object_store'],'user_id':user_id,
            'original_owner':owner['process'],'owner_exit':observed,'owner_observed_at':owner_observed_at,
            'owner_exit_event_id':owner_exit_event_id,
            'observer':observer,'observed_at':now().isoformat(),
            'native_execution_started':False}
        if modern:payload.update(preparation_sha256=digest(json_text(current_output[KEY])),recovery_protocol=IO_PROTOCOL)
        archive_event=await job_execution.attempt_event(db,job,attempt,RECOVERED,payload)
        if terminal_snapshot is not None:
            await drain.bind_archived_output(db,case,job,attempt,*terminal_snapshot,archive_event)
        await finish_before_cancel(db.commit())
    async with SessionLocal() as db:
        final=await snapshot(db,job_id,user_id)
        return response(final,final[6])


def verify_local(prefix,manifest,output):
    _,files,directories=workspace.inspect_runtime(prefix,manifest,True)
    if files!=output['files'] or directories!=output['directories']:
        raise ExecutionConflict('output_cache_unavailable','本地原执行清单已变化或丢失；保留现场，不能用封存输入重算产物')
