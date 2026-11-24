"""Verify live model cancellation across API replicas and synchronous disconnect cleanup."""
import argparse
import asyncio
import json
import secrets
import socket
import time
from urllib.parse import urlencode

from cryptography.fernet import Fernet
import httpx

from live_llm_acceptance import FIXTURE, source
from live_llm_chat_markdown_acceptance import wait_for_api_exit
from testsupport import IsolatedServer


OBSERVE = '''
import asyncio, os
from cryptography.fernet import Fernet
from app.services.process_identity_native import observe
from app.services import indexsvc
from app.services.request_deadline import current
(root/'api-birth.json').write_text(json.dumps(observe(os.getpid())),encoding='utf8')
original_entries=indexsvc._entries
def observed_entries(prefix):
 entries=original_entries(prefix)
 deadline=current()
 arm=root/'arm-scan'
 if deadline is not None and arm.is_file():
  arm.unlink()
  (root/'scan-active.json').write_text(json.dumps({'actual_entries':len(entries)}),encoding='utf8')
  until=time.monotonic()+8
  try:
   while time.monotonic()<until:
    deadline.check()
    time.sleep(.015)
  except TimeoutError:
   (root/'scan-cancelled').write_text('cooperative deadline fired',encoding='utf8')
   raise
 return entries
indexsvc._entries=observed_entries
forward=httpx.AsyncClient.send
async def observed_send(self,request,*args,**kwargs):
 tag=None
 if request.url.host=='api.deepseek.com' and request.url.path.endswith('/chat/completions'):
  payload=request.content
  if b'HOLD_CROSS_REPLICA' in payload:tag='replica'
  if b'HOLD_TRANSLATION_DISCONNECT' in payload:tag='translate'
 response=await forward(self,request,*args,**kwargs)
 if tag is not None and response.status_code==200:
  receipt({'kind':'actual_response_held','scope':tag,'status':response.status_code})
  try:
   await asyncio.Event().wait()
  finally:
   await response.aclose()
   receipt({'kind':'actual_response_closed','scope':tag})
 return response
httpx.AsyncClient.send=observed_send
'''


def configure(api, overrides, key):
    fixture='import os\nos.environ.update('+repr(overrides)+')\nlive_request_limit=4\n'+FIXTURE+OBSERVE
    fixture+='\nvault=Fernet('+repr(key)+')\nllm._vault=lambda:vault\n'
    path=api.root/'isolation_fixture.py'
    path.write_text(fixture,encoding='utf8')
    return path


def evidence(api):
    trace=api.root/'upstream.jsonl'
    return [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.is_file() else []


async def wait_until(predicate, seconds=20):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.04)


def seen(api, kind, scope):
    return any(row['kind']==kind and row.get('scope')==scope for row in evidence(api))


def open_request(api, method, path, body=None):
    payload=json.dumps(body).encode() if body is not None else b''
    connection=socket.create_connection(('127.0.0.1',api.port),timeout=10)
    request=f'{method} /api/v1{path} HTTP/1.1\r\nHost: 127.0.0.1:{api.port}\r\nAuthorization: Bearer {api.token}\r\n'
    request+=f'Content-Type: application/json\r\nContent-Length: {len(payload)}\r\n\r\n'
    connection.sendall(request.encode()+payload)
    return connection


