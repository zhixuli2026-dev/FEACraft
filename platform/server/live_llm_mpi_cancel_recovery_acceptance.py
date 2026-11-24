"""Real running workflow MPI cancellation and explicit recomputation.

Ranks are observed through their actual WSL birth identities. Inputs are real
files, not simulated execution. Only final evidence readback calls the model.
"""
import asyncio,hashlib,json,math,re,subprocess,time,uuid
from pathlib import Path
from urllib.parse import quote
from cae_fixtures import cavity_2d
from app.services.casedict import set_block
from app.services.owned_process import wsl_path
import live_llm_mpi_workflow_acceptance as bridge
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_failure_fallback_acceptance import wait_for,kill_owned_api
from live_llm_workflow_plan_migration_acceptance import submit
from testsupport import SERVER


EXTRA_FIXTURE=r'''
from app.services.case_write_capacity import committing
from app.services import file_transactions
from mpi_timing_observer import install as observe_mpi_stages
observe_mpi_stages(root,os.environ.get('VIBEFLOW_AGENT_ROLE','main'))

@app.post('/api/v1/cases/{case_id}/testing/mpi-reference-inputs')
async def mpi_reference_inputs(case_id:int,body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=True)
 start=body['start'];count=body['count']
 assert type(start) is int and type(count) is int and 0<=start<320 and 1<=count<=32 and start+count<=320
 files={'Ref/large-inputs/input-'+str(i)+'.txt':('MPI_INPUT_'+str(case.id)+'_'+str(i)).encode()
  for i in range(start,start+count)}
 async with committing(db,case):await file_transactions.write(db,case,files)
 return {'actual_input_files_written':len(files)}
'''
FIXTURE=bridge.FIXTURE+EXTRA_FIXTURE

INSPECT=r'''
import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import process_identity_native as identity
request=json.load(sys.stdin)
if 'probe' in request:
 print(json.dumps([identity.probe(row) for row in request['probe']]))
else:
 root=request['root'];assert identity.probe(root)['state']=='match'
 pending=[root['pid']];seen=set();result=[]
 while pending:
  pid=pending.pop()
  if pid in seen:continue
  assert len(seen)<128
  seen.add(pid)
  try:
   record=identity.observe(pid)
   comm=Path('/proc/'+str(pid)+'/comm').read_text().strip()
   mapping=Path('/proc/'+str(pid)+'/uid_map').read_text().strip()
   for task in Path('/proc/'+str(pid)+'/task').iterdir():
    try:pending.extend(map(int,(task/'children').read_text().split()))
    except FileNotFoundError:pass
   result.append({'identity':record,'comm':comm,'uid_map':mapping})
  except (FileNotFoundError,ProcessLookupError):pass
 print(json.dumps(result))
'''


