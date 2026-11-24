"""Real DeepSeek proposals and actual API death at approval write boundaries."""
import asyncio,hashlib,json,re,uuid
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from urllib.parse import quote
from live_llm_job_repeat_acceptance import main,put,write
import live_llm_agent_recovery_acceptance as checkpoint
from live_llm_failure_fallback_acceptance import kill_owned_api,wait_for

APPROVAL_FIXTURE=r'''
from app.models import ToolCall,DurableEvent
from app.services.tools import ToolExecutor
from app.services import file_transactions
from app import storage
approval_original_proposal=ToolExecutor.apply_proposal
approval_original_append=agent._append_event
approval_original_commit=AsyncSession.commit

def approval_selection():
 path=root/'approval-hold.json'
 return json.loads(path.read_text()) if path.exists() else None

async def approval_observation(db,cid,tid,path):
 conv=await db.get(Conversation,cid);case=await db.get(Case,conv.case_id)
 tool=await db.get(ToolCall,tid,populate_existing=True)
 execution=await db.get(ConversationExecution,cid,populate_existing=True)
 events=list(await db.scalars(select(AgentEvent).where(AgentEvent.conversation_id==cid).order_by(AgentEvent.id)))
 suffix=tid.split(':',1)[-1]
 results=[json.loads(event.payload) for event in events if event.type=='tool_result' and json.loads(event.payload).get('tool_call_id')==suffix]
 changes=list(await db.scalars(select(DurableEvent).where(DurableEvent.case_id==case.id,DurableEvent.type=='files.changed')))
 return {'pid':os.getpid(),'tool_status':tool.status if tool else None,'tool_result_count':len(results),
  'approved_result_count':sum(bool(result.get('approved')) for result in results),
  'interrupted_result_count':sum(result.get('status')=='interrupted' for result in results),
  'recovery_receipt_count':sum(event.type=='system' and json.loads(event.payload).get('kind')=='agent_recovery' for event in events),
  'phase':execution.phase if execution else None,'version':execution.version if execution else None,
  'resource_id':execution.resource_id if execution else None,'backend':execution.backend if execution else None,
  'case_stage':case.workflow_state,'committed_path_changes':sum(path in json.loads(event.payload_json).get('paths',[]) for event in changes)}

async def approval_boundary(cid,tid,stage):
 selected=approval_selection()
 if not selected or selected['cid']!=cid or selected['stage']!=stage or selected['role']!=agent_role:return
 async with SessionLocal() as observing:
  conv=await observing.get(Conversation,cid);case=await observing.get(Case,conv.case_id)
  value=await approval_observation(observing,cid,tid,selected['path'])
  value.update(stage=stage,cid=cid,tool_id=tid,
   actual_file_sha256=hashlib.sha256(storage.get_bytes(case.s3_prefix,selected['path'])).hexdigest())
 target=root/('approval-'+stage+'-witness.json');temporary=target.with_name('.'+uuid.uuid4().hex)
 temporary.write_text(json.dumps(value));temporary.replace(target)
 deadline=time.monotonic()+90
 while not (root/('approval-release-'+stage)).exists():
  if time.monotonic()>deadline:raise TimeoutError('Actual approval boundary not released: '+stage)
  await asyncio.sleep(.03)

async def approval_proposal(self,db,case,name,args):
 result=await approval_original_proposal(self,db,case,name,args)
 selected=approval_selection()
 if selected and selected['stage']=='before_commit' and selected['path']==args.get('path'):
  await approval_boundary(selected['cid'],selected['tool_id'],'before_commit')
 return result
ToolExecutor.apply_proposal=approval_proposal

async def approval_append(db,cid,etype,payload):
 result=await approval_original_append(db,cid,etype,payload)
 selected=approval_selection()
 if selected and selected['cid']==cid and selected['role']==agent_role:
  if selected['stage']=='after_commit' and etype=='tool_result' and payload.get('approved'):
   db.info['approval_boundary']=(cid,selected['tool_id'],'after_commit')
  elif selected['stage']=='new_active' and etype=='run_state' and payload.get('status')=='started':
   db.info['approval_boundary']=(cid,selected['tool_id'],'new_active')
 return result
agent._append_event=approval_append

async def approval_commit(db):
 boundary=db.info.pop('approval_boundary',None)
 result=await approval_original_commit(db)
 if boundary:await approval_boundary(*boundary)
 return result
AsyncSession.commit=approval_commit

@app.get('/api/v1/conversations/{cid}/testing/approval-state')
async def approval_state(cid:int,tool_id:str,path:str,user=Depends(get_current_user),db=Depends(get_db)):
 conv=await db.get(Conversation,cid);await get_owned_case(conv.case_id,user,db,write=False)
 return await approval_observation(db,cid,tool_id,path)
'''
FIXTURE=checkpoint.FIXTURE+'\nfrom app.models import Case\n'+APPROVAL_FIXTURE


