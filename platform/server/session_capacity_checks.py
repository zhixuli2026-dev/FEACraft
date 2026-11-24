"""Actual PTY/VTK and optional paid DeepSeek checks for session admission."""
import argparse
import asyncio
import json
import sys
import time
import uuid
from urllib.parse import quote

import aiohttp
import httpx

from testsupport import IsolatedServer, SERVER
from publication_read_capacity_checks import FIXTURE
from mixed_read_capacity_checks import postgres

OBSERVE = '''
from app import storage
from app.services import owner_storage_budget
from app.services.terminal_sessions import TerminalSession
from app.services.viz_sessions import VizSession
from app.routers import viz
import httpx
session_http_send=httpx.AsyncClient.send
async def checked_send(client,request,*args,**kwargs):
 if request.url.host=='api.deepseek.com':assert owner_storage_budget.current() is None,'model called inside owner capacity'
 return await session_http_send(client,request,*args,**kwargs)
httpx.AsyncClient.send=checked_send
def observe(name,inside):
 pool=owner_storage_budget.current()
 if storage._use_s3():assert (pool is not None and not pool.closed)==inside,name
 with (root/'boundaries.jsonl').open('a') as stream:stream.write(json.dumps({'name':name,'inside':inside})+'\\n')
prepare=storage.prepare_job_staging
def preparing(*args,**kwargs):
 observe('stage',True)
 return prepare(*args,**kwargs)
storage.prepare_job_staging=preparing
viz.prepare_job_staging=preparing
for cls in (TerminalSession,VizSession):
 session_start_original=cls.start
 def wrap(original,name):
  async def start(*args,**kwargs):
   observe(name,False)
   return await original(*args,**kwargs)
  return start
 cls.start=wrap(session_start_original,cls.__name__+'.start')
'''

STL = '''solid triangle
 facet normal 0 0 1
  outer loop
   vertex 0 0 0
   vertex 1 0 0
   vertex 0 1 0
  endloop
 endfacet
endsolid triangle
'''


