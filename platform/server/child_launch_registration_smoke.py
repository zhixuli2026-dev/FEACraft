"""Real child preparation faults, retained cleanup and dead-owner reconciliation."""
import argparse,asyncio,hashlib,json,re,tempfile,time,uuid
from pathlib import Path
from sqlalchemy.engine import make_url

from testsupport import IsolatedServer,SERVER
from pandoc_execution_smoke import CONTROLLED
from native_execution_smoke import CONTROLLED as NATIVE_CONTROLLED,NORMAL
from professional_launch_smoke import settled
from job_release_smoke import wait_file
from job_owner_startup_smoke import shared
from workflow_migration_smoke import alembic
from app.services import process_identity_native as identity

EXTRA='''
from fastapi import Depends,HTTPException
from sqlalchemy import select
from app.db import SessionLocal
from app.models import Job,JobAttempt,DurableEvent
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import job_recovery,job_execution,child_launch_registration as launches
async def defer_recovery():return {'fixture':'explicit_reconciliation'}
job_recovery.reconcile_startup=defer_recovery
event_original=job_execution.attempt_event
async def event(db,job,attempt,kind,payload):
 result=await event_original(db,job,attempt,kind,payload)
 if kind in launches.KINDS:
  stage={launches.INTENT:'intent',launches.PREPARED:'prepared',launches.ABORTED:'aborted'}.get(kind)
  if kind==launches.OBSERVED:stage=payload['preparation']['stage']
  db.info['child_stage']=(stage,job.case_id)
 return result
job_execution.attempt_event=event
child_commit=AsyncSession.commit
async def commit_child(db):
 stage,case_id=db.info.pop('child_stage',(None,None));setting=mode(case_id)
 if stage and setting==stage+'_before':raise OSError('Injected child evidence pre-commit failure')
 result=await child_commit(db)
 if stage and setting==stage+'_after':raise OSError('Injected child evidence acknowledgement loss')
 return result
AsyncSession.commit=commit_child
child_execute=native.NativeExecutor._execute
async def execute_child(self,entry,purpose,parameters,argv,timeout,limit,output):
 if not hasattr(self.owner,'job_id'):return await child_execute(self,entry,purpose,parameters,argv,timeout,limit,output)
 case_id=case(self);setting=mode(case_id)
 entry.owned.test_case_id=case_id
 (root/('child-job-'+str(case_id))).write_text(str(self.owner.job_id))
 if setting.endswith(('_before','_after','_hold')):
  marker=root/('forbidden-'+str(case_id))
  if self.runtime=='wsl':argv=[*argv[:4],'python3','-I','-S','-c','from pathlib import Path;Path('+repr(native.wsl_path(marker))+').touch()']
  else:argv=[sys.executable,'-I','-S','-c','from pathlib import Path;Path('+repr(str(marker))+').touch()']
 return await child_execute(self,entry,purpose,parameters,argv,timeout,limit,output)
native.NativeExecutor._execute=execute_child
preparation_original=native.OwnedProcess._preparation
async def preparation(self,stage,runtime,host,native_identity=None):
 case_id=getattr(self,'test_case_id',None)
 if case_id is not None:
  (root/('birth-'+str(case_id)+'.json')).write_text(json.dumps(host))
 await preparation_original(self,stage,runtime,host,native_identity)
 if case_id is not None and mode(case_id)==stage+'_hold':
  (root/('waiting-'+str(case_id))).touch()
  for _ in range(2000):
   if (root/('continue-'+str(case_id))).exists():return
   await asyncio.sleep(.02)
  raise TimeoutError('Child preparation barrier')
native.OwnedProcess._preparation=preparation
child_register=records.register
async def register_child(owner,nonce,identifier,*args):
 result=await child_register(owner,nonce,identifier,*args)
 if mode(owners[owner.job_id])=='registration_after':raise OSError('Injected child running acknowledgement loss')
 return result
records.register=register_child
saved={}
@app.post('/api/v1/fixture/child/{job_id}/{action}')
async def child_check(job_id:int,action:str,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  job=await db.get(Job,job_id);await get_owned_case(job.case_id,user,db)
  a=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id))
  if action=='clear':a.write_checkpoint_json='{}';await db.commit();return {'ok':True}
  if action=='history':
   events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==a.id).order_by(DurableEvent.seq)))
   return {'job_status':job.status,'phase':a.phase,'checkpoint':json.loads(a.write_checkpoint_json),
    'children':json.loads(a.child_processes_json),'stop':json.loads(a.output_manifest_json).get('process_stop'),
    'events':[{'type':e.type,'payload':json.loads(e.payload_json)} for e in events]}
  if action.startswith('mutate_'):
   kind={'mutate_intent':launches.INTENT,'mutate_observed':launches.OBSERVED,
    'mutate_prepared':launches.PREPARED,'mutate_aborted':launches.ABORTED}[action]
   row=await db.scalar(select(DurableEvent).where(DurableEvent.attempt_id==a.id,DurableEvent.type==kind).order_by(DurableEvent.seq).limit(1))
   key=(job_id,action)
   if key in saved:row.payload_json=saved.pop(key)
   else:
    saved[key]=row.payload_json;value=json.loads(row.payload_json)
    if kind==launches.INTENT:value['request_sha256']='0'*64
    elif kind==launches.OBSERVED:value['preparation']['host']['creation_time_100ns']='1'
    elif kind==launches.PREPARED:value['identity']['launch_nonce']='0'*64
    else:value['stop']['verified']=False
    row.payload_json=json.dumps(value)
   await db.commit();return {'ok':True}
  if action=='reclaim':
   try:await job_execution.claim(db,job_id,json.loads(a.input_manifest_json)['execution_spec'])
   except job_execution.ExecutionConflict as error:raise HTTPException(409,error.code)
   raise AssertionError('Unresolved child was reclaimed')
 return {'result':await job_recovery.reconcile_job(job_id)}
'''


