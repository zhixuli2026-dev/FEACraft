"""Actual running geometry batch cancellation, API death and explicit new batch.

Only real scanner lifecycles are stopped to select the owner API; no model or
native outcome is replaced. The wrapper performs actual DeepSeek readback.
"""
import ast,asyncio,copy,json,subprocess,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.services import process_identity_native as identity
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_failure_fallback_acceptance import wait_for,kill_owned_api
from live_llm_mpi_workflow_acceptance import FIXTURE as MPI_FIXTURE
from testsupport import IsolatedServer

EXTRA=r'''
import asyncio,hashlib
from app.models import ArtifactPublicationHead
from app import storage

@app.on_event('startup')
async def select_actual_batch_scanner_role():
 if os.environ.get('VIBEFLOW_BATCH_TEST_ROLE','observer') in {'source','recovery'}:return
 for prefix in ['delivery','workflow_dispatch']:
  stop=getattr(app.state,prefix+'_stop');task=getattr(app.state,prefix+'_task')
  stop.set();await task

@app.get('/api/v1/testing/batch-scanners')
async def actual_batch_scanners(user=Depends(get_current_user)):
 return {'role':os.environ.get('VIBEFLOW_BATCH_TEST_ROLE','observer'),'pid':os.getpid(),
  'delivery_stopped':app.state.delivery_task.done(),'workflow_stopped':app.state.workflow_dispatch_task.done()}

@app.get('/api/v1/cases/{case_id}/testing/batch-heads')
async def actual_batch_heads(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=False)
 head=await db.get(ArtifactPublicationHead,(case_id,'geometry'))
 path='A-geo/scene.json'
 return {'geometry':head.active_publication_id if head else None,'stage':case.workflow_state,
  'scene_sha256':hashlib.sha256(storage.get_bytes(case.s3_prefix,path)).hexdigest() if storage.exists(case.s3_prefix,path) else None}
'''
FIXTURE=MPI_FIXTURE+EXTRA


def peer(api,store,role):
    value=IsolatedServer('workflow-batch-'+role,app='batch_peer:app')
    shared={k:v for k,v in api.env.items() if k.startswith('VIBEFLOW_')}
    if store:shared.update(store.settings());shared['VIBEFLOW_STORAGE_FS_ROOT']=str(value.root/'s3-cache')
    shared['VIBEFLOW_BATCH_TEST_ROLE']=role
    (value.root/'batch_peer.py').write_text('import os\nos.environ.update('+repr(shared)+')\nfrom app.main import app\n'+FIXTURE,encoding='utf8')
    value.token=api.token
    return value


def ancestors(pid):
    assert type(pid) is int and pid>0
    command=("$chain=@();$q="+str(pid)+";for($i=0;$i -lt 10 -and $q -gt 0;$i++){"
        "$r=Get-CimInstance Win32_Process -Filter \"ProcessId = $q\";if($null -eq $r){break};"
        "$chain+=([int]$r.ProcessId);$q=[int]$r.ParentProcessId};@{chain=$chain}|ConvertTo-Json -Compress")
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',command],capture_output=True,text=True,timeout=15)
    assert result.returncode==0
    return json.loads(result.stdout)['chain']


