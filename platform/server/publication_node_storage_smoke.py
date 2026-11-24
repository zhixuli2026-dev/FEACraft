"""Node capacity covers publication intent/copy and drains cancelled admission."""
import argparse,asyncio,json,re,tempfile,time,uuid
from pathlib import Path
from testsupport import IsolatedServer,SERVER
from publication_preparation_recovery_smoke import CONTROLLED
from publication_finalizing_smoke import peer
from publication_storage_smoke import INSPECT,terminal
from job_release_smoke import wait_file
from node_storage_budget_smoke import stop

NODE='''
from app.services import publication_storage,node_storage_budget as node
from app.config import get_settings
from app import storage
from app.services.case_write import CaseWriteConflict
def node_mode():return (Path(__file__).parent/'mode').read_text()
original_admit=publication_storage._admit
async def admitting(*args):
 result=await original_admit(*args)
 get_settings().node_storage_limit_mb=1
 base=storage._fs_root()
 if node_mode() in {'full','recovery_full'}:(base/'node-retained.bin').write_bytes(b'x'*(2*1024*1024))
 if node_mode()=='unknown':
  parent=base/node.DIRECTORY;parent.mkdir(exist_ok=True)
  (parent/'unverified.json').write_text('{}')
 return result
publication_storage._admit=admitting
@app.get('/api/v1/fixture/node')
async def node_state(user:User=Depends(get_current_user)):
 base=storage._fs_root()
 return {'claims':[json.loads(p.read_text()) for p in (base/node.DIRECTORY).glob('*.json')],
  'bytes':sum(p.stat().st_size for p in base.rglob('*') if p.is_file())}
@app.post('/api/v1/fixture/node-compete')
async def compete(body:dict,user:User=Depends(get_current_user)):
 get_settings().node_storage_limit_mb=1
 async with node.allocation(body['bytes']):return {'admitted':True}
@app.post('/api/v1/fixture/node-cancel-admission')
async def cancel_admission(user:User=Depends(get_current_user)):
 import threading
 entered=threading.Event();release=threading.Event();previous=node._admit;yielded=[]
 def held(size):
  claim=previous(size);entered.set()
  if not release.wait(8):raise TimeoutError('Missing node admission release')
  return claim
 async def operation():
  async with node.allocation(32768):yielded.append(True)
 node._admit=held;task=asyncio.create_task(operation())
 try:
  assert await asyncio.to_thread(entered.wait,6)
  task.cancel();await asyncio.sleep(.1);assert not task.done()
  release.set()
  try:await task
  except asyncio.CancelledError:pass
  else:raise AssertionError('Admission cancellation was lost')
  assert not yielded and not list((storage._fs_root()/node.DIRECTORY).glob('*.json'))
 finally:
  release.set();node._admit=previous
  if not task.done():
   task.cancel()
   try:await task
   except asyncio.CancelledError:pass
 return {'cancelled_after_claim_drain':True}
'''


def verify(modes,url=None):
    from app.services import process_identity_native as identity
    report=Path(tempfile.mkdtemp(prefix='publication-node-runs-',dir=SERVER.parents[1]/'tmp'))
    database=url or 'sqlite+aiosqlite:///'+(report/'test.sqlite').as_posix()
    checks=[];runs=[]
    for mode in modes:
        api=IsolatedServer('publication-node-'+mode,app='node_fixture:app',database_url=url)
        settings={'VIBEFLOW_DATABASE_URL':database,'VIBEFLOW_NODE_STORAGE_MIN_FREE_MB':'0'}
        (api.root/'node_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+CONTROLLED+INSPECT+NODE,encoding='utf8')
        (api.root/'mode').write_text('before_copy' if mode=='recovery_full' else mode)
        with api:
            api.env['VIBEFLOW_DATABASE_URL']=database
            api.create_case('Publication node '+mode)
            if mode=='normal':assert api.call('POST','/fixture/node-cancel-admission')=={'cancelled_after_claim_drain':True}
            api.put('E-report/report.md','# Node capacity\n\nReal report.\n')
            job_id=api.call('POST',api.prefix+'/report:build',{})['job_id']
            if mode in {'full','unknown'}:
                final=terminal(api,job_id);after=api.call('GET',f'/fixture/capacity/{job_id}')
                details=api.call('GET',f'/fixture/finalizing/{job_id}')
                assert not (api.root/'ready').exists(),'Node full copy began'
                reader_root=None
            else:
                wait_file(api.root/'ready',seconds=85)
                before=api.call('GET',f'/fixture/capacity/{job_id}');node=api.call('GET','/fixture/node')
                assert len(node['claims'])==1 and node['claims'][0]['bytes']==before['reservation']['size']
                reader=peer(api);script=reader.root/'finalizing_peer.py'
                script.write_text(script.read_text(encoding='utf8')+INSPECT+NODE,encoding='utf8')
                (reader.root/'mode').write_text(mode)
                if mode=='recovery_full':
                    stop(api.proc);deadline=time.monotonic()+5
                    while identity.probe(before['owner'])!={'state':'exited','reason':'process_exited'}:
                        assert time.monotonic()<deadline
                        time.sleep(.02)
                with reader:
                    if mode=='normal':
                        amount=1024*1024-node['bytes']-2048
                        assert amount>0 and before['reservation']['size']>2048
                        result=reader.call('POST','/fixture/node-compete',{'bytes':amount},expect=409)
                        assert result['detail']['code']=='node_storage_full',result
                        (api.root/'release').touch()
                    final=terminal(reader,job_id)
                    after=reader.call('GET',f'/fixture/capacity/{job_id}')
                    details=reader.call('GET',f'/fixture/finalizing/{job_id}')
                    assert not reader.call('GET','/fixture/node')['claims']
                reader_root=str(reader.root)
            assert after['reservation']['remaining_bytes']==0
            assert final['status']==('succeeded' if mode=='normal' else 'failed'),final
            if mode!='normal':
                assert final['attempt']['reason_code']=='resource_limit'
                usages=[e['payload'] for e in details['events'] if e['type']=='attempt.resource_usage']
                assert len(usages)==1 and usages[0]['violation']==('disk_unverified' if mode=='unknown' else 'disk_limit'),usages
                assert after['reservation']['state']=='released'
            (api.root/'node-result.json').write_text(json.dumps({'job':final,'capacity':after,'details':details},indent=2),encoding='utf8')
            checks.append(mode);runs.append({'mode':mode,'root':str(api.root),'reader':reader_root})
            print('PASS publication node '+mode,flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs}
    (report/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--modes',nargs='+')
    args=parser.parse_args();modes=args.modes or ['normal','full','unknown','recovery_full']
    report=Path(tempfile.mkdtemp(prefix='publication-node-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify,modes)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_publicationnode_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_publicationnode_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,modes,target)
        finally:
            if created:await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('PUBLICATION_NODE_STORAGE_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