async def verify(primary, peer, config, report):
    case=primary.create_case('Scoped cancellation and read limits')
    peer.token=primary.token
    profile=primary.call('POST','/provider-profiles',{'name':'DeepSeek isolation','provider':'deepseek',
       'base_url':config['base_url'],'default_model':config['model'],'api_key':config['key']})['id']
    cid=primary.call('POST','/conversations',{'case_id':case['id'],'title':'Scoped cancellation','provider_profile_id':profile,
       'model':config['model'],'permission_mode':'ask'})['id']
    outside=primary.call('POST','/auth/register',{'email':'ff34-other@example.com','password':'test-only-pass-456','name':'Outsider'})
    address=f'/api/v1/conversations/{cid}'
    auth={'Authorization':'Bearer '+primary.token}
    frames=[]
    async with httpx.AsyncClient(trust_env=False,timeout=60) as client:
        async def run_model():
            async with client.stream('POST',primary.base+address+'/messages',headers=auth,json={
              'content':'Read this diagnostic marker and reply: HOLD_CROSS_REPLICA','provider_profile_id':profile}) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith('data:'):
                        frames.append(json.loads(line[5:]))

        task=asyncio.create_task(run_model())
        try:
            await wait_until(lambda: seen(primary,'actual_response_held','replica'))
            run=next(frame['run_id'] for frame in frames if frame['type']=='ready')
            url=peer.base+address+'/messages:cancel'
            foreign=await client.post(url,headers={'Authorization':'Bearer '+outside['token']},json={'run_id':run})
            assert foreign.status_code==404
            wrong=await client.post(url,headers=auth,json={'run_id':secrets.token_hex(16)})
            assert wrong.status_code==200 and wrong.json()['ok'] is False, wrong.text
            start=time.perf_counter()
            accepted=await client.post(url,headers=auth,json={'run_id':run})
            assert accepted.status_code==200 and accepted.json()=={'ok':True,'status':'cancelling'}, accepted.text
            await asyncio.wait_for(task,8)
            assert sum(frame['type']=='run.cancelled' for frame in frames)==1
            assert seen(primary,'actual_response_closed','replica')
            report['peer_stop']={'foreign_status':404,'wrong_run_rejected':True,'elapsed_ms':round((time.perf_counter()-start)*1000)}
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)

        with open_request(primary,'POST','/llm/translate',{
           'text':'HOLD_TRANSLATION_DISCONNECT. Translate inlet velocity into Chinese.','target':'zh','provider_profile_id':profile}):
            await wait_until(lambda: seen(primary,'actual_response_held','translate'))
        now=time.perf_counter()
        await wait_until(lambda: seen(primary,'actual_response_closed','translate'),seconds=5)
        report['translate_disconnect_ms']=round((time.perf_counter()-now)*1000)

        primary.put('Ref/f34-reference.txt','F34_INDEX_RECOVERY')
        initial=primary.call('POST',primary.prefix+'/index:rebuild')
        assert initial['ok']
        (primary.root/'arm-scan').touch()
        search=primary.prefix+'/search?'+urlencode({'q':'F34_INDEX_RECOVERY'})
        with open_request(primary,'GET',search):
            await wait_until(lambda:(primary.root/'scan-active.json').is_file())
        now=time.perf_counter()
        await wait_until(lambda:(primary.root/'scan-cancelled').is_file(),seconds=4)
        assert any(hit['path']=='Ref/f34-reference.txt' for hit in primary.call('GET',search)['hits'])
        report['scan_disconnect']={'ms':round((time.perf_counter()-now)*1000),
                                  **json.loads((primary.root/'scan-active.json').read_text(encoding='utf8'))}

        marker='AFTER_CANCEL_'+secrets.token_hex(4)
        # A fresh conversation leaves prior cancellation markers out of the provider request.
        retry=primary.call('POST','/conversations',{'case_id':case['id'],'title':'Recovery reply',
           'provider_profile_id':profile,'model':config['model'],'permission_mode':'ask'})['id']
        run=await client.post(primary.base+f'/api/v1/conversations/{retry}/messages',headers=auth,json={
            'content':'Reply exactly '+marker,'provider_profile_id':profile})
        assert run.status_code==200
        frames=[json.loads(line[5:]) for line in run.text.splitlines() if line.startswith('data:')]
        answer=''.join(frame.get('text','') for frame in frames if frame['type']=='text.delta')
        assert any(frame['type']=='run.completed' for frame in frames) and marker in answer
        report['fresh_actual_reply']=True


def run(config):
    primary=IsolatedServer('live-f34-request-owner',app='isolation_fixture:app')
    peer=IsolatedServer('live-f34-request-peer',app='isolation_fixture:app')
    key=Fernet.generate_key()
    paths=[configure(primary,{},key)]
    report={'passed':False,'model':config['model'],'mock_llm_calls':0}
    print('LIVE_F34_EVIDENCE',primary.root,flush=True)
    try:
        with primary:
            values={k:primary.env[k] for k in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
            paths.append(configure(peer,values,key))
            with peer:
                asyncio.run(verify(primary,peer,config,report))
        report['api_exit']=[wait_for_api_exit(api.root,api.port) for api in (primary,peer)]
        entries=evidence(primary)
        assert sum(row['kind']=='http' and row['status']==200 and row['path'].endswith('/chat/completions') for row in entries)==3
        assert not evidence(peer)
        report['passed']=True
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
        report['upstream']=evidence(primary)
        result=json.dumps(report,indent=2)
        assert config['key'] not in result
        (primary.root/'isolation-result.json').write_text(result,encoding='utf8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    args=parser.parse_args()
    run(asyncio.run(source(args.source_profile_id)))