def native_inspect(distro,request):
 assert re.fullmatch('[a-z0-9-]+',distro)
 result=subprocess.run(['wsl.exe','-d',distro,'--exec','python3','-I','-S','-c',INSPECT,
  wsl_path(SERVER/'app/services')],input=json.dumps(request).encode(),capture_output=True,
  timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
 assert result.returncode==0,result.stderr.decode('utf8','replace')
 return json.loads(result.stdout)


def actual_ranks(api,job_id):
 def observed():
  job=api.call('GET','/jobs/'+str(job_id))
  if job['status']=='queued':return None
  assert job['status']=='running',job['status']
  children=job['attempt']['child_processes']['children']
  active=[c for c in children.values() if c['purpose']=='mpi_run' and c['phase']=='running']
  if not active:return None
  assert len(active)==1
  child=active[0];identity=child['identity'];distro=identity['native']['distribution']
  members=native_inspect(distro,{'root':identity['native']['process']})
  ranks=[m for m in members if m['comm']=='icoFoam']
  if len(ranks)!=2:return None
  uid=identity['native']['process']['uid'];assert uid!=0
  assert all(m['identity']['uid']==uid for m in members)
  assert len({m['identity']['pid_namespace'] for m in ranks})==1
  assert all(m['uid_map'].split()==[str(uid),str(uid),'1'] for m in ranks)
  return job,members,distro
 return wait_for(observed,'Two actual owned icoFoam ranks were not observed',45)


def peer(api,store,role):
 value=bridge.peer(api,store,role)
 with (value.root/'agent_peer_fixture.py').open('a',encoding='utf8') as stream:stream.write(EXTRA_FIXTURE)
 return value


def cancellation_history(api, stopped):
    job_id = stopped['id']
    attempt_id = stopped['attempt']['id']
    rows = []
    cursor = 0
    while True:
        page = api.call('GET', api.prefix+'/workflow-events?after='+str(cursor)+'&limit=200')
        assert page['case_id'] == stopped['case_id'] and not page['blocked']
        for event in page['events']:
            if event['attempt_id'] == attempt_id:
                assert event['case_id'] == stopped['case_id']
                payload = event['payload']
                assert payload.get('job_id', job_id) == job_id
                rows.append((event['seq'], event['type'], payload.get('status')))
        if not page['has_more']:
            break
        assert page['next_after'] > cursor
        cursor = page['next_after']
    required = [('attempt.state_changed', 'running'), ('attempt.state_changed', 'cancelling'),
                ('attempt.child_stopped', None), ('attempt.content_drained', None),
                ('attempt.process_stopped', None), ('attempt.state_changed', 'cancelled')]
    lifecycle = []
    for kind, status in required:
        matches = [seq for seq, name, value in rows if name == kind and (status is None or value == status)]
        assert len(matches) == 1, (kind, status, matches)
        lifecycle.extend(matches)
    assert lifecycle == sorted(lifecycle)
    response = api.call('GET', f'/jobs/{job_id}/log')
    assert (response['case_id'], response['job_id'], response['attempt_id']) == (stopped['case_id'], job_id, attempt_id)
    assert response['available'] and not response['has_more'] and response['offset'] == response['size']
    log = response['chunk']
    assert stopped['log_backlog'] and log.endswith(stopped['log_backlog'])
    assert 'icoFoam' in log and 'Solving for Ux' in log
    return {'case_id': stopped['case_id'], 'job_id': job_id, 'attempt_id': attempt_id,
            'events': rows, 'lifecycle_sequence': lifecycle,
            'log_sha256': hashlib.sha256(log.encode()).hexdigest()}


def scenario(api,*,store=None,**_):
 (api.root/'entry-source.py').write_bytes(Path(__file__).read_bytes())
 (api.root/'timing-observer-source.py').write_bytes(Path(__file__).with_name('mpi_timing_observer.py').read_bytes())
 api.create_case('Actual workflow MPI cancel and explicit recovery')
 run_dir='C-run/case-1';checks=[];owned_jobs=[]
 def record(value):
  checks.append(value)
  (api.root/'mpi-cancel-recovery-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
 def waiting(server,identifier,key):
  def observed():
   value=server.call('GET','/workflow-runs/'+identifier)
   if value['status'] in {'failed','interrupted','cancelled'}:
    jobs={str(a['job_id']):server.call('GET','/jobs/'+str(a['job_id']))
     for s in value['steps'] for a in s['attempts']}
    (api.root/'failed-workflow.json').write_text(json.dumps({'workflow':value,'jobs':jobs},indent=2),encoding='utf8')
    raise AssertionError('Actual workflow '+identifier+' ended '+value['status']+'; see failed-workflow.json')
   step=next(s for s in value['steps'] if s['key']==key)
   return (value,step) if step['status']=='awaiting_confirmation' else None
  return wait_for(observed,'Workflow did not await '+key,180)
 def confirm(server,identifier,key):
  value,step=waiting(server,identifier,key)
  for item in value['steps']:
   if item['status']=='succeeded' and item['attempts']:
    jid=item['attempts'][-1]['job_id'];server.wait_job(jid)
    wait_for(lambda:server.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Previous producer did not drain')
  result=write(server,'POST',server.prefix+'/caseDict:advance',{'state':step['confirmation_state'],'confirmed':True,
   'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})
  dispatch = result.get('workflow_dispatch',{})
  if dispatch.get('status')=='pending':
   assert dispatch['run_id']==identifier and dispatch['code'] in {'storage_quota_busy','validation_busy'},result
   record({'stage':'confirmation_committed_dispatch_pending','step':key,'run_id':identifier,'code':dispatch['code']})
   def continued():
    current=server.call('GET','/workflow-runs/'+identifier)
    assert next(s for s in current['steps'] if s['id']==step['id'])['status']=='succeeded'
    assert current['status'] not in {'failed','cancelled','interrupted'},current['status']
    if key=='confirm_D':return current['status']=='succeeded'
    following=next(s for s in current['steps'] if s['key']=={'confirm_B':'mesh','confirm_C':'solve'}[key])
    assert len(following['attempts'])<=1,following['attempts']
    return bool(following['attempts'])
   wait_for(continued,'Durable dispatch did not start the original confirmed step',60)
  return value
 def drain(server,jid):
  wait_for(lambda:server.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Real cancelled writer did not drain')
 def cleanup_running():
  # This newly registered user's one isolated case is the only cleanup target.
  for row in api.call('GET',api.prefix+'/jobs'):
   if row['status'] in {'queued','running'}:
    api.call('POST','/jobs/'+str(row['id'])+':cancel',{})
    wait_for(lambda:api.call('GET','/jobs/'+str(row['id']))['status'] not in {'queued','running'},'Owned failure cleanup did not stop',90)
 source=peer(api,store,'mpi-cancel-source');observer=peer(api,store,'mpi-cancel-observer')
 resumed=peer(api,store,'mpi-cancel-resume')
 def observe_http(server,label):
  original=server.opener.open
  def observed(request,*args,**kwargs):
   started=time.monotonic();failure=None
   try:return original(request,*args,**kwargs)
   except BaseException as error:failure=type(error).__name__;raise
   finally:
    with (api.root/'mpi-cancel-recovery-http.jsonl').open('a',encoding='utf8') as stream:
     stream.write(json.dumps({'role':label,'method':request.get_method(),'path':request.selector,
      'seconds':round(time.monotonic()-started,4),'error':failure})+'\n')
  server.opener.open=observed
 for server,label in [(api,'main'),(source,'source'),(observer,'observer'),(resumed,'resumed')]:observe_http(server,label)
 with source,observer:
  try:
   saved=source.call('GET',source.prefix+'/caseDict')
   text=set_block(saved['content'],'mesh',{'source':'blockMesh','qualityTargets':
    {'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
   text=set_block(text,'numericalMethod',{'solver':'icoFoam'})
   text=set_block(text,'postProcessing',{'runDir':run_dir})
   write(source,'PUT',source.prefix+'/caseDict',{'content':text,'expected_revision':saved['dict_revision']})
   inputs=cavity_2d();control_path=run_dir+'/system/controlDict';mesh_path=run_dir+'/system/blockMeshDict'
   original_control=inputs[control_path]
   long_control=original_control.replace('endTime         0.5;','endTime         1000000;').replace(
    'deltaT          0.005;','deltaT          0.00001;').replace('writeInterval   20;','writeInterval   1000000000;')
   assert long_control!=original_control
   assert all(value in long_control for value in ['1000000;','0.00001;','1000000000;'])
   inputs[control_path]=long_control;inputs[mesh_path]=inputs[mesh_path].replace('(20 20 1)','(100 100 1)')
   assert '(100 100 1)' in inputs[mesh_path]
   for path,value in inputs.items():put(source,path,value)
   for start in range(0,320,32):write(source,'POST',source.prefix+'/testing/mpi-reference-inputs',{'start':start,'count':32})
   record({'stage':'actual_large_inputs_seeded','actual_reference_files':320})
   saved=put(source,'A-geo/geometry.py','import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n')
   if saved.get('auto_preview_job'):source.wait_job(saved['auto_preview_job'])
   revision=source.call('GET',source.prefix+'/caseDict')['dict_revision']
   flow=submit(source,source.prefix+'/workflow-runs',{'target':'solve','expected_revision':revision,
    'parameters':{'geometry_mode':'export','solve_mode':'mpi','subdomains':2},
    'budget':{'timeout_seconds':120,'max_log_bytes':16777216}},uuid.uuid4().hex)
   record({'stage':'workflow_created','workflow':flow['id']})
   confirm(source,flow['id'],'confirm_B');confirm(source,flow['id'],'confirm_C')
   def solve_attempt():
    active=source.call('GET','/workflow-runs/'+flow['id'])
    assert active['status'] not in {'failed','interrupted','cancelled'},active['status']
    step=next(s for s in active['steps'] if s['key']=='solve')
    return (active,step) if step['attempts'] else None
   active,step=wait_for(solve_attempt,'Confirmed solve did not receive an attempt',45)
   assert len(step['attempts'])==1;job_id=step['attempts'][0]['job_id'];owned_jobs.append(job_id)
   running,members,distro=actual_ranks(source,job_id)
   workspace=running['attempt']['workspace']
   expected={'Ref/large-inputs/input-'+str(i)+'.txt':hashlib.sha256(('MPI_INPUT_'+str(api.case_id)+'_'+str(i)).encode()).hexdigest() for i in range(320)}
   assert all(workspace['files'][p]['sha256']==h for p,h in expected.items())
   assert len(workspace['files'])>320 and len(workspace['selection'])<=256
   record({'actual_MPI_job':job_id,'observed_native_members':members,'distro':distro,
    'actual_icoFoam_ranks':2,'actual_large_reference_files':320,'sealed_total_files':len(workspace['files']),
    'selected_live_paths':workspace['selection'],'all_large_input_hashes_verified':True})
   owner_token=api.token
   try:
    other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','name':'Other isolated owner','password':'isolated-test-123'})
    api.token=other['token'];api.call('POST','/workflow-runs/'+flow['id']+':cancel',{'expected_version':active['version']},expect=404)
   finally:api.token=owner_token
   assert source.call('GET','/jobs/'+str(job_id))['status']=='running'
   source_pid=source.call('GET','/testing/workflow-api-owner')['pid']
   observer_pid=observer.call('GET','/testing/workflow-api-owner')['pid']
   actual_owner_pid=int(running['attempt']['process_identity']['worker_id'].rsplit(':',2)[1])
   cancel_peer,repeat_peer=(source,observer) if actual_owner_pid==observer_pid else (observer,source)
   canceller_pid=source_pid if cancel_peer is source else observer_pid
   assert canceller_pid!=actual_owner_pid
   current=cancel_peer.call('GET','/workflow-runs/'+flow['id']);version=current['version']
   def cancel(server):return server.call('POST','/workflow-runs/'+flow['id']+':cancel',{'expected_version':version})
   answer=cancel(cancel_peer);assert answer['status'] in {'cancelling','cancelled'}
   stopped=cancel_peer.wait_job(job_id,expected='cancelled',timeout=120);drain(cancel_peer,job_id)
   def terminal_cleanup():
    value=cancel_peer.call('GET','/jobs/'+str(job_id))
    # The legacy Job cancellation is visible immediately. The content-drain
    # checkpoint also commits before the owner's final stop output transaction.
    # Require that later actual terminal receipt, rather than a stale snapshot.
    attempt=value['attempt'];stop=attempt.get('process_stop')
    if attempt['phase']!='cancelled' or stop is None:return None
    assert value['status']=='cancelled' and stop['verified']
    return value
   stopped=wait_for(terminal_cleanup,'Cancelled owner did not commit verified terminal cleanup',60)
   (api.root/'cancelled-job.json').write_text(json.dumps(stopped,indent=2),encoding='utf8')
   children=list(stopped['attempt']['child_processes']['children'].values())
   assert len(children)==1 and children[0]['stop']['verified'] and children[0]['stop']['native_state']=='exited'
   def exited():
    states=native_inspect(distro,{'probe':[m['identity'] for m in members]})
    return states if all(s['state']=='exited' for s in states) else None
   exits=wait_for(exited,'An observed original MPI rank/helper did not exit')
   assert cancel(repeat_peer)['status']=='cancelled'
   cancelled=wait_for(lambda:(v if (v:=api.call('GET','/workflow-runs/'+flow['id']))['status']=='cancelled' else None),
    'Workflow did not preserve actual cancelled outcome')
   assert all(s['status'] in {'succeeded','cancelled','blocked'} for s in cancelled['steps'])
   history = cancellation_history(cancel_peer, stopped)
   (api.root/'mpi-cancel-events.json').write_text(json.dumps(history,indent=2),encoding='utf8')
   assert api.call('GET',api.prefix+'/caseDict')['workflow_state']=='C'
   assert source.call('GET',source.prefix+'/raw?path='+quote(control_path),raw=True).decode()==long_control
   assert (stopped['attempt']['outputs'].get('publication') or {}).get('status')!='committed'
   source.call('GET',source.prefix+'/raw?path='+quote(run_dir+'/0.5/U'),expect=404,raw=True)
   old_history=cancelled;before_jobs=[j['id'] for j in api.call('GET',api.prefix+'/jobs')]
   pid=source.call('GET','/testing/workflow-api-owner')['pid'];launcher=kill_owned_api(source,pid)
   record({'cross_owner_cancel_refused':True,'other_API_alone_cancelled_running_MPI':True,
    'actual_job_owner_api_pid':actual_owner_pid,'actual_canceller_api_pid':canceller_pid,
    'source_request_api_pid':source_pid,
    'two_API_cancel_idempotent':True,'actual_root_stop_verified':True,
    'actual_native_child_stop_verified':True,'observed_members_all_exited':exits,'workflow_cancelled':True,
    'stage_C_not_advanced':True,'incomplete_result_not_published':True,'actual_API_pid_killed':pid,'verified_launcher_pid':launcher})
  finally:cleanup_running()
 with resumed:
  try:
   assert resumed.call('GET','/workflow-runs/'+flow['id'])==old_history
   resumed.call('POST','/workflow-runs/'+flow['id']+':dispatch',{'expected_version':old_history['version']})
   resumed.call('POST','/workflow-runs/'+flow['id']+':cancel',{'expected_version':version})
   assert [j['id'] for j in api.call('GET',api.prefix+'/jobs')]==before_jobs
   assert resumed.call('GET','/workflow-runs/'+flow['id'])==old_history
   # Restore the original validated benchmark for the explicitly new run. The
   # 100x100 mesh above only makes the real cancellation window observable;
   # retaining its fine grid with the original coarse-grid deltaT is unstable.
   put(resumed,control_path,original_control)
   put(resumed,mesh_path,cavity_2d()[mesh_path])
   dictionary=resumed.call('GET',resumed.prefix+'/caseDict')
   body={'expected_version':old_history['version'],'expected_revision':dictionary['dict_revision'],'confirmed_reset':True,
    'target':'solve','parameters':{'geometry_mode':'export','solve_mode':'mpi','subdomains':2},
    'budget':{'timeout_seconds':120,'max_log_bytes':16777216}}
   key=uuid.uuid4().hex;fresh=submit(resumed,'/workflow-runs/'+flow['id']+':recompute',body,key)
   assert fresh['recomputation']['source_run_id']==flow['id']
   assert fresh['recomputation']['rebuild']==['mesh','solve'] and set(fresh['recomputation']['reused'])=={'geometry'}
   confirm(resumed,fresh['id'],'confirm_B');confirm(resumed,fresh['id'],'confirm_C')
   waiting_flow,confirmation=waiting(resumed,fresh['id'],'confirm_D')
   step=next(s for s in waiting_flow['steps'] if s['key']=='solve');new_id=step['attempts'][0]['job_id'];owned_jobs.append(new_id)
   done=resumed.wait_job(new_id,timeout=180);drain(resumed,new_id)
   submitter_pid=resumed.call('GET','/testing/workflow-api-owner')['pid']
   publication=done['attempt']['outputs']['publication'];assert publication['status']=='committed' and publication['kind']=='solve'
   manifest=api.call('GET','/testing/workflow-publication/'+publication['id'])['manifest']
   native=manifest['descriptor']['native']['receipt']['native']
   assert native['subdomains']==2 and native['uid']!=0 and set(native['steps'])=={'decompose','solve','reconstruct'}
   raw=resumed.call('GET',resumed.prefix+'/raw?path='+quote(run_dir+'/0.5/U'),raw=True).decode()
   vector=re.search(r'internalField\s+nonuniform\s+List<vector>\s+(\d+)\s*\((.*?)\)\s*;',raw,re.S);assert vector
   values=[tuple(float(x) for x in row.split()) for row in re.findall(r'\(([^()]*)\)',vector.group(2))]
   assert len(values)==int(vector.group(1))==400 and all(len(v)==3 and all(math.isfinite(x) and abs(x)<5 for x in v) for v in values)
   assert all(done['attempt']['workspace']['files'][p]['sha256']==h for p,h in expected.items())
   assert api.call('GET',api.prefix+'/caseDict')['workflow_state']=='C'
   confirm(resumed,fresh['id'],'confirm_D')
   assert resumed.call('GET','/workflow-runs/'+fresh['id'])['status']=='succeeded'
   assert submit(resumed,'/workflow-runs/'+flow['id']+':recompute',body,key)['id']==fresh['id']
   assert resumed.call('GET','/workflow-runs/'+flow['id'])==old_history
   assert api.call('GET','/jobs/'+str(job_id))['status']=='cancelled'
   record({'new_workflow':fresh['id'],'actual_recovered_MPI_job':new_id,'actual_400_cell_final_time':.5,
    'original_validated_benchmark_inputs_restored':True,'actual_10000_cell_job_cancelled':True,
    'actual_two_rank_recovery_completed':True,'large_files_remain_sealed':True,'changed_controls_rebuild_mesh':True,
    'explicit_reset_and_B_C_D_confirmation':True,'cancelled_history_unchanged':True,
    'terminal_dispatch_did_not_replay':True,'same_recompute_key_same_new_run':True})
  finally:cleanup_running()
 assert all(p.proc.poll() is not None for p in [source,observer,resumed])
 reader = peer(api, store, 'mpi-post-publication')
 with reader:
  path = '/jobs/'+str(new_id)
  before = reader.call('GET', path)
  assert before['status']=='succeeded' and before['attempt']['outputs']['publication']==publication
  reply = reader.call('POST', path+':cancel', {})
  assert reply['ok'] is False
  after = reader.call('GET', path)
  assert after['status']=='succeeded' and after['attempt']==before['attempt']
  assert reader.call('GET','/testing/workflow-publication/'+publication['id'])['manifest']==manifest
  field = reader.call('GET',reader.prefix+'/raw?path='+quote(run_dir+'/0.5/U'),raw=True)
  assert field==raw.encode() and reader.call('GET','/workflow-runs/'+fresh['id'])['status']=='succeeded'
  worker_pid=int(after['attempt']['process_identity']['worker_id'].rsplit(':',2)[1])
  record({'stage':'late_cancel_after_submitting_api_exit','job_id':new_id,'attempt_id':after['attempt']['id'],
          'publication_id':publication['id'],'reply':reply,'submitting_api_exited':True,
          'submitting_api_pid':submitter_pid,'worker_pid':worker_pid,
          'field_sha256':hashlib.sha256(field).hexdigest(),'same_attempt_and_manifest':True})
 assert reader.proc.poll() is not None
 report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'checks':checks,
  'scope':'Actual running workflow MPI cancellation, observed rank exits and explicit new workflow recomputation with 320 reference inputs; UDF/batch/original S3 UDF attribution remain'}
 (api.root/'mpi-cancel-recovery.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 api.create_case('Independent actual running MPI cancellation evidence readback')
 return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
