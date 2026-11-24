"""Actual installed Kimi in the platform PTY, reading a file through real DeepSeek."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid

import aiohttp
import httpx

from live_llm_acceptance import source
from live_llm_shared_index_acceptance import SharedIndexServer,configure
from testsupport import SERVER
from app.services.process_identity_native import probe

ROOT=SERVER.parents[1]
PACKAGE=Path(os.environ['APPDATA'])/'npm/node_modules/@moonshot-ai/kimi-code'
OBSERVER='''import fs from 'node:fs';
const original=globalThis.fetch;
let count=0;
globalThis.fetch=async function(input,init){
 const url=new URL(typeof input==='string'||input instanceof URL?input:input.url);
 const live=url.hostname==='api.deepseek.com';
 if(live && ++count>8)throw new Error('Actual request budget exceeded');
 const started=Date.now();
 const response=await original.call(this,input,init);
 if(live)fs.appendFileSync(process.env.VF_KIMI_TRACE,JSON.stringify({host:url.hostname,path:url.pathname,status:response.status,elapsed_ms:Date.now()-started,request_id:response.headers.get('x-request-id')})+'\\n');
 return response;
};
'''
PROFILE='''---
name: file-acceptance
description: Read one acceptance file and return its exact content
tools:
  - Read
subagents: []
---
Use the Read tool once to read the file requested by the user. Return its exact
content as the final answer. Do not change files or run other tasks.
'''


async def terminal_run(api,command,key):
    data=bytearray();sid=None
    url=api.base.replace('http://','ws://')+'/ws/terminal'
    async with asyncio.timeout(180), aiohttp.ClientSession(trust_env=False,timeout=aiohttp.ClientTimeout(total=180)) as client:
        async with client.ws_connect(url,params={'token':api.token,'case':api.case_id,'shell':'cmd','cols':160,'rows':30},max_msg_size=2*1024*1024) as ws:
            async for msg in ws:
                if msg.type==aiohttp.WSMsgType.TEXT:
                    value=json.loads(msg.data)
                    if value.get('type')=='ready' and sid is None:
                        sid=value['session']
                        (api.root/'terminal-session.json').write_text(json.dumps({'id':sid}),encoding='utf-8')
                        await ws.send_json({'type':'input','data':command+'\r'})
                    elif value.get('type')=='error':raise RuntimeError('Terminal error: '+value.get('code','unknown'))
                    elif value.get('type')=='exit':break
                elif msg.type==aiohttp.WSMsgType.BINARY:
                    data.extend(msg.data)
                    if len(data)>2*1024*1024:raise RuntimeError('Terminal evidence exceeds bound')
                    assert key.encode() not in data
                    (api.root/'terminal-output.bin').write_bytes(data)
                    # ConPTY asks a terminal emulator for its cursor position.
                    # Reply to the actual query, just as xterm does in the UI.
                    if b'\x1b[6n' in msg.data:
                        await ws.send_json({'type':'input','data':'\x1b[1;1R'})
                    if b'VF_KIMI_COMPLETED_' in data:break
                elif msg.type in {aiohttp.WSMsgType.ERROR,aiohttp.WSMsgType.CLOSE,aiohttp.WSMsgType.CLOSED}:break
    if sid is None:raise RuntimeError('Actual terminal never became ready')
    return sid,bytes(data)


def verify(configuration):
    api=SharedIndexServer('live-kimi-deployment',app='live_fixture:app');configure(api,{},False)
    state=api.root/'kimi-state';state.mkdir()
    skills=api.root/'empty-skills';skills.mkdir()
    observer=api.root/'observe.mjs';observer.write_text(OBSERVER,encoding='utf-8')
    profile=api.root/'reader.md';profile.write_text(PROFILE,encoding='utf-8')
    (state/'config.toml').write_text('builtin_product_skills = false\ndisable_feedback_survey = true\n[loop_control]\nmax_steps_per_turn = 4\n',encoding='utf-8')
    env={'KIMI_CODE_HOME':str(state),'KIMI_DISABLE_TELEMETRY':'1',
         'KIMI_MODEL_NAME':configuration['model'],'KIMI_MODEL_API_KEY':configuration['key'],
         'KIMI_MODEL_PROVIDER_TYPE':'openai','KIMI_MODEL_BASE_URL':configuration['base_url'],
         'KIMI_MODEL_MAX_CONTEXT_SIZE':'131072','KIMI_MODEL_CAPABILITIES':'',
         'KIMI_SHELL_PATH':'C:/Program Files/Git/bin/bash.exe',
         'NODE_OPTIONS':'--import '+observer.as_uri(),'VF_KIMI_TRACE':str(api.root/'kimi-upstream.jsonl')}
    previous={key:os.environ.get(key) for key in env}
    os.environ.update(env)
    result={'passed':False,'mock_llm_calls':0,'real_llm_calls':0,'model':configuration['model'],'checks':[],
            'call_count_scope':'HTTP responses observed from api.deepseek.com; transport retries reported separately'}
    def save():
        trace=api.root/'kimi-upstream.jsonl'
        if trace.exists():
            result['upstream']=[json.loads(x) for x in trace.read_text(encoding='utf-8').splitlines()]
            result['real_llm_calls']=len(result['upstream'])
        raw=json.dumps(result,ensure_ascii=False,indent=2)
        assert configuration['key'] not in raw
        (api.root/'kimi-result.json').write_text(raw+'\n',encoding='utf-8')
    def passed(name):result['checks'].append(name);save();print('KIMI_PASS '+name,flush=True)
    print('KIMI_EVIDENCE '+str(api.root),flush=True)
    sid=None
    try:
        try:
            with api:
                api.create_case('Actual Kimi terminal deployment')
                proof='KIMI_REAL_'+uuid.uuid4().hex
                api.put('proof.txt',proof+'\n')
                # Fixture profile only permits the real Read tool. It does not
                # fabricate CLI output or provide the random proof to the model.
                with httpx.Client(base_url=api.base,headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=120) as client:
                    response=client.get('/api/v1/runtime/capabilities');response.raise_for_status();cap=response.json()
                executable=cap['cli_agents']['kimi'];assert Path(executable).is_file()
                result['executable']=executable;result['version']=json.loads((PACKAGE/'package.json').read_text())['version']
                assert result['version']=='2.6.2'
                passed('platform_capabilities_discovers_installed_real_kimi')
                # No credential in argv, stdin, prompt, evidence or output.
                command=(f'call "{executable}" --agent-file "{profile}" --skills-dir "{skills}" '
                         '--output-format stream-json -p "Read proof.txt with the Read tool once and reply with its exact content." '
                         '> kimi-output.jsonl 2> kimi-stderr.txt')
                api.put('kimi-run.cmd','@echo off\r\n'+command+'\r\nset "VF_KIMI_CODE=%ERRORLEVEL%"\r\n> kimi-exit.txt echo %VF_KIMI_CODE%\r\necho VF_KIMI_COMPLETED_%VF_KIMI_CODE%\r\n')
                sid,terminal=asyncio.run(terminal_run(api,'call .\\kimi-run.cmd',configuration['key']));result['session_id']=sid
                assert configuration['key'].encode() not in terminal
                (api.root/'terminal-output.bin').write_bytes(terminal)
                # Explicit close waits for the real owned PTY/process tree and
                # publishes the actual session completion through its normal API.
                closed=api.call('DELETE','/terminal/sessions/'+sid)
                result['terminal_close']=closed
                row=api.call('GET','/terminal/sessions/'+sid)
                result['terminal_record']=row
                assert row['cleanup_verified'] is True
                output=api.call('GET',api.prefix+'/raw?path=kimi-output.jsonl',raw=True)
                stderr=api.call('GET',api.prefix+'/raw?path=kimi-stderr.txt',raw=True)
                assert configuration['key'].encode() not in output+stderr
                (api.root/'kimi-output.jsonl').write_bytes(output);(api.root/'kimi-stderr.txt').write_bytes(stderr)
                assert api.call('GET',api.prefix+'/raw?path=kimi-exit.txt',raw=True).strip()==b'0','Actual CLI exit was not zero'
                lines=[json.loads(line) for line in output.decode('utf-8').splitlines() if line.strip()]
                result['cli_retries']=[{k:x.get(k) for k in ['failed_attempt','error_name']} for x in lines if x.get('type')=='turn.step.retrying']
                result['event_roles']=sorted({str(x.get('role')) for x in lines})
                tools=[t for x in lines for t in x.get('tool_calls',[])]
                assert len(tools)==1 and tools[0]['function']['name']=='Read'
                assert any(x.get('role')=='tool' and x.get('tool_call_id')==tools[0]['id'] and proof in x.get('content','') for x in lines)
                assert any(x.get('role')=='assistant' and proof in x.get('content','') for x in lines),'No real final answer'
                assert not any(x.get('type')=='error' for x in lines)
                assert api.call('GET',api.prefix+'/raw?path=proof.txt',raw=True)==(proof+'\n').encode()
                save()
                assert len(result['upstream'])>=2 and all(x['status']==200 and x['host']=='api.deepseek.com' for x in result['upstream'])
                passed('installed_kimi_uses_real_deepseek_tool_and_returns_unknown_file_proof')
                passed('actual_platform_terminal_closes_and_source_file_remains_unchanged')
        finally:
            if hasattr(api,'birth'):
                deadline=time.monotonic()+10
                while probe(api.birth)['state']=='match' and time.monotonic()<deadline:time.sleep(.05)
                result['api_exit']={'birth':api.birth,'probe':probe(api.birth)}
                assert result['api_exit']['probe']['state']=='exited'
        # Temporary environment-backed credentials must not persist in CLI files.
        for path in state.rglob('*'):
            if path.is_file() and path.stat().st_size<16*1024*1024:
                assert configuration['key'].encode() not in path.read_bytes(),'Credential persisted in isolated CLI state'
        result['passed']=True;save();print('KIMI_OK '+str(api.root/'kimi-result.json'),flush=True);return 0
    except BaseException as error:
        result.update(failure_type=type(error).__name__,failure=str(error).replace(configuration['key'],'[REDACTED]')[:1200])
        save();print('KIMI_FAILED '+result['failure_type'],flush=True);return 1
    finally:
        for key,value in previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source-profile-id',type=int,required=True);parser.add_argument('--review',type=Path,required=True)
    args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf-8'))
    assert review['entry']=='platform/server/live_llm_kimi_deployment_acceptance.py' and review['classification']=='real_llm'
    for name,digest in review['sources'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
    assert hashlib.sha256((PACKAGE/'dist/main.mjs').read_bytes()).hexdigest()==review['kimi_main_sha256']
    sys.exit(verify(asyncio.run(source(args.source_profile_id))))
