"""Five real API writers: fresh-process recovery around actual file/SQL commit."""
import argparse
import asyncio
import base64
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import tempfile
import uuid
from urllib.parse import quote

import httpx
import websockets

from attachment_backup_checks import PNG
from file_transactions_smoke import FIXTURE as LOCAL_FIXTURE, interrupt
from s3_file_transactions_smoke import FIXTURE as REMOTE_FIXTURE
from s3testsupport import ObjectStore
from s3_typed_publication_smoke import RUNTIME
from testsupport import IsolatedServer, SERVER

KINDS=('scene','repair','preview','quality','images')
BOUNDARIES=('partial','before_commit','after_commit')
MESH='$MeshFormat\n2.2 0 8\n$EndMeshFormat\n$Nodes\n3\n1 0 0 0\n2 1 0 0\n3 0 1 0\n$EndNodes\n$Elements\n1\n1 2 0 1 2 3\n$EndElements\n'
LOG='Mesh non-orthogonality Max: 65 average: 0.5\nMax skewness = 0.1 OK.\nMax aspect ratio = 1 OK.\nMin Jacobian = 0.02\nMesh OK.\n'
EXTRA='''
from app.routers import agent
async def no_model(*args,**kwargs):
    yield 'data: {"type":"done"}\\n\\n'
agent._run_loop=no_model
'''


async def scene_write(api, case_id):
    try:
        uri=api.base.replace('http','ws',1)+'/ws/cadscene?case_id='+str(case_id)+'&token='+quote(api.token)
        async with websockets.connect(uri,open_timeout=10) as socket:
            assert json.loads(await socket.recv())['type']=='ready'
            await socket.send(json.dumps({'type':'scene.clear'}))
            return await socket.recv()
    except websockets.exceptions.ConnectionClosed:
        raise OSError('Scene owner terminated before acknowledgement') from None


def seed(api, kind, step):
    before={}
    if kind=='scene':
        before={'A-geo/scene.json':json.dumps({'schema':'vibeflow.cadquery.scene.v1','objects':[],'messages':['old']}).encode()}
        endpoint,body=None,None
    elif kind=='repair':
        api.put('A-geo/input.step',step.decode())
        before={'A-geo/exports/repaired-fix.step':b'old step', 'A-geo/exports/repaired-fix-validation.json':b'{"old":true}'}
        endpoint,body=api.prefix+'/geometry/repair',{'path':'A-geo/input.step','operation':'fix','overwrite':True}
    elif kind=='preview':
        api.put('B-mesh/sample.msh',MESH)
        digest=hashlib.sha256(b'B-mesh/sample.msh\0'+MESH.encode()).hexdigest()
        before={'.vibeflow/msh-preview/'+digest[:20]+'/preview.vtu':b'old preview'}
        endpoint,body=api.prefix+'/geometry/msh-preview',{'path':'B-mesh/sample.msh'}
    elif kind=='quality':
        api.put('C-run/case-1/checkMesh.log',LOG)
        before={'B-mesh/quality.json':b'{"old":true}'}
        endpoint,body=api.prefix+'/mesh:evaluate',{}
    else:
        profile=api.call('POST','/provider-profiles',{'provider':'custom','name':'isolated-images',
            'base_url':'http://127.0.0.1:1','default_model':'unused','api_key':'test-only'})
        conv=api.call('POST','/conversations',{'case_id':api.case_id,'title':'images','provider_profile_id':profile['id'],'model':'unused'})
        endpoint='/conversations/'+str(conv['id'])+'/messages'
        body={'content':'transaction image message','provider_profile_id':profile['id'],
              'images':[{'name':str(i)+'.png','mime':'image/png','data':base64.b64encode(PNG).decode()} for i in range(2)]}
    for path,data in before.items():api.put(path,data.decode())
    return before,endpoint,body


