"""Durable groups of registered workflows, with atomic membership and control.

Independent cases retain their original professional plans and confirmations.
The HTTP entry is wired only with claim-time capacity and durable cancellation.
"""
import json,re,uuid
from collections import Counter

from fastapi import HTTPException
from sqlalchemy import select,update

from ..models import Case,DurableEvent,JobAttempt,User,WorkflowBatch,WorkflowBatchMember,WorkflowRun,WorkflowStep,Workspace
from . import job_execution,workflow_scheduler as scheduler
from .async_ops import finish_before_cancel
from .case_evidence import safe_run_dir
from .durable_events import append_event,ensure_parent_transaction,json_text,lock_case_execution,now
from .workflow_state import ExecutionConflict,TERMINAL

SCHEMA='vibeflow.workflow-batch.v1'
MEMBERS='vibeflow.workflow-batch-members.v1'


def normalize(members,max_concurrent):
    if (type(max_concurrent) is not int or not 1<=max_concurrent<=4 or
            not isinstance(members,list) or not 1<=len(members)<=50):
        raise ExecutionConflict('invalid_request','A workflow batch requires 1-50 members and concurrency 1-4')
    normalized=[];keys=set();cases=set()
    for member in members:
        if not isinstance(member,dict) or set(member)-{'key','case_id','target','expected_revision','run_dir','parameters','budget'}:
            raise ExecutionConflict('invalid_request','Unsupported workflow batch member')
        key=member.get('key');cid=member.get('case_id');revision=member.get('expected_revision')
        if (not isinstance(key,str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9._-]{0,79}',key) or
                type(cid) is not int or cid<=0 or type(revision) is not int or revision<0 or key in keys):
            raise ExecutionConflict('invalid_request','Batch members require unique keys and valid case revisions')
        if cid in cases:
            raise ExecutionConflict('invalid_request','Each member requires an independent case for its engineering confirmations and results')
        target=member.get('target','geometry')
        if not isinstance(target,str) or target not in scheduler.TARGET_END:
            raise ExecutionConflict('invalid_request','Unsupported batch workflow target')
        budget=member.get('budget',{})
        if not isinstance(budget,dict) or set(budget)-{'timeout_seconds','max_log_bytes'}:
            raise ExecutionConflict('invalid_request','Unsupported batch resource budget')
        seconds=budget.get('timeout_seconds',86400);log_bytes=budget.get('max_log_bytes',16777216)
        if type(seconds) not in {int,float} or not 0<seconds<=86400 or type(log_bytes) is not int or not 1024<=log_bytes<=67108864:
            raise ExecutionConflict('invalid_request','Invalid batch resource budget')
        normalized.append({'key':key,'case_id':cid,'target':target,'expected_revision':revision,
            'run_dir':safe_run_dir(member.get('run_dir','C-run/case-1')),
            'parameters':scheduler.normalized_parameters(member.get('parameters',{})),
            'budget':{'timeout_seconds':seconds,'max_log_bytes':log_bytes}})
        keys.add(key);cases.add(cid)
    return {'schema':SCHEMA,'max_concurrent':max_concurrent,'members':normalized}


async def roster(db,batch):
    """Check the exact immutable SQL membership against its committed binding."""
    try:
        request=json.loads(batch.request_json);frozen=json.loads(batch.membership_json)
        rows=list(await db.scalars(select(WorkflowBatchMember).where(WorkflowBatchMember.batch_id==batch.id)
            .order_by(WorkflowBatchMember.ordinal).execution_options(populate_existing=True)))
        if (job_execution.request_hash(request)!=batch.request_hash or request['schema']!=SCHEMA or
                job_execution.request_hash(frozen)!=batch.membership_sha256 or frozen['schema']!=MEMBERS or
                len(rows)!=len(request['members']) or not 1<=len(rows)<=50 or len(frozen['members'])!=len(rows) or
                batch.max_concurrent!=request['max_concurrent'] or batch.delivery_mode not in {'local','redis'}):
            raise ValueError('Batch binding changed')
        for ordinal,(row,item,original) in enumerate(zip(rows,frozen['members'],request['members'])):
            observed={'ordinal':row.ordinal,'key':row.member_key,'case_id':row.case_id,'run_id':row.run_id,'request_hash':row.request_hash}
            if (observed!={k:item[k] for k in observed} or row.ordinal!=ordinal or
                    row.case_id!=original['case_id'] or row.member_key!=original['key']):
                raise ValueError('Batch membership changed')
        return rows,frozen['members']
    except (ValueError,TypeError,KeyError,AttributeError) as error:
        raise ExecutionConflict('evidence_invalid','Workflow batch membership is damaged') from error


