"""Real LaTeX completed native scope followed by owner loss; real DeepSeek."""
import asyncio,hashlib,io,json,shlex,subprocess,sys,time
import asyncpg
from pypdf import PdfReader
from app.services import process_identity_native as identity
from app.services.owned_process import probe_native,wsl_path
import workflow_batch_redis_support as workers_module
import live_llm_remote_worker_acceptance as baseline

FAULT='''
from app.services import child_launch_registration,job_native_scopes
prepared_counts={}
async def boundary(owner,child,stage):
 root=Path(FAULT_ROOT);armed=root/'armed'
 if not armed.exists() or armed.read_text()!=stage:return
 if stage=='prepared':
  prepared_counts[owner.attempt_id]=prepared_counts.get(owner.attempt_id,0)+1
  if prepared_counts[owner.attempt_id]!=2:return
 armed.unlink()
 path=root/'boundary.json';staged=path.with_suffix('.staged')
 staged.write_text(json.dumps({'job_id':owner.job_id,'attempt_id':owner.attempt_id,'child_id':child,'worker':identity.observe(os.getpid())}))
 staged.replace(path)
 while not (root/'crash-now').exists():await asyncio.sleep(.02)
 os._exit(17)
original_prepared=child_launch_registration.prepared
async def prepared(owner,parent_nonce,child,process):
 await original_prepared(owner,parent_nonce,child,process)
 await boundary(owner,child,'prepared')
child_launch_registration.prepared=prepared
original_retire=job_native_scopes.retire
async def retire(owner,entry):
 await original_retire(owner,entry)
 await boundary(owner,entry.id,'retired')
job_native_scopes.retire=retire
'''

CHECK='''
import asyncio,copy,json,sys
from sqlalchemy import select
from app.db import SessionLocal,engine
from app.models import Job,JobAttempt,Case
from app.services.job_scope_recovery import validate_certificate
from app.services.job_write_checkpoint import validate
from app.services.native_scope_control import validate_receipt
async def main():
 async with SessionLocal() as db:
  job=await db.get(Job,int(sys.argv[1]))
  attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
  case=await db.get(Case,job.case_id);original=json.loads(attempt.write_checkpoint_json)
  validate(case,job,attempt);validate_certificate(case,job,attempt,original)
  history=original['recovery']['native_history']
  old={p['binding']['child_id']:p['value'] for k,p in history if k=='attempt.native_scope_retired'}
  assert len(old)==1
  index=next(i for i,p in enumerate(original['recovery']['native_retirements']) if p['child_id'] in old)
  reused=original['recovery']['native_retirements'][index]
  assert reused['retirement']==old[reused['child_id']]
  changed_receipt=copy.deepcopy(reused['retirement'])
  changed_receipt['result']['removed_this_call']=not changed_receipt['result']['removed_this_call']
  validate_receipt(changed_receipt)  # Individually well-formed, but not the original receipt.
  edits=[('invented_stop',('stop',),{}),('missing_guardians',('recovery','guardians'),[]),
   ('live_owner',('recovery','worker_exit'),{'state':'match','reason':'birth_and_scope_match'}),
   ('changed_snapshot',('recovery','snapshot_sha256'),'0'*64),
   ('missing_kernel_history',('recovery','native_history'),[]),
   ('missing_kernel_retirements',('recovery','native_retirements'),[]),
   ('wrong_original_stop_fact',('recovery','native_retirements',index,'original_stop_missing'),not reused['original_stop_missing']),
   ('substituted_original_retirement',('recovery','native_retirements',index,'retirement'),changed_receipt),
   ('live_native_gate',('recovery','native_retirements',index,'native_gate_exit'),{'state':'match','reason':'birth_and_scope_match'}),
   ('changed_scope_inode',('recovery','native_retirements',index,'retirement','request','arguments','scope','inode'),1),
   ('unverified_native_tree',('recovery','native_retirements',index,'native_tree_verified'),False),
   ('changed_guardian_binding',('recovery','native_retirements',index,'child_guardian_sha256'),'0'*64)]
  if original['recovery'].get('child_preparations'):
   edits.append(('missing_child_preparation',('recovery','child_preparations'),{}))
  refused=[]
  for name,path,value in edits:
   altered=copy.deepcopy(original);target=altered
   for key in path[:-1]:target=target[key]
   target[path[-1]]=value
   try:validate_certificate(case,job,attempt,altered)
   except (ValueError,KeyError,TypeError,AttributeError):refused.append(name)
   else:raise AssertionError('Altered completed-child certificate accepted: '+name)
  assert json.loads(attempt.write_checkpoint_json)==original
  print(json.dumps({'original_valid':True,'refused':refused,'database_modified':False,'original_receipts_reused':len(old)}))
 await engine.dispose()
asyncio.run(main())
'''


