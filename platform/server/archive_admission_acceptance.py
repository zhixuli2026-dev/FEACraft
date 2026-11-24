"""Actual two-API backup/restore admission, streamed sends and owner exit.

No LLM calls or simulated inference. Barriers pause real completed ZIP packing
or a real ASGI response send; short-deadline injection uses production code.
"""
import concurrent.futures,hashlib,io,json,os,socket,sys,time,zipfile
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER

FIXTURE='''
import asyncio,json,time
from pathlib import Path
from app.main import app
from app.services import backup
from app.archive_admission import ArchiveAdmission
root=Path(__file__).parent
original=backup._pack_backup
def pack(*args):
 result=original(*args)
 if (root/'pause-pack').exists():
  (root/'pack-entered').touch();end=time.monotonic()+30
  while not (root/'release-pack').exists():
   if time.monotonic()>end:raise TimeoutError('actual pack barrier timed out')
   time.sleep(.01)
 return result
backup._pack_backup=pack
class Observe:
 def __init__(self,inner):self.inner=inner
 async def __call__(self,scope,receive,send):
  if scope['type']!='http':return await self.inner(scope,receive,send)
  ArchiveAdmission.seconds=.3 if (root/'short-deadline').exists() else 300
  received=0;chunks=[];paused=False
  headers=dict(scope.get('headers',[]));tag=headers.get(b'x-test-id',b'').decode('ascii')
  async def incoming():
   nonlocal received
   event=await receive()
   received+=len(event.get('body',b''))
   return event
  async def outgoing(event):
   nonlocal paused
   if event['type']=='http.response.body':chunks.append(len(event.get('body',b'')))
   await send(event)
   if (not paused and scope['path'].endswith('/backup') and event['type']=='http.response.body'
       and event.get('body') and (root/'pause-send').exists()):
    paused=True;(root/'send-entered').touch();end=time.monotonic()+30
    while not (root/'release-send').exists():
     if time.monotonic()>end:raise TimeoutError('actual send barrier timed out')
     await asyncio.sleep(.01)
  try:await self.inner(scope,incoming,outgoing)
  finally:
   if tag:(root/('request-'+tag+'.json')).write_text(json.dumps({'received_bytes':received,'chunks':chunks}))
app=Observe(app)
'''


