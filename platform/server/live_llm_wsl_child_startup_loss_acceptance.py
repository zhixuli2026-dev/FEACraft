"""Real MPI child loss between kernel admission/preparation and running registration."""
import asyncio,hashlib,json,math,re,shlex,subprocess,sys,time
from urllib.parse import quote
import asyncpg
from cae_fixtures import cavity_2d
from app.services import process_identity_native as identity
from app.services.owned_process import probe_native,wsl_path
import workflow_batch_redis_support as workers_module
import live_llm_remote_worker_acceptance as baseline
from live_llm_child_startup_loss_acceptance import FAULT,CHECK as HOST_CHECK

RUN='C-run/case-1'
ADMISSION_FAULT='''
from app.services import job_native_scopes
original_native_admitted=job_native_scopes.admitted
async def crash_after_native_admitted(owner,entry):
 await original_native_admitted(owner,entry)
 root=Path(FAULT_ROOT)
 if (root/'child-armed').exists():
  (root/'child-armed').unlink()
  target=root/'child-prepared.json';staged=target.with_suffix('.staged')
  staged.write_text(json.dumps({'job_id':owner.job_id,'attempt_id':owner.attempt_id,'child_id':entry.id,
   'worker':identity.observe(os.getpid())}))
  staged.replace(target)
  while not (root/'child-crash-now').exists():await asyncio.sleep(.02)
  os._exit(17)
job_native_scopes.admitted=crash_after_native_admitted
'''
CHECK=HOST_CHECK.replace('  refused=[]', '''  edits.extend([
   ('missing_native_history',('recovery','native_history'),[]),
   ('missing_retirement',('recovery','native_retirements'),[]),
   ('changed_scope_inode',('recovery','native_retirements',0,'retirement','request','arguments','scope','inode'),1),
   ('live_native_gate',('recovery','native_retirements',0,'native_gate_exit'),{'state':'match','reason':'birth_and_scope_match'}),
   ('unverified_native_tree',('recovery','native_retirements',0,'native_tree_verified'),False),
   ('changed_guardian_binding',('recovery','native_retirements',0,'child_guardian_sha256'),'0'*64)])
  refused=[]''')


def vectors(raw):
    match=re.search(r'internalField\s+nonuniform\s+List<vector>\s+(\d+)\s*\((.*?)\)\s*;',raw.decode(),re.S)
    assert match,'Actual solver did not publish its vector field'
    rows=[tuple(float(v) for v in row.split()) for row in re.findall(r'\(([^()]*)\)',match[2])]
    assert len(rows)==int(match[1])==400 and all(len(r)==3 and all(math.isfinite(v) and abs(v)<5 for v in r) for r in rows)
    return rows


