"""Explicit legacy-v1 migration to a fresh registered graph/current inputs.

Historical plans, steps and attempts are evidence, never rewritten or replayed.
Only the known old fixed graph is migrated; damaged/custom graphs need review.
"""
import hashlib,json,re,uuid
from sqlalchemy import select
from ..models import Case,CaseEventCounter,Job,JobAttempt,WorkflowRun,WorkflowStep
from . import workflow_scheduler as scheduler
from .durable_events import append_event,json_text,lock_case_execution
from .workflow_state import ExecutionConflict,TERMINAL

SCHEMA='vibeflow.workflow-plan.v4'
FIELDS={'source_run_id','source_run_version','source_plan_sha256','source_input_sha256',
 'source_steps_sha256','observed_revision','observed_event_sequence'}

def sha(text):return hashlib.sha256(text.encode('utf8')).hexdigest()

def make_plan(target,parameters,lineage):
 if not isinstance(lineage,dict) or set(lineage)!=FIELDS:raise ValueError('Invalid legacy migration lineage')
 identifier=lineage['source_run_id']
 if not isinstance(identifier,str) or str(uuid.UUID(identifier))!=identifier:raise ValueError('Invalid legacy workflow identity')
 for key in ['source_run_version','observed_revision','observed_event_sequence']:
  if type(lineage[key]) is not int or lineage[key]<0:raise ValueError('Invalid migration observation')
 for key in ['source_plan_sha256','source_input_sha256','source_steps_sha256']:
  if not isinstance(lineage[key],str) or not re.fullmatch('[0-9a-f]{64}',lineage[key]):raise ValueError('Invalid migration digest')
 plan=scheduler.make_plan(target,'A',scheduler.normalized_parameters(parameters))
 return dict(plan,schema=SCHEMA,legacy_migration=dict(lineage))

async def legacy_snapshot(db,source,*,ready):
 try:
  plan=json.loads(source.plan_json)
  if not isinstance(plan,dict) or plan.get('schema')!='vibeflow.workflow-plan.v1':raise ValueError('Unknown legacy plan')
  expected=scheduler.make_plan(source.target,plan['start_state'])
  if plan!=expected:raise ValueError('Legacy graph differs from its known definition')
 except (ValueError,KeyError,TypeError) as error:
  raise ExecutionConflict('recovery_required','旧计划不是可核验的v1固定图，请保留历史并人工核对') from error
 steps=list(await db.scalars(select(WorkflowStep).where(WorkflowStep.run_id==source.id)
  .order_by(WorkflowStep.step_key).execution_options(populate_existing=True)))
 keyed={step.step_key:step for step in steps}
 if set(keyed)!={item['key'] for item in expected['steps']}:raise ExecutionConflict('evidence_invalid','旧工作流步骤记录不完整')
 records=[]
 for item in expected['steps']:
  step=keyed[item['key']]
  try:dependencies=json.loads(step.dependencies_json)
  except (ValueError,TypeError) as error:
   raise ExecutionConflict('evidence_invalid','旧步骤依赖记录损坏') from error
  if (step.handler!=item['handler'] or step.handler_version!=item['version'] or
    step.confirmation_state!=item['confirmation_state'] or dependencies!=item['dependencies']):
   raise ExecutionConflict('evidence_invalid','旧工作流步骤与原计划归属不一致')
  if ready and step.status not in TERMINAL|{'blocked'}:
   raise ExecutionConflict('state_conflict','请先结清旧工作流的所有步骤')
  if step.active_attempt_id is not None:
   attempt=await db.get(JobAttempt,step.active_attempt_id,populate_existing=True)
   job=await db.get(Job,attempt.job_id,populate_existing=True) if attempt else None
   if attempt is None or attempt.step_id!=step.id or job is None or job.case_id!=source.case_id:
    raise ExecutionConflict('evidence_invalid','旧步骤的活动尝试不属于本工作流算例')
  records.append({key:getattr(step,key) for key in ['id','step_key','handler','handler_version','version','status',
   'active_attempt_id','input_manifest_json','output_manifest_json','confirmation_state','confirmation_revision','error_code','detail']})
 if ready and source.status not in TERMINAL:raise ExecutionConflict('state_conflict','迁移前请先完成或取消旧工作流')
 return {'source_run_id':source.id,'source_run_version':source.version,'source_plan_sha256':sha(source.plan_json),
  'source_input_sha256':sha(source.input_manifest_json),'source_steps_sha256':sha(json_text(records))}

