"""Explicit continuation before captured output exists; no original native replay."""
import asyncio,json,os,re,uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from sqlalchemy import select
from fastapi import HTTPException

from .. import storage
from ..db import SessionLocal
from ..models import CaseStorageOperation,CaseValidationFence,DurableEvent,User
from . import finalizing_stage as stages,attempt_workspace as workspace,attempt_output_recovery as outputs
from . import process_identity_native as identity,storage_operations as operations,job_execution,job_worker_identity
from .async_ops import finish_before_cancel
from .durable_events import append_event,json_text,lock_case_execution,now
from .workflow_state import ExecutionConflict

SCHEMA='vibeflow.finalizing-stage-recovery.v1'
CAPTURED='attempt.finalizing_stage_recovered'
IO_SCHEMA='vibeflow.finalizing-stage-io.v1'
IO_STARTED='attempt.finalizing_stage_io_started'
IO_DRAINED='attempt.finalizing_stage_io_drained'
IO_EXITED='attempt.finalizing_stage_io_exited'


def io_event(identifier,kind):return str(uuid.uuid5(uuid.UUID(identifier),IO_SCHEMA+':'+kind))


async def snapshot(db,job_id,user_id,*,locked=False):
    from .job_recovery_notice import rows
    from ..routers.workspaces import get_owned_case
    user=await db.get(User,user_id,populate_existing=True)
    if user is None or user.status!='active':raise HTTPException(403,'账户已不可用')
    case,job,attempt,head=await rows(db,job_id,lock=locked)
    if job is None:raise HTTPException(404,'作业不存在')
    await get_owned_case(job.case_id,user,db)
    if attempt is None or head is None or head.active_attempt_id!=attempt.id:
        raise ExecutionConflict('state_conflict','原收尾尝试已变化')
    value=await stages.read(db,case,job,attempt)
    if value is None:raise ExecutionConflict('early_finalization_unverified','原任务没有持久收尾阶段证明；保留原数据并核查原执行现场')
    if not value['supported']:raise ExecutionConflict('early_finalization_manual_review','原收尾包含未登记的回调或旧暂存同步；保留原数据并人工核查，不能自动重放')
    await stages.read_result(db,case,job,attempt,value)
    output=json.loads(attempt.output_manifest_json)
    if output.get('schema')!='vibeflow.attempt-outputs.v1' and attempt.phase not in {'finalizing','cancelling'}:
        raise ExecutionConflict('state_conflict','原任务已离开待接续的收尾阶段')
    return case,job,attempt,json.loads(attempt.input_manifest_json)['workspace'],output,job_worker_identity.document(job,attempt),None


async def original_exit(records,user_id):
    observed,stamp,event_id=await outputs.observe_original(records,user_id)
    if observed!={'state':'exited','reason':'process_exited'}:
        raise ExecutionConflict('recovery_required','原worker仍存活或实际退出无法核验；不接管其收尾写入')
    if event_id is not None:return event_id
    observer=await asyncio.to_thread(identity.observe,os.getpid())
    async with SessionLocal() as db:
        await lock_case_execution(db,records[0].id)
        current=await snapshot(db,records[1].id,user_id)
        if current[2].id!=records[2].id or current[4][stages.KEY]!=records[4][stages.KEY]:raise ExecutionConflict('state_conflict','原收尾阶段已变化')
        event=await outputs.read_owner_exit(db,current)
        if event is None:
            value={'schema':outputs.OWNER_SCHEMA,'job_id':current[1].id,'attempt_id':current[2].id,
                'original_owner':current[5]['process'],'owner_exit':observed,'observer':observer,'observed_at':stamp}
            event=await append_event(db,current[0].id,outputs.OWNER_EXIT,value,attempt_id=current[2].id,
                event_id=outputs.owner_event_id(current[2].id,current[5]))
            await finish_before_cancel(db.commit())
        return event.id


async def io_intent(db,row,stage):
    event=await db.get(DurableEvent,io_event(row.id,'started'),populate_existing=True)
    value=json.loads(event.payload_json) if event else None
    expected={'schema':IO_SCHEMA,'operation_id':row.id,'stage_sha256':stage.sha256,
        'job_id':stage.owner.job_id,'attempt_id':stage.owner.attempt_id,'owner':json.loads(row.owner_json)}
    if (event is None or event.type!=IO_STARTED or event.case_id!=stage.case_id or event.attempt_id!=stage.owner.attempt_id
            or value!=expected or row.event_id!=event.id or row.purpose!='recovery' or row.case_id!=stage.case_id
            or row.source_prefix!=stage.prefix or row.journal_id!=stage.owner.attempt_id or row.plan_sha256!=stage.sha256):
        raise ExecutionConflict('file_recovery_required','原存储操作不属于此收尾阶段；保留现场并按原入口恢复')
    identity.validate(expected['owner']);return expected