def new_api(label,database_url=None,overrides=None,*,runtime='windows'):
    api=IsolatedServer(label,app='child_fixture:app',database_url=database_url)
    fixture=NATIVE_CONTROLLED if runtime=='wsl' else CONTROLLED
    (api.root/'child_fixture.py').write_text('import os\nos.environ.update('+repr(overrides or {})+')\n'+fixture+EXTRA,encoding='utf8')
    return api


def call(api,job,action,**kwargs):
    return api.call('POST',f'/fixture/child/{job}/{action}',**kwargs)


def exited(birth):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        if identity.probe(birth)=={'state':'exited','reason':'process_exited'}:return
        time.sleep(.05)
    raise AssertionError('Actual fixture process did not exit')


def launch(api,mode,runtime='windows'):
    api.create_case(mode)
    (api.root/f'mode-{api.case_id}').write_text(mode)
    if runtime=='wsl':
        main='E-report/latex/main.tex';api.put(main,NORMAL)
        return api.call('POST',api.prefix+'/latex:build',{'main':main,'recipe':'pdflatex'})['job_id']
    api.put('E-report/report.md','# Actual child registration\n')
    return api.call('POST',api.prefix+'/report:build',{})['job_id']


def verify(database_url=None,*,runtime='windows'):
    checks=[];receipts=[]
    def passed(name):checks.append(name);print('PASS '+name,flush=True)
    api=new_api('child-launch-faults',database_url,runtime=runtime)
    with api:
        jobs=[]
        modes=['intent_before','intent_after','created_before','created_after','host_ready_before','host_ready_after']
        if runtime=='wsl':modes+=['native_ready_before','native_ready_after']
        modes+=['prepared_before','prepared_after','commit_fail','registration_after']
        for mode in modes:
            job=launch(api,mode,runtime);row=settled(api,job);state=call(api,job,'history')
            assert row['status']=='failed' and row['attempt']['process_stop']['verified']
            assert not (api.root/f'forbidden-{api.case_id}').exists()
            assert not (api.root/f'forbidden-payload-{api.case_id}').exists()
            children=list(state['children']['children'].values());assert len(children)==1
            child=children[0];assert child['phase']=='failed'
            kinds=[e['type'] for e in state['events']]
            assert kinds.count('attempt.child_running')==int(mode=='registration_after')
            incomplete=mode in {'intent_before','intent_after','created_before','created_after'} or (runtime=='wsl' and mode.startswith('host_ready'))
            if incomplete:
                assert child['identity'] is None and child['stop'] is None and state['checkpoint']=={}
                assert 'attempt.child_launch_aborted' not in kinds
            else:
                assert child['identity'] is not None and child['stop']['verified'] and state['checkpoint']
                assert kinds.count('attempt.child_launch_aborted')==int(mode!='registration_after')
                jobs.append((job,mode,state,api.token))
            birth=api.root/f'birth-{api.case_id}.json'
            if birth.exists():exited(json.loads(birth.read_text()))
            elif mode not in {'intent_before','intent_after'}:raise AssertionError('Missing actual child birth')
            passed(mode+'_retains_actual_evidence_without_payload_release')
            receipts.append({'mode':mode,'job':row,'history':state})
        for job,mode,state,token in jobs:
            api.token=token;call(api,job,'clear')
        birth=json.loads((api.root/'api-identity.json').read_text());api.proc.kill();api.proc.wait(timeout=10);exited(birth)
        with new_api('child-launch-reader',database_url,shared(api),runtime=runtime) as peer:
            peer.token=api.token
            for job,mode,state,token in jobs:
                peer.token=token
                if mode=='commit_fail':
                    for field in ('intent','observed','prepared','aborted'):
                        call(peer,job,'mutate_'+field)
                        assert call(peer,job,'recover')['result']=='drain_history_unverified',field
                        assert call(peer,job,'history')['checkpoint']=={}
                        call(peer,job,'mutate_'+field)
                        passed('corrupted_'+field+'_cannot_authorize_drain')
                result=call(peer,job,'recover')['result'];assert result=='content_drain_recovered',(mode,result)
                restored=call(peer,job,'history')
                assert restored['checkpoint']['schema']=='vibeflow.job-write-checkpoint.v2'
                assert restored['children']==state['children'] and restored['stop']==state['stop']
                assert call(peer,job,'recover')['result']=='content_drained'
                assert call(peer,job,'history')==restored
                passed(mode+'_complete_stop_history_recovers_once_without_replay')
                receipts.append({'mode':mode,'recovered':restored})
        (api.root/'receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
    for stage in ['created','host_ready']+(['native_ready'] if runtime=='wsl' else []):
        owner=new_api('child-launch-death',database_url,runtime=runtime)
        with owner:
            job=launch(owner,stage+'_hold',runtime);case_id=owner.case_id
            try:
                wait_file(owner.root/f'waiting-{case_id}')
                held=call(owner,job,'history')
                child=next(iter(held['children']['children'].values()))
                assert child['phase']=='preparing' and child['identity'] is None
                birth=json.loads((owner.root/'api-identity.json').read_text())
                child_birth=json.loads((owner.root/f'birth-{case_id}.json').read_text())
                owner.proc.kill();owner.proc.wait(timeout=10);exited(birth);exited(child_birth)
                with new_api('child-launch-death-reader',database_url,shared(owner),runtime=runtime) as peer:
                    peer.token=owner.token
                    assert call(peer,job,'recover')['result']=='registration_recovery_required'
                    state=call(peer,job,'history');row=peer.call('GET',f'/jobs/{job}')
                    assert state['phase']==held['phase'] and state['children']==held['children'] and state['checkpoint']=={}
                    notice=row['attempt']['registration']['recovery']
                    assert notice['state']=='requires_attention' and not notice['automatic_restart_allowed']
                    expected='unverified' if runtime=='wsl' and stage!='native_ready' else 'exited'
                    assert notice['children'][0]['observation']['state'] in {expected,'mismatch'}
                    assert row['attempt']['registration']['children'][0]['preparation']['stage']==stage
                    assert call(peer,job,'recover')['result']=='registration_recovery_required'
                    assert call(peer,job,'history')==state
                    call(peer,job,'reclaim',expect=409)
                    token=peer.token;peer.create_case('Other child reader');peer.call('GET',f'/jobs/{job}',expect=404);peer.token=token
                    assert not (owner.root/f'forbidden-{case_id}').exists()
                    (owner.root/'death-result.json').write_text(json.dumps({'before':held,'after':state,'job':row},indent=2),encoding='utf8')
                    passed(stage+'_owner_death_preserves_partial_evidence_without_stop_or_reclaim')
            finally:(owner.root/f'continue-{case_id}').touch()
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),'actual_runtime':runtime,'developer_database_touched':False}
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--runtime',choices=('windows','wsl'),default='windows');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='child-launch-evidence-',dir=SERVER.parents[1]/'tmp'))
    hashes={p.relative_to(SERVER).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (SERVER/'app').rglob('*.py')}
    (report/'source.json').write_text(json.dumps(hashes,indent=2),encoding='utf8')
    results={'sqlite':await asyncio.to_thread(verify,runtime=args.runtime)}
    (report/'sqlite-result.json').write_text(json.dumps(results['sqlite'],indent=2),encoding='utf8')
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_launch_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_launch_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,target,runtime=args.runtime)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    assert hashes=={p.relative_to(SERVER).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (SERVER/'app').rglob('*.py')},'Product source changed during run'
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('CHILD_LAUNCH_REGISTRATION_OK '+json.dumps({'evidence':str(report),'counts':{k:v['count'] for k,v in results.items()}}))


if __name__=='__main__':asyncio.run(main())
