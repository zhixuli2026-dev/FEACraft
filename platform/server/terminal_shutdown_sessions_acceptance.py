"""Actual cmd/WSL detached sessions, graceful shutdown, restart and FS/S3 readback."""
import argparse,asyncio,hashlib,json,sqlite3,subprocess,time,uuid
from pathlib import Path
import websockets
import testsupport
from testsupport import IsolatedServer,SERVER
from terminal_shutdown_acceptance import RUNNER,FIXTURE
from app.services import process_identity_native as identity
from app.services.owned_process import probe_native
from s3testsupport import ObjectStore

ROOT=SERVER.parents[1]


async def verify(use_s3):
    api=IsolatedServer('terminal-shutdown-sessions-s3' if use_s3 else 'terminal-shutdown-sessions-fs',app='shutdown_fixture:app')
    store=ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed') if use_s3 else None
    result={'passed':False,'model_calls':0,'mock_llm_calls':0,'storage':'s3' if store else 'fs','sessions':[],'api_exits':[]}
    def save():(api.root/'shutdown-result.json').write_text(json.dumps(result,indent=2)+'\n','utf8')
    original=subprocess.Popen
    def launch(argv,**kwargs):
        assert argv[1:3]==['-m','uvicorn']
        return original([argv[0],'-X','utf8','-B',str(api.root/'graceful_server.py'),*argv[3:]],**kwargs)
    async def start():
        testsupport.subprocess.Popen=launch
        try:await asyncio.to_thread(api.__enter__)
        finally:testsupport.subprocess.Popen=original
    async def stop():
        owner=json.loads((api.root/'api-birth.json').read_text('utf8'))
        (api.root/'graceful-stop').touch();await asyncio.to_thread(api.proc.wait,90)
        assert api.proc.returncode==0 and identity.probe(owner)['state']=='exited'
        result['api_exits'].append({'identity':owner,'observed':identity.probe(owner),'exit_code':api.proc.returncode});save()
        api.log.close();(api.root/'graceful-stop').unlink()
    async def call(method,path,*args,**kwargs):
        deadline=time.monotonic()+30
        while True:
            try:return await asyncio.to_thread(api.call,method,path,*args,**kwargs)
            except AssertionError as error:
                if 'storage_quota_busy' not in str(error) or time.monotonic()>deadline:raise
                await asyncio.sleep(.2)
    try:
        environment={}
        if store:
            await asyncio.to_thread(store.__enter__)
            await asyncio.to_thread(store.raw_client.put_bucket_versioning,Bucket=store.bucket,VersioningConfiguration={'Status':'Enabled'})
            environment=store.settings()
        (api.root/'graceful_server.py').write_text(RUNNER,'utf8')
        (api.root/'shutdown_fixture.py').write_text('import os\nos.environ.update('+repr(environment)+')\n'+FIXTURE,'utf8')
        await start()
        first=await asyncio.to_thread(api.create_case,'Detached cmd before shutdown')
        workspace=(await call('GET','/workspaces'))[0]
        second=await call('POST','/workspaces/'+str(workspace['id'])+'/cases',{'name':'Detached WSL before shutdown'})
        with sqlite3.connect(api.root/'test.sqlite') as db:email=db.execute('SELECT email FROM users ORDER BY id LIMIT 1').fetchone()[0]
        for shell,case in [('cmd',first),('wsl',second)]:
            nonce=uuid.uuid4().hex;path='Ref/shutdown-'+shell+'.txt'
            url=api.base.replace('http:','ws:')+'/ws/terminal?token='+api.token+'&shell='+shell+'&case='+str(case['id'])
            async with websockets.connect(url,proxy=None) as ws:
                while True:
                    frame=await asyncio.wait_for(ws.recv(),60)
                    if isinstance(frame,str):
                        message=json.loads(frame)
                        assert message.get('type')!='error','Actual '+shell+' terminal failed to start'
                        if message.get('type')=='ready':sid=message['session'];break
                with sqlite3.connect(api.root/'test.sqlite') as db:profile=json.loads(db.execute('SELECT profile_json FROM native_sessions WHERE id=?',(sid,)).fetchone()[0])
                local=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/(profile['staging_prefix'] or profile['source_prefix'])/path
                command='echo '+nonce+'>'+path.replace('/','\\')+'\r' if shell=='cmd' else "printf '%s\\n' '"+nonce+"' > "+path+'\r'
                await ws.send(json.dumps({'type':'input','data':command}))
                deadline=time.monotonic()+30
                while not local.exists():
                    assert time.monotonic()<deadline,'Actual terminal did not produce its file'
                    await asyncio.sleep(.1)
                assert local.read_text().strip()==nonce
            before=await call('GET','/terminal/sessions/'+sid)
            assert before['phase']=='active' and before['stop'] is None
            assert identity.probe(before['process']['root']['host'])['state']=='match'
            if shell=='wsl':assert before['process']['root']['native']['process']['uid']==65534
            result['sessions'].append({'id':sid,'case_id':case['id'],'shell':shell,'path':path,'nonce':nonce,'before':before,
                'profile':profile,'detached_alive':True,'file_sha256':hashlib.sha256(local.read_bytes()).hexdigest()});save()
        await stop()
        for entry in result['sessions']:
            with sqlite3.connect(api.root/'test.sqlite') as db:
                db.row_factory=sqlite3.Row
                row=dict(db.execute('SELECT phase,stop_json,process_json,ended_at,error FROM native_sessions WHERE id=?',(entry['id'],)).fetchone())
            entry['after']=row;save()
            assert row['phase']=='closed' and json.loads(row['stop_json']).get('verified') is True and row['ended_at'] is not None and row['error'] is None
            assert json.loads(row['stop_json'])['reason']=='shutdown'
            births={}
            def collect(value):
                if isinstance(value,dict):
                    if value.get('schema')==identity.SCHEMA:births[json.dumps(value,sort_keys=True)]=value
                    for child in value.values():collect(child)
                elif isinstance(value,list):
                    for child in value:collect(child)
            collect(entry['before']['process']);entry['independent_exits']=[]
            for birth in births.values():
                observed=await probe_native(entry['profile']['distro'] or 'vibeflow-linux',birth) if birth['platform']=='linux' else await asyncio.to_thread(identity.probe,birth)
                assert observed['state']=='exited';entry['independent_exits'].append({'identity':birth,'observed':observed})
            stage=entry['profile']['staging_prefix']
            if stage:assert not (Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/stage).exists()
            save()
        await start()
        api.token=(await call('POST','/auth/login',{'email':email,'password':'isolated-test-123'}))['token']
        for entry in result['sessions']:
            current=await call('GET','/terminal/sessions/'+entry['id']);entry['after_restart']=current
            assert current['phase']=='closed' and current['cleanup_verified'] is True and current['owner_available'] is False
            raw=await call('GET','/cases/'+str(entry['case_id'])+'/raw?path='+entry['path'],raw=True)
            assert hashlib.sha256(raw).hexdigest()==entry['file_sha256']
            entry['restart_readback_verified']=True;save()
        await stop();result['passed']=True
        print(json.dumps({'passed':True,'storage':result['storage'],'sessions':len(result['sessions']),'model_calls':0,'evidence':str(api.root)}),flush=True)
    except BaseException as error:
        result['failure_type']=type(error).__name__;raise
    finally:
        testsupport.subprocess.Popen=original
        if api.proc is not None:await asyncio.to_thread(api.__exit__,None,None,None)
        if store:
            await asyncio.to_thread(store.__exit__,None,None,None)
            result['store_stopped']=store.proc is None or store.proc.poll() is not None
        save()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--review',type=Path,required=True);parser.add_argument('--s3',action='store_true');args=parser.parse_args()
    review=json.loads(args.review.read_text('utf8'));assert review['entry']=='platform/server/terminal_shutdown_sessions_acceptance.py'
    for name,digest in review['sources'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
    asyncio.run(verify(args.s3))
