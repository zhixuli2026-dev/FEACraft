"""Actual frozen retry/current rerun, API policy checks and real model readback."""
import argparse,asyncio,hashlib,json,re,tempfile,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from live_llm_acceptance import source
from live_llm_cli_startup_acceptance import verify
from testsupport import IsolatedServer,SERVER


def write(api,method,path,body):
    """Wait only for explicit pre-admission writer busy responses."""
    deadline=time.monotonic()+30
    while True:
        value=api.call(method,path,body,expect=(200,409))
        detail=value.get('detail')
        if isinstance(detail,dict) and detail.get('code') in {'storage_quota_busy','validation_busy'}:
            if time.monotonic()>deadline:raise TimeoutError('Actual write admission did not drain: '+detail['code'])
            time.sleep(.1);continue
        assert not detail,'Actual file/dictionary write refused: '+json.dumps(value)
        return value


def put(api,path,content):
    return write(api,'PUT',api.prefix+'/file',{'path':path,'content':content})


def repeat(api,identifier,body,key,*,expect=202):
    """Retry only documented transient busy responses, preserving the same key."""
    deadline=time.monotonic()+30
    while True:
        value=api.call('POST','/jobs/'+str(identifier)+':repeat',body,expect=(expect,409),extra_headers={'Idempotency-Key':key})
        detail=value.get('detail')
        if isinstance(detail,dict) and detail.get('code') in {'storage_quota_busy','validation_busy'}:
            with (api.root/'repeat-transient-busy.jsonl').open('a',encoding='utf8') as stream:
                stream.write(json.dumps({'job_id':identifier,'code':detail['code'],'same_key_preserved':True})+'\n')
            if time.monotonic()>=deadline:raise TimeoutError('Actual repeat busy response did not drain: '+detail['code'])
            time.sleep(.1);continue
        if expect==202:assert 'id' in value,'Repeat was refused: '+json.dumps(value)
        return value


