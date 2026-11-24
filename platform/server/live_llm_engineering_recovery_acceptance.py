"""Real legacy failure, model checkpoint/repair, explicit approval and recompute.

Private routes seed only the initial defective input and observe durable rows.
Model replies, tool choices and native engineering outcomes are never replaced.
"""
import ast,asyncio,hashlib,json,re,uuid
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from urllib.parse import quote
import live_llm_agent_recovery_acceptance as checkpoint
import live_llm_workflow_plan_migration_acceptance as migration
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_failure_fallback_acceptance import kill_owned_api,wait_for


ENGINEERING_FIXTURE=r'''
from app.models import DurableEvent
from app.services import file_transactions,workflow_fallback
from app.services.case_write_capacity import committing

@app.post('/api/v1/cases/{case_id}/testing/engineering-input')
async def engineering_input(case_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=True)
 content=body['content']
 assert 'VF_REAL_ENGINEERING_FAILURE' in content
 async with committing(db,case):
  await file_transactions.write(db,case,{'A-geo/geometry.py':content.encode('utf8')})
 return {'seeded_defective_input':True,'automatic_preview_started':False,
  'scope':'Initial test input only; subsequent repair must come from the real model'}

@app.get('/api/v1/cases/{case_id}/testing/engineering-fallbacks')
async def engineering_fallbacks(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=False)
 rows=list(await db.scalars(select(DurableEvent).where(DurableEvent.case_id==case_id,
  DurableEvent.type==workflow_fallback.APPLIED)))
 from app.services.job_write_checkpoint import validate
 for row in rows:
  attempt=await db.get(JobAttempt,row.attempt_id);job=await db.get(Job,attempt.job_id)
  validate(case,job,attempt)
 return {'stage':case.workflow_state,'write_drain_verified':True,'applied':[json.loads(row.payload_json) for row in rows]}
'''
CHECKPOINT_CONDITION="and etype=='run_checkpoint' and len(payload.get('content',''))>=32):"
NOTE_CONDITION="and etype=='run_checkpoint' and re.search(r'MODEL_NOTE_[A-Z0-9]{8}(?![A-Z0-9])',payload.get('content',''))):"
assert CHECKPOINT_CONDITION in checkpoint.FIXTURE
FIXTURE=migration.FIXTURE+checkpoint.FIXTURE.replace(CHECKPOINT_CONDITION,NOTE_CONDITION)+ENGINEERING_FIXTURE


def peer(api,store,role):
 value=checkpoint.peer(api,store,role)
 fixture=value.root/'agent_peer_fixture.py'
 fixture.write_text(fixture.read_text(encoding='utf8').replace(CHECKPOINT_CONDITION,NOTE_CONDITION),encoding='utf8')
 return value


def stream(api,path,body,label,configuration):
 events=checkpoint.stream(api,path,body,label,configuration)
 assert not any(e.get('type') in {'stream.error','request.failed','request.timed_out'} for e in events),[e.get('type') for e in events]
 return events