async def binding(db,run_id):
    member=await db.get(WorkflowBatchMember,run_id,populate_existing=True)
    if member is None:
        original=await db.scalar(select(DurableEvent.id).where(DurableEvent.run_id==run_id,
            DurableEvent.type=='workflow.batch_member_created').limit(1))
        if original is not None:raise ExecutionConflict('evidence_invalid','Workflow batch member record is missing')
        return None
    batch=await db.get(WorkflowBatch,member.batch_id,populate_existing=True)
    if batch is None:raise ExecutionConflict('evidence_invalid','Workflow batch parent is missing')
    rows,frozen=await roster(db,batch)
    run=await db.get(WorkflowRun,run_id,populate_existing=True)
    record=next((item for item in frozen if item['run_id']==run_id),None)
    try:input_digest=job_execution.request_hash(json.loads(run.input_manifest_json)) if run is not None else None
    except (ValueError,TypeError) as error:
        raise ExecutionConflict('evidence_invalid','Workflow batch member input is damaged') from error
    if (run is None or record is None or run.case_id!=member.case_id or run.created_by!=batch.created_by or
            run.request_hash!=member.request_hash or input_digest!=record['input_sha256']):
        raise ExecutionConflict('evidence_invalid','Workflow batch member no longer matches its admitted run')
    return batch,member,rows


async def owned(db,batch_id,user,*,write=False):
    from ..routers.workspaces import get_owned_case
    batch=await db.get(WorkflowBatch,batch_id,populate_existing=True)
    if batch is None or batch.created_by!=user.id:raise HTTPException(404,'工作流批次不存在')
    rows,_=await roster(db,batch)
    for row in rows:await get_owned_case(row.case_id,user,db,write=write)
    return batch,rows


def status(batch,statuses):
    if batch.cancel_requested_at is not None:return 'cancelled' if set(statuses)<=TERMINAL else 'cancelling'
    if set(statuses)<=TERMINAL:
        for selected in ['interrupted','failed','cancelled']:
            if selected in statuses:return selected
        return 'succeeded'
    if set(statuses)<={'awaiting_confirmation',*TERMINAL}:return 'awaiting_confirmation'
    return 'queued' if set(statuses)=={'queued'} else 'running'


async def detail(db,batch_id,user):
    from .workflow_execution import run_summary
    batch,rows=await owned(db,batch_id,user);members=[]
    for row in rows:
        await binding(db,row.run_id)
        run=await db.get(WorkflowRun,row.run_id,populate_existing=True)
        members.append({'key':row.member_key,'ordinal':row.ordinal,**run_summary(run),
            'parameters':json.loads(run.plan_json).get('parameters',{})})
    states=[r['status'] for r in members]
    response={'schema':SCHEMA,'id':batch.id,'version':batch.version,'max_concurrent':batch.max_concurrent,
        'status':status(batch,states),'counts':dict(Counter(states)),'members':members,
        'cancel_requested_at':batch.cancel_requested_at.isoformat() if batch.cancel_requested_at else None,
        'created_at':batch.created_at.isoformat()}
    study=json.loads(batch.request_json).get('study')
    if study is not None:
        response['study']=study
    return response