def main():
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    api=IsolatedServer('archive-admission',app='archive_fixture:app')
    peer=IsolatedServer('archive-admission-peer',app='archive_fixture:app')
    (api.root/'archive_fixture.py').write_text(FIXTURE,encoding='utf8')
    report={'passed':False,'real_model_calls':0,'mock_llm_calls':0,'checks':[]}
    def wait(predicate,label):
        end=time.monotonic()+30
        while not predicate():
            if time.monotonic()>end:raise TimeoutError(label)
            time.sleep(.03)
    def call(host,method,path,token,**kw):
        with httpx.Client(base_url=host.base+'/api/v1',headers={'Authorization':'Bearer '+token},trust_env=False,timeout=40) as client:
            return client.request(method,path,**kw)
    def busy(host,ws,token):
        r=call(host,'GET',f'/workspaces/{ws}/backup',token)
        assert r.status_code==429 and r.json()['detail']['code']=='archive_busy',r.text[:500]
    entered=False;peer_entered=False
    try:
        api.__enter__();entered=True
        shared={k:v for k,v in api.env.items() if k in {'VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT',
            'VIBEFLOW_INDEX_ROOT','VIBEFLOW_AUDIT_DIR','VIBEFLOW_RUNTIME_CONFIG','VIBEFLOW_JWT_SECRET'}}
        (peer.root/'archive_fixture.py').write_text('import os\nos.environ.update('+repr(shared)+')\n'+FIXTURE,encoding='utf8')
        peer.__enter__();peer_entered=True
        api.create_case('Archive owner A');token_a=api.token
        ws_a=api.call('GET','/workspaces')[0]['id'];case_a=api.case_id
        # Incompressible printable bytes exercise the actual ZIP response chunks.
        payload=os.urandom(2*1024*1024).hex();api.put('Ref/random.txt',payload)
        api.create_case('Archive owner B');token_b=api.token;ws_b=api.call('GET','/workspaces')[0]['id']
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            (api.root/'pause-pack').touch()
            future=pool.submit(call,api,'GET',f'/workspaces/{ws_a}/backup',token_a,headers={'X-Test-Id':'pack'})
            wait(lambda:(api.root/'pack-entered').exists() or future.done(),'pack did not reach actual ZIP boundary')
            assert not future.done()
            busy(peer,ws_b,token_b)
            rejected=call(peer,'POST','/workspaces:restore',token_b,content=b'x'*4096,
                headers={'Content-Type':'multipart/form-data; boundary=never-read','X-Test-Id':'busy-restore'})
            assert rejected.status_code==429 and rejected.json()['detail']['code']=='archive_busy'
            wait(lambda:(peer.root/'request-busy-restore.json').exists(),'missing receive evidence')
            assert json.loads((peer.root/'request-busy-restore.json').read_text())['received_bytes']==0
            (api.root/'release-pack').touch();response=future.result();assert response.status_code==200,response.text[:300]
            (api.root/'pause-pack').unlink();data=response.content
            assert int(response.headers['content-length'])==len(data)
            report['checks'].append('two users/two API processes share one slot; busy restore rejected before body read')
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                manifest=json.loads(z.read('manifest.json'))
            import base64
            row=next(x for x in manifest['files'] if x['path']=='Ref/random.txt')
            assert base64.b64decode(row['data'])==payload.encode()
            (api.root/'pause-send').touch()
            future=pool.submit(call,api,'GET',f'/workspaces/{ws_a}/backup',token_a,headers={'X-Test-Id':'send'})
            wait(lambda:(api.root/'send-entered').exists() or future.done(),'send did not reach actual response boundary')
            assert not future.done();busy(peer,ws_b,token_b)
            (api.root/'release-send').touch();response=future.result();assert response.status_code==200
            (api.root/'pause-send').unlink()
            wait(lambda:(api.root/'request-send.json').exists(),'missing chunk evidence')
            chunks=json.loads((api.root/'request-send.json').read_text())['chunks']
            assert len(chunks)>2 and max(chunks)<=65536,chunks[:5]
            report['checks'].append('slot retained through real response sends; ZIP chunks at most64KiB')
            restored=call(peer,'POST','/workspaces:restore',token_a,files={'file':('backup.zip',data,'application/zip')})
            assert restored.status_code==200,restored.text[:600]
            restored_ws=restored.json()['workspace_id']
            cases=call(peer,'GET',f'/workspaces/{restored_ws}/cases',token_a).json()
            value=call(peer,'GET',f'/cases/{cases[0]["id"]}/raw?path=Ref/random.txt',token_a)
            assert value.content==payload.encode()
            report['checks'].append('unchanged v1 ZIP restores exact source bytes through peer API')
            # Expiration must retain capacity while an already-started real
            # compression thread is draining. No late success is permitted.
            (api.root/'pack-entered').unlink();(api.root/'release-pack').unlink()
            (api.root/'pause-pack').touch();(api.root/'short-deadline').touch()
            future=pool.submit(call,api,'GET',f'/workspaces/{ws_b}/backup',token_b)
            wait(lambda:(api.root/'pack-entered').exists() or future.done(),'deadline pack did not reach actual thread')
            assert not future.done();time.sleep(.5)
            assert not future.done(),'deadline released an active compression thread'
            busy(peer,ws_b,token_b)
            (api.root/'release-pack').touch();late=future.result();assert late.status_code==504,late.text[:500]
            (api.root/'pause-pack').unlink();(api.root/'short-deadline').unlink()
            assert call(peer,'GET',f'/workspaces/{ws_b}/backup',token_b).status_code==200
            report['checks'].append('expired pack drains its real thread before releasing capacity; late success rejected')
            # Incomplete multipart request uses a real socket, not a fake body.
            (api.root/'short-deadline').touch()
            with socket.create_connection(('127.0.0.1',api.port),timeout=10) as sock:
                sock.settimeout(10)
                head=('POST /api/v1/workspaces:restore HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '+token_a+
                    '\r\nContent-Type: multipart/form-data; boundary=slow\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n')
                sock.sendall(head.encode('ascii'));reply=b''
                while b'\r\n\r\n' not in reply:reply+=sock.recv(4096)
                assert b' 504 ' in reply.split(b'\r\n')[0],reply[:500]
            (api.root/'short-deadline').unlink()
            r=call(peer,'GET',f'/workspaces/{ws_b}/backup',token_b);assert r.status_code==200
            report['checks'].append('actual stalled multipart times out before route; capacity reusable afterwards')
            # Kill precisely the API which owns the OS slot; peer must reuse it.
            (api.root/'pack-entered').unlink();(api.root/'release-pack').unlink();(api.root/'pause-pack').touch()
            future=pool.submit(call,api,'GET',f'/workspaces/{ws_a}/backup',token_a)
            wait(lambda:(api.root/'pack-entered').exists() or future.done(),'crash owner did not reach pack boundary')
            assert not future.done();busy(peer,ws_b,token_b)
            api.proc.terminate();api.proc.wait(timeout=10)
            try:future.result()
            except httpx.HTTPError:pass
            else:raise AssertionError('Killed API returned a complete backup')
            r=call(peer,'GET',f'/workspaces/{ws_b}/backup',token_b);assert r.status_code==200,r.text[:500]
            report['checks'].append('actual API process exit releases OS slot without TTL or deletion')
        report.update(passed=True,backup_bytes=len(data),payload_sha256=hashlib.sha256(payload.encode()).hexdigest())
    except BaseException as error:report['failure']={'type':type(error).__name__,'message':str(error)[:1500]}
    finally:
        for host in (api,peer):
            (host.root/'release-pack').touch();(host.root/'release-send').touch()
        if peer_entered:peer.__exit__(None,None,None)
        if entered:api.__exit__(None,None,None)
        report['API_exits_verified']=all(h.proc is not None and h.proc.poll() is not None for h in (api,peer))
        report['passed']=report['passed'] and report['API_exits_verified']
        report['peer_root']=str(peer.root)
        (api.root/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        print('ARCHIVE_ADMISSION_RESULT '+str(api.root/'acceptance.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