async def require_binding(db,run,lineage):
 source=await db.get(WorkflowRun,lineage['source_run_id'],populate_existing=True)
 if source is None or source.id==run.id or source.case_id!=run.case_id:
  raise ExecutionConflict('evidence_invalid','迁移来源工作流归属不一致')
 snapshot=await legacy_snapshot(db,source,ready=True)
 if any(snapshot[key]!=lineage[key] for key in snapshot):
  raise ExecutionConflict('input_stale','迁移来源版本或历史记录已变化，请保留原流程并重新核对')

async def preview(db,run_id,user):
 from .workflow_execution import owned
 from .workflow_recompute import checked_snapshot
 from .step_handlers import CONTRACT
 source=await owned(db,run_id,user,write=False)
 try:plan=json.loads(source.plan_json)
 except (ValueError,TypeError) as error:raise ExecutionConflict('recovery_required','旧工作流计划损坏，无法自动迁移') from error
 if isinstance(plan,dict) and plan.get('execution_contract')==CONTRACT:
  return {'required':False,'source_run_id':source.id,'reason':'already_registered'}
 await lock_case_execution(db,source.case_id)
 source=await db.get(WorkflowRun,run_id,populate_existing=True)
 case=await db.get(Case,source.case_id,populate_existing=True)
 snapshot=await legacy_snapshot(db,source,ready=True);checked_snapshot(case)
 counter=await db.get(CaseEventCounter,case.id,populate_existing=True)
 return dict(snapshot,required=True,current_revision=case.dict_revision,
  current_event_sequence=counter.next_seq if counter else 1,current_stage=case.workflow_state,
  original_target=source.target,original_run_dir=source.run_dir,
  input_policy='explicit_current_inputs_rebuild_all',requires_reset_confirmation=True)

async def source_drained(db,case,source):
 from .job_delivery import occupying
 for step in await db.scalars(select(WorkflowStep).where(WorkflowStep.run_id==source.id)):
  if step.active_attempt_id is None:continue
  attempt=await db.get(JobAttempt,step.active_attempt_id,populate_existing=True)
  job=await db.get(Job,attempt.job_id,populate_existing=True) if attempt else None
  if attempt is None or attempt.step_id!=step.id or job is None or job.case_id!=case.id:
   raise ExecutionConflict('evidence_invalid','旧步骤的当前尝试归属无法核验，请保留原流程人工核查')
 if await db.scalar(select(Job.id).where(Job.case_id==case.id,Job.status.in_(['queued','running'])).limit(1)) is not None:
  raise ExecutionConflict('state_conflict','算例仍有排队或运行中的作业，不能重置工程阶段')
 rows=await db.execute(select(Job,JobAttempt).join(JobAttempt,JobAttempt.job_id==Job.id)
  .join(WorkflowStep,WorkflowStep.id==JobAttempt.step_id).where(WorkflowStep.run_id==source.id))
 for job,attempt in rows:
  if job.case_id!=case.id or job.status not in TERMINAL or attempt.phase not in TERMINAL or occupying(case,job,attempt):
   raise ExecutionConflict('recovery_required','旧尝试的归属、停止或写入排空尚未核验')
  if attempt.claim_token is None:
   try:
    unknown=any(json.loads(value)!={} for value in (attempt.worker_identity_json,attempt.process_identity_json,
     attempt.child_processes_json,attempt.write_checkpoint_json,attempt.output_manifest_json,attempt.publication_json))
   except (ValueError,TypeError):unknown=True
   if unknown or any(value is not None for value in (attempt.worker_id,attempt.started_at,attempt.heartbeat_at,attempt.lease_expires_at)):
    raise ExecutionConflict('recovery_required','未领取的旧尝试包含未知执行或写入记录，请保留现场核查')