async def io_closed(db,row,stage):
    started=await io_intent(db,row,stage)
    local=await db.get(DurableEvent,io_event(row.id,'drained'));exited=await db.get(DurableEvent,io_event(row.id,'exited'))
    if local is not None and exited is not None:raise ValueError('Early IO has conflicting closure proofs')
    event=local or exited
    if event is None:return False
    value=json.loads(event.payload_json)
    expected={'schema':IO_SCHEMA,'operation_id':row.id,'intent_sha256':stages.digest(started)}
    if local:expected['io_returned']=True
    else:
        expected.update(owner_exit={'state':'exited','reason':'process_exited'},observer=value.get('observer'))
        observer=identity.validate(expected['observer']);writer=started['owner']
        if observer==writer or observer['platform']!=writer['platform'] or observer['host_id']!=writer['host_id']:
            raise ValueError('Early IO lacks an independent exact exit witness')
    if event.case_id!=stage.case_id or event.attempt_id!=stage.owner.attempt_id or event.type!=(IO_DRAINED if local else IO_EXITED) or value!=expected:
        raise ValueError('Early IO closure belongs to another stage')
    return True


async def acquire(records,user_id):
    case,job,attempt,manifest,output,*_=records
    claim=job_execution.Claim(job.id,attempt.id,attempt.root_job_id,attempt.number,attempt.claim_token)
    stage=stages.Stage(claim,case.id,case.s3_prefix,manifest,output[stages.KEY]);candidates=[]
    async with SessionLocal() as db:
        rows=list(await db.scalars(select(CaseStorageOperation).where(CaseStorageOperation.case_id==case.id,
            CaseStorageOperation.state.in_(('active','drained','exited')))))
        for row in rows:
            await io_intent(db,row,stage);closed=await io_closed(db,row,stage)
            candidates.append((row.id,row.owner_json,row.state,closed))
    for _,owner,_,closed in candidates:
        if not closed and await asyncio.to_thread(identity.probe,json.loads(owner))!={'state':'exited','reason':'process_exited'}:
            raise ExecutionConflict('early_finalization_busy','原收尾接续请求仍在执行或实际结束尚未核验')
    owner_json=json_text(await asyncio.to_thread(identity.observe,os.getpid()));identifier=str(uuid.uuid4())
    async def checked(db):return (await snapshot(db,job.id,user_id))[0]
    lease=operations.Lease(identifier,SimpleNamespace(id=case.id,s3_prefix=case.s3_prefix),owner_json,checked,'recovery');stage.lease=lease
    try:
        async with SessionLocal() as db:
            await lock_case_execution(db,case.id);current=await snapshot(db,job.id,user_id)
            if current[4][stages.KEY]!=stage.value:raise ExecutionConflict('state_conflict','登记前原收尾阶段发生变化')
            actual=list(await db.scalars(select(CaseStorageOperation).where(CaseStorageOperation.case_id==case.id,
                CaseStorageOperation.state.in_(('active','drained','exited')))))
            if {(r.id,r.owner_json,r.state) for r in actual}!={(r[0],r[1],r[2]) for r in candidates}:
                raise ExecutionConflict('early_finalization_busy','其他存储请求正在变更原收尾阶段')
            fence=await db.get(CaseValidationFence,case.id)
            if fence is not None and fence.status=='held':raise ExecutionConflict('case_validation_busy','算例正在校验')
            for row in actual:
                started=await io_intent(db,row,stage)
                if not await io_closed(db,row,stage):
                    value={'schema':IO_SCHEMA,'operation_id':row.id,'intent_sha256':stages.digest(started),
                        'owner_exit':{'state':'exited','reason':'process_exited'},'observer':json.loads(owner_json)}
                    await append_event(db,case.id,IO_EXITED,value,attempt_id=attempt.id,event_id=io_event(row.id,'exited'))
                row.state='settled';row.ended_at=row.ended_at or now()
            await db.flush()
            value={'schema':IO_SCHEMA,'operation_id':identifier,'stage_sha256':stage.sha256,
                'job_id':job.id,'attempt_id':attempt.id,'owner':json.loads(owner_json)}
            db.add(CaseStorageOperation(id=identifier,case_id=case.id,source_prefix=case.s3_prefix,purpose='recovery',state='active',
                owner_json=owner_json,journal_id=attempt.id,plan_sha256=stage.sha256,event_id=io_event(identifier,'started')))
            await append_event(db,case.id,IO_STARTED,value,attempt_id=attempt.id,event_id=io_event(identifier,'started'))
            await finish_before_cancel(db.commit())
    except BaseException:
        await finish_before_cancel(finish(stage));raise
    return stage