def policy_scenario(api,**_):
    """Real cancellation, two API processes, sealed corruption and selectors."""
    permit=api.root/'cancelled-source-may-complete'
    started=api.root/'actual-cancel-started'
    completed=api.root/'actual-retry-completed'
    code="from pathlib import Path\nimport time\nPath("+repr(str(started))+").touch()\nwhile not Path("+repr(str(permit))+").exists():time.sleep(.05)\nwith Path("+repr(str(completed))+").open('a') as f:f.write('once\\n')\nprint('ACTUAL_CANCEL_RETRY_SUCCESS')\n"
    api.put('Ref/cancel.py',code)
    original=api.call('POST',api.prefix+'/jobs',{'command':'python Ref/cancel.py'})
    deadline=time.monotonic()+30
    while not started.exists():
        if time.monotonic()>deadline:raise TimeoutError('Actual cancel source never started')
        time.sleep(.05)
    api.call('POST','/jobs/'+str(original['id'])+':cancel')
    deadline=time.monotonic()+30
    while True:
        cancelled=api.call('GET','/jobs/'+str(original['id']))
        option=api.call('GET','/jobs/'+str(original['id'])+'/operations')
        if cancelled['attempt'].get('process_stop') and option['retry']['available']:break
        if time.monotonic()>deadline:raise TimeoutError('Actual cancel did not drain for retry')
        time.sleep(.1)
    assert cancelled['attempt']['process_stop']['verified'] and not completed.exists()
    candidates=list((api.root/'files').rglob(cancelled['attempt']['workspace']['run_path']))
    assert len(candidates)==1
    source_run=candidates[0].resolve();retained=(api.root/'cancelled-source-run').resolve()
    assert source_run.is_relative_to((api.root/'files').resolve()) and retained.is_relative_to(api.root.resolve()) and not retained.exists()
    source_run.rename(retained);assert not source_run.exists()
    assert option['retry']['available'];permit.touch()
    body={'action':'retry','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version']}
    peer=IsolatedServer('job-repeat-peer',app='peer_fixture:app')
    shared={key:value for key,value in api.env.items() if key.startswith('VIBEFLOW_')}
    (peer.root/'peer_fixture.py').write_text('import os\nos.environ.update('+repr(shared)+')\nfrom app.main import app\n',encoding='utf8')
    with peer:
        peer.token=api.token;peer.case_id=api.case_id;peer.prefix=api.prefix
        key=uuid.uuid4().hex
        def request(server):
            return repeat(server,original['id'],body,key)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(request,server) for server in (api,peer)]
            copies=[future.result() for future in futures]
        assert copies[0]['id']==copies[1]['id'] and sum(value['created'] for value in copies)==1
        done=api.wait_job(copies[0]['id']);assert done['attempt']['number']==2
        assert done['attempt']['operation']['action']=='retry'
        assert completed.read_text().splitlines()==['once']
        assert not source_run.exists(),'Retry must not recreate or execute the old writable directory'
        replay=request(peer);assert replay['id']==done['id'] and not replay['created']
        # A different key cannot create a second child from the retired head.
        refused=peer.call('POST','/jobs/'+str(original['id'])+':repeat',body,expect=409,extra_headers={'Idempotency-Key':uuid.uuid4().hex})
        assert refused['detail']['code']=='state_conflict'
        assert api.call('GET','/jobs/'+str(original['id']))['status']=='cancelled'
    assert peer.proc.poll() is not None
    # Explicit selection rerun keeps the selection but seals its current bytes.
    api.put('Ref/selected.py',"from pathlib import Path\nprint(Path('Ref/selected-value.txt').read_text())\n")
    api.put('Ref/selected-value.txt','SELECTED_OLD')
    api.put('Ref/unselected.txt','UNSELECTED')
    selected=api.call('POST',api.prefix+'/jobs',{'command':'python Ref/selected.py','input_paths':['Ref/selected.py','Ref/selected-value.txt']})
    api.wait_job(selected['id']);api.put('Ref/selected-value.txt','SELECTED_CURRENT')
    option=api.call('GET','/jobs/'+str(selected['id'])+'/operations')
    assert not option['retry']['available'] and option['retry']['reason']['code']=='state_conflict'
    rerun={'action':'rerun','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version'],
        'expected_revision':option['expected_revision'],'expected_event_sequence':option['expected_event_sequence']}
    fresh=repeat(api,selected['id'],rerun,uuid.uuid4().hex)
    done=api.wait_job(fresh['id']);assert 'SELECTED_CURRENT' in done['log_backlog']
    assert set(done['attempt']['workspace']['files'])=={'Ref/selected.py','Ref/selected-value.txt'}
    # Tamper only this test-owned sealed cache; it cannot silently use live files.
    api.put('Ref/corrupt.py',"raise SystemExit(19)\n")
    failed=api.call('POST',api.prefix+'/jobs',{'command':'python Ref/corrupt.py','input_paths':['Ref/corrupt.py']})
    ended=api.wait_job(failed['id'],expected='failed');option=api.call('GET','/jobs/'+str(failed['id'])+'/operations')
    attempt=ended['attempt'];candidates=list((api.root/'files').rglob(attempt['workspace']['inputs_path']))
    assert len(candidates)==1,'Expected exactly one test-owned sealed input folder'
    target=candidates[0]/'Ref/corrupt.py'
    assert target.is_relative_to((api.root/'files').resolve()) and target.is_file(),'Test-owned sealed input path does not exist'
    target.write_text('print("TAMPERED_SHOULD_NOT_RUN")\n',encoding='utf8')
    body={'action':'retry','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version']}
    response=api.call('POST','/jobs/'+str(failed['id'])+':repeat',body,expect=(400,409),extra_headers={'Idempotency-Key':uuid.uuid4().hex})
    target.unlink()
    missing=api.call('POST','/jobs/'+str(failed['id'])+':repeat',body,expect=(400,409),extra_headers={'Idempotency-Key':uuid.uuid4().hex})
    jobs=api.call('GET',api.prefix+'/jobs');assert all(value['status'] not in {'queued','running'} for value in jobs)
    # These are actual protocol refusals; model readback is performed by verify.
    report={'passed':True,'mock_llm_calls':0,'real_model_requests':0,'actual_cancel_stop_verified':True,
        'cancelled_source_history_preserved':True,'two_independent_apis':True,'concurrent_same_key_exactly_one_job':True,
        'native_retry_marker_count':1,'source_runtime_missing':True,'source_runtime_not_recreated':True,
        'late_idempotent_replay':True,'different_key_retired_head_refused':True,
        'current_explicit_selection_preserved':True,'corrupt_sealed_input_refused':response,'missing_sealed_input_refused':missing,
        'peer_root':str(peer.root),'peer_exited':True,'source_job':original['id'],'retry_job':copies[0]['id']}
    (api.root/'job-repeat-policy.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report

def scenario(api,**_):
    permit=api.root/'transient-condition-cleared'
    code="from pathlib import Path\nprint(Path('Ref/value.txt').read_text().strip())\nif not Path("+repr(str(permit))+").exists():raise SystemExit(17)\n"
    old='ORIGINAL_'+uuid.uuid4().hex;current='CURRENT_'+uuid.uuid4().hex
    api.put('Ref/repeat.py',code);api.put('Ref/value.txt',old)
    first=api.call('POST',api.prefix+'/jobs',{'command':'python Ref/repeat.py'})
    failed=api.wait_job(first['id'],expected='failed')
    assert failed['attempt']['process_stop']['verified'] and old in failed['log_backlog']
    options=api.call('GET','/jobs/'+str(first['id'])+'/operations')
    assert options['retry']['available'] and options['rerun']['available'] and not options['resume']['available']
    api.put('Ref/value.txt',current);permit.touch()
    retry={'action':'retry','source_attempt_id':options['source_attempt_id'],'source_version':options['source_version']}
    key=uuid.uuid4().hex
    retried=repeat(api,first['id'],retry,key)
    repeated=repeat(api,first['id'],retry,key)
    assert retried['id']==repeated['id'] and not repeated['created']
    done=api.wait_job(retried['id']);assert old in done['log_backlog'] and current not in done['log_backlog']
    assert done['attempt']['number']==2 and done['attempt']['root_job_id']==first['id']
    assert done['attempt']['retry_of_attempt_id']==options['source_attempt_id']
    frozen=done['attempt']['workspace']['files']['Ref/value.txt']['sha256'];assert frozen==hashlib.sha256(old.encode()).hexdigest()
    fresh_options=api.call('GET','/jobs/'+str(first['id'])+'/operations')
    rerun={'action':'rerun','source_attempt_id':fresh_options['source_attempt_id'],'source_version':fresh_options['source_version'],
        'expected_revision':fresh_options['expected_revision'],'expected_event_sequence':fresh_options['expected_event_sequence']}
    new=repeat(api,first['id'],rerun,uuid.uuid4().hex)
    fresh=api.wait_job(new['id']);assert current in fresh['log_backlog'] and old not in fresh['log_backlog']
    assert fresh['attempt']['number']==1 and fresh['attempt']['root_job_id']==new['id']
    conflict=api.call('POST','/jobs/'+str(first['id'])+':repeat',rerun,expect=409,extra_headers={'Idempotency-Key':key})
    assert conflict['detail']['code']=='idempotency_conflict'
    stale={**retry,'source_version':retry['source_version']+1}
    assert api.call('POST','/jobs/'+str(first['id'])+':repeat',stale,expect=409,extra_headers={'Idempotency-Key':uuid.uuid4().hex})['detail']['code']=='state_conflict'
    assert api.call('POST','/jobs/'+str(first['id'])+':repeat',retry,expect=409)['detail']['code']=='idempotency_required'
    owner=api.token
    other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','name':'Other owner','password':'isolated-test-123'})
    api.token=other['token'];api.call('GET','/jobs/'+str(first['id'])+'/operations',expect=404);api.token=owner
    original=api.call('GET','/jobs/'+str(first['id']));assert original['status']=='failed' and original['attempt']['id']==options['source_attempt_id']
    report={'passed':True,'real_model_requests':0,'source_job':first['id'],'retry_job':retried['id'],'rerun_job':new['id'],
        'actual_transient_failure_exit_code':failed['exit_code'],'original_frozen_value':old,'current_value':current,
        'retry_uses_original_bytes':True,'rerun_uses_current_bytes':True,'retry_keeps_lineage':True,'rerun_is_new_root':True,
        'idempotent_retry':True,'idempotency_conflict_refused':True,'stale_source_refused':True,'missing_key_refused':True,
        'other_user_refused':True,'original_history_preserved':True,'old_frozen_sha256':frozen}
    (api.root/'job-repeat-checks.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return report

def geometry_scenario(api,**_):
    permit=api.root/'cadquery-runtime-ready'
    def program(size):
        return "import cadquery as cq\nfrom pathlib import Path\nif not Path("+repr(str(permit))+").exists():raise RuntimeError('transient native condition')\ndef build_geometry(ctx):return {'box':cq.Workplane('XY').box("+','.join([str(size)]*3)+")}\n"
    old=program(1);api.put('A-geo/geometry.py',old)
    first=api.call('POST',api.prefix+'/geometry:preview')['job_id'];failed=api.wait_job(first,expected='failed')
    assert failed['attempt']['process_stop']['verified'] and failed['attempt']['handler']['key']=='geometry.preview'
    option=api.call('GET','/jobs/'+str(first)+'/operations');permit.touch()
    body={'action':'retry','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version']}
    repeated=repeat(api,first,body,uuid.uuid4().hex)
    done=api.wait_job(repeated['id']);assert done['attempt']['handler']['key']=='geometry.preview'
    assert done['attempt']['workspace']['files']['A-geo/geometry.py']['sha256']==hashlib.sha256(old.encode()).hexdigest()
    assert done['attempt']['number']==2 and done['attempt']['root_job_id']==first
    assert done['attempt']['outputs']['publication']['status']=='committed'
    new=program(2);api.put('A-geo/geometry.py',new)
    option=api.call('GET','/jobs/'+str(first)+'/operations')
    refreshes=0
    deadline=time.monotonic()+30
    key=uuid.uuid4().hex
    while True:
        body={'action':'rerun','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version'],
            'expected_revision':option['expected_revision'],'expected_event_sequence':option['expected_event_sequence'],'parameters':{}}
        rerun=api.call('POST',f'/jobs/{first}:repeat',body,expect=(202,409),extra_headers={'Idempotency-Key':key})
        if 'id' in rerun:break
        reason=rerun['detail']['code']
        assert reason in {'input_stale','storage_quota_busy','validation_busy'},rerun
        assert time.monotonic()<deadline,rerun
        if reason=='input_stale':
            # A delivery/settlement event may advance this selector after the
            # isolated write. Confirm identical source and bytes on refresh.
            fresh_option=api.call('GET',f'/jobs/{first}/operations')
            for field in ('source_attempt_id','source_version','expected_revision'):
                assert fresh_option[field]==option[field],field
            assert fresh_option['expected_event_sequence']>option['expected_event_sequence']
            assert api.call('GET',api.prefix+'/raw?path=A-geo/geometry.py',raw=True)==new.encode()
            option=fresh_option
            refreshes+=1
        else:
            time.sleep(.1)
    fresh=api.wait_job(rerun['id']);assert fresh['attempt']['handler']['key']=='geometry.preview'
    assert fresh['attempt']['workspace']['files']['A-geo/geometry.py']['sha256']==hashlib.sha256(new.encode()).hexdigest()
    assert fresh['attempt']['outputs']['publication']['status']=='committed' and fresh['attempt']['root_job_id']==rerun['id']
    report={'passed':True,'real_model_requests':0,'source_job':first,'retry_job':repeated['id'],'rerun_job':rerun['id'],
        'actual_cadquery_worker':True,'registered_handler_binding_preserved':True,'original_failed_history_preserved':True,
        'rerun_event_selector_refreshes':refreshes,
        'old_geometry_sha256':hashlib.sha256(old.encode()).hexdigest(),'current_geometry_sha256':hashlib.sha256(new.encode()).hexdigest(),
        'retry_publication_committed':True,'rerun_publication_committed':True}
    (api.root/'job-repeat-geometry.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report

async def main(*,selected_override=None,selected_factory=None,fixture_override='',store_volume_max=3,environment_overrides=None,store_factory=None):
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True)
    modes=parser.add_mutually_exclusive_group();modes.add_argument('--geometry',action='store_true');modes.add_argument('--policy',action='store_true')
    parser.add_argument('--postgres',action='store_true');parser.add_argument('--s3',action='store_true');args=parser.parse_args()
    configuration=await source(args.source_profile_id)
    selected=selected_factory(configuration) if selected_factory else selected_override or (geometry_scenario if args.geometry else policy_scenario if args.policy else scenario)
    def run(database_url=None):
        if not args.s3:return verify(configuration,database_url=database_url,fixture=fixture_override,scenario=selected,environment_overrides=environment_overrides)
        from functools import partial
        from s3testsupport import ObjectStore
        store=(store_factory() if store_factory is not None else
            ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',volume_max=store_volume_max));receipt={'passed':False,'volume_max':store_volume_max}
        print('JOB_REPEAT_OBJECT_STORE '+str(store.root),flush=True)
        try:
            with store:
                result=verify(configuration,database_url=database_url,fixture=fixture_override,scenario=partial(selected,store=store),store=store,environment_overrides=environment_overrides)
                receipt.update(passed=True,root=str(result))
        finally:
            receipt['store_stopped']=store.proc is not None and store.proc.poll() is not None
            (store.root/'job-repeat-run.json').write_text(json.dumps(receipt,indent=2),encoding='utf8')
        return result
    if not args.postgres:
        await asyncio.to_thread(run);return
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic
    configured=make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert configured.drivername=='postgresql+asyncpg' and configured.host in {'127.0.0.1','localhost','::1'}
    name='tmp_vf_repeat_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_repeat_[0-9a-f]{32}',name)
    root=Path(tempfile.mkdtemp(prefix='job-repeat-postgres-',dir=SERVER.parents[1]/'tmp'))
    report={'passed':False,'database':name,'developer_database_modified':False,'mock_llm_calls':0}
    print('JOB_REPEAT_POSTGRES '+str(root),flush=True)
    admin=await asyncpg.connect(host=configured.host,port=configured.port or 5432,user=configured.username,
        password=configured.password,database='postgres',timeout=10);created=False
    try:
        await admin.execute('CREATE DATABASE "'+name+'"');created=True
        url=configured.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic,url,root,'upgrade','head')
        report['root']=str(await asyncio.to_thread(run,url));report['passed']=True
    finally:
        try:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"');report['temporary_database_dropped']=True
        finally:
            await admin.close();(root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')

if __name__=='__main__':asyncio.run(main())
