import { useEffect, useRef, useState } from 'react';
import { ApiError, isCaseReadOnly } from '../api';
import { useStore } from '../store';
import { activatePanel } from '../shell/dock';
import { jobRecoveryRequest } from '../utils/jobRecovery';
import { caseWorkflowError, CORE_KINDS, dependencyLabel, dependencyReason, freshnessLabel, readStageRequest, stageReceiptMatches, stageRequestKey, STAGES } from '../utils/caseWorkflow';
import type { DependencySnapshot, StageRequest } from '../utils/caseWorkflow';
import type { CaseDictSnapshot } from '../utils/useCaseDraft';
import type { WorkflowRunDetail, WorkflowRunSummary } from '../utils/workflowRuns';
import { workflowReasonLabel } from '../utils/workflowRuns';
import { workflowStatusLabel, workflowTargetLabel } from '../utils/workflowRecovery';
import WorkflowInputRecovery from './WorkflowInputRecovery';
import './JobRecovery.css';
import './CaseWorkflowControls.css';

interface Props { caseId:number;userId:number;snapshot:CaseDictSnapshot;onChanged:()=>Promise<void> }
interface RunPage {items:WorkflowRunSummary[];next_cursor:string | null}
interface Draft {request:StageRequest;stamp:string}
const stageName=(value:string)=>`${value} ${STAGES.find(s=>s.k===value)?.label ?? value}`;
const producerFor:Record<string,string>={B:'geometry',C:'mesh',D:'solve',E:'postprocess'};

export default function CaseWorkflowControls(props:Props) {
  const selection=useStore(s=>s.workflowSelection);
  const readOnly=useStore(s=>isCaseReadOnly(s.currentCase,s.currentWs));
  const selected=selection?.caseId===props.caseId?selection.runId:null;
  return <CaseWorkflowSurface key={`${props.userId}:${props.caseId}:${selected ?? 'unbound'}`} {...props} selected={selected} readOnly={readOnly} />;
}