async def start(db,run_id,user,*,key,expected_version,expected_revision,expected_event_sequence,
 expected_source_plan_sha256,confirmed_reset,target,run_dir,parameters,budget,request=None):
 from ..models import User
 from . import workflow_execution as execution,job_execution,workflow_reset
 from .workflow_recompute import replay,checked_snapshot
 from .case_evidence import safe_run_dir
 from .case_provision import io
 if key is None or confirmed_reset is not True:
  raise ExecutionConflict('confirmation_required','迁移需要明确确认A回退及幂等键')
 job_execution.validate_key(key);parameters=scheduler.normalized_parameters(parameters);run_dir=safe_run_dir(run_dir)
 if (set(budget)!={'timeout_seconds','max_log_bytes'} or isinstance(budget['timeout_seconds'],bool) or
  not 0<budget['timeout_seconds']<=86400 or type(budget['max_log_bytes']) is not int or not 1024<=budget['max_log_bytes']<=64*1024*1024):
  raise ExecutionConflict('invalid_request','迁移预算不符合注册执行策略')
 scheduler.make_plan(target,'A',parameters)
 source=await execution.owned(db,run_id,user);case_id,user_id=source.case_id,user.id
 payload={'operation':'workflow.migrate','source_run_id':run_id,'expected_version':expected_version,
  'expected_revision':expected_revision,'expected_event_sequence':expected_event_sequence,
  'expected_source_plan_sha256':expected_source_plan_sha256,'confirmed_reset':True,'target':target,
  'run_dir':run_dir,'parameters':parameters,'budget':budget}
 digest=job_execution.request_hash(payload)
 existing=await replay(db,case_id,user_id,key,digest)
 if existing is not None:return await execution.detail(db,existing.id,user)

 async def observe(session,current):
  source=await session.get(WorkflowRun,run_id,populate_existing=True)
  if source is None or source.case_id!=current.id:
   raise ExecutionConflict('state_conflict','旧流程来源已变化，请重新查看迁移方案')
  observed=await legacy_snapshot(session,source,ready=True)
  if source.version!=expected_version or observed['source_plan_sha256']!=expected_source_plan_sha256:
   raise ExecutionConflict('state_conflict','旧计划或版本已变化，请重新查看迁移方案')
  await workflow_reset.require_sequence(session,current.id,expected_event_sequence,'当前算例事件已变化，请重新确认')
  if await session.scalar(select(WorkflowRun.id).where(WorkflowRun.case_id==current.id,
      WorkflowRun.status.not_in(TERMINAL)).limit(1)) is not None:
   raise ExecutionConflict('state_conflict','算例还有活动工作流，不能重置工程阶段')
  await source_drained(session,current,source)
  return observed

 case,repeated=await workflow_reset.prepare_case(db,case_id,user_id,key,digest)
 if repeated is not None:return repeated
 await lock_case_execution(db,case_id)
 await db.refresh(case)
 existing=await replay(db,case_id,user_id,key,digest)
 if existing is not None:
  identifier=existing.id
  await db.commit()
  actor=await db.get(User,user_id,populate_existing=True)
  return await execution.detail(db,identifier,actor)
 if case.dict_revision!=expected_revision:
  raise ExecutionConflict('revision_conflict','算例修订已变化，请重新查看迁移方案')
 observed=await observe(db,case)
 await db.commit()
 snapshot=await io(checked_snapshot,case)
 lineage=dict(observed,observed_revision=expected_revision,observed_event_sequence=expected_event_sequence)
 plan=make_plan(target,parameters,lineage)

 async def validate(session,current,_draft):
  if await observe(session,current)!=observed:
   raise ExecutionConflict('input_stale','迁移来源记录已变化，请重新查看迁移方案')

 async def create(session,current,draft):
  run,reset=await workflow_reset.create_run(session,current,user_id,target=target,run_dir=run_dir,
   key=key,digest=digest,budget=budget,plan=plan,draft=draft,reset_name='legacy_migration_reset')
  await append_event(session,current.id,'workflow.legacy_migrated',{'source_run_id':run_id,'source_version':expected_version,
   'source_plan_sha256':lineage['source_plan_sha256'],'input_policy':'current_inputs_rebuild_all','reset':reset},run_id=run.id)
  await append_event(session,current.id,'workflow.created',{'status':'queued','target':target,'version':0,
   'step_count':len(plan['steps']),'source_run_id':run_id,'migration':True},run_id=run.id)
  return run.id

 return await workflow_reset.publish(db,case,user_id,key=key,digest=digest,revision=expected_revision,
  expected_sha256=snapshot['case_dict_sha256'],validate=validate,create=create,request=request)