async def scenario(api,worker,selected,*,stage='prepared',filename='wsl-child-startup-loss.json',cancel_after_exit=False,certificate_check=None,fault_override=None,handoff=False):
    assert stage in {'prepared','admitted','unobserved','created','host_ready','native_ready'} or handoff and fault_override is not None
    result={'passed':False,'mock_llm_calls':0,'jobs':[],'stage':stage,'cancel_after_exit':cancel_after_exit}
    fault_root=api.root/('wsl-child-'+stage);fault_root.mkdir()
    def save():(api.root/filename).write_text(json.dumps(result,indent=2)+'\n','utf8')
    async def call(method,path,**kwargs):
        deadline=time.monotonic()+45
        while True:
            try:return await asyncio.to_thread(api.call,method,path,**kwargs)
            except AssertionError as error:
                if not any(c in str(error) for c in ('storage_quota_busy','validation_busy')) or time.monotonic()>deadline:raise
                await asyncio.sleep(.2)
    async def raw(path):return await call('GET',api.prefix+'/raw?path='+quote(path,safe=''),raw=True)
    async def settled(identifier):
        deadline=time.monotonic()+300
        while True:
            job=await call('GET','/jobs/'+str(identifier))
            if job['status']=='succeeded' and job['attempt']['outputs'].get('completion',{}).get('state')=='settled':
                assert job['attempt']['process_stop']['verified']
                result['jobs'].append(job);save();return job
            if job['status'] in {'failed','cancelled','interrupted'}:
                result['failed_job']=job;save();raise AssertionError('Actual engineering job failed')
            assert time.monotonic()<deadline,'Actual engineering publication did not settle'
            await asyncio.sleep(.3)
    connection=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,
        password=selected.password,database=selected.database,timeout=10)
    async def snapshot(identifier):
        value=dict(await connection.fetchrow('''SELECT j.status,a.id,a.phase,a.reason_code,a.cancel_requested_at,a.worker_identity_json,
          a.process_identity_json,a.child_processes_json,a.write_checkpoint_json,a.output_manifest_json,
          a.input_manifest_json,a.publication_json FROM jobs j JOIN job_attempts a ON a.job_id=j.id WHERE j.id=$1''',identifier))
        value['cancel_requested_at']=value['cancel_requested_at'].isoformat() if value['cancel_requested_at'] else None
        return value
    api.env.update(VIBEFLOW_WORKER_HOST_QUEUE='true',VIBEFLOW_JOB_HEARTBEAT_SECONDS='1')
    windows=workers_module.Workers(api,worker.broker,worker.store);original=workers_module.WORKER
    windows.sequence=len(list(api.root.glob('batch-worker-*')))
    from child_observation_acceptance_support import fault_for
    fault=fault_override if fault_override is not None else FAULT if stage=='prepared' else ADMISSION_FAULT if stage=='admitted' else fault_for(stage)
    workers_module.WORKER=original.replace('asyncio.run(main())','FAULT_ROOT='+repr(str(fault_root))+'\n'+fault+'\nasyncio.run(main())')
    try:
        await asyncio.to_thread(windows.start)
        for path,value in cavity_2d().items():await call('PUT',api.prefix+'/file',body={'path':path,'content':value})
        serial=await call('POST',api.prefix+'/jobs',body={'type':'convert','runtime':'wsl','timeout_seconds':180,
            'command':'test "$(id -u)" -eq 65534 && source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd '+RUN+' && blockMesh && icoFoam'})
        serial=await settled(serial['id'])
        assert serial['attempt']['process_identity']['native']['process']['uid']==65534
        result['serial_command_nonprivileged']=True;save()
        reference=vectors(await raw(RUN+'/0.5/U'))
        parameters={'run_dir':RUN,'solver':'icoFoam','subdomains':2}
        first=await call('POST',api.prefix+'/mpi:run',body=parameters);first=await settled(first['job_id'])
        field=RUN+'/0.5/U';prior=await raw(field);parallel=vectors(prior)
        error=max(abs(a-b) for va,vb in zip(reference,parallel) for a,b in zip(va,vb))/max(1,max(abs(v) for row in reference for v in row))
        assert error<=1e-5;result['serial_parallel_relative_error']=error
        result['prior_field_sha256']=hashlib.sha256(prior).hexdigest();save()
        (fault_root/'child-armed').touch()
        created=await call('POST',api.prefix+'/mpi:run',body=parameters);identifier=created['job_id']
        deadline=time.monotonic()+90
        while not (fault_root/'child-prepared.json').exists():
            assert time.monotonic()<deadline,'MPI child did not reach its actual prepared boundary'
            await asyncio.sleep(.05)
        marker=json.loads((fault_root/'child-prepared.json').read_text());assert marker['job_id']==identifier
        result['independent_fault_observation']=marker
        before=await snapshot(identifier);result['before']=before;save()
        child=json.loads(before['child_processes_json'])['children'][marker['child_id']]
        assert before['phase']=='running' and before['write_checkpoint_json']=='{}'
        assert child['phase']=='preparing' and child['purpose']=='mpi_run' and child['identity'] is None and child['stop'] is None
        rows=await connection.fetch('SELECT type,payload_json FROM durable_events WHERE attempt_id=$1 ORDER BY seq',before['id'])
        admitted=[json.loads(r['payload_json']) for r in rows if r['type']=='attempt.native_scope_admitted']
        assert len(admitted)==(1 if stage in {'prepared','admitted'} else 0)
        prepared=[json.loads(r['payload_json']) for r in rows if r['type']=='attempt.child_launch_prepared']
        assert len(prepared)==(1 if stage=='prepared' else 0)
        process=admitted[0]['value']['identity'] if admitted else marker.get('preparation')
        if prepared:assert process==prepared[0]['identity']
        result['original_child_prepared_count']=len(prepared)
        native=process.get('native') if process else None
        if native is not None:
            assert native['process']['uid']==65534
            assert (await probe_native(native['distribution'],native['process']))['state']=='match'
        result['actual_prepared_identity']=process;result['actual_admission']=admitted[0] if admitted else None;save()
        await asyncio.sleep(1);assert (await snapshot(identifier))['write_checkpoint_json']=='{}'
        crashed=next(r for r in windows.running if r['identity']==marker['worker'])
        if cancel_after_exit:
            windows.running.remove(crashed);await asyncio.to_thread(windows.stop);windows.running.append(crashed)
        (fault_root/'child-crash-now').touch();await asyncio.to_thread(crashed['process'].wait,30)
        crashed['log'].close();windows.running.remove(crashed)
        assert crashed['process'].returncode==17 and identity.probe(marker['worker'])['state']=='exited'
        result['crash']={'identity':marker['worker'],'exit_code':17,'actual_probe':identity.probe(marker['worker'])};save()
        if cancel_after_exit:
            await call('POST','/jobs/'+str(identifier)+':cancel')
            pending=await snapshot(identifier);result['cancelled_before_recovery']=pending;save()
            assert pending['status']=='cancelled' and pending['phase']=='cancelling' and pending['cancel_requested_at']
            await asyncio.to_thread(windows.start)
        expected='cancelled' if cancel_after_exit else 'interrupted'
        deadline=time.monotonic()+120
        while True:
            after=await snapshot(identifier);result['after']=after;save()
            if after['phase']==expected and after['write_checkpoint_json']!='{}':break
            assert time.monotonic()<deadline,'Prepared WSL child remains unrecovered after exact worker loss'
            await asyncio.sleep(.3)
        assert after['status']==expected and after['reason_code']==('cancel_requested' if cancel_after_exit else 'recovery_required')
        for key in ['id','worker_identity_json','process_identity_json','child_processes_json','input_manifest_json','publication_json']:
            assert before[key]==after[key],key
        assert json.loads(after['output_manifest_json']).get('process_stop') is None
        proof=json.loads(after['write_checkpoint_json'])['recovery'];assert proof['schema']==('vibeflow.job-child-handoff-recovery.v1' if handoff else 'vibeflow.job-child-startup-recovery.v1')
        if handoff:
            from child_handoff_acceptance_support import verify_independent
            result['independent_handoff_checks']=await verify_independent(marker)
            assert await connection.fetchval('SELECT count(*) FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.child_launch_observed')==0
        elif stage not in {'prepared','admitted'}:
            observed=identity.probe(marker['preparation']['host']);assert observed['state']=='exited'
            result['independent_child_host_exit']=observed
            assert await connection.fetchval('SELECT count(*) FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.native_scope_admitted')==0
        if cancel_after_exit:assert proof['cancel_requested_at']==pending['cancel_requested_at']==after['cancel_requested_at']
        if handoff:
            assert proof['native_retirements']==[]
            receipt=proof['child_handoff_closures'][marker['child_id']]['closure']['retirement']
            assert receipt is None if stage=='allocation_intent' else receipt['result']['state']=='absent'
        else:assert len(proof['native_retirements'])==1 and proof['native_retirements'][0]['retirement']['result']['state']=='absent'
        if native is not None:
            result['native_gate_exit']=await probe_native(native['distribution'],native['process'])
            assert result['native_gate_exit']['state']=='exited'
        assert await connection.fetchval('SELECT count(*) FROM job_attempts WHERE job_id=$1',identifier)==1
        assert await connection.fetchval('SELECT count(*) FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.child_running')==0
        assert await connection.fetchval('SELECT count(*) FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.child_launch_prepared')==len(prepared)
        assert await raw(field)==prior;result['original_field_preserved']=True
        check=CHECK if certificate_check is None else certificate_check
        if stage=='admitted':
            check=check.replace("child,-1,1,'identity','host','pid'", "child,-1,1,'value','identity','host','pid'")
            check=check.replace('  refused=[]', '''  edits.extend([
   ('missing_native_ready',('recovery','child_preparations',child),original['recovery']['child_preparations'][child][:-2]+original['recovery']['child_preparations'][child][-1:]),
   ('changed_admission_seed',('recovery','child_preparations',child,-1,1,'binding','seed'),'0'*64),
   ('changed_admission_observation',('recovery','child_preparations',child,-1,1,'value','observation','result','state'),'absent')])
  refused=[]''')
        env=dict(api.env);env.update(worker.broker.settings());env.update(worker.store.settings())
        checked=await asyncio.to_thread(subprocess.run,[sys.executable,'-X','utf8','-B','-c',check,str(identifier)],
            cwd=baseline.SERVER,env=env,capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        assert checked.returncode==0,checked.stderr[-1500:]
        result['certificate_checks']=json.loads(checked.stdout);save()
        prefix="import json,os,sys;from pathlib import Path;os.environ.update(json.loads(Path(sys.argv[1]).read_text()));sys.path.insert(0,"+repr(wsl_path(baseline.SERVER))+");del sys.argv[1]\n"
        checked=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec',baseline.PYTHON,
            '-B','-c',prefix+check,wsl_path(worker.root/'settings.json'),str(identifier)],
            capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        assert checked.returncode==0,checked.stderr[-1500:]
        result['linux_certificate_checks']=json.loads(checked.stdout);save()
        fresh=await call('POST',api.prefix+'/mpi:run',body=parameters);fresh=await settled(fresh['job_id'])
        assert fresh['attempt']['outputs']['publication']['id']!=first['attempt']['outputs']['publication']['id']
        current=await raw(field);values=vectors(current)
        assert max(abs(a-b) for va,vb in zip(parallel,values) for a,b in zip(va,vb))<1e-5
        result['fresh_field_sha256']=hashlib.sha256(current).hexdigest()
        native_children=[c for c in fresh['attempt']['child_processes']['children'].values() if c['identity']['native']]
        assert native_children and all(c['identity']['native']['process']['uid']==65534 and c['stop']['verified'] for c in native_children)
        digest=result['fresh_field_sha256'];code="from pathlib import Path;Path('Ref/proof.txt').write_text("+repr('RETIREMENT_'+digest+'\n')+")"
        proof_job=await call('POST',api.prefix+'/jobs',body={'type':'convert','runtime':'windows','command':'python3 -c '+shlex.quote(code)})
        await settled(proof_job['id']);result['passed']=True;save()
        print('WSL_CHILD_STARTUP_LOSS_PASS '+stage+': actual kernel admission/retirement; missing running/stop preserved; fresh two-rank 400-cell solve published',flush=True)
    finally:
        (fault_root/'child-crash-now').touch();workers_module.WORKER=original
        try:
            await asyncio.to_thread(windows.stop);result['windows_workers']=windows.receipts
        finally:await connection.close();save()


async def verify(configuration,selected,admin,observed):
    windows={'host_id':identity.host_id(),'platform':'windows'}
    environment={'VIBEFLOW_JOB_EXECUTION_HOSTS':json.dumps({'windows':{'host_id':observed['host_id'],'platform':'linux'},'wsl':windows}),
                 'VIBEFLOW_JOB_HANDLER_EXECUTION_HOSTS':json.dumps({'mpi.openfoam':windows})}
    return await baseline.verify(configuration,selected,admin,observed,after_publish=scenario,environment_overrides=environment)

if __name__=='__main__':asyncio.run(baseline.main(entry='platform/server/live_llm_wsl_child_startup_loss_acceptance.py',verifier=verify))