def scenario(api,*,configuration,store=None,**_):
 checks=[]
 def save():
  (api.root/'engineering-recovery-progress.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf8')
 def record(value):checks.append(value);save()
 def content(path):return api.call('GET',api.prefix+'/file?path='+quote(path))['content']
 def flow(identifier,status):
  def observed():
   result=api.call('GET','/workflow-runs/'+identifier)
   if result['status'] in {'failed','interrupted','cancelled'} and result['status']!=status:raise AssertionError(result)
   return result if result['status']==status else None
  return wait_for(observed,'Actual engineering workflow did not reach '+status,180)
 def settled_failure(job_id):
  failed=api.wait_job(job_id,expected='failed')
  assert failed['attempt']['process_stop']['verified']
  def applied():
   value=api.call('GET',api.prefix+'/testing/engineering-fallbacks')
   assert value['write_drain_verified']
   return next((row for row in value['applied'] if row['job_id']==job_id),None)
  receipt=wait_for(applied,'Failure fallback was not durably applied')
  return failed,receipt
 def timeline(job_id):
  raw=api.call('GET',api.prefix+'/raw?path=0-caseDict/case-events.jsonl',raw=True)
  return [json.loads(line) for line in raw.decode().splitlines() if line.strip()
   and json.loads(line).get('type')=='workflow.fallback' and json.loads(line).get('job_id')==job_id]
 def body(profile,text):return {'content':text,'provider_profile_id':profile['id'],'model':configuration['model']}
 def proposals(events):return [e['tool_call'] for e in events if e.get('type')=='approval.requested']
 def unchanged_histories():
  assert api.call('GET',old_route+'/testing/history')==old_history
  assert api.call('GET',failed_route+'/testing/history')==failed_history
 def confirmation(value):return next(s for s in value['steps'] if s['key']=='confirm_B')
 def advance_payload(value,confirmed=True):
  step=confirmation(value)
  return {'state':'B','confirmed':confirmed,'expected_revision':step['confirmation_revision'],
   'workflow_step_id':step['id'],'expected_step_version':step['version']}

 api.create_case('Real Agent repair from failed migrated engineering workflow')
 good='import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n'
 seeded=put(api,'A-geo/geometry.py',good)
 preview=api.wait_job(seeded['auto_preview_job']) if seeded.get('auto_preview_job') else api.wait_job(write(api,'POST',api.prefix+'/geometry:preview',{})['job_id'])
 assert preview['attempt']['outputs']['publication']['status']=='committed'
 api.advance('B')
 old_counter=api.root/'actual-engineering-legacy-executions'
 old_code='from pathlib import Path\nwith Path('+repr(str(old_counter))+').open("a") as f:f.write("once\\n")\nraise SystemExit(23)\n'
 put(api,'Ref/legacy.py',old_code)
 legacy=write(api,'POST',api.prefix+'/testing/legacy-workflow',{})
 old_failed,old_receipt=settled_failure(legacy['job_id'])
 old_route='/workflow-runs/'+legacy['run_id'];old_flow=flow(legacy['run_id'],'failed')
 old_history=api.call('GET',old_route+'/testing/history')
 assert old_failed['exit_code']==23 and not old_receipt['result']['regressed']
 assert api.call('GET',api.prefix+'/caseDict')['workflow_state']=='B'
 counter=api.root/'actual-engineering-registered-executions'
 defective=('import cadquery as cq\nfrom pathlib import Path\ndef build_geometry(ctx):\n'
  '    with Path('+repr(str(counter))+').open("a") as stream:stream.write("once\\n")\n'
  '    raise RuntimeError("VF_REAL_ENGINEERING_FAILURE")\n'
  '    return {"box":cq.Workplane("XY").box(1,1,1)}\n')
 write(api,'POST',api.prefix+'/testing/engineering-input',{'content':defective})
 plan=api.call('GET',old_route+'/migration')
 migration_body={'expected_version':plan['source_run_version'],'expected_revision':plan['current_revision'],
  'expected_event_sequence':plan['current_event_sequence'],'expected_source_plan_sha256':plan['source_plan_sha256'],
  'confirmed_reset':True,'target':'geometry','run_dir':'C-run/case-1','parameters':{'geometry_mode':'export'},
  'budget':{'timeout_seconds':120,'max_log_bytes':16777216}}
 migration_key=uuid.uuid4().hex
 migrated=migration.submit(api,old_route+':migrate',migration_body,migration_key)
 assert migrated['legacy_migration']['source_run_id']==legacy['run_id']
 failed_flow=flow(migrated['id'],'failed');failed_route='/workflow-runs/'+migrated['id']
 failed_step=next(s for s in failed_flow['steps'] if s['key']=='geometry')
 assert len(failed_step['attempts'])==1
 failed_job,failed_receipt=settled_failure(failed_step['attempts'][0]['job_id'])
 assert 'VF_REAL_ENGINEERING_FAILURE' in failed_job['log_backlog'] and failed_job['attempt']['handler']['key']=='geometry.export'
 assert not failed_receipt['result']['regressed'] and counter.read_text().splitlines()==['once']
 assert api.call('GET',api.prefix+'/caseDict')['workflow_state']=='A'
 failed_history=api.call('GET',failed_route+'/testing/history')
 assert len(timeline(legacy['job_id']))==len(timeline(failed_job['id']))==1
 record({'legacy_run':legacy['run_id'],'migrated_run':migrated['id'],'actual_legacy_exit_code':23,
  'actual_registered_failure':failed_job['id'],'failure_does_not_advance_A':True,'timeline_per_failed_job':1})

 profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Real engineering repair recovery',
  'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
 conv=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Actual failed geometry repair',
  'permission_mode':'approve_writes','provider_profile_id':profile['id'],'model':configuration['model']})
 cid=conv['id'];route='/conversations/'+str(cid)
 hold=api.root/'agent-hold.json';hold.write_text(json.dumps({'cid':cid,'stage':'checkpoint'}))
 source=peer(api,store,'crash');recovery=peer(api,store,'recovery')
 try:
  with source:
   source.token=api.token
   prompt=('先仅调用read_text_file读取A-geo/geometry.py。读完后第一行输出MODEL_NOTE_，紧接你自行选择的8位大写字母或数字；'
    '再用约300字分析本次实际失败的直接原因及最小修复方案。只分析，不提案、不修改、不运行命令。实际失败日志末尾：\n'+failed_job['log_backlog'][-1800:])
   with ThreadPoolExecutor(max_workers=1) as pool:
    future=pool.submit(stream,source,route+'/messages',body(profile,prompt),'engineering-analysis-interrupted',configuration)
    try:
     marker=api.root/'agent-checkpoint-witness.json'
     def reached():
      if future.done() and not marker.exists():raise AssertionError('Actual model finished without held checkpoint: '+str(future.result()))
      return marker.exists()
     wait_for(reached,'Real engineering analysis did not persist its checkpoint')
     witness=json.loads(marker.read_text());assert witness['actual_SQL_checkpoint_committed']
     notes=re.findall(r'MODEL_NOTE_[A-Z0-9]{8}(?![A-Z0-9])',witness['actual_content']);assert notes
     note=notes[0]
     history=api.call('GET',route)
     assert any(e['type']=='tool_result' and e['payload'].get('tool')=='read_text_file' and 'VF_REAL_ENGINEERING_FAILURE' in e['payload'].get('result','') for e in history['events'])
     live=api.call('GET',route+'/recovery');assert not live['can_recover'] and live['owner_state']['state']=='match'
     api.call('POST',route+':recover',{'expected_version':live['version'],'expected_evidence_sha256':live['evidence_sha256']},expect=409)
     launcher=kill_owned_api(source,witness['pid'])
     try:future.result(timeout=10)
     except Exception:pass
     else:raise AssertionError('Killed model stream ended normally')
    finally:
     (api.root/'agent-release-checkpoint').touch()
     if not future.done() and source.proc.poll() is None:source.call('POST',route+'/messages:cancel')
  unchanged_histories()
  assert content('A-geo/geometry.py')==defective
  plan=api.call('GET',route+'/recovery');assert plan['can_recover']
  old_recovery={'expected_version':plan['version'],'expected_evidence_sha256':plan['evidence_sha256']}
  with recovery:
   recovery.token=api.token
   history=api.call('GET',route)
   receipts=[e for e in history['events'] if e['type']=='system' and e['payload'].get('kind')=='agent_recovery']
   assert len(receipts)==1 and note in receipts[0]['payload']['partial_excerpt']
   events=stream(recovery,route+'/messages',body(profile,
    '从恢复正文找回并先复述你自己生成的完整MODEL_NOTE_标识。然后仅调用一次propose_file_update或propose_text_replacement，'
    '修复刚读过的A-geo/geometry.py中阻止build_geometry返回的异常语句；保留原有实际执行计数代码和1x1x1盒体。'
    '不要读取其他文件，不运行命令，不修改caseDict。等待用户审批，收到批准结果后只简短说明，不再调用工具。'),
    'engineering-real-repair-proposal',configuration)
   assert note in ''.join(e.get('text','') for e in events if e.get('type')=='text.delta')
   pending=proposals(events);assert len(pending)==1
   repair=pending[0];assert repair['tool'] in {'propose_file_update','propose_text_replacement'} and repair['args']['path']=='A-geo/geometry.py'
   assert repair['args']['baseline_sha256']==hashlib.sha256(defective.encode()).hexdigest()
   assert content('A-geo/geometry.py')==defective
   approved=stream(recovery,route+'/tool-calls/'+repair['id']+':approve',{},'engineering-real-repair-approved',configuration)
   assert any(e.get('type')=='tool.result' and e['ok'] for e in approved) and any(e.get('type')=='run.completed' for e in approved)
   repaired=content('A-geo/geometry.py');assert repaired!=defective;ast.parse(repaired)
   recovery.call('POST',route+'/tool-calls/'+repair['id']+':approve',{},expect=409)
   observations=[json.loads(s) for s in (api.root/'agent-request-observations.jsonl').read_text().splitlines()]
   assert any(row['role']=='recovery' and note in row['recovery_note_markers'] for row in observations)
   unchanged_histories()
   assert counter.read_text().splitlines()==['once'] and api.call('GET',api.prefix+'/caseDict')['workflow_state']=='A'
   record({'actual_model_note':note,'actual_API_pid_killed':witness['pid'],'verified_launcher_pid':launcher,
    'actual_model_read_failed_input':True,'actual_checkpoint_used_by_model':True,'real_repair_tool':repair['tool'],
    'approval_explicit':True,'old_tool_reapproval_refused':True,'old_histories_unchanged':True})

   source_run=api.call('GET',failed_route);dictionary=api.call('GET',api.prefix+'/caseDict')
   recompute_body={'expected_version':source_run['version'],'expected_revision':dictionary['dict_revision'],'confirmed_reset':True}
   recompute_key=uuid.uuid4().hex
   new=migration.submit(api,failed_route+':recompute',recompute_body,recompute_key)
   assert new['recomputation']['source_run_id']==migrated['id']
   waiting=flow(new['id'],'awaiting_confirmation')
   step=next(s for s in waiting['steps'] if s['key']=='geometry');assert len(step['attempts'])==1
   actual=api.wait_job(step['attempts'][0]['job_id'])
   assert actual['attempt']['outputs']['publication']['status']=='committed'
   assert actual['attempt']['workspace']['files']['A-geo/geometry.py']['sha256']==hashlib.sha256(repaired.encode()).hexdigest()
   raw=api.call('GET',api.prefix+'/raw?path='+quote('A-geo/exports/box.step'),raw=True)
   artifact=api.root/'actual-model-repaired-box.step';artifact.write_bytes(raw)
   import cadquery as cq
   volume=cq.importers.importStep(str(artifact)).val().Volume();assert abs(volume-1)<1e-9
   assert counter.read_text().splitlines()==['once','once'] and old_counter.read_text().splitlines()==['once']
   assert api.call('GET',api.prefix+'/caseDict')['workflow_state']=='A'
   denied=api.call('POST',api.prefix+'/caseDict:advance',advance_payload(waiting,False),expect=409)
   assert denied['detail']['code']=='confirmation_required',denied
   record({'recomputed_run':new['id'],'actual_repaired_job':actual['id'],'actual_STEP_volume':volume,
    'registered_native_executions':2,'legacy_native_executions':1,'unconfirmed_B_refused':True})

   # A consistent dictionary update must reach the workflow gate, not fail schema validation first.
   from app.services.casedict import set_workflow_state
   dictionary_path='0-caseDict/caseDict';before=content(dictionary_path);after=set_workflow_state(before,'B')
   assert after!=before
   stage_events=stream(recovery,route+'/messages',body(profile,
    '这是隔离验收中的受保护阶段写入测试。仅提出一次propose_file_update，不执行工程命令，不读取其他文件；'
    'path为0-caseDict/caseDict，完整content如下JSON字符串（已同步派生status，使字典格式合法）：\n'+
    json.dumps(after,ensure_ascii=False)+
    '\n等待批准。若平台拒绝该写入，只说明拒绝，不重试，不再调用工具。'),
    'engineering-real-stage-gate-proposal',configuration)
   stage_proposals=proposals(stage_events)
   if stage_proposals:
    assert len(stage_proposals)==1;stage_proposal=stage_proposals[0]
    assert stage_proposal['tool']=='propose_file_update' and stage_proposal['args']['path']==dictionary_path
    assert stage_proposal['args']['content']==after
    refused=stream(recovery,route+'/tool-calls/'+stage_proposal['id']+':approve',{},'engineering-real-stage-gate-refused',configuration)
    failure=next(e for e in refused if e.get('type')=='tool.result')
    assert not failure['ok'] and 'workflowState' in failure['result'],failure
    stage_outcome='actual_model_proposal_refused_by_shared_workflow_gate'
   else:
    assert any(e.get('type')=='run.completed' for e in stage_events)
    stage_outcome='actual_model_declined_protected_stage_proposal'
   assert content(dictionary_path)==before
   current_revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
   ordinary=api.call('PUT',api.prefix+'/caseDict',{'content':after,'expected_revision':current_revision},expect=409)
   assert 'workflowState' in str(ordinary),ordinary
   assert api.call('GET',api.prefix+'/caseDict')['workflow_state']=='A'
   record({'stage_gate':stage_outcome,'ordinary_dictionary_stage_bypass_refused':True})

   error_id='recovery-review-'+uuid.uuid4().hex
   current=api.call('GET',api.prefix+'/caseDict')
   write(api,'PUT',api.prefix+'/caseDict/block',{'block':'errors','expected_revision':current['dict_revision'],
    'entries':{'entries':{error_id:{'title':'Recovered engineering result requires verification','stage':'geometry',
     'category':'geometry','severity':'error','status':'open','blocking':True}},
     'summary':{'total':1,'open':1,'blocking':1,'resolved':0}}})
   current=api.call('GET','/workflow-runs/'+new['id']);confirm=confirmation(current)
   current=write(api,'POST','/workflow-runs/'+new['id']+'/steps/'+confirm['id']+':refresh-confirmation',{'expected_version':confirm['version']})
   denied=api.call('POST',api.prefix+'/caseDict:advance',advance_payload(current),expect=409)
   assert '未结清错误' in str(denied) or '阻断错误' in str(denied),denied
   assert api.call('GET',api.prefix+'/caseDict')['workflow_state']=='A'
   revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
   write(api,'PATCH',api.prefix+'/caseDict/errors/'+error_id,{'status':'verified','resolution':'Actual model repair approved and registered recomputation succeeded',
    'verification':'Actual STEP volume 1 and current publication input digest checked','expected_revision':revision})
   current=api.call('GET','/workflow-runs/'+new['id']);confirm=confirmation(current)
   current=write(api,'POST','/workflow-runs/'+new['id']+'/steps/'+confirm['id']+':refresh-confirmation',{'expected_version':confirm['version']})
   write(api,'POST',api.prefix+'/caseDict:advance',advance_payload(current))
   assert flow(new['id'],'succeeded')['status']=='succeeded'
   before_replay=api.call('GET',api.prefix+'/caseDict')
   assert recovery.call('POST',route+':recover',old_recovery)['replayed']
   assert migration.submit(api,old_route+':migrate',migration_body,migration_key)['id']==migrated['id']
   assert migration.submit(api,failed_route+':recompute',recompute_body,recompute_key)['id']==new['id']
   assert api.call('GET',api.prefix+'/caseDict')==before_replay
   unchanged_histories()
   assert len(timeline(legacy['job_id']))==len(timeline(failed_job['id']))==1
   assert counter.read_text().splitlines()==['once','once'] and old_counter.read_text().splitlines()==['once']
   assert len(api.call('GET',api.prefix+'/workflow-runs'))==3
   record({'recomputed_run':new['id'],'actual_repaired_job':actual['id'],'actual_STEP_volume':volume,
    'source_lineage':[legacy['run_id'],migrated['id'],new['id']],'stage_gate':stage_outcome,
    'ordinary_dictionary_stage_bypass_refused':True,'blocking_error_refused_after_fresh_confirmation':True,
    'error_explicitly_verified':True,'B_confirmation_explicit':True,'old_histories_unchanged':True,
    'fallback_timeline_per_failed_job':1,'legacy_native_executions':1,'registered_native_executions':2,
    'old_recovery_and_idempotent_replays_do_not_regress_B':True})
  assert source.proc.poll() is not None and recovery.proc.poll() is not None
 finally:
  (api.root/'agent-release-checkpoint').touch();hold.unlink(missing_ok=True)
 report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'checks':checks,
  'scope':'Complete geometry failure/migration/real Agent checkpoint and approved repair/recompute/error-confirmation chain; other failure-stage policy coverage remains pending'}
 (api.root/'engineering-recovery.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
 api.create_case('Independent real engineering recovery evidence readback')
 return report


if __name__=='__main__':asyncio.run(main(selected_factory=lambda configuration:partial(scenario,configuration=configuration),fixture_override=FIXTURE,store_volume_max=16))