function CaseWorkflowSurface({caseId,userId,snapshot,onChanged,selected,readOnly}:Props & {selected:string | null;readOnly:boolean}) {
  const [rows,setRows]=useState<WorkflowRunSummary[]>([]),[run,setRun]=useState<WorkflowRunDetail | null>(null);
  const [cursor,setCursor]=useState<string | null>(null),[waiting,setWaiting]=useState(false);
  const [controlError,setControlError]=useState(''),[controlLoading,setControlLoading]=useState(true);
  const [graph,setGraph]=useState<DependencySnapshot | null>(null),[graphError,setGraphError]=useState(''),[graphLoading,setGraphLoading]=useState(true);
  const [epoch,setEpoch]=useState(0),[targetChoice,setTargetChoice]=useState('');
  const [draft,setDraft]=useState<Draft | null>(null),[busy,setBusy]=useState(false),[recoveryBusy,setRecoveryBusy]=useState(false);
  const [error,setError]=useState(''),[notice,setNotice]=useState('');
  const [recoveryOpened,setRecoveryOpened]=useState(false),[recoveryActive,setRecoveryActive]=useState(false);
  const [restored]=useState(()=>{try{return {value:readStageRequest(userId,caseId),error:''};}catch(e){return {value:null,error:caseWorkflowError(e)};}});
  const [pending,setPending]=useState<StageRequest | null>(restored.value),[manualRead,setManualRead]=useState<CaseDictSnapshot | null>(null),[acknowledge,setAcknowledge]=useState(false);
  const live=useRef(true),mutation=useRef(false),paging=useRef(false),olderLoaded=useRef(false);
  const feedback=useRef<HTMLParagraphElement>(null);
  useEffect(()=>{live.current=true;return()=>{live.current=false;};},[]);
  const configured=snapshot.parsed?.postProcessing?.runDir;
  const runDir=run?.run_dir ?? (typeof configured==='string'&&configured?configured:'C-run/case-1');
  const next=({A:'B',B:'C',C:'D',D:'E'})[snapshot.workflow_state] ?? snapshot.workflow_state;
  const target=targetChoice||next;
  const step=run?.steps.find(s=>s.confirmation_state===target&&s.status==='awaiting_confirmation');
  const graphCurrent=!!graph&&graph.case_id===caseId&&graph.dict_revision===snapshot.dict_revision&&graph.run_dir===runDir&&!graphLoading&&!graphError;
  const producer=producerFor[target],channel=graph?.channels.find(c=>c.kind===producer);
  const selectedPublication=run?.steps.find(s=>s.key===producer)?.publication;
  const special=target==='A'||target==='V';
  const stamp=JSON.stringify([snapshot.content_sha256,snapshot.dict_revision,snapshot.workflow_state,selected,run?.version,step?.id,step?.version,step?.confirmation_revision,graph?.dict_revision,graph?.event_sequence,target]);
  const disabled=busy||recoveryBusy||!!pending||!!restored.error;
  let blocked='';
  if(readOnly)blocked='当前工作区为只读。可以查看阶段、流程和保留产物；推进阶段或重新计算需要写权限。';
  else if(controlLoading||controlError)blocked='请先读取当前流程和步骤。';
  else if(snapshot.workflow_state===target)blocked='算例已经处于此阶段。';
  else if(!special&&target!==next)blocked='阶段只能逐级推进；请先完成当前阶段。';
  else if(!special&&snapshot.parse_status!=='ok')blocked='caseDict尚未通过解析，请先修复并重新读取。';
  else if(!special&&!graphCurrent)blocked='产物检查尚未完成或已过期，请刷新阶段与证据。';
  else if(!special&&channel?.status!=='fresh')blocked=`${dependencyLabel(producer)}产物${freshnessLabel(channel?.status ?? 'missing')}，请检查下方原因并生成或修复产物。`;
  else if(!special&&selected&&!step)blocked='所选流程当前没有等待此阶段的确认步骤，请选择对应流程或查看流程详情。';
  else if(!special&&selected&&!selectedPublication)blocked='原流程未记录可核验的阶段产物，请查看保留记录与恢复入口。';
  else if(!special&&selected&&selectedPublication?.id!==channel?.publication_id)blocked='此流程选定的产物已被替换，与当前活动发布不同。旧版本保留，请核对输入并按目标重新计算。';
  else if(!special&&step?.confirmation_revision!==undefined&&step.confirmation_revision!==snapshot.dict_revision)blocked='确认步骤仍绑定旧输入修订，请先重新核对该步骤，再明确确认。';
  else if(!special&&!selected&&waiting)blocked='此算例存在等待阶段确认的流程，请先选择要确认的工作流。';

  useEffect(()=>{
    let stopped=false,inFlight=false;const controller=new AbortController();
    const load=async()=>{
      if(inFlight)return;inFlight=true;
      try{
        const [page,wait,detail]=await Promise.all([
          jobRecoveryRequest<RunPage>(`/cases/${caseId}/workflow-runs-page?limit=20`,{signal:controller.signal}),
          jobRecoveryRequest<RunPage>(`/cases/${caseId}/workflow-runs-page?status=awaiting_confirmation&limit=1`,{signal:controller.signal}),
          selected?jobRecoveryRequest<WorkflowRunDetail>(`/workflow-runs/${selected}`,{signal:controller.signal}):Promise.resolve(null),
        ]);
        if(detail&&(detail.case_id!==caseId||detail.id!==selected))throw new Error('流程的算例归属不一致，请保留记录并重新读取。');
        if(stopped)return;
        setRows(previous=>[...page.items,...(olderLoaded.current?previous.filter(row=>!page.items.some(x=>x.id===row.id)):[])]);
        if(!olderLoaded.current)setCursor(page.next_cursor);setWaiting(wait.items.length>0);
        if(detail)setRun(previous=>previous&&previous.version>detail.version?previous:detail);
        setControlError('');
      }catch(e){if(!stopped)setControlError(caseWorkflowError(e));}
      finally{inFlight=false;if(!stopped)setControlLoading(false);}
    };
    void load();const timer=window.setInterval(()=>void load(),3000);
    return()=>{stopped=true;controller.abort();clearInterval(timer);};
  },[caseId,selected,epoch]);
  useEffect(()=>{
    const controller=new AbortController();
    const load=async()=>{
      setGraphLoading(true);
      try{
        const value=await jobRecoveryRequest<DependencySnapshot>(`/cases/${caseId}/dependencies?run_dir=${encodeURIComponent(runDir)}`,{signal:controller.signal});
        if(value.case_id!==caseId||value.run_dir!==runDir)throw new Error('产物检查的算例或目录归属不一致。');
        if(!controller.signal.aborted){setGraph(value);setGraphError('');}
      }catch(e){if(!controller.signal.aborted)setGraphError(caseWorkflowError(e));}
      finally{if(!controller.signal.aborted)setGraphLoading(false);}
    };
    void load();return()=>controller.abort();
  },[caseId,runDir,snapshot.dict_revision,epoch]);
  useEffect(()=>{
    let timer:number | undefined;
    const reconcile=(event:Event)=>{
      if((event as CustomEvent).detail?.caseId!==caseId)return;
      clearTimeout(timer);timer=window.setTimeout(()=>setEpoch(value=>value+1),400);
    };
    window.addEventListener('vibeflow:case-reconciled',reconcile);
    return()=>{clearTimeout(timer);window.removeEventListener('vibeflow:case-reconciled',reconcile);};
  },[caseId]);
  function selectRun(id:string){if(disabled)return;useStore.getState().setWorkflowSelection(id?{caseId,runId:id}:null);}
  function openJobs(jobId?:number){useStore.getState().requestJobPanel(jobId?{caseId,jobId}:{caseId,runId:selected!});activatePanel('jobs');}
  function refresh(){setEpoch(value=>value+1);void onChanged();}
  async function more(){
    if(!cursor||paging.current)return;paging.current=true;
    try{const page=await jobRecoveryRequest<RunPage>(`/cases/${caseId}/workflow-runs-page?limit=20&cursor=${encodeURIComponent(cursor)}`);
      if(live.current){olderLoaded.current=true;setRows(previous=>[...previous,...page.items.filter(row=>!previous.some(x=>x.id===row.id))]);setCursor(page.next_cursor);}
    }catch(e){if(live.current)setControlError(caseWorkflowError(e));}finally{paging.current=false;}
  }
  function prepare(){
    if(disabled||blocked)return;setError('');setNotice('');
    setDraft({stamp,request:{schema:1,caseId,userId,runId:!special&&step?selected:null,
      body:{state:target,confirmed:true,expected_revision:snapshot.dict_revision,
        ...(!special&&step?{workflow_step_id:step.id,expected_step_version:step.version}:{})}}});
  }
  function clearPending(){sessionStorage.removeItem(stageRequestKey(userId,caseId));if(live.current){setPending(null);setManualRead(null);setAcknowledge(false);}}
  async function inspectRequest(request:StageRequest,rejected=false){
    const [info,detail]=await Promise.all([
      jobRecoveryRequest<CaseDictSnapshot>(`/cases/${caseId}/caseDict`),
      request.runId?jobRecoveryRequest<WorkflowRunDetail>(`/workflow-runs/${request.runId}`):Promise.resolve(null),
    ]);
    if(detail&&stageReceiptMatches(request,detail)){
      clearPending();if(live.current){setError('');setDraft(null);setTargetChoice('');setNotice('已从原流程读取匹配的阶段确认回执，没有重复提交。');}return true;
    }
    if(rejected&&info.dict_revision===request.body.expected_revision&&info.workflow_state!==request.body.state){
      clearPending();if(live.current)setDraft(null);return false;
    }
    if(live.current){setManualRead(info);setNotice(request.runId?'暂未读取到匹配回执，原请求保留，请勿重复提交。':`服务器当前阶段为${stageName(info.workflow_state)}、修订${info.dict_revision}。独立阶段请求没有流程回执，请核对后再继续。`);}
    return false;
  }
  async function confirm(){
    if(!draft||draft.stamp!==stamp||disabled||blocked||mutation.current)return;
    const request=draft.request;mutation.current=true;setBusy(true);setError('');
    try{
      sessionStorage.setItem(stageRequestKey(userId,caseId),JSON.stringify(request));readStageRequest(userId,caseId);setPending(request);
      try{
        const receipt=await jobRecoveryRequest<{workflow_state:string;dict_revision:number;workflow_dispatch?:{status:string}}>(`/cases/${caseId}/caseDict:advance`,{body:request.body});
        if(receipt.workflow_state!==request.body.state||receipt.dict_revision!==request.body.expected_revision+1)throw new Error('阶段回执无法核验，请读取服务器记录。');
        clearPending();if(live.current){setDraft(null);setTargetChoice('');setNotice(receipt.workflow_dispatch?.status==='pending'?'阶段已确认，后续任务尚待派发，请查看流程详情。':'阶段已明确确认，后续阶段仍需分别核对。');}
      }catch(failure){
        if(live.current)setError(caseWorkflowError(failure));
        try{await inspectRequest(request,failure instanceof ApiError&&failure.status>=400&&failure.status<500);}catch{/* Retain the original pending request across network/read failures. */}
      }
      await onChanged();
    }catch(failure){if(live.current)setError(caseWorkflowError(failure));}
    finally{mutation.current=false;if(live.current){setBusy(false);setEpoch(value=>value+1);feedback.current?.focus();}}
  }
  async function inspectPending(){
    if(!pending||mutation.current)return;mutation.current=true;setBusy(true);setError('');
    try{await inspectRequest(pending);await onChanged();}catch(e){if(live.current)setError(caseWorkflowError(e));}
    finally{mutation.current=false;if(live.current){setBusy(false);setEpoch(value=>value+1);}}
  }
  async function rearm(){
    if(!selected||!step||disabled||readOnly||mutation.current)return;mutation.current=true;setBusy(true);setError('');setDraft(null);
    try{await jobRecoveryRequest(`/workflow-runs/${selected}/steps/${step.id}:refresh-confirmation`,{body:{expected_version:step.version}});if(live.current)setNotice('步骤已重新核对输入与产物，仍需明确确认阶段。');await onChanged();}
    catch(e){if(live.current)setError(caseWorkflowError(e));}
    finally{mutation.current=false;if(live.current){setBusy(false);setEpoch(value=>value+1);}}
  }
  return <section className="case-workflow" aria-label="阶段、输入与工作流" aria-busy={busy||recoveryBusy}>
    <div className="case-flow-heading"><h3>当前阶段 {stageName(snapshot.workflow_state)}</h3><button onClick={refresh} disabled={busy||recoveryBusy}>刷新阶段与证据</button></div>
    <p className="case-flow-muted">输入修订 {snapshot.dict_revision} · 解析状态 {snapshot.parse_status}</p>
    {(snapshot.parsed?.errors?.summary?.open??0)>0&&<p>仍有{snapshot.parsed?.errors?.summary?.open}项未关闭错误，阶段接口将核验阻止项；请查看下方错误记录。</p>}
    <label>选择工作流<select aria-label="阶段确认所属工作流" value={selected??''} disabled={disabled} onChange={e=>selectRun(e.target.value)}>
      <option value="">独立作业阶段（不绑定工作流）</option>
      {selected&&!rows.some(row=>row.id===selected)&&<option value={selected}>{run?`${workflowTargetLabel(run.target)} · ${workflowStatusLabel(run.status)}`:'正在读取所选流程'} · {selected}</option>}
      {rows.map(row=><option key={row.id} value={row.id}>{workflowTargetLabel(row.target)} · {workflowStatusLabel(row.status)} · {row.id}</option>)}
    </select></label>
    {cursor&&<button disabled={disabled} onClick={()=>void more()}>读取更早的工作流</button>}
    {controlError&&<p role="alert">流程读取失败：{controlError}</p>}
    {run&&<><p className="case-flow-id">{run.id}</p><p>所选流程{workflowStatusLabel(run.status)} · 起始输入修订{run.expected_revision} · {run.run_dir}</p>
      {run.error_code&&<p>{workflowReasonLabel(run.error_code)}</p>}
      {(run.detail||run.error_code)&&<details><summary>原流程诊断</summary><p>{run.detail}</p><code>{run.error_code}</code></details>}
      <button disabled={busy||recoveryBusy} onClick={()=>openJobs()}>在作业面板查看此流程</button></>}
    <label>目标阶段<select aria-label="目标阶段" value={target} disabled={disabled} onChange={e=>{setTargetChoice(e.target.value);setDraft(null);}}>{STAGES.map(s=><option key={s.k} value={s.k}>{stageName(s.k)}</option>)}</select></label>
    {special&&<p>回退A或切换开发阶段不会停止原任务。请先核对活动作业；已有确认和产物仍须按输入版本重新检查。</p>}
    {blocked&&<p role="status">{blocked}</p>}
    {!special&&step&&step.confirmation_revision!==snapshot.dict_revision&&<button disabled={disabled||readOnly||!!controlError} onClick={()=>void rearm()}>重新核对该确认步骤</button>}
    {pending||restored.error?<div className="case-flow-confirm">
      <p role="status">{restored.error||'上次阶段请求结果仍待核对，原请求保留，不会自动重复提交。'}</p>
      {pending&&<><p>原请求：确认{stageName(pending.body.state)}，输入修订{pending.body.expected_revision}。{pending.runId?`绑定流程 ${pending.runId}`:'独立阶段请求'}</p><button disabled={busy||recoveryBusy} onClick={()=>void inspectPending()}>读取服务器阶段与原回执</button>
        {pending.runId&&pending.runId!==selected&&<button disabled={busy||recoveryBusy} onClick={()=>useStore.getState().setWorkflowSelection({caseId,runId:pending.runId!})}>查看原确认所属流程</button>}
        {!pending.runId&&manualRead&&manualRead.workflow_state===pending.body.state&&manualRead.dict_revision>pending.body.expected_revision&&<>
          <label className="case-flow-check"><input type="checkbox" checked={acknowledge} onChange={e=>setAcknowledge(e.target.checked)}/>我已核对服务器当前阶段，后续基于此版本操作；这不证明原请求由谁提交。</label>
          <button disabled={busy||!acknowledge} onClick={()=>{try{sessionStorage.setItem(stageRequestKey(userId,caseId)+':readback',JSON.stringify({request:pending,observed_stage:manualRead.workflow_state,observed_revision:manualRead.dict_revision}));clearPending();setDraft(null);}catch(e){setError(caseWorkflowError(e));}}}>保留核对记录并继续</button>
        </>}
      </>}
    </div>:draft?<div className="case-flow-confirm">
      <p>确认将算例推进到{stageName(draft.request.body.state)}，使用输入修订{draft.request.body.expected_revision}。</p>
      <p>{draft.request.runId?`绑定流程 ${draft.request.runId}，仅确认此流程当前等待的阶段步骤。`:'这是独立阶段操作，不会代替任何工作流确认步骤。'}</p>
      {draft.stamp!==stamp&&<p role="status">输入、流程或证据记录已变化，请重新核对。</p>}
      <div className="case-flow-actions"><button disabled={busy||recoveryBusy||!!blocked||draft.stamp!==stamp} onClick={()=>void confirm()}>明确确认{draft.request.body.state}阶段</button><button disabled={busy} onClick={()=>setDraft(null)}>返回核对</button></div>
    </div>:<button disabled={disabled||!!blocked} onClick={prepare}>核对阶段确认</button>}
    <p ref={feedback} tabIndex={-1} role={error?'alert':'status'}>{error||notice}</p>
    <details className="case-flow-evidence"><summary>产物证据与输入变化{graphCurrent?` · ${graph!.channels.filter(c=>c.status==='fresh').length}项当前有效`:''}</summary>
      <p>检查目录 {runDir}。检查结果用于说明当前来源和依赖，提交阶段或重算时服务端仍会重新核验。</p>
      {graphLoading&&<p role="status">正在检查不可变产物和当前输入…</p>}{graphError&&<p role="alert">{graphError}</p>}
      {graph&&!graphCurrent&&<p role="status">当前显示的是上次检查记录，请刷新；尚不能作为当前阶段确认依据。</p>}
      {graph&&<><p className="case-flow-muted">检查输入修订 {graph.dict_revision} · 事件序号 {graph.event_sequence??'未记录'}</p>
        {selectedPublication&&<p>所选流程固定使用发布版本 {selectedPublication.generation}：<span className="case-flow-id">{selectedPublication.id}</span>。当前阶段需要的活动发布：<span className="case-flow-id">{channel?.publication_id??'尚无'}</span>。旧结果保留不等于仍适用于当前输入。</p>}
        <ul className="case-flow-publications">{graph.channels.map(channel=>{const publication=graph.publications.find(p=>p.publication_id===channel.publication_id);return <li key={channel.kind}>
          <strong>{dependencyLabel(channel.kind)} · {freshnessLabel(channel.status)}</strong>
          {publication?<><p>发布版本 {publication.generation}</p><p className="case-flow-id">{publication.publication_id}</p>
            {publication.reasons.map((reason,index)=><div key={index}><p>{dependencyReason(reason.code)}{reason.fields?.length?`变化字段：${reason.fields.join('、')}`:''}</p><details><summary>原始诊断</summary><code>{reason.code}</code></details></div>)}
            {publication.dependencies.map(edge=><p key={edge.publication_id}>上游{edge.require_current?'要求当前有效':'保留历史引用'} · {freshnessLabel(edge.status)} · <span className="case-flow-id">{edge.publication_id}</span></p>)}
          </>:<p>尚无可供此通道使用的正式发布。旧文件可能仍被保留，不能据此确认产物有效。</p>}
        </li>;})}</ul>
        <p>整条主线建议生成或更新：{graph.recompute.map(dependencyLabel).join(' → ')||'未发现缺失或过期通道'}。</p>
        {!!graph.needs_inspection.length&&<p>需先核查：{graph.needs_inspection.map(dependencyLabel).join('、')}。无法核验的产物不能当作有效结果。</p>}
      </>}
    </details>
    {run?.recomputation&&<div className="case-flow-lineage"><h4>重算计划与执行记录</h4><p>来源流程 <button disabled={disabled} onClick={()=>selectRun(run.recomputation!.source_run_id)}>{run.recomputation.source_run_id}</button></p>
      <p>计划复用：{Object.keys(run.recomputation.reused).map(dependencyLabel).join('、')||'无'}。计划重新计算：{run.recomputation.rebuild.map(dependencyLabel).join(' → ')||'无'}。</p>
      <ul>{CORE_KINDS.filter(kind=>kind in run.recomputation!.reused||run.recomputation!.rebuild.includes(kind)).map(kind=>{
        const step=run.steps.find(item=>item.key===kind),plannedReuse=run.recomputation!.reused[kind];
        const verifiedReuse=!!plannedReuse&&step?.execution_mode==='reuse'&&step.status==='succeeded'&&step.publication?.id===plannedReuse.id;
        return <li key={kind}><strong>{dependencyLabel(kind)} · {step?workflowStatusLabel(step.status):'尚无步骤记录'}</strong>
          {plannedReuse?<p>{verifiedReuse?'已完成核验复用，没有新的执行尝试。':'计划复用，尚无匹配的完成回执。'}</p>
            :step?.attempts.length?<ul>{step.attempts.map(attempt=><li key={attempt.id}><button disabled={disabled} onClick={()=>openJobs(attempt.job_id)}>作业 #{attempt.job_id} · 第{attempt.number}次尝试 · {workflowStatusLabel(attempt.phase)}</button></li>)}</ul>
              :<p>{step?'未创建新的执行尝试。':'无法核对执行尝试，请查看原流程记录。'}</p>}
        </li>;
      })}</ul>
    </div>}
    {selected&&<details className="job-recovery case-flow-recovery" onToggle={event=>{const open=event.currentTarget.open;setRecoveryActive(open);if(open)setRecoveryOpened(true);}}><summary>按目标核对输入、重算与关联流程</summary><div className="job-recovery-content">
      {recoveryOpened&&<WorkflowInputRecovery runId={selected} caseId={caseId} userId={userId} disabled={busy||!!controlError||!!pending||!!restored.error} onBusy={setRecoveryBusy}
        onChanged={refresh} onNavigate={id=>openJobs(id)} onRunNavigate={id=>useStore.getState().setWorkflowSelection({caseId,runId:id})} dependency={graphCurrent?graph!:undefined}
        active={recoveryActive} observedRevision={snapshot.dict_revision} observedRunVersion={run?.version} observedEventSequence={graph?.event_sequence??undefined}/>}
    </div></details>}
  </section>;
}