def verify(store=None, database_url=None, group='cmd', configuration=None):
    api = IsolatedServer('session-capacity', app='session_fixture:app', database_url=database_url)
    live = ''
    if configuration:
        from live_llm_acceptance import FIXTURE as LIVE
        live = LIVE
    if store:
        store.raw_client.put_bucket_versioning(Bucket=store.bucket, VersioningConfiguration={'Status': 'Enabled'})
    (api.root/'session_fixture.py').write_text('import os\nos.environ.update(' + repr(store.settings() if store else {}) +
        ')\n' + live + FIXTURE + OBSERVE, encoding='utf8')
    result = {'passed': False, 'storage': 's3' if store else 'fs', 'database': 'postgresql' if database_url else 'sqlite',
              'group': group, 'mock_llm_calls': 0, 'llm_calls': 0, 'checks': []}
    def save():
        trace = api.root/'upstream.jsonl'
        if trace.exists():
            result['upstream'] = [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()]
            result['llm_calls'] = sum(row['kind']=='http' and row['path'].endswith('/chat/completions') for row in result['upstream'])
        (api.root/'checks.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
    def check(name, **fields):
        result['checks'].append({'name': name, **fields}); save(); print('SESSION_PASS '+name, flush=True)
    def credit():
        state = api.call('GET', '/fixture/credit')
        if store:
            assert state['rows'] and all(r['state']=='settled' and r['receipt']['closed'] for r in state['rows']), state
    def raw(path):
        return api.call('GET', api.prefix+'/raw?path='+quote(path,safe=''), raw=True)
    async def terminal():
        proof = 'PTY_PROOF_'+uuid.uuid4().hex
        api.put('Ref/session-proof.txt', proof+'\n')
        before = api.call('GET', '/fixture/credit?measure=true')['bytes']
        zero = api.root/'quota-zero'
        async with aiohttp.ClientSession() as client:
            url = api.base+'/ws/terminal'
            params = {'token':api.token,'case':api.case_id,'shell':group}
            zero.touch()
            try:
                async with client.ws_connect(url, params=params) as ws:
                    message = await ws.receive_json(timeout=30)
                    assert message['code']=='storage_capacity' and message['status']==429, message
                assert api.call('GET','/terminal/sessions?include_closed=true')['sessions']==[]
                assert api.call('GET','/fixture/credit?measure=true')['bytes']==before
            finally:zero.unlink(missing_ok=True)
            check('zero_quota_no_terminal_or_storage_growth')
            async with client.ws_connect(url, params=params) as ws:
                ready = await ws.receive_json(timeout=60)
                assert ready['type']=='ready' and ready['alive'],ready
                sid = ready['session']; result['session_id']=sid
                credit();check('actual_'+group+'_started_after_capacity_release',session=sid)
                try:
                    zero.touch()
                    for message in ({'type':'input','data':'echo REJECTED > Ref/denied.txt\r\n'},
                                    {'type':'resize','cols':100,'rows':32}):
                        await ws.send_json(message)
                        async with asyncio.timeout(30):
                            while True:
                                response = await ws.receive()
                                if response.type == aiohttp.WSMsgType.TEXT:
                                    value = json.loads(response.data)
                                    assert value['code']=='storage_capacity' and value['status']==429,value
                                    break
                                assert response.type == aiohttp.WSMsgType.BINARY,response
                    assert api.call('GET','/terminal/sessions/'+sid)['phase']=='active'
                finally:zero.unlink(missing_ok=True)
                check('quota_rejects_input_and_resize_without_closing_session')
                await ws.send_json({'type':'resize','cols':100,'rows':32})
                command = ('copy /y Ref\\session-proof.txt Ref\\terminal-output.txt && type Ref\\terminal-output.txt\r\n'
                           if group=='cmd' else 'cp Ref/session-proof.txt Ref/terminal-output.txt && cat Ref/terminal-output.txt\r')
                await ws.send_json({'type':'input','data':command})
                output = b''
                async with asyncio.timeout(45):
                    while proof.encode() not in output:
                        response = await ws.receive()
                        assert response.type == aiohttp.WSMsgType.BINARY,response
                        output += response.data
                check('same_socket_recovers_and_actual_shell_reads_random_file')
                api.call('DELETE','/terminal/sessions/'+sid)
            closed = api.call('GET','/terminal/sessions/'+sid)
            (api.root/'terminal-result.json').write_text(json.dumps(closed,indent=2),encoding='utf8')
            assert closed['phase']=='closed' and closed['cleanup_verified'] and closed['stop']['verified'],closed
            assert raw('Ref/terminal-output.txt') == (proof+'\n').encode()
            api.call('GET',api.prefix+'/raw?path=Ref%2Fdenied.txt',expect=404)
            credit();check('actual_terminal_stop_and_output_sync_no_rejected_write')
    def visualization():
        api.put('D-post/triangle.stl',STL)
        before = api.call('GET','/fixture/credit?measure=true')['bytes']
        zero=api.root/'quota-zero';zero.touch()
        try:
            api.call('POST',api.prefix+'/viz:sessions',{'file_path':'D-post/triangle.stl'},expect=429)
            assert api.call('GET',api.prefix+'/viz:sessions?include_closed=true')==[]
            assert api.call('GET','/fixture/credit?measure=true')['bytes']==before
        finally:zero.unlink(missing_ok=True)
        credit();check('zero_quota_no_viz_or_storage_growth')
        api.call('POST',api.prefix+'/viz:sessions',{'file_path':'D-post/missing.stl'},expect=404)
        credit();check('missing_viz_source_settles_capacity')
        selected=api.call('POST',api.prefix+'/viz:sessions',{'file_path':'D-post/triangle.stl'})
        sid=selected['id'];result['session_id']=sid
        try:
            assert selected['alive'] and selected['phase']=='active',selected
            credit();check('actual_vtk_kernel_started_after_capacity_release',session=sid)
            with httpx.Client(trust_env=False,timeout=30) as browser:
                response=browser.get(selected['url']);response.raise_for_status()
                assert 'html' in response.headers.get('content-type','') and len(response.content)>100
            check('actual_vtk_http_client_served')
        finally:api.call('DELETE','/viz:sessions/'+str(sid))
        closed=api.call('GET','/viz:sessions/'+str(sid))
        (api.root/'viz-result.json').write_text(json.dumps(closed,indent=2),encoding='utf8')
        assert closed['phase']=='closed' and closed['cleanup_verified'] and closed['stop']['verified'],closed
        assert len(raw('.vibeflow/viz/viz-'+str(selected['port'])+'.log'))>0
        credit();check('actual_viz_stop_and_log_sync')
    def settings():
        assert configuration is not None,'Live settings check needs authorized profile'
        profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual session settings',
            'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
        conv=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Actual session settings',
            'provider_profile_id':profile['id'],'model':configuration['model'],'permission_mode':'plan'})
        path='/codex/conversations/'+str(conv['id'])+'/settings'
        body={'provider_profile_id':profile['id'],'model':configuration['model'],'permission_mode':'ask'}
        before=api.call('GET','/fixture/credit?measure=true')['bytes']
        zero=api.root/'quota-zero';zero.touch()
        try:
            api.call('PATCH',path,body,expect=429)
            assert api.call('GET','/fixture/credit?measure=true')['bytes']==before
        finally:zero.unlink(missing_ok=True)
        saved=api.call('GET','/conversations/'+str(conv['id']))
        assert saved['permission_mode']=='plan',saved
        credit();check('zero_quota_keeps_conversation_settings_and_physical_bytes')
        api.call('PATCH',path,{**body,'provider_profile_id':2147483647},expect=404)
        credit();check('unavailable_profile_rejected_and_capacity_settled')
        changed=api.call('PATCH',path,body)
        assert all(changed[key]==value for key,value in body.items()),changed
        credit();check('actual_conversation_settings_committed')
        events=[]
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=180) as client:
            with client.stream('POST','/conversations/'+str(conv['id'])+'/messages',json={
                    'provider_profile_id':changed['provider_profile_id'],'model':changed['model'],
                    'content':'用一句中文说明有限体积法中守恒是什么意思。不使用工具。'}) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith('data:'):events.append(json.loads(line[5:]))
        assert configuration['key'] not in json.dumps(events)
        (api.root/'live-events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf8')
        assert not any(e.get('type') in {'stream.error','request.failed','request.timed_out'} for e in events),events
        assert any(e.get('type')=='run.completed' for e in events),events
        answer=''.join(e.get('text','') for e in events if e.get('type')=='text.delta')
        assert answer.strip() and any(e.get('type')=='message.completed' for e in events)
        credit();check('real_deepseek_response_using_saved_profile_and_model',answer=answer)
        assert result['llm_calls']>=1 and all(row['status']==200 for row in result['upstream'] if row['kind']=='http')
    print('SESSION_EVIDENCE '+str(api.root),flush=True)
    try:
        with api:
            api.create_case('Session capacity '+group)
            try:
                if group=='viz':visualization()
                elif group=='settings':settings()
                else:asyncio.run(terminal())
            except BaseException:
                if result.get('session_id') is not None:
                    try:api.call('DELETE',('/viz:sessions/' if group=='viz' else '/terminal/sessions/')+str(result['session_id']))
                    except Exception as cleanup:result['cleanup_failure']=type(cleanup).__name__
                raise
            result['passed']=True;save()
    except BaseException as error:
        detail=str(error)
        if configuration:detail=detail.replace(configuration['key'],'[REDACTED]')
        result['failure']={'type':type(error).__name__,'detail':detail[:2500]};save()
        print('SESSION_FAILED '+json.dumps(result['failure'],ensure_ascii=False),flush=True);return 1
    print('SESSION_OK '+str(api.root/'checks.json'),flush=True);return 0


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',choices=('sqlite','postgresql'),default='sqlite')
    parser.add_argument('--storage',choices=('fs','s3'),default='s3')
    parser.add_argument('--group',choices=('cmd','wsl','wsl-sandbox','viz','settings'),default='cmd')
    parser.add_argument('--source-profile-id',type=int)
    args=parser.parse_args();configuration=None
    if args.group=='settings':
        assert args.source_profile_id is not None,'Existing authorized profile required'
        from live_llm_acceptance import source
        configuration=asyncio.run(source(args.source_profile_id))
    def run(store,database_url=None,group='cmd'):return verify(store,database_url,group,configuration)
    if args.storage=='s3':
        from s3testsupport import ObjectStore
        with ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed') as store:
            code=asyncio.run(postgres(store,args.group,run)) if args.database=='postgresql' else run(store,group=args.group)
    else:code=asyncio.run(postgres(None,args.group,run)) if args.database=='postgresql' else run(None,group=args.group)
    sys.exit(code)