def peer(api,store,role):
 value=checkpoint.peer(api,store,role)
 with (value.root/'agent_peer_fixture.py').open('a',encoding='utf8') as stream:
  stream.write('\nfrom app.models import Case\n'+APPROVAL_FIXTURE)
 value.token=api.token
 return value


def request_body(configuration,profile,content):
 return {'content':content,'provider_profile_id':profile['id'],'model':configuration['model']}


def stream(api,path,body,label,configuration):
 events=checkpoint.stream(api,path,body,label,configuration)
 assert not any(e.get('type') in {'stream.error','request.failed','request.timed_out'} for e in events),[e.get('type') for e in events]
 return events


def propose(api,cid,profile,configuration,path,before,after,label):
 prompt=(f'这是隔离测试文件{path}，当前内容准确为{before}。请仅调用一次propose_file_update，'
  f'把完整内容改为{after}，不加换行，reason写真实恢复验收。不要调用其他工具。'
  '等待用户批准；收到工具结果后只简短说明结果，不再调用工具，不重试。')
 events=stream(api,f'/conversations/{cid}/messages',request_body(configuration,profile,prompt),label,configuration)
 proposals=[e['tool_call'] for e in events if e.get('type')=='approval.requested']
 assert len(proposals)==1,events
 proposal=proposals[0]
 assert proposal['tool']=='propose_file_update' and proposal['args']['path']==path and proposal['args']['content']==after
 assert proposal['args']['baseline_sha256']==hashlib.sha256(before.encode()).hexdigest()
 return proposal


def observe(api,cid,tid,path):
 return api.call('GET',f'/conversations/{cid}/testing/approval-state?tool_id='+quote(tid)+'&path='+quote(path))


def file_content(api,path):return api.call('GET',api.prefix+'/file?path='+quote(path))['content']


def http_count(api):
 path=api.root/'upstream.jsonl'
 return sum(json.loads(line)['kind']=='http' for line in path.read_text().splitlines()) if path.exists() else 0


