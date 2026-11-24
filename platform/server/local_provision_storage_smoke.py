"""Real local create/copy/recovery quota peaks and drained ownership."""
import argparse,asyncio,json,re,tempfile,time,uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from testsupport import IsolatedServer,SERVER
from publication_finalizing_smoke import peer
from job_release_smoke import wait_file
from node_storage_budget_smoke import stop

OBSERVE='''
import asyncio,json,time
from pathlib import Path
from fastapi import Depends
from app.main import app
from app import storage
from app.db import SessionLocal
from app.models import User
from app.deps import get_current_user
from app.services import quota,storage_usage,case_provision as provision
root=Path(__file__).parent
original_limit=quota._storage_limit
def limit(q):
 p=storage._fs_root()/'.test-quota-limit'
 return int(p.read_text()) if p.exists() else original_limit(q)
quota._storage_limit=limit
original_measure=storage_usage._measure
def measuring(*args,**kwargs):
 if (storage._fs_root()/'.test-quota-unknown').exists():raise storage_usage.MeasurementUnavailable('injected inaccessible volume')
 return original_measure(*args,**kwargs)
storage_usage._measure=measuring
def owner_root(user):return storage._fs_root()/f'workspaces/{user.id}'
def physical(user):return sum(p.stat().st_size for p in owner_root(user).rglob('*') if p.is_file())
@app.post('/api/v1/fixture/configure')
async def configure(body:dict,user:User=Depends(get_current_user)):
 mode=body['mode'];(root/'mode').write_text(mode)
 base=storage._fs_root();(base/'.test-quota-limit').write_text('0' if mode=='empty' else str(4*1024*1024))
 (base/'.test-quota-unknown').unlink(missing_ok=True)
 filler=owner_root(user)/'.quota-filler';filler.unlink(missing_ok=True)
 if mode in {'payload','history'}:
  free=400*1024 if mode=='payload' else 1
  amount=4*1024*1024-physical(user)-free
  assert amount>0;filler.write_bytes(b'x'*amount)
 if mode=='unknown':(base/'.test-quota-unknown').touch()
 return {'bytes':physical(user)}
@app.get('/api/v1/fixture/usage')
async def usage(user:User=Depends(get_current_user)):
 async with SessionLocal() as db:return {'physical':physical(user),'usage':await quota.usage(db,user)}
@app.post('/api/v1/fixture/compete')
async def compete(user:User=Depends(get_current_user)):
 async with SessionLocal() as db:await quota.check_storage_quota(db,user,0)
 return {'admitted':True}
@app.post('/api/v1/fixture/unscoped')
async def unscoped(user:User=Depends(get_current_user)):
 await asyncio.to_thread(provision.put_new,f'workspaces/{user.id}/.case-provisions','unscoped.json',b'not admitted')
 return {'unexpected':True}
'''

FAULT='''
active_task=None
original_create=provision._create_from_manifest
async def creating(*args,**kwargs):
 global active_task
 active_task=asyncio.current_task()
 try:return await original_create(*args,**kwargs)
 finally:
  if (root/'ready').exists():(root/'ended').touch()
provision._create_from_manifest=creating
@app.post('/api/v1/fixture/cancel')
async def cancel():
 assert active_task is not None;active_task.cancel()
 return {'cancelled':True}
original_link=provision.os.link
def linking(source,target,*args,**kwargs):
 result=original_link(source,target,*args,**kwargs)
 mode=(root/'mode').read_text() if (root/'mode').exists() else ''
 if Path(target).name=='large.bin' and mode in {'hold','death','cancel'}:
  record={'source':str(source),'target':str(target),'intent':str(Path(source).with_suffix('.json'))}
  (root/'installation.json').write_text(json.dumps(record));(root/'ready').touch()
  deadline=time.monotonic()+90
  while not (root/'release').exists():
   if time.monotonic()>deadline:raise TimeoutError('Local quota link hold')
   time.sleep(.02)
 return result
provision.os.link=linking
'''


