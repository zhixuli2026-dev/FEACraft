"""Actual multi-API input replacement, native IO, ACL and preparation interruption.

SQLite is optional; default owns a disposable local PostgreSQL database. The
fixture pauses only after the real document preparation returns. No inference
or simulated model/CLI responses are used.
"""
import argparse,asyncio,hashlib,json,os,re,shutil,sys,tempfile,time,traceback,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import httpx
from testsupport import SERVER,IsolatedServer
from job_input_recovery_acceptance import APP as BASE
from workflow_migration_smoke import alembic,legacy

ROOT=SERVER.parents[1]
PREFIX="import os,json\nfrom pathlib import Path\np=Path(__file__).parent/'shared-runtime.json'\nif p.exists():os.environ.update(json.loads(p.read_text(encoding='utf8')))\n"
EXTRA='''
import asyncio
from sqlalchemy import inspect
from app.models import JobInputRecovery,NativeRequest,Case,Workspace,Membership
from app.services import document_admission
holds={}
actual_complete=document_admission.complete
async def observed_complete(prepared,case_id,user_id,**kwargs):
    value=await actual_complete(prepared,case_id,user_id,**kwargs)
    state=holds.get(case_id)
    if state is not None and not state['used']:
        state['used']=True;state['ready'].set();await state['release'].wait()
    return value
document_admission.complete=observed_complete
@app.post('/api/v1/test/input-hold/{case_id}')
async def arm(case_id:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:await get_owned_case(case_id,user,db)
    holds[case_id]={'ready':asyncio.Event(),'release':asyncio.Event(),'used':False}
    return {'armed':True}
@app.get('/api/v1/test/input-hold/{case_id}')
async def held(case_id:int,user=Depends(get_current_user)):
    return {'ready':holds[case_id]['ready'].is_set()}
@app.post('/api/v1/test/input-release/{case_id}')
async def release(case_id:int,user=Depends(get_current_user)):
    holds[case_id]['release'].set();return {'released':True}
@app.get('/api/v1/test/input-state/{identifier}')
async def state(identifier:int,user=Depends(get_current_user)):
    def raw(row):
        return {c.key:(getattr(row,c.key).isoformat() if hasattr(getattr(row,c.key),'isoformat') else getattr(row,c.key)) for c in inspect(type(row)).columns}
    async with SessionLocal() as db:
        job=await db.get(Job,identifier);await get_owned_case(job.case_id,user,db)
        attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==identifier))
        links=list(await db.scalars(select(JobInputRecovery).where(JobInputRecovery.source_job_id==identifier)))
        jobs=list(await db.scalars(select(Job.id).where(Job.case_id==job.case_id).order_by(Job.id)))
        from app.services.native_request_records import public
        requests=[public(row) for row in await db.scalars(select(NativeRequest).where(NativeRequest.case_id==job.case_id))]
        return {'job':raw(job),'attempt':raw(attempt),'links':[json.loads(row.payload_json) for row in links],
            'case_job_ids':jobs,'native_requests':requests}
@app.post('/api/v1/test/input-access')
async def access(body:dict,user=Depends(get_current_user)):
    # Explicit isolated ACL mutation, not a product transfer API.
    from app.services import sharing
    async with SessionLocal() as db:
        case=await db.get(Case,body['case_id']);workspace=await db.get(Workspace,case.workspace_id)
        if body['mode']=='restore':workspace.user_id=user.id
        else:
            workspace.user_id=body['other_user_id']
            owner=await db.scalar(select(Membership).where(Membership.user_id==body['other_user_id'],Membership.role=='admin').limit(1))
            membership=await db.scalar(select(Membership).where(Membership.user_id==user.id,Membership.org_id==owner.org_id))
            if membership is None:membership=Membership(user_id=user.id,org_id=owner.org_id,role='viewer');db.add(membership)
            membership.role='member' if body['mode']=='member' else 'viewer'
            await sharing.set_share(db,workspace,owner.org_id,'member' if body['mode']=='member' else 'viewer',body['mode']!='revoked')
        await db.commit()
    return {'mode':body['mode']}
'''
APP=PREFIX+BASE+EXTRA


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def verify(args,database_url=None):
    from app.services.windows_process_scope import ProcessHandle
    review=json.loads(args.review.read_text(encoding='utf8'));assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for p,d in review['source_hashes'].items():assert sha(ROOT/p)==d,p
    api=IsolatedServer('input-multi-API',app='input_multi_fixture:app',database_url=database_url)
    (api.root/'input_multi_fixture.py').write_text(APP,encoding='utf8')
    peer=None;owners={};new_jobs=[];source_ids=[];held_cases=set();active_future=None;pool=ThreadPoolExecutor(max_workers=2)
    report={'passed':False,'backend':'postgresql' if database_url else 'sqlite','new_real_model_calls':0,'new_mock_llm_calls':0,
        'developer_database_modified':False,'API_exit_witnesses':[],'source_review_sha256':sha(args.review),'checks':[]}
    def save():(api.root/'multi-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    def call(server,method,path,body=None,expect=200,*,key=None):
        headers={'Authorization':'Bearer '+server.token} if server.token else {}
        if key is not None:headers['Idempotency-Key']=key
        with httpx.Client(base_url=server.base+'/api/v1',headers=headers,timeout=180,trust_env=False) as client:
            r=client.request(method,path,json=body)
            assert r.status_code in ((expect,) if isinstance(expect,int) else expect),'Actual HTTP '+method+' '+path+' '+str(r.status_code)
            return r.status_code,r.json()
    def enter(server):
        server.__enter__();birth=call(server,'GET','/test/input-birth')[1];handle=ProcessHandle.query(birth);owners[id(server)]=(server,birth,handle)
    def stop(server):
        found=owners.get(id(server))
        if found is None:return
        _,birth,handle=found
        try:
            try:call(server,'POST','/test/input-exit')
            except (OSError,httpx.TransportError):pass
            handle.wait_exit(10);report['API_exit_witnesses'].append({'birth':birth,'state':'exited','proof':'retained_creation_handle'})
        finally:server.__exit__(RuntimeError if 'failure' in report else None,None,None);handle.close();owners.pop(id(server),None);save()
    def request(server,identifier,body,key):
        until=time.monotonic()+30
        while True:
            status,value=call(server,'POST','/jobs/'+str(identifier)+':recover-inputs',body,expect=(202,409),key=key)
            if status==409 and value.get('detail',{}).get('code')=='storage_quota_busy':
                if time.monotonic()>=until:raise TimeoutError('Actual writer admission did not drain')
                time.sleep(.05);continue
            return status,value
    def seed(name,kind='shell'):
        workspace=api.call('GET','/workspaces')[0]
        case=api.call('POST','/workspaces/'+str(workspace['id'])+'/cases',{'name':name});case_id=case['id']
        api.call('PUT','/cases/'+str(case_id)+'/file',{'path':'Ref/input.txt','content':'CURRENT_MULTI_API'})
        if kind=='convert':api.call('PUT','/cases/'+str(case_id)+'/file',{'path':'E-report/report.md','content':'# Actual prepared input\nMULTI_API_REPORT\n'})
        original=api.call('POST','/test/unsealed/'+str(case_id),{'command':'echo ORIGINAL_MUST_NOT_EXECUTE','job_type':kind});source_ids.append(original['id'])
        plan=api.call('GET','/jobs/'+str(original['id'])+'/input-recovery');assert plan['available']==(kind in {'shell','convert'})
        command='"'+sys.executable+'" -I -S -c "from pathlib import Path; print(Path(\'Ref/input.txt\').read_text()); Path(\'executions.txt\').open(\'a\').write(\'once\\n\')"'
        body={'action':'reprepare_current_inputs','source_attempt_id':plan['source_attempt_id'],'source_snapshot_sha256':plan['source_snapshot_sha256'],
            'expected_revision':plan['expected_revision'],'expected_event_sequence':plan['expected_event_sequence'],'command':command if kind=='shell' else None,
            'handler':None if kind=='shell' else {'key':'report.build','version':'1'},'parameters':None if kind=='shell' else {'run_dir':'C-run/case-1','mode':'markdown','allow_standalone':True},
            'runtime':'windows','input_paths':['Ref/input.txt'] if kind=='shell' else None,'timeout_seconds':120,'max_log_bytes':1048576,'note':name}
        return case_id,original['id'],body
    def refresh_body(identifier,body):
        plan=api.call('GET','/jobs/'+str(identifier)+'/input-recovery');assert plan['available']
        return {**body,**{k:plan[k] for k in ['source_attempt_id','source_snapshot_sha256','expected_revision','expected_event_sequence']}}
    def done(identifier,case_id,kind):
        if identifier not in new_jobs:new_jobs.append(identifier)
        value=api.wait_job(identifier,timeout=180);assert value['attempt']['process_stop']['verified'] is True
        path=value['attempt']['workspace']['run_path']
        if kind=='shell':
            raw=api.call('GET','/cases/'+str(case_id)+'/raw?path='+path+'/executions.txt',raw=True)
            assert raw.replace(bytes([13,10]),bytes([10]))==bytes([111,110,99,101,10])
        else:
            assert value['attempt']['handler']['key']=='report.build'
            assert b'MULTI_API_REPORT' in api.call('GET','/cases/'+str(case_id)+'/raw?path='+path+'/E-report/build/report.html',raw=True)
        return value
    print('INPUT_MULTI_API '+str(api.root),flush=True)
    try:
        if database_url:
            alembic(database_url,api.root,'upgrade','c20e87f143b9');old=asyncio.run(legacy(database_url,seed=True))
            for op,target in [('upgrade','head'),('downgrade','c20e87f143b9'),('upgrade','head')]:alembic(database_url,api.root,op,target)
            assert asyncio.run(legacy(database_url))==old
            report['actual_PG_incremental_migration_preserves_legacy_Job']=True;report['full_mixed_native_migration_claimed']=False
        enter(api);api.create_case('Actual multi API input recovery');user=api.call('GET','/auth/me')
        shared={k:v for k,v in api.env.items() if k.startswith('VIBEFLOW_')};(api.root/'shared-runtime.json').write_text(json.dumps(shared),encoding='utf8')
        peer=IsolatedServer('input-recovery-peer',app='input_multi_fixture:app',database_url=database_url)
        (peer.root/'shared-runtime.json').write_text(json.dumps(shared),encoding='utf8');(peer.root/'input_multi_fixture.py').write_text(APP,encoding='utf8')
        enter(peer);peer.token=api.token;report['peer_root']=str(peer.root)
        for kind in ['shell','convert']:
            case,original,body=seed('Same key actual '+kind,kind);key=uuid.uuid4().hex
            futures=[pool.submit(request,server,original,body,key) for server in [api,peer]];results=[f.result(timeout=180) for f in futures]
            assert all(status==202 for status,_ in results) and results[0][1]['id']==results[1][1]['id']
            target=results[0][1]['id'];assert results[0][1]['input_recovery']==results[1][1]['input_recovery'];done(target,case,kind)
            state=api.call('GET','/test/input-state/'+str(original));assert state['case_job_ids']==[original,target] and state['attempt']['claim_token'] is None
            assert state['attempt']['process_identity_json']=='{}' and state['job']['status']=='cancelled'
            conflict=request(peer,original,{**body,'note':'different confirmation'},key);assert conflict[0]==409 and conflict[1]['detail']['code']=='idempotency_conflict'
            report['checks'].append({'kind':'same_key_'+kind,'source':original,'target':target,'same_receipt':results[0][1]['input_recovery'],'one_new_native_execution':True});save()
            if kind=='shell':replay_sample=(case,original,body,key,target)
        case,original,body=seed('Different key same original');results=[f.result(timeout=180) for f in [pool.submit(request,s,original,body,uuid.uuid4().hex) for s in [api,peer]]]
        assert sorted(status for status,_ in results)==[202,409]
        rejected=next(value for status,value in results if status==409);assert rejected['detail']['code']=='replacement_exists'
        target=next(value['id'] for status,value in results if status==202);done(target,case,'shell')
        assert api.call('GET','/test/input-state/'+str(original))['case_job_ids']==[original,target]
        report['checks'].append({'kind':'different_key_one_replacement','source':original,'target':target});save()
        case,original,body,key,target=replay_sample
        foreign=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Other input recovery reader'})
        token=api.token;api.token=foreign['token'];other=api.call('GET','/auth/me')['id']
        call(api,'GET','/jobs/'+str(original)+'/input-recovery',expect=404);call(api,'POST','/jobs/'+str(original)+':recover-inputs',body,expect=404,key=key);api.token=token
        access={'case_id':case,'other_user_id':other}
        try:
            api.call('POST','/test/input-access',{**access,'mode':'viewer'});call(api,'GET','/cases/'+str(case))
            call(api,'POST','/jobs/'+str(original)+':recover-inputs',body,expect=404,key=key)
            api.call('POST','/test/input-access',{**access,'mode':'member'});assert request(peer,original,body,key)[1]['id']==target
            api.call('POST','/test/input-access',{**access,'mode':'revoked'});call(api,'GET','/cases/'+str(case),expect=404)
            call(peer,'POST','/jobs/'+str(original)+':recover-inputs',body,expect=404,key=key)
        finally:api.call('POST','/test/input-access',{**access,'mode':'restore'})
        report['checks'].append({'kind':'foreign_viewer_member_revoked_before_replay','source':original});save()
        case,original,body=seed('Unknown historical kind','UnregisteredMetadataFaultOnly')
        unsupported=api.call('GET','/jobs/'+str(original)+'/input-recovery')
        assert unsupported['available'] is False and unsupported['handler_choices']==[] and unsupported['reason']['code']=='recovery_required'
        assert request(api,original,{**body,'handler':None,'parameters':None,'command':'echo NEVER_EXECUTED'},uuid.uuid4().hex)[0]==409
        assert api.call('GET','/test/input-state/'+str(original))['case_job_ids']==[original]
        report['checks'].append({'kind':'unregistered_metadata_kind_unavailable_no_new_job','source':original});save()
        for fault in ['cancel','input_change','API_exit']:
            case,original,body=seed('Actual preparation '+fault,'convert');api.call('POST','/test/input-hold/'+str(case));held_cases.add(case);before=api.call('GET','/test/input-state/'+str(original))
            key=uuid.uuid4().hex;future=pool.submit(request,api,original,body,key);active_future=future;until=time.monotonic()+120
            while not api.call('GET','/test/input-hold/'+str(case))['ready']:
                if future.done():raise AssertionError('Actual preparation ended before hold boundary')
                if time.monotonic()>=until:raise TimeoutError('Actual native preparation did not reach observed boundary')
                time.sleep(.1)
            paused=peer.call('GET','/test/input-state/'+str(original));assert paused['case_job_ids']==[original] and paused['links']==[]
            assert paused['native_requests'] and all(row['cleanup_verified'] is True for row in paused['native_requests'])
            if fault=='API_exit':
                stop(api)
                held_cases.clear()
                try:future.result(timeout=10)
                except httpx.TransportError:pass
                else:raise AssertionError('Actual API exit did not interrupt held response')
                shutil.copyfile(api.root/'api.log',api.root/'before-preparation-exit.log');enter(api)
                state=peer.call('GET','/test/input-state/'+str(original));assert state['job']==before['job'] and state['attempt']==before['attempt'] and state['case_job_ids']==[original]
                old_retry=request(api,original,body,key);assert old_retry[0]==409 and old_retry[1]['detail']['code']=='input_stale'
            else:
                if fault=='cancel':peer.call('POST','/jobs/'+str(original)+':cancel')
                else:peer.call('PUT','/cases/'+str(case)+'/file',{'path':'E-report/report.md','content':'# New source\nMULTI_API_REPORT\n'})
                api.call('POST','/test/input-release/'+str(case));held_cases.discard(case);status,result=future.result(timeout=180)
                assert status==409 and result['detail']['code']==('state_conflict' if fault=='cancel' else 'input_stale')
                state=peer.call('GET','/test/input-state/'+str(original));assert state['case_job_ids']==[original] and state['links']==[]
            active_future=None;body=refresh_body(original,body);status,created=request(api,original,body,uuid.uuid4().hex);assert status==202
            done(created['id'],case,'convert');report['checks'].append({'kind':'prepared_'+fault,'source':original,'target':created['id'],'no_implicit_new_job':True,'native_preparation_stopped_before_fault':True});save()
        assert len(report['checks'])==8;report['functional_passed']=True;save()
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]};save()
    finally:
        if id(api) in owners:
            for case in held_cases:
                try:api.call('POST','/test/input-release/'+str(case))
                except BaseException as error:report.setdefault('cleanup_failures',[]).append(type(error).__name__)
        if active_future is not None:
            try:active_future.result(timeout=180)
            except BaseException:pass  # Main failure and the persisted source/target state remain authoritative.
        pool.shutdown(wait=True,cancel_futures=True)
        if id(api) in owners:
            try:
                for identifier in source_ids:
                    state=api.call('GET','/test/input-state/'+str(identifier))
                    for link in state['links']:
                        if link['target_job_id'] not in new_jobs:new_jobs.append(link['target_job_id'])
                for identifier in new_jobs:
                    value=api.wait_job(identifier,timeout=180);assert value['attempt']['process_stop']['verified'] is True
                for identifier in source_ids:
                    state=api.call('GET','/test/input-state/'+str(identifier));assert all(row['cleanup_verified'] is True for row in state['native_requests'])
                report['actual_native_job_and_preparation_stops_verified']=True
            except BaseException as error:report.setdefault('cleanup_failures',[]).append(type(error).__name__)
        for server,_,_ in list(owners.values())[::-1]:
            try:stop(server)
            except BaseException as error:report.setdefault('cleanup_failures',[]).append(type(error).__name__)
        report['passed']=bool(report.get('functional_passed') and report.get('actual_native_job_and_preparation_stops_verified') and len(report['API_exit_witnesses'])==3 and not report.get('cleanup_failures'));save()
    print('INPUT_MULTI_API_RESULT '+str(api.root/'multi-result.json'),flush=True)
    return report['passed'],api.root


async def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--review',type=Path,required=True);parser.add_argument('--sqlite',action='store_true');args=parser.parse_args()
    if args.sqlite:return 0 if (await asyncio.to_thread(verify,args))[0] else 1
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    selected=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert selected.drivername=='postgresql+asyncpg' and selected.host in {'127.0.0.1','localhost','::1'}
    name='tmp_vf_inputrecovery_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_inputrecovery_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='input-recovery-postgres-',dir=ROOT/'tmp'));report={'passed':False,'database':name,'developer_database_modified':False};created=False
    admin=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,password=selected.password,database='postgres',timeout=10)
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        passed,case_root=await asyncio.to_thread(verify,args,selected.set(database=name).render_as_string(hide_password=False));report.update(passed=passed,root=str(case_root))
    finally:
        try:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"');report['temporary_database_absent']=await admin.fetchval('SELECT datname FROM pg_database WHERE datname=$1',name) is None
        finally:await admin.close();(root/'result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    return 0 if report['passed'] and report.get('temporary_database_absent') else 1


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
