import { ApiError } from '../api';
import type { WorkflowRunDetail } from './workflowRuns';

export const STAGES = [{k:'A',label:'几何'}, {k:'B',label:'网格'}, {k:'C',label:'求解'}, {k:'D',label:'后处理'}, {k:'E',label:'报告'}, {k:'V',label:'开发'}];
export const CORE_KINDS = ['geometry','mesh','solve','postprocess','report'];
export const dependencyLabel = (value:string) => ({geometry:'几何',mesh:'网格',solve:'求解',postprocess:'后处理',report:'报告'})[value] ?? value;
export const freshnessLabel = (value:string) => ({fresh:'当前有效',stale:'输入或依赖已过期',missing:'尚无正式发布',unverifiable:'无法核验',historical_available:'历史版本可读取'})[value] ?? '状态需要核查';
export interface DependencySnapshot {
  case_id:number; dict_revision:number; event_sequence:number | null; run_dir:string | null; advisory:true;
  channels:{kind:string;publication_id:string | null;status:string}[];
  publications:{publication_id:string;kind:string;generation:number;status:string;run_dir?:string;
    reasons:{code:string;fields?:string[];publication_id?:string}[];
    dependencies:{publication_id:string;require_current:boolean;status:string}[]}[];
  recompute:string[];needs_inspection:string[];
}
export const dependencyReason = (code:string) => ({
  source_changed:'生成此产物的来源已经变化，需要重新计算。',not_current:'此版本已被替代，保留为历史记录。',
  run_directory_changed:'当前选择的运行目录与此产物不同。',dependency_stale:'要求当前有效的上游产物已过期。',
  dependency_unverifiable:'上游产物无法核验，请保留记录并检查原作业。',publication_not_verified:'没有完整的成功作业与正式发布绑定。',
  bundle_unverifiable:'不可变产物文件或清单无法核验，请保留现有版本。',source_unverifiable:'来源无法核验，请检查输入、处理器与原作业。',
})[code] ?? '此产物存在尚待核查的依赖或来源，请查看原始诊断。';
export function caseWorkflowError(error:unknown):string {
  if (error instanceof ApiError) {
    if ([401,403,404].includes(error.status)) return '当前账户无法读取或操作此算例，请核对登录和权限。';
    const known:Record<string,string>={revision_conflict:'输入修订已变化，原草稿保留。请重新读取并核对后再确认。',
      state_conflict:'流程或步骤已变化，请读取当前状态后重新核对。',input_stale:'输入或选定产物已经过期，请核对原因和重算范围。',
      dependency_changed:'检查期间输入或产物发生变化，请刷新证据。',evidence_invalid:'当前执行或产物证据未通过核验，请查看原始诊断和恢复入口。'};
    return known[error.code ?? ''] ?? error.message;
  }
  return error instanceof TypeError ? '无法连接服务，请刷新读取状态。' : error instanceof Error ? error.message : '结果无法核验，请保留原记录。';
}
export interface StageRequest {
  schema:1;caseId:number;userId:number;runId:string | null;
  body:{state:string;confirmed:true;expected_revision:number;workflow_step_id?:string;expected_step_version?:number};
}
export const stageRequestKey=(userId:number,caseId:number)=>`vf-stage-request-v1:${userId}:${caseId}`;
export function readStageRequest(userId:number,caseId:number):StageRequest | null {
  const raw=sessionStorage.getItem(stageRequestKey(userId,caseId));if(!raw)return null;
  const value=JSON.parse(raw) as StageRequest,b=value?.body;
  if(raw.length>4000||value.schema!==1||value.caseId!==caseId||value.userId!==userId||!b||b.confirmed!==true
    ||!STAGES.some(s=>s.k===b.state)||!Number.isSafeInteger(b.expected_revision)||b.expected_revision<0
    ||(value.runId!==null&&(!/^[0-9a-f-]{36}$/.test(value.runId)||!/^[0-9a-f-]{36}$/.test(b.workflow_step_id??'')
      ||!Number.isSafeInteger(b.expected_step_version)||b.expected_step_version!<0))
    ||(value.runId===null&&(b.workflow_step_id!==undefined||b.expected_step_version!==undefined)))throw new Error('本机阶段确认记录无法核验，请保留记录后核查。');
  return value;
}
export function stageReceiptMatches(pending:StageRequest,run:WorkflowRunDetail):boolean {
  const step=run.steps.find(s=>s.id===pending.body.workflow_step_id),receipt=step?.confirmation;
  return run.id===pending.runId&&run.case_id===pending.caseId&&step?.status==='succeeded'
    &&step.version===pending.body.expected_step_version!+1&&receipt?.user_id===pending.userId
    &&receipt.state===pending.body.state&&receipt.from_revision===pending.body.expected_revision
    &&Number.isSafeInteger(receipt.to_revision)&&receipt.to_revision>receipt.from_revision;
}
