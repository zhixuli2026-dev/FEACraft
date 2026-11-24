"""Real parallel model proposals survive restart and wait for every approval."""
import asyncio,hashlib,json,uuid
from functools import partial
from urllib.parse import quote
import live_llm_agent_recovery_acceptance as checkpoint
from live_llm_job_repeat_acceptance import main,put
from live_llm_failure_fallback_acceptance import kill_owned_api


def scenario(api,*,configuration,store=None,**_):
 api.create_case('Real pending approvals and goal restart')
 paths=['Ref/parallel-first.txt','Ref/parallel-second.txt']
 before={p:'BEFORE_'+uuid.uuid4().hex for p in paths};after={p:'AFTER_'+uuid.uuid4().hex for p in paths}
 for path in paths:put(api,path,before[path])
 profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Real parallel approvals',
  'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
 conv=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Actual parallel proposals',
  'permission_mode':'approve_writes','provider_profile_id':profile['id'],'model':configuration['model']})
 cid=conv['id'];route='/conversations/'+str(cid)
 def http_count():
  p=api.root/'upstream.jsonl'
  return sum(json.loads(s).get('kind')=='http' for s in p.read_text().splitlines()) if p.exists() else 0
 def content(path):return api.call('GET',api.prefix+'/file?path='+quote(path))['content']
 def stream(server,endpoint,body,label):
  events=checkpoint.stream(server,endpoint,body,label,configuration)
  assert not any(e.get('type') in {'stream.error','request.failed','request.timed_out'} for e in events),events
  return events
 source=checkpoint.peer(api,store,'parallel-source');recovery=checkpoint.peer(api,store,'parallel-recovery')
 with source:
  source.token=api.token
  events=stream(source,route+'/messages',{'provider_profile_id':profile['id'],'model':configuration['model'],
   'content':'请登记活动目标并提出两个独立文件修改。在同一回复中调用update_goal（objective为批准两个独立文件修改，status为active，'
    'budget为max_turns=3,max_tool_calls=8），以及两个propose_file_update，具体path和完整content为：'+json.dumps(after)+
    '。两个文件提案必须同一回复一起发出，不要只发第一个就等待，不要读文件或调用其他工具。'
    '随后等待用户逐一批准；两个工具的结果全部到齐后，只回复一句结果，不再调用工具。'},'parallel-real-proposals')
  proposed=[e['tool_call'] for e in events if e.get('type')=='approval.requested']
  assert len(proposed)==2
  assert {p['args']['path'] for p in proposed}==set(paths)
  for p in proposed:
   assert p['tool']=='propose_file_update' and p['args']['content']==after[p['args']['path']]
   assert p['args']['baseline_sha256']==hashlib.sha256(before[p['args']['path']].encode()).hexdigest()
  history=api.call('GET',route)
  assert len(history['pending_tools'])==2 and history['current_goal']['status']=='active'
  assert all(content(p)==before[p] for p in paths)
  state=source.call('GET','/testing/agent-state/'+str(cid));assert state['phase']=='idle'
  launcher=kill_owned_api(source,state['pid'])
 with recovery:
  recovery.token=api.token
  history=api.call('GET',route);assert len(history['pending_tools'])==2
  calls=http_count()
  goal_events=stream(recovery,route+'/goal:run',{'max_turns':1},'parallel-goal-waits')
  assert any(e.get('type')=='goal.waiting_approval' for e in goal_events)
  assert http_count()==calls and api.call('GET',route)['events']==history['events']
  first,second=proposed
  first_events=stream(recovery,route+'/tool-calls/'+first['id']+':approve',{},'parallel-first-approved')
  assert any(e.get('type')=='tool.result' and e['ok'] for e in first_events)
  assert any(e.get('type')=='stream.done' and e.get('reason')=='awaiting_approval' for e in first_events)
  assert not any(e.get('type')=='run.completed' for e in first_events) and http_count()==calls
  assert content(first['args']['path'])==after[first['args']['path']]
  assert content(second['args']['path'])==before[second['args']['path']]
  waiting=api.call('GET',route);assert len(waiting['pending_tools'])==1 and waiting['pending_tools'][0]['id']==second['id']
  recovery.call('POST',route+'/tool-calls/'+first['id']+':approve',{},expect=409)
  assert http_count()==calls
  second_events=stream(recovery,route+'/tool-calls/'+second['id']+':approve',{},'parallel-second-approved')
  assert any(e.get('type')=='tool.result' and e['ok'] for e in second_events)
  assert any(e.get('type')=='run.completed' for e in second_events) and http_count()==calls+1
  final=api.call('GET',route);assert not final['pending_tools']
  assert all(content(p)==after[p] for p in paths)
  results=[e for e in final['events'] if e['type']=='tool_result' and e['payload'].get('approved')]
  assert len(results)==2 and all(e['payload']['ok'] for e in results)
  for p in proposed:recovery.call('POST',route+'/tool-calls/'+p['id']+':approve',{},expect=409)
  assert http_count()==calls+1
 assert source.proc.poll() is not None and recovery.proc.poll() is not None
 report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,
  'real_model_proposals':2,'actual_API_pid_killed':state['pid'],'verified_launcher_pid':launcher,
  'both_pending_survived_restart':True,'goal_waited_without_model_or_new_message':True,
  'first_approval_waited_without_model':True,'second_approval_real_continuation_calls':1,
  'approved_tool_results':2,'duplicate_approvals_refused_without_model':True,'owned_peers_exited':True}
 (api.root/'pending-approval-recovery.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 api.create_case('Independent parallel approval evidence readback')
 return report


if __name__=='__main__':asyncio.run(main(selected_factory=lambda configuration:partial(scenario,configuration=configuration),fixture_override=checkpoint.FIXTURE,store_volume_max=16))
