"""Actual DeepSeek checkpoints and owned API death; no simulated inference."""
import asyncio,hashlib,json,os,re,time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
import httpx
from live_llm_acceptance import FIXTURE as LIVE
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_failure_fallback_acceptance import kill_owned_api,wait_for
from testsupport import IsolatedServer

FIXTURE=r'''
import asyncio,hashlib,json,os,re,time,uuid
from pathlib import Path
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import SessionLocal,get_db
from app.deps import get_current_user
from app.models import AgentEvent,ConversationExecution,Conversation,ToolCall
from app.routers import agent
from app.routers.workspaces import get_owned_case
from app.services import llm
from app.services import agent_recovery,conversation_execution
from app.services.durable_events import lock_case_execution
root=Path(os.environ.get('VIBEFLOW_AGENT_SHARED_ROOT',str(Path(__file__).parent)))
llm._VAULT_KEY_PATH=root/'.vault_key'
agent_role=os.environ.get('VIBEFLOW_AGENT_ROLE','main')
agent_original_append=agent._append_event
agent_original_commit=AsyncSession.commit
agent_original_send=httpx.AsyncClient.send

def agent_selection():
 path=root/'agent-hold.json'
 return json.loads(path.read_text()) if path.exists() else None

async def agent_append(db,cid,etype,payload):
 result=await agent_original_append(db,cid,etype,payload)
 selected=agent_selection()
 if (selected and selected['cid']==cid and selected['stage'] in ('checkpoint','finish') and agent_role==selected.get('role','crash')
  and etype=='run_checkpoint' and len(payload.get('content',''))>=32):
  row=await db.scalar(select(AgentEvent).where(AgentEvent.conversation_id==cid,AgentEvent.type=='run_checkpoint')
   .order_by(AgentEvent.id.desc()).limit(1))
  db.info['agent_real_boundary']={'cid':cid,'stage':selected['stage'],'event_id':row.event_id,'run_id':payload['run_id'],
   'actual_content':payload['content'],'content_sha256':hashlib.sha256(payload['content'].encode()).hexdigest()}
 return result
agent._append_event=agent_append

async def agent_commit(db):
 boundary=db.info.pop('agent_real_boundary',None)
 result=await agent_original_commit(db)
 if boundary:
  async with SessionLocal() as observed:
   saved=await observed.scalar(select(AgentEvent).where(AgentEvent.event_id==boundary['event_id']))
   assert saved and json.loads(saved.payload)['content']==boundary['actual_content']
  boundary.update(pid=os.getpid(),actual_SQL_checkpoint_committed=True)
  stage=boundary['stage']
  target=root/f'agent-{stage}-witness.json';temporary=target.with_name('.'+uuid.uuid4().hex)
  temporary.write_text(json.dumps(boundary));temporary.replace(target)
  deadline=time.monotonic()+90
  while not (root/f'agent-release-{stage}').exists():
   if time.monotonic()>deadline:raise TimeoutError('Actual checkpoint hold was not released')
   await asyncio.sleep(.03)
 return result
AsyncSession.commit=agent_commit

async def agent_send(self,request,*args,**kwargs):
 if request.url.host=='api.deepseek.com' and request.url.path.endswith('/chat/completions'):
  body=json.loads(await request.aread());notes=[]
  for message in body.get('messages',[]):
   text=message.get('content')
   if isinstance(text,str) and '系统恢复记录：' in text:
    notes+=re.findall(r'MODEL_NOTE_[A-Z0-9]{8}(?![A-Z0-9])',text)
  with (root/'agent-request-observations.jsonl').open('a') as stream:
   stream.write(json.dumps({'role':agent_role,'pid':os.getpid(),'actual_outgoing_body_sha256':hashlib.sha256(await request.aread()).hexdigest(),
    'recovery_note_markers':notes})+'\n')
 return await agent_original_send(self,request,*args,**kwargs)
httpx.AsyncClient.send=agent_send

@app.post('/api/v1/testing/agent-startup/{case_id}/{operation}')
async def startup_records(case_id:int,operation:str,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 await get_owned_case(case_id,user,db,write=True)
 await lock_case_execution(db,case_id)
 if operation=='legacy-status':
  q=select(AgentEvent.type,AgentEvent.payload).join(Conversation,Conversation.id==AgentEvent.conversation_id).where(
   Conversation.case_id==case_id,Conversation.title=='Legacy startup lifecycle fixture')
  counts={}
  for kind,raw in await db.execute(q):
   status=kind+'.'+json.loads(raw)['status']
   counts[status]=counts.get(status,0)+1
  return counts
 if operation=='legacy':
  ids=[]
  for i in range(agent_recovery.PAGE_SIZE+3):
   conv=Conversation(case_id=case_id,title='Legacy startup lifecycle fixture')
   db.add(conv)
   await db.flush()
   ids.append(conv.id)
   # This is SQL lifecycle data only. No assistant text, model decision or provider is fabricated.
   kind='request_state' if i%2 else 'run_state'
   payload={'run_id':uuid.uuid4().hex,'status':'started'}
   if kind=='request_state':payload['request_id']=uuid.uuid4().hex
   db.add(AgentEvent(conversation_id=conv.id,seq=1,event_id=uuid.uuid4().hex,type=kind,payload=json.dumps(payload)))
   if i==agent_recovery.PAGE_SIZE+2:
    db.add(AgentEvent(conversation_id=conv.id,seq=2,event_id=uuid.uuid4().hex,type=kind,
     payload=json.dumps({**payload,'status':'completed'})))
  await db.commit()
  return {'ids':ids}
 if operation=='claim':
  cid=body['cid']
  conv=await db.get(Conversation,cid)
  assert conv and conv.case_id==case_id
  old=await db.get(ToolCall,f'{cid}:old')
  assert old.status=='queued'
  old.status='executed'
  await db.flush()
  await conversation_execution.acquire(db,cid,'api',uuid.uuid4().hex)
  db.add(ToolCall(id=f'{cid}:new',conversation_id=cid,tool='read_text_file',status='running'))
  await db.commit()
  return {'id':cid}
 assert operation in ('owners','orphan')
 ids=[]
 for _ in range(agent_recovery.PAGE_SIZE+1 if operation=='owners' else 2):
  conv=Conversation(case_id=case_id,title='Startup concurrency fixture')
  db.add(conv)
  await db.flush()
  ids.append(conv.id)
  if operation=='owners':
   await conversation_execution.acquire(db,conv.id,'api',uuid.uuid4().hex)
  else:
   db.add(ToolCall(id=f'{conv.id}:old',conversation_id=conv.id,tool='read_text_file',status='queued'))
 await db.commit()
 return {'ids':ids}

original_pages=agent_recovery.conversation_pages
async def observe_pages(db,column,*conditions):
 async for ids in original_pages(db,column,*conditions):
  if agent_role=='witness' and column is ToolCall.conversation_id:
   (root/'orphan-candidates.json').write_text(json.dumps(ids))
   deadline=time.monotonic()+45
   while not (root/'orphan-resume').exists():
    if time.monotonic()>deadline:raise TimeoutError('Owner admission never completed')
    await asyncio.sleep(.03)
  if agent_role=='recovery' and column is ConversationExecution.conversation_id:
   with (root/'owner-pages.jsonl').open('a') as log:log.write(json.dumps(ids)+'\n')
  elif agent_role=='recovery' and column is not ToolCall.conversation_id:
   with (root/'legacy-pages.jsonl').open('a') as log:log.write(json.dumps(ids)+'\n')
  yield ids
agent_recovery.conversation_pages=observe_pages

@app.get('/api/v1/testing/agent-state/{cid}')
async def agent_state(cid:int,user=Depends(get_current_user),db=Depends(get_db)):
 conv=await db.get(Conversation,cid);case=await get_owned_case(conv.case_id,user,db,write=False)
 row=await db.get(ConversationExecution,cid)
 tools=(await db.execute(select(ToolCall.id,ToolCall.status).where(ToolCall.conversation_id==cid))).all()
 return {'pid':os.getpid(),'phase':row.phase if row else None,'version':row.version if row else None,
  'case_stage':case.workflow_state,'registered':cid in agent._RUN_CANCELLATIONS,'tools':dict(tools),
  'startup':app.state.agent_recovery}
'''


