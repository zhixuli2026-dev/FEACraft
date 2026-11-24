"""FS creation/clone installation reserves both links and its recovery intent."""
import argparse,asyncio,json,re,tempfile,time,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from testsupport import IsolatedServer,SERVER
from publication_finalizing_smoke import peer,INSPECT
from job_release_smoke import wait_file

FIXTURE='''
from pathlib import Path
import asyncio,json,time
from app.main import app
from app import storage
from app.config import get_settings
from app.services import case_provision as provision,node_storage_budget as node
root=Path(__file__).parent
@app.get('/api/v1/fixture/node')
async def node_state():
 base=storage._fs_root()
 return {'claims':[json.loads(p.read_text()) for p in (base/node.DIRECTORY).glob('*.json')],
  'bytes':sum(p.stat().st_size for p in base.rglob('*') if p.is_file())}
@app.post('/api/v1/fixture/node-compete')
async def compete(body:dict):
 get_settings().node_storage_limit_mb=4;get_settings().node_storage_min_free_mb=0
 async with node.allocation(body['bytes']):return {'admitted':True}
@app.post('/api/v1/fixture/configure')
async def configure(body:dict):
 get_settings().node_storage_limit_mb=4;get_settings().node_storage_min_free_mb=0
 (root/'mode').write_text(body['mode'])
 base=storage._fs_root()
 if body['mode']=='full':(base/'other-cache.bin').write_bytes(b'x'*(4*1024*1024))
 if body['mode']=='unknown':
  parent=base/node.DIRECTORY;parent.mkdir(exist_ok=True);(parent/'unknown.json').write_text('{}')
 return {'configured':True}
original_link=provision.os.link
def linked(source,target,*args,**kwargs):
 result=original_link(source,target,*args,**kwargs)
 if (root/'mode').exists() and (root/'mode').read_text()=='hold' and Path(target).name=='large.bin':
  (root/'target.json').write_text(json.dumps({'path':str(target),'temporary':str(source),'intent':str(Path(source).with_suffix('.json'))}))
  (root/'ready').touch()
  deadline=time.monotonic()+60
  while not (root/'release').exists():
   if time.monotonic()>deadline:raise TimeoutError('Copy hold not released')
   time.sleep(.02)
 return result
provision.os.link=linked
'''


def verify(url=None,modes=None):
    checks=[];runs=[]
    for mode in modes or ('hold','full','unknown'):
        api=IsolatedServer('provision-node-'+mode,app='provision_node:app',database_url=url)
        (api.root/'provision_node.py').write_text(FIXTURE,encoding='utf8')
        with api:
            api.create_case('Node provision '+mode);api.put('Ref/large.bin','x'*(300*1024))
            api.call('POST','/fixture/configure',{'mode':mode})
            if mode=='hold':
                reader=peer(api);script=reader.root/'finalizing_peer.py'
                # Peer has the same cache and an independent process/node lock.
                script.write_text(script.read_text(encoding='utf8')+FIXTURE.split('from app.main import app\n',1)[1].split('original_link=',1)[0],encoding='utf8')
                with reader,ThreadPoolExecutor(max_workers=1) as pool:
                    copying=pool.submit(api.call,'POST',api.prefix+':copy',{'name':'Node copy'})
                    wait_file(api.root/'ready',seconds=20)
                    try:
                        before=reader.call('GET','/fixture/node');assert len(before['claims'])==1,before
                        paths=json.loads((api.root/'target.json').read_text());intent=Path(paths['intent'])
                        assert before['claims'][0]['bytes']==2*300*1024+intent.stat().st_size,before
                        assert Path(paths['path']).stat().st_ino==Path(paths['temporary']).stat().st_ino
                        free=4*1024*1024-before['bytes']-4096
                        assert free>0
                        reader.call('POST','/fixture/node-compete',{'bytes':free},expect=409)
                    finally:(api.root/'release').touch()
                    copied=copying.result(timeout=25)
                    after=reader.call('GET','/fixture/node');assert not after['claims']
                    assert reader.call('GET',f"/cases/{copied['id']}/raw?path=Ref/large.bin",raw=True)==b'x'*(300*1024)
                    reader.call('POST','/fixture/node-compete',{'bytes':free})
                    result={'before':before,'after':after,'copied':copied}
            else:
                response=api.call('POST',api.prefix+':copy',{'name':'Denied copy'},expect=409)
                assert response['detail']['code']==('node_storage_full' if mode=='full' else 'node_storage_unavailable'),response
                assert api.call('GET',api.prefix+'/raw?path=Ref/large.bin',raw=True)==b'x'*(300*1024)
                assert len(list((api.root/'files').glob('workspaces/*/cases/**/large.bin')))==1
                assert not (api.root/'ready').exists()
                result={'rejected':response}
            (api.root/'node-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
            checks.append(mode);runs.append({'mode':mode,'root':str(api.root)})
            print('PASS provision node '+mode,flush=True)
    return {'passed':True,'count':len(checks),'checks':checks,'runs':runs}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');parser.add_argument('--modes',nargs='+');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='provision-node-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify,None,args.modes)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_provisionnode_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_provisionnode_[0-9a-f]{32}',name)
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
    print('PROVISION_NODE_STORAGE_OK '+str(report),flush=True)


if __name__=='__main__':asyncio.run(main())
