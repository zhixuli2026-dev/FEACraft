"""Actual UDF compiler, CDLL and icoFoam cancellation plus explicit recovery.

Bounded C++ compilation/initialization and a long physical solve expose each
real phase. No LLM, compiler, loader, solver or stop result is substituted.
"""
import asyncio,hashlib,json,re,subprocess,time,uuid
from pathlib import Path
from urllib.parse import quote

from cae_fixtures import cavity_2d
from app.services.casedict import set_block
from app.services.owned_process import wsl_path
import live_llm_udf_workflow_acceptance as bridge
from live_llm_job_repeat_acceptance import main,put,write
from live_llm_failure_fallback_acceptance import wait_for,kill_owned_api
from live_llm_workflow_plan_migration_acceptance import submit
from live_llm_workflow_batch_professional_acceptance import confirmed_dispatch_pending_kind
from testsupport import SERVER


EXTRA_FIXTURE=r'''
import hashlib
from app import storage
from app.services.artifact_publication import current

@app.get('/api/v1/cases/{case_id}/testing/udf-heads')
async def udf_heads(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=False);heads={}
 for kind in ['mesh','udf-build','solve']:
  _,row=await current(db,case.id,kind)
  heads[kind]={'id':row.id,'generation':row.generation} if row else None
 path='UDF/myFunctionObject/libmyFunctionObject.so'
 heads['mutable_library_sha256']=hashlib.sha256(storage.get_bytes(case.s3_prefix,path)).hexdigest() if storage.exists(case.s3_prefix,path) else None
 path='C-run/case-1/0.5/U'
 heads['mutable_U_sha256']=hashlib.sha256(storage.get_bytes(case.s3_prefix,path)).hexdigest() if storage.exists(case.s3_prefix,path) else None
 return heads
'''
FIXTURE=bridge.FIXTURE+EXTRA_FIXTURE