async def finish(stage):
    async with SessionLocal() as db:
        await lock_case_execution(db,stage.case_id);row=await db.get(CaseStorageOperation,stage.lease.id,populate_existing=True)
        if row is None:return
        started=await io_intent(db,row,stage)
        if await io_closed(db,row,stage):return
        if row.owner_json!=stage.lease.owner_json or row.state!='active':raise ValueError('Early IO ownership changed before actual return')
        value={'schema':IO_SCHEMA,'operation_id':row.id,'intent_sha256':stages.digest(started),'io_returned':True}
        await append_event(db,stage.case_id,IO_DRAINED,value,attempt_id=stage.owner.attempt_id,event_id=io_event(row.id,'drained'))
        row.state='settled';row.ended_at=now();await finish_before_cancel(db.commit())


@asynccontextmanager
async def hold(records,user_id):
    stage=None
    async def reserve():
        nonlocal stage
        stage=await acquire(records,user_id)
    try:
        await finish_before_cancel(reserve())
        with operations.owned_context(stage.lease):yield stage
    finally:
        if stage is not None:await finish_before_cancel(finish(stage))


async def plans(db,stage):
    rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==stage.owner.attempt_id,DurableEvent.type==stages.PLANNED)))
    result={}
    for event in rows:
        value=json.loads(event.payload_json);relative=value['relative'];workspace.canonical_path(relative)
        if (set(value)!={'schema','id','stage_sha256','relative','record','generator'}
                or event.case_id!=stage.case_id or value['schema']!=stages.PLAN or value['stage_sha256']!=stage.sha256
                or event.id!=value['id'] or event.id!=stages.event_id(stage.owner.attempt_id,'file:'+relative)
                or relative in result or value['generator'] not in {'original_native_log','solver_view_marker','solve_evidence_v1','original_finalizing_log'}
                or set(value['record'])!={'size','sha256'} or type(value['record']['size']) is not int
                or not 0<=value['record']['size']<=64*1024*1024+65536 or not re.fullmatch('[0-9a-f]{64}',value['record']['sha256'])):
            raise ValueError('Early file plan changed its original stage')
        result[relative]=value
    return result


def verify_run(stage,file_plans):
    run,files,directories=workspace.inspect_runtime(stage.prefix,stage.manifest,True)
    allowed=dict(stage.value['files']);allowed_dirs=set(stage.value['directories'])
    for relative,plan in file_plans.items():
        allowed_dirs.update('/'.join(relative.split('/')[:i]) for i in range(1,len(relative.split('/'))))
        if relative in files:allowed[relative]={**plan['record'],'executable':False}
    if files!=allowed or not set(stage.value['directories'])<=set(directories) or not set(directories)<=allowed_dirs:
        raise ExecutionConflict('finalizing_file_conflict','原执行输出或收尾文件发生未登记变化；保留现场，不重跑原程序')
    return run,files,directories


async def options(job_id,user_id):
    async with SessionLocal() as db:records=await snapshot(db,job_id,user_id)
    case,job,attempt,manifest,output,*_=records;stage=output[stages.KEY]
    completed=output.get('schema')=='vibeflow.attempt-outputs.v1'
    observed,_,_=await outputs.observe_original(records,user_id)
    return {'state':'captured' if completed else 'preparation_pending','job_id':job.id,'attempt_id':attempt.id,
        'stage_sha256':stages.digest(stage),'owner_observation':observed,'native_execution_started':False,
        'solver_execution_started':False,'finalization_pending':attempt.phase not in {'succeeded','failed','cancelled','interrupted'} or job_execution.completion_visibility(attempt) in {'pending','invalid'},'automatic_replay':False,
        **({'inventory_sha256':outputs.inventory_digest(output)} if completed else {})}


