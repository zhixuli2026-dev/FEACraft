"""Actual disconnected PTY and graceful ASGI shutdown; no model inference involved."""
import argparse,asyncio,hashlib,json,os,sqlite3,subprocess,sys,time,uuid
from pathlib import Path
import websockets
import testsupport
from testsupport import IsolatedServer,SERVER
from app.services import process_identity_native as identity

ROOT=SERVER.parents[1]
RUNNER='''
import argparse,asyncio,sys
from pathlib import Path
import uvicorn
p=argparse.ArgumentParser();p.add_argument('app');p.add_argument('--app-dir');p.add_argument('--host');p.add_argument('--port',type=int);p.add_argument('--no-access-log',action='store_true');args=p.parse_args()
sys.path.insert(0,args.app_dir)
server=uvicorn.Server(uvicorn.Config(args.app,host=args.host,port=args.port,access_log=False,timeout_graceful_shutdown=15))
async def main():
 async def signal():
  while not Path('graceful-stop').exists():await asyncio.sleep(.05)
  server.should_exit=True
 watcher=asyncio.create_task(signal())
 try:await server.serve()
 finally:watcher.cancel();await asyncio.gather(watcher,return_exceptions=True)
asyncio.run(main())
'''
FIXTURE='''
import json,os
from pathlib import Path
from app.main import app
from app.services.process_identity_native import observe
@app.on_event('startup')
async def birth():Path('api-birth.json').write_text(json.dumps(observe(os.getpid())))
'''


async def verify(require_closed):
    api=IsolatedServer('terminal-graceful-shutdown',app='shutdown_fixture:app')
    (api.root/'graceful_server.py').write_text(RUNNER,'utf8');(api.root/'shutdown_fixture.py').write_text(FIXTURE,'utf8')
    report={'passed':False,'model_calls':0,'mock_llm_calls':0,'scope':'actual Windows cmd PTY and graceful API shutdown'}
    def save():(api.root/'shutdown-result.json').write_text(json.dumps(report,indent=2)+'\n','utf8')
    original=subprocess.Popen
    def launch(argv,**kwargs):
        assert argv[1:3]==['-m','uvicorn'],'Unexpected child launch in isolated server bootstrap'
        return original([argv[0],'-X','utf8','-B',str(api.root/'graceful_server.py'),*argv[3:]],**kwargs)
    try:
        testsupport.subprocess.Popen=launch
        try:await asyncio.to_thread(api.__enter__)
        finally:testsupport.subprocess.Popen=original
        case=await asyncio.to_thread(api.create_case,'Actual terminal shutdown')
        url=api.base.replace('http:','ws:')+'/ws/terminal?token='+api.token+'&shell=cmd&case='+str(api.case_id)
        with sqlite3.connect(api.root/'test.sqlite') as db:
            prefix=db.execute('SELECT s3_prefix FROM cases WHERE id=?',(case['id'],)).fetchone()[0]
        nonce=uuid.uuid4().hex;proof=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])/prefix/'Ref/shutdown-proof.txt'
        async with websockets.connect(url,proxy=None) as ws:
            while True:
                frame=await asyncio.wait_for(ws.recv(),30)
                if isinstance(frame,str):
                    message=json.loads(frame)
                    assert message.get('type')!='error','Actual terminal did not start'
                    if message.get('type')=='ready':sid=message['session'];break
            await ws.send(json.dumps({'type':'input','data':'echo '+nonce+'>Ref\\shutdown-proof.txt\r'}))
            deadline=time.monotonic()+20
            while not proof.exists():
                assert time.monotonic()<deadline,'Actual shell did not create its proof file'
                await asyncio.sleep(.1)
            assert proof.read_text().strip()==nonce
        before=await asyncio.to_thread(api.call,'GET','/terminal/sessions/'+sid)
        assert before['phase']=='active' and before['stop'] is None
        root=before['process']['root']['host'];assert identity.probe(root)['state']=='match'
        report.update(session_id=sid,before=before,detached_session_alive=True,proof_sha256=hashlib.sha256(proof.read_bytes()).hexdigest());save()
        (api.root/'graceful-stop').touch();await asyncio.to_thread(api.proc.wait,40)
        assert api.proc.returncode==0
        owner=json.loads((api.root/'api-birth.json').read_text('utf8'));assert identity.probe(owner)['state']=='exited'
        with sqlite3.connect(api.root/'test.sqlite') as db:
            db.row_factory=sqlite3.Row
            after=dict(db.execute('SELECT phase,process_json,stop_json,ended_at,error FROM native_sessions WHERE id=?',(sid,)).fetchone())
        deadline=time.monotonic()+10
        while identity.probe(root)['state']!='exited':
            assert time.monotonic()<deadline,'Actual terminal root remained live after API exit'
            await asyncio.sleep(.1)
        report.update(after=after,api_exit={'identity':owner,'observed':identity.probe(owner)},root_exit={'identity':root,'observed':identity.probe(root)},api_exit_code=api.proc.returncode)
        stop=json.loads(after['stop_json'])
        report['passed']=after['phase']=='closed' and stop.get('verified') is True and after['ended_at'] is not None
        report['normal_shutdown_left_unfinished_session']=not report['passed'];save()
        print(json.dumps({'evidence':str(api.root),'passed':report['passed'],'phase':after['phase'],'stop_recorded':bool(stop),'actual_api_and_root_exited':True,'model_calls':0}),flush=True)
        if require_closed:assert report['passed'],'Normal API shutdown did not persist complete terminal cleanup'
    except BaseException as error:
        report['failure_type']=type(error).__name__;raise
    finally:
        testsupport.subprocess.Popen=original
        if api.proc is not None:await asyncio.to_thread(api.__exit__,None,None,None)
        save()


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--review',type=Path,required=True);parser.add_argument('--require-closed',action='store_true');args=parser.parse_args()
    review=json.loads(args.review.read_text('utf8'));assert review['entry']=='platform/server/terminal_shutdown_acceptance.py'
    for name,digest in review['sources'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
    asyncio.run(verify(args.require_closed))
