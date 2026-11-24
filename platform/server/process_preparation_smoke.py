"""Real root handshake faults and deaths; partial observations never authorize replay."""
import argparse,asyncio,hashlib,json,re,tempfile,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from sqlalchemy.engine import make_url
from testsupport import SERVER
from job_launch_registration_smoke import new_api as base_api
from job_owner_startup_smoke import shared
from job_release_smoke import wait_file
from child_launch_registration_smoke import exited
from workflow_migration_smoke import alembic

EXTRA='''
from contextvars import ContextVar
partial_stage=ContextVar('partial_stage',default=None)
partial_commit=AsyncSession.commit
async def commit_partial(db):
 stage=partial_stage.get();mode=(root/'mode').read_text()
 if stage and mode==stage+'_before':raise OSError('Injected partial observation pre-commit fault')
 result=await partial_commit(db)
 if stage and mode==stage+'_after':raise OSError('Injected partial observation acknowledgement loss')
 return result
AsyncSession.commit=commit_partial
observed_original=launches.observed
async def observed(owner,value):
 (root/'partial.json').write_text(json.dumps(value))
 token=partial_stage.set(value['stage'])
 try:await observed_original(owner,value)
 finally:partial_stage.reset(token)
 if (root/'mode').read_text()==value['stage']+'_hold':await barrier('partial')
launches.observed=observed
'''


def new_api(label,database_url=None,overrides=None):
    api=base_api(label,database_url,overrides)
    p=api.root/'launch_fixture.py';p.write_text(p.read_text(encoding='utf8')+EXTRA,encoding='utf8')
    return api