def run(store=None,database_url=None,kinds=KINDS,boundaries=BOUNDARIES,output=None):
    checks,runs=[],[]
    settings=store.settings() if store else {}
    if store and database_url is None:settings['VIBEFLOW_DATABASE_URL']='sqlite+aiosqlite:///'+(store.root/'visible.sqlite').as_posix()
    fixture=(REMOTE_FIXTURE if store else LOCAL_FIXTURE)+EXTRA
    step=b''
    if 'repair' in kinds:
        import cadquery as cq
        target=output/'input.step'
        cq.exporters.export(cq.Workplane('XY').box(1,2,3),str(target));step=target.read_bytes()
    for kind in kinds:
        for boundary in boundaries:
            api=IsolatedServer('visible-'+kind+'-'+boundary,app='visible_fixture:app',database_url=database_url)
            (api.root/'visible_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+fixture,encoding='utf8')
            with api:
                case=api.create_case('Visible '+kind+' '+boundary)
                api.put('fault.txt','preserved user source')
                before,endpoint,body=seed(api,kind,step)
                mode=('after_one' if store else 'after_apply') if boundary=='partial' else boundary
                (api.root/'mode').write_text(mode)
                with httpx.Client(base_url=api.base,headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=90) as client:
                    request=(lambda:asyncio.run(scene_write(api,case['id']))) if kind=='scene' else (lambda:client.post('/api/v1'+endpoint,json=body))
                    interrupt(api,request)
                peer=IsolatedServer('visible-reader',app='visible_reader:app',database_url=api.database_url)
                overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_JWT_SECRET')}
                overrides.update(settings)
                if not store:overrides.update({key:api.env[key] for key in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_INDEX_ROOT')})
                (peer.root/'visible_reader.py').write_text('import os\nos.environ.update('+repr(overrides)+')\n'+fixture,encoding='utf8')
                peer.token=api.token
                with peer:
                    diagnosis=peer.call('GET',api.prefix+'/file-transactions')
                    committed=boundary=='after_commit'
                    assert diagnosis['status']=='pending' and diagnosis['database_committed']==committed,diagnosis
                    paths=[row['path'] for row in diagnosis['files']]
                    assert len(paths)==(2 if kind in {'repair','images'} else 1)
                    assert peer.call('GET',api.prefix+'/raw?path=fault.txt',raw=True)==b'preserved user source'
                    assert peer.call('GET',api.prefix+'/file-transactions')['status']=='idle'
                    if kind=='images':
                        events=peer.call('GET',endpoint.removesuffix('/messages'))['events']
                        messages=[r['payload'] for r in events if r['type']=='message' and r['payload'].get('role')=='user']
                        assert len(messages)==int(committed),messages
                        if committed:
                            assert {r['path'] for r in messages[0]['images']}==set(paths)
                            for path in paths:assert peer.call('GET',api.prefix+'/raw?path='+quote(path),raw=True)==PNG
                        else:
                            for path in paths:peer.call('GET',api.prefix+'/raw?path='+quote(path),expect=404)
                    else:
                        retained={path:peer.call('GET',api.prefix+'/raw?path='+quote(path),raw=True) for path in before}
                        if not committed:assert retained==before,(kind,retained)
                        else:
                            assert all(retained[path]!=before[path] for path in before)
                            if kind=='scene':assert json.loads(retained[paths[0]])['messages']==['cadscene']
                            if kind=='repair':
                                validation=json.loads(retained['A-geo/exports/repaired-fix-validation.json'])
                                assert validation['sourceSha256']==hashlib.sha256(step).hexdigest() and validation['summary']['watertight']
                                assert b'ISO-10303-21' in retained['A-geo/exports/repaired-fix.step']
                            if kind=='preview':
                                from xml.etree import ElementTree
                                xml=ElementTree.fromstring(retained[paths[0]])
                                assert xml.tag=='VTKFile' and xml.find('.//Piece').attrib['NumberOfPoints']=='3'
                            if kind=='quality':
                                report=json.loads(retained[paths[0]])
                                assert report['log_sha256']==hashlib.sha256(LOG.encode()).hexdigest() and report['mesh_ok']
                        peer.call('GET',api.prefix+'/tree')
                        assert retained=={path:peer.call('GET',api.prefix+'/raw?path='+quote(path),raw=True) for path in before}
                checks.append(kind+'_'+boundary)
                runs.append({'mode':checks[-1],'api':str(api.root),'reader':str(peer.root)})
                (output/'progress.json').write_text(json.dumps({'checks':checks,'runs':runs},indent=2),encoding='utf8')
                print('PASS '+checks[-1],flush=True)
    return {'checks':checks,'count':len(checks),'runs':runs,'storage':'s3' if store else 'fs'}


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--s3',action='store_true');parser.add_argument('--postgres-only',action='store_true')
    parser.add_argument('--kinds',nargs='+',choices=KINDS,default=KINDS)
    parser.add_argument('--boundaries',nargs='+',choices=BOUNDARIES,default=BOUNDARIES)
    args=parser.parse_args()
    output=Path(tempfile.mkdtemp(prefix='visible-transactions-evidence-',dir=SERVER.parents[1]/'tmp'))
    with ObjectStore(executable=RUNTIME) if args.s3 else nullcontext() as store:
        if args.postgres_only:
            import asyncpg
            from sqlalchemy.engine import make_url
            from app.config import Settings
            from workflow_migration_smoke import alembic
            pg=make_url(Settings(_env_file=SERVER/'.env').database_url)
            assert pg.drivername=='postgresql+asyncpg' and pg.host in {'127.0.0.1','localhost','::1'}
            name='tmp_vf_visible_'+uuid.uuid4().hex
            admin=await asyncpg.connect(host=pg.host,port=pg.port or 5432,user=pg.username,password=pg.password,database='postgres',timeout=10)
            created=False
            try:
                await admin.execute('CREATE DATABASE "'+name+'"');created=True
                url=pg.set(database=name).render_as_string(hide_password=False)
                await asyncio.to_thread(alembic,url,output,'upgrade','head')
                result=await asyncio.to_thread(run,store,url,args.kinds,args.boundaries,output)
            finally:
                if created:
                    assert await admin.fetchval('SELECT current_database()')=='postgres'
                    await admin.execute('DROP DATABASE "'+name+'"')
                await admin.close()
            result.update(database='postgresql',temporary_database_removed=True)
        else:
            result=await asyncio.to_thread(run,store,None,args.kinds,args.boundaries,output)
            result['database']='sqlite'
    (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'status':'passed','count':result['count'],'evidence':str(output)}),flush=True)


if __name__=='__main__':asyncio.run(main())
