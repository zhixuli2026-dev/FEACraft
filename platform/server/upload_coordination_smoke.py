"""Upload mutation cancellation retains coordination through thread completion.

Route cancellation must drain the four upload mutation threads before a peer
can mutate or move the case. This does not promise filesystem/DB atomicity.
"""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import tempfile
import time
import urllib.error
import urllib.request
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer,SERVER
from workflow_migration_smoke import alembic
from job_release_smoke import wait_file

CONTROLLED='''import asyncio,json,time
from pathlib import Path
from fastapi import Depends
from app.services import uploads
root=Path(__file__).parent
tasks={}
def selected():
 path=root/'control.json'
 return json.loads(path.read_text()) if path.exists() else None
def pause():
 (root/'operation.ready').touch()
 deadline=time.monotonic()+40
 while not (root/'operation.release').exists() and time.monotonic()<deadline:time.sleep(.02)
 if not (root/'operation.release').exists():raise TimeoutError('Upload mutation barrier')
names=('create_session','write_chunk','cancel_session')
wrapped={}
for name in names:
 original=getattr(uploads,name)
 def make(name,original):
  def operation(*args,**kwargs):
   control=selected()
   active=control and control['operation']==name
   try:
    if active and name!='complete_session':pause()
    return original(*args,**kwargs)
   finally:
    if active:(root/'operation.drained').touch()
  return operation
 wrapped[name]=make(name,original)
 setattr(uploads,name,wrapped[name])
from app.services import file_transactions as journal
apply=journal._apply
def applying(case,directory,index,entry,side,expected):
 control=selected()
 active=control and control['operation']=='complete_session' and side=='after'
 try:
  if active:pause()
  return apply(case,directory,index,entry,side,expected)
 finally:
  if active:(root/'operation.drained').touch()
journal._apply=applying
from app.main import app
from app.deps import get_current_user
# Capture the route task before it allocates the drained operation task. The
# files router's coordinator is stable across eager/lazy router registration.
from contextlib import asynccontextmanager
from app.routers import files as upload_routes
coordinate=upload_routes.coordinate
@asynccontextmanager
async def coordinating(*args,**kwargs):
 control=selected()
 if control:tasks[control['operation']]=asyncio.current_task()
 async with coordinate(*args,**kwargs) as value:yield value
upload_routes.coordinate=coordinating
@app.post('/api/v1/fixture/cancel')
async def cancel(user=Depends(get_current_user)):
 task=tasks[selected()['operation']];task.cancel();await asyncio.sleep(.05)
 return {'done':task.done(),'cancelled':task.cancelled()}
'''


def verify(database_url=None):
    api=IsolatedServer('upload-drain',app='upload_drain_fixture:app',database_url=database_url)
    (api.root/'upload_drain_fixture.py').write_text(CONTROLLED,encoding='utf8')
    checks=[];receipts=[]
    with api:
        api.create_case('Upload drain');workspace=api.call('GET',api.prefix)['workspace_id']
        peer=IsolatedServer('upload-drain-peer',app='peer:app',database_url=database_url)
        overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        (peer.root/'peer.py').write_text('import os\nos.environ.update('+repr(overrides)+')\nfrom app.main import app\n',encoding='utf8')
        with peer:
            peer.token=api.token
            def chunk(prefix,session):
                request=urllib.request.Request(api.base+'/api/v1'+prefix+'/uploads/'+session+'?offset=0',
                    data=b'UPLOAD',method='PUT',headers={'Authorization':'Bearer '+api.token})
                try:
                    response=api.opener.open(request,timeout=30)
                except urllib.error.HTTPError as error:response=error
                with response:return response.status,response.read().decode('utf8','replace')
            for operation in ('create_session','write_chunk','complete_session','cancel_session'):
                case=api.call('POST',f'/workspaces/{workspace}/cases',{'name':operation})
                prefix=f"/cases/{case['id']}";target='Ref/upload-target.txt'
                api.call('PUT',prefix+'/file',{'path':target,'content':'BASE'})
                session=None
                if operation!='create_session':
                    session=api.call('POST',prefix+'/uploads',{'path':target,'size':6,'overwrite':True})['id']
                    if operation in ('complete_session','cancel_session'):assert chunk(prefix,session)[0]==200
                for suffix in ('ready','release','drained'):(api.root/('operation.'+suffix)).unlink(missing_ok=True)
                (api.root/'control.json').write_text(json.dumps({'operation':operation}),encoding='utf8')
                def invoke():
                    if operation=='write_chunk':return chunk(prefix,session)
                    if operation=='create_session':return 500,api.call('POST',prefix+'/uploads',{'path':target,'size':6,'overwrite':True},expect=500,raw=True).decode()
                    method,suffix=('POST',':complete') if operation=='complete_session' else ('DELETE','')
                    return 500,api.call(method,prefix+'/uploads/'+session+suffix,expect=500,raw=True).decode()
                with ThreadPoolExecutor(max_workers=2) as pool:
                    task=pool.submit(invoke);competing=None
                    try:
                        wait_file(api.root/'operation.ready',seconds=20)
                        first=api.call('POST','/fixture/cancel')
                        second=api.call('POST','/fixture/cancel')
                        assert not first['done'] and not second['done'],(operation,first,second)
                        assert not task.done() and not (api.root/'operation.drained').exists()
                        if operation=='complete_session':
                            competing=pool.submit(peer.call,'PUT',prefix+'/file',{'path':target,'content':'NEWER'})
                        else:competing=pool.submit(peer.call,'DELETE',prefix)
                        time.sleep(.25)
                        assert not competing.done(),operation
                        assert peer.call('GET','/api/health',api_prefix=False)['ok']
                    finally:(api.root/'operation.release').touch()
                    response=task.result(timeout=20)
                    assert response[0]==500,response
                    competing.result(timeout=20)
                    wait_file(api.root/'operation.drained',seconds=5)
                (api.root/'control.json').unlink()
                source=api.root/f"files/workspaces/1/cases/{case['id']}"
                if operation=='complete_session':
                    assert peer.call('GET',prefix+'/file?path='+target)['content']=='NEWER'
                    assert (source/target).read_text()=='NEWER'
                    peer.call('DELETE',prefix)
                else:
                    assert not source.exists(),operation
                    peer.call('POST',prefix+':restore')
                    assert (source/target).read_text()=='BASE'
                    if session is not None:
                        staged=source/'.vibeflow/uploads'/session
                        if operation=='write_chunk':
                            assert (staged/'data.part').read_bytes()==b'UPLOAD'
                            assert peer.call('GET',prefix+'/uploads/'+session)['offset']==6
                        else:assert not staged.exists()
                checks.append(operation+'_repeated_cancel_drains_before_peer_mutation')
                receipts.append({'operation':operation,'first_cancel':first,'second_cancel':second,
                    'http_status':response[0],'thread_drained_before_peer_return':True})
                print('PASS '+checks[-1],flush=True)
    result={'passed':True,'count':len(checks),'checks':checks,'receipts':receipts,'evidence':str(api.root),
        'peer_evidence':str(peer.root),'developer_database_touched':False,
        'limits':'Injected ASGI task cancellation. Drain/order only; filesystem changes and DB commit are not atomic.'}
    (api.root/'upload-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='upload-drain-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_uploaddrain_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_uploaddrain_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('UPLOAD_DRAIN_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