def scenario(api,*,configuration,store=None,**_):
 checks=[]
 for mode in ['pending','before_commit','after_commit']:
  api.create_case('Actual model approval '+mode)
  saved=put(api,'A-geo/geometry.py','import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n')
  if saved.get('auto_preview_job'):api.wait_job(saved['auto_preview_job'])
  dictionary=api.call('GET',api.prefix+'/caseDict')
  flow=api.call('POST',api.prefix+'/workflow-runs',{'target':'geometry','expected_revision':dictionary['dict_revision'],
   'parameters':{'geometry_mode':'export'},'budget':{'timeout_seconds':120,'max_log_bytes':16777216}})
  def waiting_flow():
   value=api.call('GET','/workflow-runs/'+flow['id'])
   if value['status'] in {'failed','interrupted'}:raise AssertionError(value)
   return value if value['status']=='awaiting_confirmation' else None
  waiting=wait_for(waiting_flow,'Registered geometry did not await B',180)
  profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual approval recovery '+mode,
   'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
  conv=api.call('POST','/conversations',{'case_id':api.case_id,'title':mode,'permission_mode':'approve_writes',
   'provider_profile_id':profile['id'],'model':configuration['model']})
  cid=conv['id'];route=f'/conversations/{cid}';path='Ref/approval-recovery.txt'
  before='BEFORE_'+uuid.uuid4().hex;after='AFTER_'+uuid.uuid4().hex;put(api,path,before)
  source=peer(api,store,mode+'-source');recovery=peer(api,store,mode+'-recovery')
  hold=api.root/'approval-hold.json';kill=None;old_recovery_body=None
  try:
   with source:
    proposal=propose(source,cid,profile,configuration,path,before,after,mode+'-proposal')
    tid=proposal['id'];tool_route=route+'/tool-calls/'+tid
    initial=observe(source,cid,tid,path)
    assert initial['tool_status']=='pending' and initial['case_stage']=='A' and file_content(api,path)==before
    calls=http_count(api)
    source.call('POST',route+'/messages',request_body(configuration,profile,'继续'),expect=409)
    assert http_count(api)==calls and file_content(api,path)==before
    if mode=='pending':
     witness=initial;launcher=kill_owned_api(source,witness['pid']);kill=witness['pid']
    else:
     hold.write_text(json.dumps({'cid':cid,'stage':mode,'role':mode+'-source','path':path,'tool_id':tid}))
     with ThreadPoolExecutor(max_workers=1) as pool:
      future=pool.submit(stream,source,tool_route+':approve',{},mode+'-interrupted-approval',configuration)
      try:
       marker=api.root/('approval-'+mode+'-witness.json')
       def reached():
        if future.done() and not marker.exists():raise AssertionError('Approval ended before real boundary: '+str(future.result()))
        return marker.exists()
       wait_for(reached,'Actual approval did not reach '+mode)
       witness=json.loads(marker.read_text());assert witness['actual_file_sha256']==hashlib.sha256(after.encode()).hexdigest()
       assert witness['tool_status']==('running' if mode=='before_commit' else 'executed')
       assert witness['approved_result_count']==(0 if mode=='before_commit' else 1)
       assert witness['committed_path_changes']==initial['committed_path_changes']+(mode=='after_commit')
       launcher=kill_owned_api(source,witness['pid']);kill=witness['pid']
       try:future.result(timeout=10)
       except Exception:pass
       else:raise AssertionError('Terminated approval returned a normal response')
      finally:(api.root/('approval-release-'+mode)).touch()
   if mode!='pending':
    plan=api.call('GET',route+'/recovery');assert plan['can_recover'] and plan['backend']=='approval'
    old_recovery_body={'expected_version':plan['version'],'expected_evidence_sha256':plan['evidence_sha256']}
   with recovery:
    state=observe(recovery,cid,tid,path);assert state['phase']=='idle' and state['case_stage']=='A'
    history=api.call('GET',route)
    if mode=='pending':
     assert len(history['pending_tools'])==1 and history['pending_tools'][0]['id']==tid
     assert file_content(api,path)==before and state['tool_status']=='pending' and state['tool_result_count']==0
     approved=stream(recovery,tool_route+':approve',{},mode+'-approved-after-restart',configuration)
     assert any(e.get('type')=='tool.result' and e['ok'] for e in approved)
     assert any(e.get('type')=='run.completed' for e in approved)
    elif mode=='before_commit':
     assert not history['pending_tools'] and state['tool_status']=='interrupted'
     assert state['interrupted_result_count']==1 and state['approved_result_count']==0
     assert state['committed_path_changes']==initial['committed_path_changes'] and file_content(api,path)==before
     calls=http_count(api);recovery.call('POST',tool_route+':approve',{},expect=409);assert http_count(api)==calls
     replacement=propose(recovery,cid,profile,configuration,path,before,after,mode+'-fresh-proposal')
     assert replacement['id']!=tid and file_content(api,path)==before
     approved=stream(recovery,route+'/tool-calls/'+replacement['id']+':approve',{},mode+'-fresh-approved',configuration)
     assert any(e.get('type')=='tool.result' and e['ok'] for e in approved)
    else:
     assert not history['pending_tools'] and state['tool_status']=='executed' and state['approved_result_count']==1
     assert file_content(api,path)==after and state['committed_path_changes']==initial['committed_path_changes']+1
     calls=http_count(api);recovery.call('POST',tool_route+':approve',{},expect=409);assert http_count(api)==calls
     (api.root/'approval-release-new_active').unlink(missing_ok=True)
     hold.write_text(json.dumps({'cid':cid,'stage':'new_active','role':mode+'-recovery','path':path,'tool_id':tid}))
     with ThreadPoolExecutor(max_workers=1) as pool:
      future=pool.submit(stream,recovery,route+'/messages',request_body(configuration,profile,
       '请根据已保存的工具结果说明刚才修改是否成功；不要再调用工具，不重新执行修改，只回复一句话。'),
       mode+'-real-resume',configuration)
      try:
       marker=api.root/'approval-new_active-witness.json'
       wait_for(marker.exists,'New real model run did not acquire its owner')
       active_before=observe(api,cid,tid,path);assert active_before['phase']=='active' and active_before['backend']=='api'
       replay=api.call('POST',route+':recover',old_recovery_body)
       active_after=observe(api,cid,tid,path)
       assert replay['replayed'] and replay['current_phase']=='active'
       assert active_after['phase']=='active' and active_after['resource_id']==active_before['resource_id']
      finally:(api.root/'approval-release-new_active').touch();hold.unlink(missing_ok=True)
      resumed=future.result(timeout=180)
     assert any(e.get('type')=='run.completed' for e in resumed) and not any(e.get('type')=='tool.result' for e in resumed)
    assert file_content(api,path)==after
    final=observe(api,cid,tid,path)
    assert final['committed_path_changes']==initial['committed_path_changes']+1
    calls=http_count(api);recovery.call('POST',tool_route+':approve',{},expect=409);assert http_count(api)==calls
    if old_recovery_body:
     assert recovery.call('POST',route+':recover',old_recovery_body)['replayed']
     assert observe(api,cid,tid,path)['committed_path_changes']==final['committed_path_changes']
    assert api.call('GET','/workflow-runs/'+flow['id'])==waiting
    confirmation=next(step for step in waiting['steps'] if step['key']=='confirm_B')
    write(api,'POST',api.prefix+'/caseDict:advance',{'state':'B','confirmed':True,
     'expected_revision':confirmation['confirmation_revision'],'workflow_step_id':confirmation['id'],
     'expected_step_version':confirmation['version']})
    assert api.call('GET','/workflow-runs/'+flow['id'])['status']=='succeeded'
   assert source.proc.poll() is not None and recovery.proc.poll() is not None
   checks.append({'mode':mode,'conversation_id':cid,'tool_id':tid,'actual_API_pid_killed':kill,
    'verified_launcher_pid':launcher,'actual_boundary':witness,'pending_message_refused_without_model':True,
    'old_tool_never_replayed':True,'committed_target_file_changes':1,'workflow_B_confirmation_explicit':True,
    'old_recovery_during_new_active_run_isolated':mode=='after_commit','owned_apis_exited':True})
   (api.root/'agent-approval-recovery-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
  finally:
   for stage in [mode,'new_active']:(api.root/('approval-release-'+stage)).touch()
   hold.unlink(missing_ok=True)
 report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'real_model_tool_proposals':True,
  'checks':checks,'scope':'Real proposal restart and approval commit boundaries; explicit workflow gates preserved; error/other-stage integration pending'}
 (api.root/'agent-approval-recovery.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 api.create_case('Independent real approval recovery evidence readback')
 return report


if __name__=='__main__':asyncio.run(main(selected_factory=lambda configuration:partial(scenario,configuration=configuration),fixture_override=FIXTURE,store_volume_max=16))