def scenario(api,*,store=None,**_):
    (api.root/'batch-cancel-entry-source.py').write_bytes(Path(__file__).read_bytes())
    checks=[];actors=[];release=api.root/'batch-actual-export-release'
    def record(value):
        checks.append(value);(api.root/'workflow-batch-cancel-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
    assert api.call('GET','/testing/batch-scanners')['delivery_stopped']
    workspace=api.call('GET','/workspaces')[0]['id']
    def actor(label):
        case=api.call('POST','/workspaces/'+str(workspace)+'/cases',{'name':label})
        value=copy.copy(api);value.case_id=case['id'];value.prefix='/cases/'+str(case['id']);actors.append(value)
        marker=api.root/('batch-actual-export-'+str(value.case_id)+'.json')
        code=('import cadquery as cq\nimport json,os,time\nfrom pathlib import Path\n'
            'def build_geometry(ctx):\n shape=cq.Workplane("XY").box(1,2,3)\n if ctx.mode=="export":\n'
            '  Path('+repr(str(marker))+').write_text(json.dumps({"pid":os.getpid(),"actual_CadQuery_shape_built":True}))\n'
            '  deadline=time.monotonic()+120\n  while not Path('+repr(str(release))+').exists():\n'
            '   if time.monotonic()>deadline:raise TimeoutError("Actual export hold expired")\n   time.sleep(.05)\n return {"box":shape}\n')
        ast.parse(code,filename='actual-batch-held-geometry.py')
        saved=put(value,'A-geo/geometry.py',code)
        if saved.get('auto_preview_job'):
            jid=saved['auto_preview_job'];value.wait_job(jid)
            wait_for(lambda:value.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Actual baseline preview did not drain')
        return value,marker
    def body():
        return {'max_concurrent':1,'members':[{'key':'member-'+str(v.case_id),'case_id':v.case_id,'target':'geometry',
            'run_dir':'C-run/case-1','expected_revision':v.call('GET',v.prefix+'/caseDict')['dict_revision'],
            'parameters':{'geometry_mode':'export'},'budget':{'timeout_seconds':180,'max_log_bytes':16777216}} for v in actors]}
    def jobs(server,ids):
        return [j for v in actors for j in server.call('GET',v.prefix+'/jobs') if j.get('run_id') in ids]
    source=peer(api,store,'source');observer=peer(api,store,'observer');recovery=peer(api,store,'recovery')
    try:
        first,m1=actor('Actual batch cancel one');second,m2=actor('Actual batch cancel two');markers=[m1,m2]
        original_heads=[v.call('GET',v.prefix+'/testing/batch-heads') for v in actors]
        assert all(h['geometry'] and h['scene_sha256'] and h['stage']=='A' for h in original_heads)
        original_body=body();key=uuid.uuid4().hex
        with observer,source:
            observer_state=observer.call('GET','/testing/batch-scanners');source_state=source.call('GET','/testing/batch-scanners')
            assert observer_state['delivery_stopped'] and not source_state['delivery_stopped']
            batch=source.call('POST','/workflow-batches',original_body,expect=202,extra_headers={'Idempotency-Key':key})
            ids={r['id'] for r in batch['members']}
            wait_for(lambda:any(p.exists() for p in markers),'Actual batch export body did not execute',120)
            assert sum(p.exists() for p in markers)==1
            active_marker=next(p for p in markers if p.exists());witness=json.loads(active_marker.read_text())
            assert witness['actual_CadQuery_shape_built'];native=identity.observe(witness['pid']);chain=ancestors(witness['pid'])
            assert source_state['pid'] in chain
            pair=wait_for(lambda:(rows if len(rows:=jobs(observer,ids))==2 else None),'Second durable batch Job was not admitted',60)
            running=[j for j in pair if j['attempt']['phase']=='running'];queued=[j for j in pair if j['attempt']['phase']=='queued']
            assert len(running)==len(queued)==1
            record({'stage':'actual_running_and_queued_members','batch_id':batch['id'],'run_ids':sorted(ids),
                'source_API_pid':source_state['pid'],'observer_API_pid':observer_state['pid'],'actual_geometry_process':native,
                'actual_ancestry':chain,'actual_job':running[0]['id'],'queued_job':queued[0]['id'],'maximum_observed_exports':1})
            def cancel(server):return server.call('POST','/workflow-batches/'+batch['id']+':cancel',{'expected_version':batch['version']})
            with ThreadPoolExecutor(max_workers=2) as pool:
                answers=[f.result() for f in [pool.submit(cancel,observer),pool.submit(cancel,api)]]
            assert all(r['status'] in {'cancelling','cancelled'} and r['version']==1 for r in answers)
            stopped=observer.wait_job(running[0]['id'],expected='cancelled',timeout=90)
            wait_for(lambda:observer.call('GET','/testing/workflow-job-drain/'+str(stopped['id']))['verified'],'Cancelled native batch member did not drain',90)
            def terminal_stop():
                current=observer.call('GET','/jobs/'+str(stopped['id']));attempt=current['attempt']
                if attempt['phase']!='cancelled' or attempt.get('process_stop') is None:return None
                assert attempt['process_stop']['verified']
                return current
            stopped=wait_for(terminal_stop,'Cancelled batch member did not persist its final stop proof',90)
            wait_for(lambda:identity.probe(native)['state']=='exited','Original actual geometry process did not exit',60)
            queued_done=observer.wait_job(queued[0]['id'],expected='cancelled')
            assert queued_done['attempt']['process_identity'] is None and sum(p.exists() for p in markers)==1
            settled=wait_for(lambda:(b if (b:=observer.call('GET','/workflow-batches/'+batch['id']))['status']=='cancelled' else None),
                'Persisted member cancellation did not settle',60)
            histories={rid:observer.call('GET','/workflow-runs/'+rid) for rid in ids}
            assert [v.call('GET',v.prefix+'/testing/batch-heads') for v in actors]==original_heads
            assert cancel(observer)['version']==1
            before_jobs=[[j['id'] for j in v.call('GET',v.prefix+'/jobs')] for v in actors]
            launcher=kill_owned_api(source,source_state['pid'])
            record({'stage':'cross_API_atomic_cancel_and_owner_death','all_member_runs_cancelled':True,'repeat_cancel_single_version':True,
                'actual_process_exited':identity.probe(native),'queued_member_never_started':True,'old_geometry_and_scene_preserved':True,
                'root_stop_verified':True,'source_API_pid_killed':source_state['pid'],'verified_launcher_pid':launcher})
        if store:assert not (recovery.root/'s3-cache').exists()
        with recovery:
            assert recovery.call('GET','/workflow-batches/'+batch['id'])==settled
            for rid in ids:assert recovery.call('GET','/workflow-runs/'+rid)==histories[rid]
            assert recovery.call('POST','/workflow-batches',original_body,expect=202,extra_headers={'Idempotency-Key':key})['id']==batch['id']
            assert before_jobs==[[j['id'] for j in recovery.call('GET',v.prefix+'/jobs')] for v in actors]
            release.touch()
            fresh=recovery.call('POST','/workflow-batches',body(),expect=202,extra_headers={'Idempotency-Key':uuid.uuid4().hex})
            assert fresh['id']!=batch['id'];proofs=[]
            for v in actors:
                summary=next(r for r in fresh['members'] if r['case_id']==v.case_id)
                def waiting():
                    flow=recovery.call('GET','/workflow-runs/'+summary['id'])
                    assert flow['status'] not in {'failed','interrupted','cancelled'},flow
                    return flow if flow['status']=='awaiting_confirmation' else None
                flow=wait_for(waiting,'Explicit replacement batch did not reach B',180)
                producer=next(s for s in flow['steps'] if s['key']=='geometry');confirmation=next(s for s in flow['steps'] if s['key']=='confirm_B')
                jid=producer['attempts'][-1]['job_id'];recovery.wait_job(jid)
                wait_for(lambda:recovery.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Replacement geometry did not drain')
                publication=recovery.call('GET','/testing/workflow-publication/'+producer['publication']['id'])['manifest']
                files=[p for p,r in publication['files'].items() if p.lower().endswith(('.step','.stp')) and r['size']>100];assert files
                write(recovery,'POST',v.prefix+'/caseDict:advance',{'state':'B','confirmed':True,
                    'expected_revision':confirmation['confirmation_revision'],'workflow_step_id':confirmation['id'],
                    'expected_step_version':confirmation['version']})
                proofs.append({'run_id':flow['id'],'actual_job':jid,'actual_STEP_files':files,'explicit_B':True})
            completed=wait_for(lambda:(b if (b:=recovery.call('GET','/workflow-batches/'+fresh['id']))['status']=='succeeded' else None),
                'Explicit replacement batch did not succeed')
            assert recovery.call('GET','/workflow-batches/'+batch['id'])==settled
            for rid in ids:assert recovery.call('GET','/workflow-runs/'+rid)==histories[rid]
            record({'stage':'new_API_explicit_replacement_completed','old_history_and_membership_preserved':True,'no_old_jobs_replayed':True,
                'independent_empty_S3_cache':bool(store),'new_batch_id':completed['id'],'actual_geometry_proofs':proofs})
        report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'checks':checks,
            'scope':'Geometry running/queued members, concurrent cross-API SQL cancellation, owner API death after drain, explicit new batch; professional/Redis and in-flight owner death remain.'}
        (api.root/'workflow-batch-cancel.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        api.create_case('Independent actual batch cancellation evidence readback')
        return report
    finally:
        release.touch()
        for v in actors:
            for j in v.call('GET',v.prefix+'/jobs'):
                if j['status'] in {'queued','running'}:
                    v.call('POST','/jobs/'+str(j['id'])+':cancel',{});v.wait_job(j['id'],expected='cancelled',timeout=120)


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