async def recover(job_id,user_id,attempt_id,stage_sha256):
    from .request_deadline import scope
    async with scope(300):
        try:await asyncio.wait_for(outputs.SLOTS.acquire(),.1)
        except TimeoutError as error:raise ExecutionConflict('early_finalization_busy','收尾准备或输出恢复请求已达到并发上限，请稍后重试') from error
        try:return await _recover(job_id,user_id,attempt_id,stage_sha256)
        finally:outputs.SLOTS.release()


async def _recover(job_id,user_id,attempt_id,stage_sha256):
    async with SessionLocal() as db:records=await snapshot(db,job_id,user_id)
    if records[2].id!=attempt_id or stages.digest(records[4][stages.KEY])!=stage_sha256:
        raise ExecutionConflict('state_conflict','请求对应的原收尾阶段已变化')
    if records[4].get('schema')=='vibeflow.attempt-outputs.v1':
        async with SessionLocal() as db:
            leftover=await db.scalar(select(CaseStorageOperation.id).where(CaseStorageOperation.case_id==records[0].id,
                CaseStorageOperation.journal_id==attempt_id,CaseStorageOperation.plan_sha256==stage_sha256,
                CaseStorageOperation.state.in_(('active','drained','exited'))).limit(1))
        if leftover is not None:
            async with hold(records,user_id):pass
        return await options(job_id,user_id)
    owner_event_id=await original_exit(records,user_id)
    async with hold(records,user_id) as stage:
        async with SessionLocal() as db:file_plans=await plans(db,stage)
        await finish_before_cancel(asyncio.to_thread(verify_run,stage,file_plans))
        return await capture(stage,records,user_id,owner_event_id)


async def capture(stage,records,user_id,owner_event_id):
    """Continue deterministic evidence and record a new recovery provenance."""
    from . import execution_budget,execution_storage
    budget=await execution_budget.load(stage.owner);budget.restore(stage.value['usage'])
    with budget.activate():
        async with execution_storage.recovery(budget,records[0],records[2]):
            return await _capture_owned(stage,records,user_id,owner_event_id,budget)


async def _capture_owned(stage,records,user_id,owner_event_id,budget):
    from . import job_finalizing_source as source
    run=workspace.checked_path(storage._fs_path(stage.prefix,'.'),stage.manifest['run_path'])
    path=workspace.checked_path(storage._fs_path(stage.prefix,'.'),stage.value['source_log_key'])
    original_result=records[4].get(stages.RESULT)
    completion=dict(original_result['completion'] if original_result is not None else stage.value['completion'])
    expected_log=original_result.get('log',stage.value['log']) if original_result is not None else stage.value['log']
    data=await finish_before_cancel(asyncio.to_thread(stages.capture_log_source,path,budget.max_log_bytes+65536,expected_log))
    with budget.activate():
        if original_result is None and completion['status']=='succeeded' and stage.value['evidence_input'] is not None:
            proc=SimpleNamespace(cwd=run,log_path=path,job_id=stage.owner.job_id)
            try:await stages.write_evidence(proc,stage)
            except ValueError:completion.update(status='failed',exit_code=-1,reason='evidence_invalid')
        log=await stages.write_file(stage,source.LOG,data,generator='original_finalizing_log')
        async with SessionLocal() as db:file_plans=await plans(db,stage)
        _,files,directories=await finish_before_cancel(asyncio.to_thread(verify_run,stage,file_plans))
        if budget.storage_watch is not None:await budget.storage_watch.sample()
        async with SessionLocal() as db:
            await lock_case_execution(db,stage.case_id);case,job,attempt=await stages.require_authority(db,stage)
            await outputs.drain.history(db,case,job,attempt,pending=True)
            output={'schema':'vibeflow.attempt-outputs.v1','run_path':stage.manifest['run_path'],'files':files,'directories':directories,
                'input_attempt_id':attempt.id,stages.KEY:stage.value}
            if original_result is not None:output[stages.RESULT]=original_result
            if stage.manifest.get('publications'):
                output['publications']=stage.manifest['publications'];output['dependency_error']=None
            if storage._use_s3():
                from .attempt_objects import binding
                output['object_store']={'state':'pending','storage':binding(stage.prefix)}
            else:output['local_store']={'schema':SCHEMA,'stage_sha256':stage.sha256}
            identifier=stages.event_id(attempt.id,'captured')
            provenance={'schema':SCHEMA,'stage_sha256':stage.sha256,'capture_event_id':identifier,
                'owner_exit_event_id':owner_event_id,'io_operation_id':stage.lease.id}
            output['early_recovery']=provenance
            prepared=outputs.proof(case,job,attempt,output,completion['status'],completion['exit_code'],stage.value['stop'],budget.usage(),schema=outputs.RECOVERED_SCHEMA)
            output[outputs.KEY]=prepared
            value={'schema':source.RECOVERED_SCHEMA,'job_id':job.id,'attempt_id':attempt.id,'input_sha256':outputs.digest(attempt.input_manifest_json),
                'worker_sha256':outputs.digest(attempt.worker_identity_json),**completion,'log_path':source.LOG,
                'source_log_key':stage.value['source_log_key'],'log':log,'pre_capture_stages_returned':False,'recovered':provenance}
            output[source.KEY]=value
            attempt.output_manifest_json=json_text(output);attempt.version+=1
            payload={'schema':SCHEMA,'stage_sha256':stage.sha256,'preparation_sha256':stages.digest(prepared),
                'source_sha256':stages.digest(value),'inventory_sha256':outputs.inventory_digest(output),'provenance':provenance}
            await append_event(db,case.id,CAPTURED,payload,attempt_id=attempt.id,event_id=identifier)
            await append_event(db,case.id,source.RECOVERED_EVENT,value,attempt_id=attempt.id,event_id=stages.event_id(attempt.id,'source_recovered'))
            await finish_before_cancel(db.commit())
    return await options(stage.owner.job_id,user_id)