# Read-only /proc observations use the same birth-identity implementation as
# production supervision. Only relevant library hashes and role flags return.
INSPECT=r'''
import hashlib,json,sys
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
   record=identity.observe(pid);base=Path('/proc')/str(pid)
   comm=(base/'comm').read_text().strip();args=(base/'cmdline').read_bytes()
   mapping=(base/'uid_map').read_text().strip();libraries=[]
   for line in (base/'maps').read_text().splitlines():
    fields=line.split(None,5)
    if len(fields)==6 and fields[-1].endswith('/libmyFunctionObject.so'):
     path=Path(fields[-1]);assert path.stat().st_size<=67108864
     if not any(r['path']==str(path) for r in libraries):
      libraries.append({'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
   for task in (base/'task').iterdir():
    try:pending.extend(map(int,(task/'children').read_text().split()))
    except FileNotFoundError:pass
   if identity.probe(record)['state']!='match':continue
   result.append({'identity':record,'comm':comm,'uid_map':mapping,
    'actual_CDLL_loader':b'ctypes.CDLL' in args,'loaded_UDF_libraries':libraries})
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


def peer(api,store,role):
    value=bridge.peer(api,store,role)
    with (value.root/'agent_peer_fixture.py').open('a',encoding='utf8') as stream:stream.write(EXTRA_FIXTURE)
    return value


def waiting(api,root,identifier,key):
    def observed():
        value=api.call('GET','/workflow-runs/'+identifier)
        if value['status'] in {'failed','interrupted','cancelled'}:
            jobs={str(a['job_id']):api.call('GET','/jobs/'+str(a['job_id'])) for s in value['steps'] for a in s['attempts']}
            (root/'failed-workflow.json').write_text(json.dumps({'workflow':value,'jobs':jobs},indent=2),encoding='utf8')
            raise AssertionError('Actual UDF workflow ended '+value['status']+'; see failed-workflow.json')
        step=next(s for s in value['steps'] if s['key']==key)
        return (value,step) if step['status']=='awaiting_confirmation' else None
    return wait_for(observed,'UDF workflow did not reach '+key,240)


def drain(api,jid):
    wait_for(lambda:api.call('GET','/testing/workflow-job-drain/'+str(jid))['verified'],'Actual UDF producer did not drain',90)


def confirm(api,root,identifier,key):
    value,step=waiting(api,root,identifier,key)
    for item in value['steps']:
        if item['status']=='succeeded' and item['attempts']:
            jid=item['attempts'][-1]['job_id'];api.wait_job(jid);drain(api,jid)
    result=write(api,'POST',api.prefix+'/caseDict:advance',{'state':step['confirmation_state'],'confirmed':True,
        'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})
    kind=confirmed_dispatch_pending_kind(result)
    if kind:
        with (root/'udf-confirmation-backpressure.jsonl').open('a',encoding='utf8') as stream:
            stream.write(json.dumps({'workflow':identifier,'confirmation':key,'known_wait':kind,
                'actual_dispatch':result['workflow_dispatch']})+'\n')


def actual_phase(api,root,identifier,phase):
    def observed():
        flow=api.call('GET','/workflow-runs/'+identifier)
        if flow['status'] in {'failed','interrupted','cancelled'}:
            (root/'failed-phase-workflow.json').write_text(json.dumps(flow,indent=2),encoding='utf8')
            raise AssertionError('UDF '+phase+' ended before actual observation')
        step=next(s for s in flow['steps'] if s['key']==('solve' if phase=='solve' else 'udf-build'))
        if not step['attempts']:return None
        assert len(step['attempts'])==1
        job=api.call('GET','/jobs/'+str(step['attempts'][0]['job_id']))
        if job['status']=='queued':return None
        assert job['status']=='running',(phase,job['status'])
        active=[c for c in job['attempt']['child_processes']['children'].values() if c['purpose']=='udf_run' and c['phase']=='running']
        if not active:return None
        assert len(active)==1
        identity=active[0]['identity']['native'];distro=identity['distribution']
        members=native_inspect(distro,{'root':identity['process']})
        wanted=[m for m in members if m['comm']=='cc1plus'] if phase=='compile' else [
            m for m in members if m['actual_CDLL_loader'] and m['loaded_UDF_libraries']] if phase=='load' else [
            m for m in members if m['comm']=='icoFoam' and m['loaded_UDF_libraries']]
        if not wanted:return None
        # Build traces its compiler; its subsequent CDLL check is an ordinary
        # loader process. Integration traces the actual load/solve pair.
        trace_required=phase in {'compile','solve'}
        if trace_required:assert any(member['comm']=='strace' for member in members),'Actual UDF phase lacks its current trace process'
        (root/('udf-'+phase+'-observed-processes.json')).write_text(
            json.dumps({'members':members,'wanted_pids':[m['identity']['pid'] for m in wanted],
                'trace_required_in_this_mode':trace_required},indent=2),encoding='utf8')
        uid=identity['process']['uid'];assert uid!=0
        # The outer unshare supervisor retains its parent user namespace. The
        # compiler/loader/solver runs in the mapped child namespace (process_gate).
        assert all(m['identity']['uid']==uid for m in members),members
        assert all(m['uid_map'].split()==[str(uid),str(uid),'1'] for m in wanted),wanted
        return flow,job,members,distro
    return wait_for(observed,'Actual UDF '+phase+' process was not observed',90)


def solution(api,root,flow,marker):
    value,_=waiting(api,root,flow['id'],'confirm_D');jobs=[];publications=[]
    for key in ['udf-build','solve']:
        step=next(s for s in value['steps'] if s['key']==key);assert len(step['attempts'])==1
        job=api.wait_job(step['attempts'][0]['job_id'],timeout=240);drain(api,job['id']);jobs.append(job)
        pub=job['attempt']['outputs']['publication'];assert pub['status']=='committed'
        assert pub['kind']==('udf-build' if key=='udf-build' else 'solve')
        publications.append(api.call('GET','/testing/workflow-publication/'+pub['id'])['manifest'])
    build,solve=[p['descriptor']['native']['receipt']['native'] for p in publications]
    assert set(build['steps'])=={'build','load'} and set(solve['steps'])=={'load','solve'}
    assert build['uid']!=0 and solve['uid']!=0 and build['library']['sha256']==solve['library']['sha256']
    compiler=build['compiler_dependencies'];runtime=solve['case_queries']
    assert compiler['policy']['query_schema']=='vibeflow.udf-file-queries.v2'
    assert compiler['helper_sha256']==hashlib.sha256((SERVER/'app/services/udf_dependencies.py').read_bytes()).hexdigest()
    assert compiler['file_query_evidence']['events']>0 and compiler['file_query_evidence']['bytes']>0
    assert runtime['evidence']['events']>0 and runtime['evidence']['bytes']>0
    assert publications[1]['descriptor']['native']['source']['schema']=='vibeflow.case-file-queries.v1'
    assert marker in api.call('GET',api.prefix+'/raw?path='+quote('C-run/case-1/udf-solve.log'),raw=True).decode()
    assert len(bridge.bridge.vectors(api.call('GET',api.prefix+'/raw?path='+quote('C-run/case-1/0.5/U'),raw=True)))==400
    confirm(api,root,flow['id'],'confirm_D')
    assert api.call('GET','/workflow-runs/'+flow['id'])['status']=='succeeded'
    return {'build_job':jobs[0]['id'],'solve_job':jobs[1]['id'],'actual_400_cell_final_time':.5,
        'actual_wmake_CDLL_icoFoam':True,'library_sha256':build['library']['sha256'],'actual_marker':marker,
        'compiler_query_contract':compiler['policy']['query_schema'],'actual_compile_query_events':compiler['file_query_evidence']['events'],
        'actual_runtime_query_events':runtime['evidence']['events'],
        'explicit_B_C_D_confirmation':True}


def scenario(api,*,store=None,**_):
    (api.root/'udf-cancel-entry-source.py').write_bytes(Path(__file__).read_bytes())
    modules=['udf_dependencies','case_file_queries','native_execution','process_gate','job_native_scopes',
        'process_cgroup_native','job_scope_guardians','job_write_checkpoint','udf_native','udf_worker']
    runtime_sources={name:hashlib.sha256((SERVER/('app/services/'+name+'.py')).read_bytes()).hexdigest() for name in modules}
    (api.root/'udf-cancel-runtime-sources.json').write_text(json.dumps(runtime_sources,indent=2),encoding='utf8')
    checks=[];run_dir='C-run/case-1';cpp='UDF/myFunctionObject/myFunctionObject.C'
    parameters={'geometry_mode':'export','solve_mode':'udf','udf_name':'myFunctionObject'}
    budget={'timeout_seconds':180,'max_log_bytes':16777216}
    def record(row):
        checks.append(row)
        (api.root/'udf-cancel-recovery-progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
        print('UDF_TRACED_CANCEL_STAGE '+row['stage'],flush=True)
    def cleanup():
        # Only this newly registered user's current isolated case is targeted.
        for row in api.call('GET',api.prefix+'/jobs'):
            if row['status'] in {'queued','running'}:
                api.call('POST','/jobs/'+str(row['id'])+':cancel',{})
                wait_for(lambda:api.call('GET','/jobs/'+str(row['id']))['status'] not in {'queued','running'},
                    'Owned UDF failure cleanup did not stop',90)
                drain(api,row['id'])
                (api.root/('failure-cleanup-job-'+str(row['id'])+'.json')).write_text(
                    json.dumps(api.call('GET','/jobs/'+str(row['id'])),indent=2),encoding='utf8')
    for phase in ['compile','load','solve']:
        api.create_case('Actual UDF '+phase+' cancellation and explicit recovery')
        source=peer(api,store,'udf-cancel-'+phase+'-source')
        observer=peer(api,store,'udf-cancel-'+phase+'-observer')
        recovery=peer(api,store,'udf-cancel-'+phase+'-recovery')
        for server,label in [(api,'main'),(source,'source'),(observer,'observer'),(recovery,'recovery')]:
            # Main's opener is already instrumented after the first case.
            if server is api and phase!='compile':continue
            original=server.opener.open
            def observed(request,*args,_original=original,_label=label,**kwargs):
                started=time.monotonic();failure=None
                try:return _original(request,*args,**kwargs)
                except BaseException as error:failure=type(error).__name__;raise
                finally:
                    with (api.root/'udf-cancel-recovery-http.jsonl').open('a',encoding='utf8') as stream:
                        stream.write(json.dumps({'role':_label,'method':request.get_method(),'path':request.selector,
                            'seconds':round(time.monotonic()-started,4),'error':failure})+'\n')
            server.opener.open=observed
        with source,observer:
            try:
                saved=source.call('GET',source.prefix+'/caseDict')
                text=set_block(saved['content'],'mesh',{'source':'blockMesh','qualityTargets':
                    {'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
                text=set_block(text,'numericalMethod',{'solver':'icoFoam'})
                text=set_block(text,'postProcessing',{'runDir':run_dir})
                write(source,'PUT',source.prefix+'/caseDict',{'content':text,'expected_revision':saved['dict_revision']})
                inputs=cavity_2d();control_path=run_dir+'/system/controlDict';mesh_path=run_dir+'/system/blockMeshDict'
                for path,value in inputs.items():put(source,path,value)
                source.call('POST',source.prefix+'/udf/scaffold',{'name':'myFunctionObject','objective':'Actual '+phase+' cancellation'})
                original_cpp=source.call('GET',source.prefix+'/raw?path='+quote(cpp),raw=True).decode()
                assert 'P0_UDF_OK' in original_cpp
                marker='UDF_BASELINE_'+phase.upper()
                put(source,cpp,original_cpp.replace('P0_UDF_OK',marker))
                saved=put(source,'A-geo/geometry.py','import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1)}\n')
                if saved.get('auto_preview_job'):source.wait_job(saved['auto_preview_job'])
                revision=source.call('GET',source.prefix+'/caseDict')['dict_revision']
                baseline=source.call('POST',source.prefix+'/workflow-runs',{'target':'solve','expected_revision':revision,
                    'parameters':parameters,'budget':budget})
                confirm(source,api.root,baseline['id'],'confirm_B');confirm(source,api.root,baseline['id'],'confirm_C')
                baseline_proof=solution(source,api.root,baseline,marker)
                record({'stage':phase+'_baseline_passed','case_id':api.case_id,'actual_baseline':baseline_proof})
                baseline_history=source.call('GET','/workflow-runs/'+baseline['id'])
                original_heads=source.call('GET',source.prefix+'/testing/udf-heads')
                assert original_heads['mutable_library_sha256']==baseline_proof['library_sha256']
                original_control=inputs[control_path];active_control=original_control
                if phase=='compile':
                    # Actual bounded optimizer work, with the same successful
                    # compiler workload used by the prior native scope probe.
                    workload='\n'+''.join('double cancel_fn_'+str(n)+'(double x) { return x*x+'+str(n)+'.0; }\n' for n in range(12000))
                    put(source,cpp,original_cpp.replace('P0_UDF_OK','UDF_CANCEL_COMPILE')+workload)
                elif phase=='load':
                    # Actual ELF initializer CPU work keeps ctypes inside dlopen.
                    # No sleeping process stands in for a compiler or loader.
                    workload='''