def peer(api,store,role):
 value=IsolatedServer('agent-recovery-'+role,app='agent_peer_fixture:app')
 shared={key:value for key,value in api.env.items() if key.startswith('VIBEFLOW_')}
 if store:shared.update(store.settings())
 shared.update(VIBEFLOW_AGENT_SHARED_ROOT=str(api.root),VIBEFLOW_AGENT_ROLE=role)
 live=LIVE.replace('root=Path(__file__).parent',"root=Path(os.environ['VIBEFLOW_AGENT_SHARED_ROOT'])")
 assert live!=LIVE
 (value.root/'agent_peer_fixture.py').write_text('import os\nos.environ.update('+repr(shared)+')\n'+live+FIXTURE,encoding='utf8')
 return value


def stream(api,path,body,label,configuration):
 events=[]
 try:
  with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=180,trust_env=False) as client:
   with client.stream('POST',path,json=body) as response:
    response.raise_for_status()
    for line in response.iter_lines():
     if line.startswith('data:'):events.append(json.loads(line[5:]))
 finally:
  encoded=json.dumps(events,ensure_ascii=False,indent=2);assert configuration['key'] not in encoded
  (api.root/(label+'-events.json')).write_text(encoded,encoding='utf8')
 return events


def scenario(api,*,configuration,store=None,**_):
 api.create_case('Actual Agent checkpoint in registered engineering workflow')
 geometry='import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n'
 seeded=put(api,'A-geo/geometry.py',geometry)
 if seeded.get('auto_preview_job'):api.wait_job(seeded['auto_preview_job'])
 saved=api.call('GET',api.prefix+'/caseDict')
 flow=api.call('POST',api.prefix+'/workflow-runs',{'target':'geometry','expected_revision':saved['dict_revision'],
  'parameters':{'geometry_mode':'export'},'budget':{'timeout_seconds':120,'max_log_bytes':16777216}})
 def awaiting():
  value=api.call('GET','/workflow-runs/'+flow['id'])
  if value['status'] in {'failed','interrupted'}:raise AssertionError(value)
  return value if value['status']=='awaiting_confirmation' else None
 waiting=wait_for(awaiting,'Actual registered geometry did not await confirmation',180)
 profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual Agent recovery',
  'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
 startup_route='/testing/agent-startup/'+str(api.case_id)+'/'
 owners=api.call('POST',startup_route+'owners',{})['ids']
 conversation=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Actual interrupted model checkpoint',
  'permission_mode':'ask','provider_profile_id':profile['id'],'model':configuration['model']})
 cid=conversation['id'];route='/conversations/'+str(cid)
 (api.root/'agent-hold.json').write_text(json.dumps({'cid':cid,'stage':'checkpoint'}))
 crash=peer(api,store,'crash');recovery=peer(api,store,'recovery');failed_request=None
 try:
  with crash:
   crash.token=api.token
   prompt=('第一行写MODEL_NOTE_，紧接你自行选择的8位大写字母或数字。'
    '然后用三个自然段、总计约300字说明有限体积法。无需编号，不用工具，直接输出正文。')
   with ThreadPoolExecutor(max_workers=1) as pool:
    request=pool.submit(stream,crash,route+'/messages',{'content':prompt,'provider_profile_id':profile['id'],
     'model':configuration['model']},'actual-checkpoint-crash',configuration)
    try:
     marker=api.root/'agent-checkpoint-witness.json'
     def reached():
      if request.done() and not marker.exists():raise AssertionError('Real model ended before checkpoint hold: '+str(request.result()))
      return marker.exists()
     wait_for(reached,'Actual DeepSeek did not persist its checkpoint')
     witness=json.loads(marker.read_text());assert witness['actual_SQL_checkpoint_committed']
     matches=re.findall(r'MODEL_NOTE_[A-Z0-9]{8}(?![A-Z0-9])',witness['actual_content']);assert matches,witness['actual_content'][:200]
     note=matches[0]
     orphan,changing=api.call('POST',startup_route+'orphan',{})['ids']
     witness_api=peer(api,store,'witness')
     def inspect_peer():
      with witness_api:
       witness_api.token=api.token
       return witness_api.call('GET','/testing/agent-state/'+str(cid))
     with ThreadPoolExecutor(max_workers=1) as startup_pool:
      starting=startup_pool.submit(inspect_peer)
      try:
       wait_for(lambda:starting.result() if starting.done() else (api.root/'orphan-candidates.json').exists(),
        'Peer never selected orphan tool conversations',45)
       assert json.loads((api.root/'orphan-candidates.json').read_text())==[orphan,changing]
       api.call('POST',startup_route+'claim',{'cid':changing})
      finally:(api.root/'orphan-resume').touch()
      peer_state=starting.result(timeout=60)
     assert peer_state['phase']=='active'
     def tool_states(identifier):
      return api.call('GET','/testing/agent-state/'+str(identifier))['tools']
     assert tool_states(orphan)=={f'{orphan}:old':'interrupted'}
     assert tool_states(changing)=={f'{changing}:old':'executed',f'{changing}:new':'running'}
     live=api.call('GET',route+'/recovery');assert live['required'] and not live['can_recover'] and live['owner_state']['state']=='match'
     refused=api.call('POST',route+':recover',{'expected_version':live['version'],'expected_evidence_sha256':live['evidence_sha256']},expect=409)
     assert refused['detail']['code']=='recovery_required'
     original=api.call('GET',route)
     live_states=[e['payload']['status'] for e in original['events'] if e['type']=='run_state' and e['payload'].get('run_id')==witness['run_id']]
     assert live_states==['started'],live_states
     assert not any(e['type']=='message' and e['payload'].get('role')=='assistant' and note in e['payload'].get('content','') for e in original['events'])
     launcher=kill_owned_api(crash,witness['pid'])
     try:request.result(timeout=10)
     except Exception as error:failed_request=type(error).__name__
     else:raise AssertionError('Actual killed stream returned normally')
    finally:
     (api.root/'agent-release-checkpoint').touch()
     if not request.done() and crash.proc.poll() is None:
      crash.call('POST',route+'/messages:cancel')
  plan=api.call('GET',route+'/recovery');assert plan['can_recover'] and plan['owner_state']['state']=='exited'
  body={'expected_version':plan['version'],'expected_evidence_sha256':plan['evidence_sha256']}
  historical=api.call('POST',startup_route+'legacy',{})['ids']
  with recovery:
   recovery.token=api.token
   state=recovery.call('GET','/testing/agent-state/'+str(cid));assert state['phase']=='idle' and state['case_stage']=='A'
   pages=[json.loads(line) for line in (api.root/'owner-pages.jsonl').read_text().splitlines()]
   assert len(pages)>=2 and len(pages[0])==64 and all(0<len(page)<=64 for page in pages)
   assert [identifier for page in pages for identifier in page]==sorted([*owners,cid,changing])
   assert state['startup']=={'checked':67,'recovered':1,'skipped':66,'replayed':0,'failed':0,'first_errors':[]}
   assert cid not in pages[0]
   assert tool_states(changing)[f'{changing}:new']=='running'
   legacy_pages=[json.loads(line) for line in (api.root/'legacy-pages.jsonl').read_text().splitlines()]
   assert [identifier for page in legacy_pages for identifier in page]==historical[:-1],legacy_pages
   legacy_states=api.call('POST',startup_route+'legacy-status',{})
   assert legacy_states=={'run_state.started':34,'request_state.started':33,'run_state.completed':1,
    'run_state.interrupted':33,'request_state.interrupted':33},legacy_states
   history=api.call('GET',route)
   receipts=[e for e in history['events'] if e['type']=='system' and e['payload'].get('kind')=='agent_recovery']
   assert len(receipts)==1 and note in receipts[0]['payload']['partial_excerpt']
   receipt=receipts[0]['payload'];original_owner=receipt['original_execution']
   from app.services.durable_events import json_text
   assert hashlib.sha256(json_text(original_owner).encode()).hexdigest()==body['expected_evidence_sha256']
   assert original_owner['backend']=='api' and original_owner['resource_id']==witness['run_id']
   assert json.loads(original_owner['owner_identity_json'])['pid']==witness['pid']
   states=[e['payload']['status'] for e in history['events'] if e['type']=='run_state' and e['payload'].get('run_id')==witness['run_id']]
   assert states==['started','interrupted'],states
   replay=recovery.call('POST',route+':recover',body);assert replay['replayed']
   events=stream(recovery,route+'/messages',{'content':'停止长输出。中断回复第一行你自己生成的MODEL_NOTE_完整标识是什么？请从已恢复的正文片段找回，只回复该标识，不要使用工具。',
    'provider_profile_id':profile['id'],'model':configuration['model']},'actual-checkpoint-resumed',configuration)
   answer=''.join(e.get('text','') for e in events if e.get('type')=='text.delta')
   assert note in answer and any(e.get('type')=='run.completed' for e in events),answer
   assert not any(e.get('type') in {'stream.error','request.failed','request.timed_out'} for e in events)
   observations=[json.loads(line) for line in (api.root/'agent-request-observations.jsonl').read_text().splitlines()]
   assert any(note in row['recovery_note_markers'] and row['role']=='recovery' for row in observations)
   assert api.call('GET','/workflow-runs/'+flow['id'])==waiting
   assert api.call('GET',api.prefix+'/caseDict')['workflow_state']=='A'
   confirmation=next(s for s in waiting['steps'] if s['key']=='confirm_B')
   write(api,'POST',api.prefix+'/caseDict:advance',{'state':'B','confirmed':True,
    'expected_revision':confirmation['confirmation_revision'],'workflow_step_id':confirmation['id'],
    'expected_step_version':confirmation['version']})
   assert api.call('GET','/workflow-runs/'+flow['id'])['status']=='succeeded'
   before=recovery.call('GET','/testing/agent-state/'+str(cid))
   assert recovery.call('POST',route+':recover',body)['replayed']
   after=recovery.call('GET','/testing/agent-state/'+str(cid));assert before==after
   # A second real stream is allowed to finish after a separate API starts.
   continuing=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Live peer startup',
    'permission_mode':'ask','provider_profile_id':profile['id'],'model':configuration['model']})['id']
   continuing_route=f'/conversations/{continuing}'
   (api.root/'agent-hold.json').write_text(json.dumps({'cid':continuing,'stage':'finish','role':'recovery'}))
   standby=peer(api,store,'standby')
   with ThreadPoolExecutor(max_workers=1) as pool:
    operation=pool.submit(stream,recovery,continuing_route+'/messages',{'content':'请用约200字解释有限体积法如何守恒，直接给正文，无需使用工具。',
     'provider_profile_id':profile['id'],'model':configuration['model']},'peer-startup-completed',configuration)
    try:
     marker=api.root/'agent-finish-witness.json'
     wait_for(lambda:operation.result() if operation.done() else marker.is_file(),'Continuing stream never persisted its checkpoint')
     active_run=json.loads(marker.read_text())['run_id']
     with standby:
      standby.token=api.token
      held=standby.call('GET',continuing_route)
      def states(history):
       return [e['payload']['status'] for e in history['events'] if e['type']=='run_state' and e['payload'].get('run_id')==active_run]
      assert states(held)==['started']
      (api.root/'agent-release-finish').touch()
      finished=operation.result(timeout=120)
      final=standby.call('GET',continuing_route)
      assert states(final)==['started','completed'] and any(e['type']=='run.completed' for e in finished)
      assert next(e['payload']['content'] for e in final['events'] if e['type']=='message' and e['payload'].get('role')=='assistant')
      assert not any(e['type']=='system' and e['payload'].get('kind')=='agent_recovery' for e in final['events'])
    finally:(api.root/'agent-release-finish').touch()
  assert crash.proc.poll() is not None and recovery.proc.poll() is not None
 finally:
  (api.root/'agent-release-checkpoint').touch();(api.root/'agent-hold.json').unlink(missing_ok=True)
 report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'actual_DeepSeek_checkpoint':True,
  'actual_generated_model_note':note,'persisted_content_sha256':witness['content_sha256'],
  'actual_API_pid_killed':witness['pid'],'verified_launcher_pid':launcher,'actual_interrupted_HTTP':failed_request,
  'live_owner_takeover_refused':True,'startup_recovered_exited_API_owner':True,'single_recovery_receipt':True,
  'owner_scan_page_sizes':[len(page) for page in pages],'earlier_live_owners':len(owners),
  'startup_summary':state['startup'],
  'legacy_page_sizes':[len(page) for page in legacy_pages],'legacy_lifecycle_totals':legacy_states,
  'live_model_run_preserved_during_peer_startup':True,'tool_owner_changed_after_selection':True,
  'separate_live_stream_completed_after_peer_startup':True,
  'legacy_orphan_interrupted':True,'new_owner_tool_preserved':True,
  'actual_model_remembered_checkpoint_note':True,'outgoing_resume_context_observed':True,
  'original_owner_snapshot_and_digest_preserved':True,
  'workflow_preserved_and_B_confirmation_explicit':True,'repeated_old_recovery_no_effect':True,
  'scope':'Actual model checkpoint and registered geometry confirmation; approval crash/other engineering gates remain pending'}
 (api.root/'agent-checkpoint-recovery.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 api.create_case('Independent checkpoint recovery evidence readback')
 return report


if __name__=='__main__':asyncio.run(main(selected_factory=lambda configuration:partial(scenario,configuration=configuration),fixture_override=FIXTURE,store_volume_max=16))