def verify(database_url=None,*,wsl=False):
    checks=[];receipts=[]
    def passed(name):checks.append(name);print('PASS '+name,flush=True)
    runtimes=['windows']+(['wsl'] if wsl else [])
    api=new_api('partial-launch-faults',database_url)
    with api:
        for runtime in runtimes:
            stages=['created','host_ready']+(['native_ready'] if runtime=='wsl' else [])
            for stage in stages:
                for fault in ('before','after'):
                    mode=stage+'_'+fault;api.create_case(runtime+' '+mode)
                    (api.root/'mode').write_text(mode)
                    marker=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))/'payload.txt'
                    api.call('POST',api.prefix+'/jobs',{'command':'echo UNRELEASED>payload.txt','runtime':runtime},expect=409)
                    job=int((api.root/'job-id').read_text());row=api.wait_job(job,expected='failed')
                    state=api.call('GET',f'/fixture/history/{job}')
                    observations=[e['payload'] for e in state['events'] if e['type']=='attempt.launch_observed']
                    assert len(observations)==stages.index(stage)+int(fault=='after')
                    partial=json.loads((api.root/'partial.json').read_text());exited(partial['host'])
                    full=stage==('native_ready' if runtime=='wsl' else 'host_ready')
                    if full:
                        assert row['attempt']['process_stop']['verified'] and state['checkpoint']
                        assert row['attempt']['registration']['stage']=='aborted'
                    else:
                        assert row['attempt']['process_stop'] is None and row['attempt']['process_identity'] is None
                        assert state['checkpoint']=={}
                    assert not marker.exists()
                    passed(runtime+'_'+mode+'_preserves_exact_committed_observations_and_actual_cleanup')
                    receipts.append({'runtime':runtime,'mode':mode,'job':row,'history':state})
        (api.root/'receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
    for runtime in runtimes:
        for stage in ['created','host_ready']+(['native_ready'] if runtime=='wsl' else []):
            owner=new_api('partial-launch-death',database_url)
            with owner:
                owner.create_case(runtime+' '+stage+' death');(owner.root/'mode').write_text(stage+'_hold')
                marker=next((owner.root/'files/workspaces').glob(f'*/cases/{owner.case_id}'))/'payload.txt'
                birth=owner.call('GET','/fixture/identity')
                with ThreadPoolExecutor(max_workers=1) as pool:
                    submitted=pool.submit(owner.call,'POST',owner.prefix+'/jobs',{'command':'echo UNRELEASED>payload.txt','runtime':runtime})
                    try:
                        wait_file(owner.root/'partial.ready');job=int((owner.root/'job-id').read_text())
                        partial=json.loads((owner.root/'partial.json').read_text())
                        initial=owner.call('GET',f'/jobs/{job}')
                        assert initial['attempt']['registration']['preparation']==partial
                        owner.proc.kill();owner.proc.wait(timeout=10);exited(birth);exited(partial['host'])
                        try:submitted.result(timeout=15)
                        except (OSError,ValueError):pass
                        else:raise AssertionError('Killed API acknowledged held launch')
                        with new_api('partial-launch-reader',database_url,shared(owner)) as peer:
                            peer.token=owner.token
                            row=peer.call('GET',f'/jobs/{job}');reg=row['attempt']['registration']
                            assert reg['stage']=='preparing' and reg['preparation']==partial and reg['prepared_identity'] is None
                            assert row['attempt']['phase']=='starting' and row['attempt']['process_stop'] is None
                            notice=reg['recovery'];assert notice['state']=='requires_attention' and not notice['automatic_restart_allowed']
                            expected='unverified' if runtime=='wsl' and stage!='native_ready' else 'exited'
                            # The exact birth was observed exited above. Windows
                            # may reuse that PID while starting the reader API;
                            # recovery must preserve the mismatch, not claim exit.
                            assert notice['process_state'] in {expected,'mismatch'},notice
                            history=peer.call('GET',f'/fixture/history/{job}')
                            if notice['process_state']=='mismatch':
                                audit=[e['payload'] for e in history['events'] if e['type']=='attempt.registration_recovery_required'][-1]
                                assert audit['process']['host']=={'state':'mismatch','reason':'birth_or_scope_changed'}
                            assert history['identity']=={} and history['checkpoint']=={}
                            peer.call('POST','/fixture/reconcile')
                            repeated=peer.call('GET',f'/fixture/history/{job}')
                            assert {k:v for k,v in repeated.items() if k!='events'}=={k:v for k,v in history.items() if k!='events'}
                            assert repeated['events'][:len(history['events'])]==history['events']
                            added=repeated['events'][len(history['events']):]
                            assert len(added)<=1 and all(e['type']=='attempt.registration_recovery_required' for e in added)
                            if added:
                                previous=[e['payload'] for e in history['events'] if e['type']=='attempt.registration_recovery_required'][-1]
                                latest=added[0]['payload']
                                assert {k:v for k,v in previous.items() if k not in {'owner','process','children','observed_at'}}=={k:v for k,v in latest.items() if k not in {'owner','process','children','observed_at'}}
                                assert any(previous[k]!=latest[k] for k in ('owner','process','children')),'Duplicate audit for identical observation'
                            history=repeated
                            peer.call('POST',f'/fixture/reclaim/{job}',expect=409)
                            assert not marker.exists()
                            (owner.root/'death-result.json').write_text(json.dumps({'initial':initial,'after':row,'history':history},indent=2),encoding='utf8')
                            passed(runtime+'_'+stage+'_death_keeps_partial_identity_without_stop_or_replay')
                    finally:(owner.root/'partial.continue').touch()
    return {'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),
        'actual_runtimes':runtimes,'developer_database_touched':False}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--wsl',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='partial-launch-evidence-',dir=SERVER.parents[1]/'tmp'))
    hashes={p.relative_to(SERVER).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (SERVER/'app').rglob('*.py')}
    (report/'source.json').write_text(json.dumps(hashes,indent=2),encoding='utf8')
    results={'sqlite':await asyncio.to_thread(verify,wsl=args.wsl)}
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
            results['postgresql']=await asyncio.to_thread(verify,target,wsl=args.wsl)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    assert hashes=={p.relative_to(SERVER).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (SERVER/'app').rglob('*.py')}
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('PROCESS_PREPARATION_OK '+json.dumps({'evidence':str(report),'counts':{k:v['count'] for k,v in results.items()}}))


if __name__=='__main__':asyncio.run(main())