async def start(db,*,user,members,max_concurrent,key,study=None):
    from ..routers.workspaces import get_owned_case
    from .distributed_queue import distributed_enabled
    job_execution.validate_key(key)
    if key is None:raise ExecutionConflict('invalid_request','Workflow batches require an idempotency key')
    request=normalize(members,max_concurrent)
    if study is not None:
        from .workflow_studies import bind_parameter, normalize as normalize_study

        study=normalize_study(study,request['members'])
        request['study']=study
    digest=job_execution.request_hash(request)
    async def previous():
        result=await db.scalar(select(WorkflowBatch).where(WorkflowBatch.created_by==user.id,
            WorkflowBatch.idempotency_key==key).execution_options(populate_existing=True))
        if result is not None and result.request_hash!=digest:
            raise ExecutionConflict('idempotency_conflict','Batch request key has a different payload')
        return result
    existing=await previous()
    if existing is not None:
        await owned(db,existing.id,user,write=True)
        return await detail(db,existing.id,user)
    from .storage_admission import sqlite_write_intent
    await sqlite_write_intent(db)
    async with db.begin_nested():
        owners=set()
        # Authorization precedes counter creation, including unknown case IDs.
        for item in request['members']:await get_owned_case(item['case_id'],user,db)
        for cid in sorted(item['case_id'] for item in request['members']):
            await lock_case_execution(db,cid)
            case=await get_owned_case(cid,user,db)
            await db.refresh(case)
            workspace=await db.get(Workspace,case.workspace_id,populate_existing=True)
            await get_owned_case(cid,user,db)
            owners.add(workspace.user_id)
        if len(owners)!=1:raise ExecutionConflict('invalid_request','Batch members must have the same billing owner')
        owner_id=owners.pop()
        # Match native admission's case -> billing owner order; do not take a
        # user lock before acquiring the complete ordered member case set.
        for identity in sorted({owner_id,user.id}):
            await db.execute(update(User).where(User.id==identity).values(id=User.id))
        batch=await previous()
        if batch is None:
            batch=WorkflowBatch(id=str(uuid.uuid4()),created_by=user.id,owner_id=owner_id,idempotency_key=key,
                request_hash=digest,request_json=json_text(request),membership_json='{}',membership_sha256='0'*64,
                max_concurrent=max_concurrent,delivery_mode='redis' if distributed_enabled() else 'local')
            db.add(batch);await db.flush();frozen=[]
            for ordinal,item in enumerate(request['members']):
                run,created=await scheduler.create_run(db,case_id=item['case_id'],user=user,target=item['target'],
                    run_dir=item['run_dir'],expected_revision=item['expected_revision'],parameters=item['parameters'],
                    idempotency_key='batch.'+batch.id+'.'+str(ordinal),**item['budget'])
                if not created:raise ExecutionConflict('evidence_invalid','New batch reused an unexpected run identity')
                db.add(WorkflowBatchMember(run_id=run.id,batch_id=batch.id,case_id=run.case_id,
                    ordinal=ordinal,member_key=item['key'],request_hash=run.request_hash))
                admitted={'ordinal':ordinal,'key':item['key'],'case_id':run.case_id,'run_id':run.id,
                    'request_hash':run.request_hash,'input_sha256':job_execution.request_hash(json.loads(run.input_manifest_json))}
                if study is not None:
                    if not any(step['key']=='postprocess' for step in json.loads(run.plan_json)['steps']):
                        raise ExecutionConflict('invalid_request','Study workflows must produce their own postprocess publication')
                    study_case=await get_owned_case(run.case_id,user,db)
                    admitted['parameter_value']=bind_parameter(study_case,run,study)
                frozen.append(admitted)
                await append_event(db,run.case_id,'workflow.batch_member_created',{'schema':SCHEMA,'batch_id':batch.id,
                    'member_key':item['key'],'ordinal':ordinal,'max_concurrent':max_concurrent},run_id=run.id)
            membership={'schema':MEMBERS,'members':frozen}
            batch.membership_json=json_text(membership);batch.membership_sha256=job_execution.request_hash(membership)
            await db.flush();await roster(db,batch)
    await finish_before_cancel(db.commit())
    # The existing persisted workflow scanner dispatches only after all member
    # runs and their parent binding commit together. No private in-memory queue.
    return await detail(db,batch.id,user)