def verify(url=None,modes=None):
    checks=[];runs=[]
    for mode in modes or ('normal','empty','payload','history','unknown','hold','death','cancel','unscoped'):
        api=IsolatedServer('local-provision-'+mode,app='local_provision_fixture:app',database_url=url)
        (api.root/'local_provision_fixture.py').write_text(OBSERVE+FAULT,encoding='utf8')
        with api:
            api.create_case('Local quota '+mode);api.put('Ref/large.bin','x'*(300*1024))
            api.call('POST','/case-provisions:recover')
            ws=api.call('GET','/workspaces')[0]['id'];owner=api.call('GET','/auth/me')['id']
            base=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT']);owned=base/f'workspaces/{owner}'
            plans=owned/'.case-provisions/pending'
            if mode=='history':api.call('POST',f'/workspaces/{ws}/cases',{'name':'Waiting receipt'})
            before_files={str(p):p.read_bytes() for p in (owned/'.case-provisions').rglob('*') if p.is_file()}
            api.call('POST','/fixture/configure',{'mode':mode})
            result={}
            if mode in {'hold','death','cancel'}:
                reader=peer(api);script=reader.root/'finalizing_peer.py'
                script.write_text(script.read_text(encoding='utf8')+OBSERVE,encoding='utf8')
                with reader,ThreadPoolExecutor(max_workers=2) as pool:
                    future=pool.submit(api.call,'POST',api.prefix+':copy',{'name':'Held copy'},expect=500 if mode=='cancel' else 200,raw=mode=='cancel')
                    wait_file(api.root/'ready',seconds=30)
                    installed=json.loads((api.root/'installation.json').read_text())
                    state=reader.call('GET','/fixture/usage')
                    assert state['physical']==state['usage']['storage_bytes_used']
                    assert Path(installed['source']).samefile(installed['target'])
                    assert Path(installed['intent']).is_file()
                    if mode=='death':
                        stop(api.proc)
                        try:future.result(timeout=15)
                        except OSError:pass
                        else:raise AssertionError('Dead creator returned success')
                    else:
                        if mode=='cancel':
                            api.call('POST','/fixture/cancel');time.sleep(.25)
                            assert not (api.root/'ended').exists(),'Cancellation released a running filesystem writer'
                        if url:
                            rejected=reader.call('POST','/fixture/compete',expect=409)
                            assert rejected['detail']['code']=='storage_quota_busy'
                            competing=None
                        else:
                            competing=pool.submit(reader.call,'POST','/fixture/compete')
                            time.sleep(.25);assert not competing.done(),'SQLite owner write lock was released early'
                        (api.root/'release').touch();reply=future.result(timeout=30)
                        if competing is not None:assert competing.result(timeout=15)['admitted']
                        if mode=='hold':
                            assert reader.call('GET',f"/cases/{reply['id']}/raw?path=Ref/large.bin",raw=True)==b'x'*(300*1024)
                    retained=reader.call('GET','/fixture/usage')
                    assert retained['physical']==retained['usage']['storage_bytes_used']
                    response=reader.call('POST','/case-provisions:recover')
                    assert response['recovery_required']==0,response
                    assert response['results'][-1]['status']==('committed' if mode=='hold' else 'rolled_back'),response
                    after=reader.call('GET','/fixture/usage')
                    assert after['physical']==after['usage']['storage_bytes_used']
                    assert not Path(installed['source']).exists() and not Path(installed['intent']).exists()
                    assert not script.with_suffix('.replayed').exists()
                    result={'held':state,'retained':retained,'after':after,'recovery':response,'reader':str(reader.root)}
            elif mode=='history':
                api.call('POST','/case-provisions:recover',expect=429)
                assert {str(p):p.read_bytes() for p in (owned/'.case-provisions').rglob('*') if p.is_file()}==before_files
                api.call('POST','/fixture/configure',{'mode':'normal'})
                response=api.call('POST','/case-provisions:recover')
                assert response['recovery_required']==0 and response['results'][0]['status']=='committed'
                assert not list(plans.glob('*.json'));result={'recovered_after_capacity_available':True}
            elif mode=='unscoped':
                api.call('POST','/fixture/unscoped',expect=500,raw=True)
                assert not (owned/'.case-provisions/unscoped.json').exists()
            else:
                endpoint=f'/workspaces/{ws}/cases' if mode in {'empty','unknown'} else api.prefix+':copy'
                expected=503 if mode=='unknown' else 429 if mode in {'empty','payload'} else 200
                reply=api.call('POST',endpoint,{'name':'Quota target'},expect=expected)
                if mode in {'empty','unknown'}:
                    assert {str(p):p.read_bytes() for p in (owned/'.case-provisions').rglob('*') if p.is_file()}==before_files
                elif mode=='payload':
                    assert len(list((owned/'cases').rglob('large.bin')))==1
                    assert list(plans.glob('*.json')),'Partial creation must retain its recovery plan'
                    api.call('POST','/fixture/configure',{'mode':'normal'})
                    response=api.call('POST','/case-provisions:recover')
                    assert response['recovery_required']==0 and response['results'][0]['status']=='rolled_back',response
                    result={'rejected_payload_installation_peak':True,'recovery':response}
                else:
                    assert api.call('GET',f"/cases/{reply['id']}/raw?path=Ref/large.bin",raw=True)==b'x'*(300*1024)
                    api.call('POST','/case-provisions:recover')
                    state=api.call('GET','/fixture/usage');assert state['physical']==state['usage']['storage_bytes_used']
                    result=state
            (api.root/'quota-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
            checks.append(mode);runs.append({'mode':mode,'root':str(api.root)})
            print('PASS local provision capacity '+mode,flush=True)
    return {'passed':True,'count':len(checks),'checks':checks,'runs':runs}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--modes',nargs='+');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='local-provision-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify,None,args.modes)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_localprovision_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_localprovision_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,target,args.modes)
        finally:
            if created:await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('LOCAL_PROVISION_STORAGE_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