async def scenario(api,worker,selected,*,stage):
    root=api.root/('completed-child-'+stage);root.mkdir()
    result={'passed':False,'stage':stage,'jobs':[],'mock_llm_calls':0}
    def save():(root/'result.json').write_text(json.dumps(result,indent=2)+'\n','utf8')
    async def call(method,path,**kwargs):
        deadline=time.monotonic()+45
        while True:
            try:return await asyncio.to_thread(api.call,method,path,**kwargs)
            except AssertionError as error:
                if not any(c in str(error) for c in ('storage_quota_busy','validation_busy')) or time.monotonic()>deadline:raise
                await asyncio.sleep(.2)
    async def settled(identifier):
        deadline=time.monotonic()+300
        while True:
            job=await call('GET','/jobs/'+str(identifier))
            if job['status']=='succeeded' and job['attempt']['outputs'].get('completion',{}).get('state')=='settled':
                assert job['attempt']['process_stop']['verified'];result['jobs'].append(job);save();return job
            if job['status'] in {'failed','cancelled','interrupted'}:
                result['failed_job']=job;save();raise AssertionError('Actual LaTeX publication failed')
            assert time.monotonic()<deadline,'Actual LaTeX publication did not settle'
            await asyncio.sleep(.3)
    connection=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,
        password=selected.password,database=selected.database,timeout=10)
    async def snapshot(identifier):
        return dict(await connection.fetchrow('''SELECT j.status,a.id,a.phase,a.reason_code,a.worker_identity_json,
          a.process_identity_json,a.child_processes_json,a.write_checkpoint_json,a.output_manifest_json,
          a.input_manifest_json,a.publication_json FROM jobs j JOIN job_attempts a ON a.job_id=j.id WHERE j.id=$1''',identifier))
    api.env.update(VIBEFLOW_WORKER_HOST_QUEUE='true',VIBEFLOW_JOB_HEARTBEAT_SECONDS='1')
    windows=workers_module.Workers(api,worker.broker,worker.store)
    windows.sequence=len(list(api.root.glob('batch-worker-*')));original=workers_module.WORKER
    workers_module.WORKER=original.replace('asyncio.run(main())','FAULT_ROOT='+repr(str(root))+'\n'+FAULT+'\nasyncio.run(main())')
    main='E-report/latex/main.tex';pdf='E-report/latex/build/main.pdf'
    def document(text):return '\\documentclass{article}\n\\begin{document}'+text+'\\end{document}\n'
    parameters={'main':main,'recipe':'pdflatex','resources':[]}
    try:
        await asyncio.to_thread(windows.start)
        await call('PUT',api.prefix+'/file',body={'path':main,'content':document('BEFORELOSS '+stage)})
        initial=await call('POST',api.prefix+'/latex:build',body=parameters);await settled(initial['job_id'])
        prior=await call('GET',api.prefix+'/raw?path='+pdf,raw=True)
        assert 'BEFORELOSS' in ''.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(prior)).pages)
        result['prior_pdf_sha256']=hashlib.sha256(prior).hexdigest();save()
        await call('PUT',api.prefix+'/file',body={'path':main,'content':document('AFTERLOSS '+stage)})
        (root/'armed').write_text(stage)
        created=await call('POST',api.prefix+'/latex:build',body=parameters);identifier=created['job_id']
        deadline=time.monotonic()+90
        while not (root/'boundary.json').exists():
            assert time.monotonic()<deadline,'Actual LaTeX did not reach original retirement boundary'
            await asyncio.sleep(.05)
        marker=json.loads((root/'boundary.json').read_text());assert marker['job_id']==identifier
        before=await snapshot(identifier);result['before']=before
        children=json.loads(before['child_processes_json'])['children'];assert before['phase']=='running'
        rows=await connection.fetch('SELECT type,payload_json FROM durable_events WHERE attempt_id=$1 ORDER BY seq',before['id'])
        original_retired={json.loads(r['payload_json'])['binding']['child_id']:json.loads(r['payload_json'])['value']
            for r in rows if r['type']=='attempt.native_scope_retired'}
        assert len(original_retired)==1;finished_id=next(iter(original_retired));finished=children[finished_id]
        assert finished['purpose']=='latex_probe' and finished['identity']['native']['process']['uid']==65534
        if stage=='prepared':
            assert len(children)==2 and finished['phase']=='stopped' and finished['stop']['verified']
            waiting=children[marker['child_id']]
            assert waiting['purpose']=='latex_compile' and waiting['phase']=='preparing' and waiting['identity'] is None and waiting['stop'] is None
        else:
            assert len(children)==1 and finished_id==marker['child_id'] and finished['phase']=='running' and finished['stop'] is None
        result['original_retirements']=original_retired;save()
        await asyncio.sleep(1);assert (await snapshot(identifier))['write_checkpoint_json']=='{}'
        crashed=next(r for r in windows.running if r['identity']==marker['worker'])
        (root/'crash-now').touch();await asyncio.to_thread(crashed['process'].wait,30)
        crashed['log'].close();windows.running.remove(crashed)
        assert crashed['process'].returncode==17 and identity.probe(marker['worker'])['state']=='exited'
        result['crash']={'identity':marker['worker'],'exit_code':17,'actual_probe':identity.probe(marker['worker'])};save()
        deadline=time.monotonic()+120
        while True:
            after=await snapshot(identifier);result['after']=after;save()
            if after['phase']=='interrupted' and after['write_checkpoint_json']!='{}':break
            assert time.monotonic()<deadline,'Completed native scope prevented recovery of its original attempt'
            await asyncio.sleep(.3)
        assert after['status']=='interrupted' and after['reason_code']=='recovery_required'
        for key in ['id','worker_identity_json','process_identity_json','child_processes_json','input_manifest_json','publication_json']:
            assert before[key]==after[key],key
        assert json.loads(after['output_manifest_json']).get('process_stop') is None
        checkpoint=json.loads(after['write_checkpoint_json']);assert checkpoint['stop'] is None
        proof=checkpoint['recovery'];assert len(proof['native_retirements'])==len(children)
        reused=next(v for v in proof['native_retirements'] if v['child_id']==finished_id)
        assert reused['retirement']==original_retired[finished_id]
        assert reused['original_stop_missing']==(finished['stop'] is None)
        assert proof['original_child_stop_missing']==sorted(k for k,c in children.items() if c['stop'] is None)
        current_retired=await connection.fetch('SELECT payload_json FROM durable_events WHERE attempt_id=$1 AND type=$2',before['id'],'attempt.native_scope_retired')
        assert len(current_retired)==1 and json.loads(current_retired[0]['payload_json'])['value']==original_retired[finished_id]
        assert await connection.fetchval('SELECT count(*) FROM job_attempts WHERE job_id=$1',identifier)==1
        assert await call('GET',api.prefix+'/raw?path='+pdf,raw=True)==prior
        result['original_pdf_preserved']=True
        env=dict(api.env);env.update(worker.broker.settings());env.update(worker.store.settings())
        checked=await asyncio.to_thread(subprocess.run,[sys.executable,'-X','utf8','-B','-c',CHECK,str(identifier)],
            cwd=baseline.SERVER,env=env,capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        assert checked.returncode==0,checked.stderr[-1500:]
        result['certificate_checks']=json.loads(checked.stdout);save()
        prefix="import json,os,sys;from pathlib import Path;os.environ.update(json.loads(Path(sys.argv[1]).read_text()));sys.path.insert(0,"+repr(wsl_path(baseline.SERVER))+");del sys.argv[1]\n"
        checked=await asyncio.to_thread(subprocess.run,['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec',baseline.PYTHON,
            '-B','-c',prefix+CHECK,wsl_path(worker.root/'settings.json'),str(identifier)],
            capture_output=True,text=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
        assert checked.returncode==0,checked.stderr[-1500:]
        result['linux_certificate_checks']=json.loads(checked.stdout);save()
        fresh=await call('POST',api.prefix+'/latex:build',body=parameters);await settled(fresh['job_id'])
        current=await call('GET',api.prefix+'/raw?path='+pdf,raw=True)
        text=''.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(current)).pages)
        assert 'AFTERLOSS' in text and 'BEFORELOSS' not in text
        result['fresh_pdf_sha256']=hashlib.sha256(current).hexdigest()
        code="from pathlib import Path;Path('Ref/proof.txt').write_text("+repr('RETIREMENT_'+result['fresh_pdf_sha256']+'\n')+")"
        fresh=await call('POST',api.prefix+'/jobs',body={'type':'convert','runtime':'windows','command':'python3 -c '+shlex.quote(code)})
        await settled(fresh['id']);result['passed']=True;save()
        print('COMPLETED_NATIVE_CHILD_PASS '+stage+' original retirement reused; fresh actual PDF and Linux publication',flush=True)
    finally:
        (root/'crash-now').touch();workers_module.WORKER=original
        try:
            await asyncio.to_thread(windows.stop);result['windows_workers']=windows.receipts
        finally:await connection.close();save()


async def scenarios(api,worker,selected):
    await scenario(api,worker,selected,stage='prepared')
    await scenario(api,worker,selected,stage='retired')


async def verify(configuration,selected,admin,observed):
    environment={'VIBEFLOW_JOB_HANDLER_EXECUTION_HOSTS':json.dumps({'latex.build':{'host_id':identity.host_id(),'platform':'windows'}})}
    return await baseline.verify(configuration,selected,admin,observed,after_publish=scenarios,environment_overrides=environment)


if __name__=='__main__':asyncio.run(baseline.main(entry='platform/server/live_llm_completed_native_child_acceptance.py',verifier=verify))