async def require_open(db,run):
    selected=await binding(db,run.id)
    if selected is not None and selected[0].cancel_requested_at is not None:
        raise ExecutionConflict('cancelled','Workflow batch cancellation is already durable')


async def delivery_mode(db,attempt):
    from .distributed_queue import distributed_enabled
    default='redis' if distributed_enabled() else 'local'
    if attempt.step_id is None:return default
    step=await db.get(WorkflowStep,attempt.step_id)
    if step is None:raise ExecutionConflict('evidence_invalid','Workflow batch attempt lost its step')
    selected=await binding(db,step.run_id)
    return selected[0].delivery_mode if selected is not None else default


async def require_capacity(db,case,job,attempt,used):
    from .job_delivery import DeliveryDeferred
    if attempt.step_id is None:return
    step=await db.get(WorkflowStep,attempt.step_id)
    if step is None:raise ExecutionConflict('evidence_invalid','Typed attempt lost its workflow step')
    selected=await binding(db,step.run_id)
    if selected is None:return
    batch,_,rows=selected
    if batch.cancel_requested_at is not None:raise DeliveryDeferred('Workflow batch cancellation is pending')
    workspace=await db.get(Workspace,case.workspace_id)
    if workspace.user_id!=batch.owner_id:raise ExecutionConflict('evidence_invalid','Workflow batch billing owner changed')
    case_ids={row.case_id for row in rows}
    owned_cases=await db.scalars(select(Case.id).join(Workspace,Case.workspace_id==Workspace.id)
        .where(Case.id.in_(case_ids),Workspace.user_id==workspace.user_id))
    if set(owned_cases)!=case_ids:
        raise ExecutionConflict('evidence_invalid','Workflow batch member billing ownership changed')
    run_ids={row.run_id for row in rows}
    steps=list(await db.scalars(select(WorkflowStep).where(WorkflowStep.run_id.in_(run_ids))))
    member_steps={row.id for row in steps}
    attempts=list(await db.scalars(select(JobAttempt).where(JobAttempt.step_id.in_(member_steps))))
    member_jobs={row.job_id for row in attempts}
    if job.id not in member_jobs:raise ExecutionConflict('evidence_invalid','Job is outside its workflow batch')
    if len(used&member_jobs)>=batch.max_concurrent:
        raise DeliveryDeferred('Workflow batch concurrency capacity is occupied')


async def cancel(db,batch_id,user,*,expected_version):
    from . import jobrunner
    batch,rows=await owned(db,batch_id,user,write=True)
    await ensure_parent_transaction(db);identifiers=set()
    async with db.begin_nested():
        for cid in sorted(row.case_id for row in rows):await lock_case_execution(db,cid)
        batch,rows=await owned(db,batch_id,user,write=True)
        runs=[await db.get(WorkflowRun,row.run_id,populate_existing=True) for row in rows]
        if batch.cancel_requested_at is None and any(run.status not in TERMINAL for run in runs):
            if batch.version!=expected_version:raise ExecutionConflict('state_conflict','Workflow batch version changed')
            batch.cancel_requested_at=now();batch.version+=1
            for run in runs:
                if run.status in TERMINAL:continue
                steps=await scheduler.request_cancel(db,run.id,expected_version=run.version)
                for step in steps:
                    if step.active_attempt_id:
                        attempt=await db.get(JobAttempt,step.active_attempt_id,populate_existing=True)
                        if attempt and attempt.phase not in TERMINAL:identifiers.add(attempt.job_id)
                await append_event(db,run.case_id,'workflow.batch_cancel_requested',{'schema':SCHEMA,
                    'batch_id':batch.id,'version':batch.version},run_id=run.id)
            from . import audit
            await audit.user_event(db, user, 'workflow.batch.cancel.requested', 'workflow_batch', batch.id,
                                   {'count': len(runs)})
        await db.flush()
    await finish_before_cancel(db.commit())
    for job_id in identifiers:
        proc=jobrunner.get_proc(job_id)
        if proc is not None:proc.cancel()
    # Redis workers observe the same committed cancellation through their
    # normal ownership heartbeat. Control does not wait for a Redis connection.
    return await detail(db,batch_id,user)