async def verify_capture(db,case,job,attempt,output):
    from .job_finalizing_source import validate as validate_source
    value=await stages.read(db,case,job,attempt)
    if value is None or not value['supported']:raise ValueError('Recovered preparation lost its original supported stage')
    prepared=output[outputs.KEY];source=output['finalizing_source'];provenance=output.get('early_recovery')
    validate_source(case,job,attempt,output,source)
    fields={'schema','stage_sha256','capture_event_id','owner_exit_event_id','io_operation_id'}
    identifier=stages.event_id(attempt.id,'captured')
    if (not isinstance(provenance,dict) or set(provenance)!=fields or provenance['schema']!=SCHEMA
            or provenance['stage_sha256']!=stages.digest(value) or provenance['capture_event_id']!=identifier
            or prepared['stage_sha256']!=provenance['stage_sha256'] or source.get('recovered')!=provenance):
        raise ValueError('Recovered capture changed its exact original stage')
    event=await db.get(DurableEvent,identifier)
    expected={'schema':SCHEMA,'stage_sha256':stages.digest(value),'preparation_sha256':stages.digest(prepared),
        'source_sha256':stages.digest(source),'inventory_sha256':outputs.inventory_digest(output),'provenance':provenance}
    if event is None or event.type!=CAPTURED or event.case_id!=case.id or event.attempt_id!=attempt.id or json.loads(event.payload_json)!=expected:
        raise ValueError('Recovered output has no exact capture history')
    owner=job_worker_identity.document(job,attempt)
    original=await outputs.read_owner_exit(db,(case,job,attempt,None,output,owner,None))
    if original is None or original.id!=provenance['owner_exit_event_id']:raise ValueError('Recovered capture lost original worker exit')
    claim=job_execution.Claim(job.id,attempt.id,attempt.root_job_id,attempt.number,attempt.claim_token)
    stage=stages.Stage(claim,case.id,case.s3_prefix,json.loads(attempt.input_manifest_json)['workspace'],value)
    row=await db.get(CaseStorageOperation,provenance['io_operation_id'])
    if row is None:raise ValueError('Recovered capture lost its independent IO ownership')
    await io_intent(db,row,stage)
    if any(prepared['output_usage'][k]!=value['usage'][k] for k in value['usage'] if k not in {'violation','violation_source'}):
        raise ValueError('Recovered capture reset original output credit')
    original_result=await stages.read_result(db,case,job,attempt,value)
    completion=original_result['completion'] if original_result is not None else value['completion']
    current={k:source[k] for k in ('status','exit_code','reason')}
    if current!=completion and not (original_result is None and completion['status']=='succeeded' and value['evidence_input'] is not None
            and current=={'status':'failed','exit_code':-1,'reason':'evidence_invalid'}):
        raise ValueError('Recovered capture changed the original outcome without a deterministic evidence failure')
    return event