namespace {
volatile unsigned long udfLoadChecksum = 0;
struct UdfActualLoadWork {
 UdfActualLoadWork() {
  for (unsigned long i=0; i<6000000000UL; ++i) udfLoadChecksum ^= i;
 }
};
UdfActualLoadWork udfActualLoadWork;
}
'''
                    put(source,cpp,original_cpp.replace('P0_UDF_OK','UDF_CANCEL_LOAD')+workload)
                else:
                    active_control=original_control.replace('endTime         0.5;','endTime         1000000;').replace(
                        'deltaT          0.005;','deltaT          0.00001;').replace('writeInterval   20;','writeInterval   1000000000;')
                    assert all(value in active_control for value in ['1000000;','0.00001;','1000000000;'])
                    put(source,control_path,active_control)
                    fine_mesh=inputs[mesh_path].replace('(20 20 1)','(100 100 1)');assert fine_mesh!=inputs[mesh_path]
                    put(source,mesh_path,fine_mesh)
                dictionary=source.call('GET',source.prefix+'/caseDict')
                body={'expected_version':baseline_history['version'],'expected_revision':dictionary['dict_revision'],
                    'confirmed_reset':True,'target':'solve','parameters':parameters,'budget':budget}
                cancelled_flow=submit(source,'/workflow-runs/'+baseline['id']+':recompute',body,uuid.uuid4().hex)
                confirm(source,api.root,cancelled_flow['id'],'confirm_B');confirm(source,api.root,cancelled_flow['id'],'confirm_C')
                active,running,members,distro=actual_phase(observer,api.root,cancelled_flow['id'],phase)
                jid=running['id'];before_cancel_heads=source.call('GET',source.prefix+'/testing/udf-heads')
                if phase=='solve':
                    published=source.call('GET','/testing/workflow-publication/'+before_cancel_heads['udf-build']['id'])['manifest']
                    expected=published['files']['UDF/myFunctionObject/libmyFunctionObject.so']['sha256']
                    assert any(m['comm']=='icoFoam' and any(lib['sha256']==expected for lib in m['loaded_UDF_libraries']) for m in members)
                record({'stage':'actual_'+phase+'_observed','case_id':api.case_id,'workflow':cancelled_flow['id'],
                    'actual_job':jid,'baseline':baseline_proof,'observed_native_members':members,'distribution':distro})
                owner_token=api.token
                try:
                    other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','name':'Other isolated owner','password':'isolated-test-123'})
                    api.token=other['token']
                    api.call('POST','/workflow-runs/'+cancelled_flow['id']+':cancel',{'expected_version':active['version']},expect=404)
                finally:api.token=owner_token
                assert observer.call('GET','/jobs/'+str(jid))['status']=='running'
                owner_pid=int(running['attempt']['process_identity']['worker_id'].rsplit(':',2)[1])
                source_pid=source.call('GET','/testing/workflow-api-owner')['pid']
                observer_pid=observer.call('GET','/testing/workflow-api-owner')['pid']
                canceller,repeater=(source,observer) if owner_pid==observer_pid else (observer,source)
                canceller_pid=source_pid if canceller is source else observer_pid
                assert canceller_pid!=owner_pid
                current=canceller.call('GET','/workflow-runs/'+cancelled_flow['id']);version=current['version']
                def cancel(server):
                    return server.call('POST','/workflow-runs/'+cancelled_flow['id']+':cancel',{'expected_version':version})
                assert cancel(canceller)['status'] in {'cancelling','cancelled'}
                canceller.wait_job(jid,expected='cancelled',timeout=120);drain(canceller,jid)
                def terminal():
                    job=canceller.call('GET','/jobs/'+str(jid));attempt=job['attempt'];stop=attempt.get('process_stop')
                    if attempt['phase']!='cancelled' or stop is None:return None
                    assert job['status']=='cancelled' and stop['verified']
                    return job
                stopped=wait_for(terminal,'UDF owner did not commit verified cancelled cleanup',60)
                (api.root/('udf-'+phase+'-cancelled-job.json')).write_text(json.dumps(stopped,indent=2),encoding='utf8')
                children=list(stopped['attempt']['child_processes']['children'].values())
                assert len(children)==1 and children[0]['purpose']=='udf_run'
                assert children[0]['stop']['verified'] and children[0]['stop']['native_state']=='exited'
                def exited():
                    states=native_inspect(distro,{'probe':[m['identity'] for m in members]})
                    return states if all(s['state']=='exited' for s in states) else None
                exits=wait_for(exited,'An original UDF '+phase+' process remained alive',60)
                assert cancel(repeater)['status']=='cancelled'
                old_history=wait_for(lambda:(v if (v:=api.call('GET','/workflow-runs/'+cancelled_flow['id']))['status']=='cancelled' else None),
                    'UDF workflow did not settle cancelled')
                assert api.call('GET',api.prefix+'/caseDict')['workflow_state']=='C'
                assert (stopped['attempt']['outputs'].get('publication') or {}).get('status')!='committed'
                after_cancel_heads=source.call('GET',source.prefix+'/testing/udf-heads')
                assert after_cancel_heads['solve']==original_heads['solve']
                assert after_cancel_heads['mutable_U_sha256']==original_heads['mutable_U_sha256']
                if phase!='solve':
                    assert after_cancel_heads['udf-build']==original_heads['udf-build']
                    assert after_cancel_heads['mutable_library_sha256']==original_heads['mutable_library_sha256']
                assert source.call('GET',source.prefix+'/raw?path='+quote(control_path),raw=True).decode()==active_control
                before_jobs=[j['id'] for j in api.call('GET',api.prefix+'/jobs')]
                launcher=kill_owned_api(source,source_pid)
                record({'stage':phase+'_cancelled','other_owner_refused':True,'cross_API_cancel':True,
                    'actual_owner_api_pid':owner_pid,'actual_canceller_api_pid':canceller_pid,
                    'repeat_cancel_same_terminal':True,'root_and_native_stop_verified':True,
                    'observed_members_all_exited':exits,'stage_C_not_advanced':True,
                    'incomplete_result_not_published':True,'old_library_preserved':phase!='solve',
                    'prior_solve_head_and_field_preserved':True,'actual_source_API_pid_killed':source_pid,'verified_launcher_pid':launcher})
            finally:cleanup()
        if store:assert not (recovery.root/'s3-cache').exists()
        with recovery:
            try:
                assert recovery.call('GET','/workflow-runs/'+cancelled_flow['id'])==old_history
                recovery.call('POST','/workflow-runs/'+cancelled_flow['id']+':dispatch',{'expected_version':old_history['version']})
                recovery.call('POST','/workflow-runs/'+cancelled_flow['id']+':cancel',{'expected_version':version})
                assert [j['id'] for j in api.call('GET',api.prefix+'/jobs')]==before_jobs
                assert recovery.call('GET','/workflow-runs/'+cancelled_flow['id'])==old_history
                marker='UDF_RECOVERED_'+phase.upper()
                put(recovery,cpp,original_cpp.replace('P0_UDF_OK',marker))
                if phase=='solve':
                    put(recovery,control_path,inputs[control_path]);put(recovery,mesh_path,inputs[mesh_path])
                dictionary=recovery.call('GET',recovery.prefix+'/caseDict')
                body={'expected_version':old_history['version'],'expected_revision':dictionary['dict_revision'],
                    'confirmed_reset':True,'target':'solve','parameters':parameters,'budget':budget}
                key=uuid.uuid4().hex;fresh=submit(recovery,'/workflow-runs/'+cancelled_flow['id']+':recompute',body,key)
                assert fresh['recomputation']['source_run_id']==cancelled_flow['id']
                assert fresh['recomputation']['rebuild']==(['mesh','udf-build','solve'] if phase=='solve' else ['udf-build','solve'])
                confirm(recovery,api.root,fresh['id'],'confirm_B');confirm(recovery,api.root,fresh['id'],'confirm_C')
                proof=solution(recovery,api.root,fresh,marker)
                assert proof['build_job']!=jid and proof['solve_job']!=jid
                assert recovery.call('GET',recovery.prefix+'/raw?path='+quote(control_path),raw=True).decode()==inputs[control_path]
                assert submit(recovery,'/workflow-runs/'+cancelled_flow['id']+':recompute',body,key)['id']==fresh['id']
                assert recovery.call('GET','/workflow-runs/'+cancelled_flow['id'])==old_history
                assert recovery.call('GET','/jobs/'+str(jid))['status']=='cancelled'
                assert recovery.call('GET','/workflow-runs/'+baseline['id'])==baseline_history
                record({'stage':phase+'_explicit_recovery','new_workflow':fresh['id'],'actual_recovery':proof,
                    'new_API_started_with_empty_independent_cache':bool(store),'cancelled_history_unchanged':True,
                    'baseline_history_unchanged':True,'terminal_dispatch_did_not_replay':True,
                    'same_recompute_key_same_new_run':True,'original_400_cell_controls_restored':True})
            finally:cleanup()
        assert all(server.proc.poll() is not None for server in [source,observer,recovery])
    assert len([r for r in checks if r['stage'].endswith('_explicit_recovery')])==3
    report={'passed':True,'backend':'s3' if store else 'fs','mock_llm_calls':0,'checks':checks,
        'scope':'Real cc1plus, ctypes CDLL and loaded-UDF icoFoam cancellation, observed birth identities exited, old publications preserved and explicit fresh workflow build/load/solve recovery; complete source policy, typed batches and original UDF409 remain'}
    (api.root/'udf-cancel-recovery.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    api.create_case('Independent actual UDF cancellation evidence readback')
    return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE,store_volume_max=16))
