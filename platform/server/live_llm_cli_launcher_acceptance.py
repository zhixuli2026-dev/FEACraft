"""Browser CLI quick launch via a real custom Kimi launcher and real DeepSeek."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid

import httpx

from live_llm_acceptance import source
from live_llm_kimi_deployment_acceptance import OBSERVER, PROFILE, PACKAGE
from live_llm_shared_index_acceptance import SharedIndexServer, configure
from testsupport import SERVER
from app.services.process_identity_native import observe, probe
from app.services.process_stop import validate

ROOT=SERVER.parents[1]


def contract_api_checks(api):
    from app.services.cli_contracts import BEGIN,END
    prefix=api.prefix+'/cli-contract'
    originals={'AGENTS.md':'User AGENTS instructions\r\n','CLAUDE.md':'User Claude instructions\n'}
    for name,value in originals.items():api.put(name,value)
    initial=api.call('GET',prefix);assert initial['selected'] is None
    first=None
    for harness in ['claude','codex','opencode','kimi']:
        state=api.call('GET',prefix)
        answer=api.call('POST',prefix,{'expected_revision':state['revision'],'harness':harness,'instructions':'Protocol acceptance '+harness})
        first=first or answer['backup_id']
        applied=api.call('GET',prefix);assert applied['selected']==harness and applied['applied']
        target='CLAUDE.md' if harness=='claude' else 'AGENTS.md'
        for name,original in originals.items():
            raw=api.call('GET',api.prefix+'/raw?path='+name,raw=True).decode('utf-8')
            assert raw.startswith(original)
            assert raw.count(BEGIN)==raw.count(END)==(1 if name==target else 0)
        api.call('POST',prefix,{'expected_revision':state['revision'],'harness':harness},expect=409)
    current=api.call('GET',prefix)
    api.call('POST',prefix+'/backups/'+('0'*32)+':restore',{'expected_revision':current['revision']},expect=404)
    api.call('POST',prefix+'/backups/'+first+':restore',{'expected_revision':current['revision']})
    assert api.call('GET',prefix)['selected'] is None
    for name,value in originals.items():assert api.call('GET',api.prefix+'/raw?path='+name,raw=True)==value.encode()
    owner_token=api.token
    other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Other contract reader'})
    try:
        api.token=other['token'];api.call('GET',prefix,expect=(403,404))
        api.call('POST',prefix,{'expected_revision':current['revision'],'harness':'kimi'},expect=(403,404))
    finally:api.token=owner_token
    api.call('POST','/runtime/opencode:apply-prompts',{},expect=410)
    return originals,first


def verify(configuration,contracts=False,provider=False):
    api=SharedIndexServer('live-kimi-provider' if provider else 'live-cli-contract' if contracts else 'live-cli-launcher',app='live_fixture:app')
    launcher=api.root/'CLI tools & 中文'/'selected-kimi.cmd';launcher.parent.mkdir()
    configure(api,{'VIBEFLOW_KIMI':str(launcher.relative_to(api.root))},False)
    state=api.root/'kimi-state';state.mkdir()
    skills=api.root/'empty-skills';skills.mkdir()
    observer=api.root/'observe.mjs';observer.write_text(OBSERVER,encoding='utf-8')
    profile_text=PROFILE
    if contracts:
        profile_text=profile_text.replace('Return its exact\ncontent as the final answer.',
            'Return the file content and the requested project verification label as the final answer.')+'\nProject instructions:\n${agents_md}\n'
    profile=api.root/'reader.md';profile.write_text(profile_text,encoding='utf-8')
    (state/'config.toml').write_text('builtin_product_skills = false\ndisable_feedback_survey = true\n[loop_control]\nmax_steps_per_turn = 4\n',encoding='utf-8')
    node=shutil.which('node.exe');assert node
    # This is a real command launcher, not an inference implementation. Every
    # response and tool choice is produced by installed Kimi and live DeepSeek.
    prompt='Read proof.txt with the Read tool once and reply with its exact content.'
    if contracts:prompt+=' Also include the verification label specified in the project instructions.'
    command=(f'"{node}" "{PACKAGE / "dist/main.mjs"}" --agent-file "{profile}" --skills-dir "{skills}" '
             f'--output-format stream-json -p "{prompt}" '
             '> kimi-output.jsonl 2> kimi-stderr.txt')
    launcher.write_bytes(('@echo off\r\n'+command+'\r\nset "VF_KIMI_CODE=%ERRORLEVEL%"\r\n'
                         '> kimi-exit.txt echo %VF_KIMI_CODE%\r\necho VF_KIMI_COMPLETED_%VF_KIMI_CODE%\r\n').encode('utf-8'))
    env={'KIMI_CODE_HOME':str(state),'KIMI_DISABLE_TELEMETRY':'1','KIMI_MODEL_NAME':configuration['model'],
         'KIMI_MODEL_API_KEY':configuration['key'],'KIMI_MODEL_PROVIDER_TYPE':'openai',
         'KIMI_MODEL_BASE_URL':configuration['base_url'],'KIMI_MODEL_MAX_CONTEXT_SIZE':'131072',
         'KIMI_MODEL_CAPABILITIES':'','KIMI_SHELL_PATH':'C:/Program Files/Git/bin/bash.exe',
         'NODE_OPTIONS':'--import '+observer.as_uri(),'VF_KIMI_TRACE':str(api.root/'kimi-upstream.jsonl')}
    if provider:
        env={k:v for k,v in env.items() if not k.startswith('KIMI_MODEL_')}
        assert not any(k.startswith('KIMI_MODEL_') for k in os.environ),'Remove inherited model configuration before provider acceptance'
    previous={k:os.environ.get(k) for k in env}
    result={'passed':False,'checks':[],'model':configuration['model'],'mock_llm_calls':0,'real_http_responses':0}
    web=None;web_log=None

    def restore():
        for k,v in previous.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v

    def save():
        trace=api.root/'kimi-upstream.jsonl'
        if trace.exists():
            result['upstream']=[json.loads(x) for x in trace.read_text(encoding='utf-8').splitlines()]
            result['real_http_responses']=len(result['upstream'])
        raw=json.dumps(result,ensure_ascii=False,indent=2)
        assert configuration['key'] not in raw
        (api.root/'launcher-result.json').write_text(raw+'\n',encoding='utf-8')

    def passed(name):result['checks'].append(name);save();print('LAUNCHER_PASS '+name,flush=True)

    print('LAUNCHER_EVIDENCE '+str(api.root),flush=True)
    try:
        try:
            os.environ.update(env)
            with api:
                restore()  # Never pass model credentials to Vite or the browser.
                api.create_case('CLI custom installation acceptance')
                proof='LAUNCHER_REAL_'+uuid.uuid4().hex;api.put('proof.txt',proof+'\n')
                contract_info={}
                if provider:
                    profiles=[api.call('POST','/provider-profiles',{'name':'Kimi acceptance '+name,'provider':configuration['provider'],
                        'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']}) for name in ['A','B']]
                    contract_info={'provider_ids':[profiles[0]['id'],profiles[1]['id'],profiles[0]['id']]}
                    result['provider_ids']=contract_info['provider_ids']
                    async def rejected(token,query,status):
                        import websockets
                        url=api.base.replace('http:','ws:')+'/ws/terminal?'+query
                        async with websockets.connect(url, additional_headers={'Authorization': 'Bearer ' + token}, proxy=None) as ws:
                            reply=json.loads(await asyncio.wait_for(ws.recv(),30))
                            assert reply['type']=='error' and reply['status']==status,reply
                    asyncio.run(rejected(api.token,f'cli_harness=kimi&provider_profile_id={profiles[0]["id"]}&shell=wsl',400))
                    asyncio.run(rejected(api.token,f'cli_harness=claude&provider_profile_id={profiles[0]["id"]}',400))
                    other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Other provider reader'})
                    asyncio.run(rejected(other['token'],f'cli_harness=kimi&provider_profile_id={profiles[0]["id"]}',403))
                    passed('actual_API_rejects_foreign_key_and_unsupported_shell_or_harness_before_launch')
                if contracts:
                    originals,initial_backup=contract_api_checks(api)
                    contract_label='CONTRACT_REAL_'+uuid.uuid4().hex
                    contract_info={'contracts':True,'contract_label':contract_label,'initial_backup':initial_backup}
                    passed('four_actual_contract_switches_exact_backup_restore_stale_write_and_other_user_rejection')
                with httpx.Client(base_url=api.base,headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=120) as client:
                    response=client.get('/api/v1/runtime/capabilities');response.raise_for_status();cap=response.json()
                selected=cap['cli_agents']['kimi'];assert Path(selected)==launcher
                assert cap['cli_agent_details']['kimi']['source']=='explicit'
                assert launcher.parent not in [Path(p) for p in os.environ.get('PATH','').split(os.pathsep) if p]
                result['selected_executable']=selected
                passed('relative_config_resolves_to_absolute_custom_install_outside_PATH')
                with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
                web_env=dict(os.environ,VITE_API_TARGET=api.base,VITE_WS_TARGET=api.base.replace('http:','ws:'))
                assert 'KIMI_MODEL_API_KEY' not in web_env
                web_log=(api.root/'web.log').open('w',encoding='utf-8')
                web=subprocess.Popen([node,str(ROOT/'platform/web/node_modules/vite/bin/vite.js'),'--host','127.0.0.1','--port',str(port),'--strictPort'],
                    cwd=ROOT/'platform/web',env=web_env,stdout=web_log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
                result['web_birth']=observe(web.pid)
                web_url=f'http://127.0.0.1:{port}'
                with httpx.Client(trust_env=False,timeout=2) as client:
                    deadline=time.monotonic()+30
                    while True:
                        assert web.poll() is None,'Vite exited'
                        try:
                            if client.get(web_url).status_code==200:break
                        except httpx.HTTPError:pass
                        if time.monotonic()>deadline:raise TimeoutError('Vite not ready')
                        time.sleep(.2)
                user=api.call('GET','/auth/me')
                (api.root/'browser.json').write_text(json.dumps({'url':web_url,'email':user['email'],'case_id':api.case_id,
                    'expected_command':'"'+selected+'"','model_key_in_browser':False,**contract_info},ensure_ascii=False,indent=2),encoding='utf-8')
                print('BROWSER_READY '+web_url,flush=True)
                deadline=time.monotonic()+900
                done=api.root/'browser-done.json'
                while not done.exists():
                    if web.poll() is not None:raise RuntimeError('Browser server exited')
                    if time.monotonic()>deadline:raise TimeoutError('Browser acceptance not completed')
                    time.sleep(.5)
                browser=json.loads(done.read_text(encoding='utf-8'));result['browser']=browser
                assert browser['passed'] and browser['sent_command']=='"'+selected+'"\r'
                if provider:
                    runs=browser['provider_runs'];assert len(runs)==3
                    for run,pid in zip(runs,contract_info['provider_ids']):
                        item=api.call('GET','/terminal/sessions/'+run['session_id'])
                        assert item['credential']['profile_id']==pid and item['cleanup_verified'] and item['phase']=='closed'
                        validate(item['stop'],item['process']['root'])
                        assert probe(item['process']['root']['host'])['state']=='exited'
                        assert probe(item['process']['gate']['process'])['state']=='exited'
                        events=[json.loads(x) for x in run['output'].splitlines() if x.strip()]
                        assert any(x.get('role')=='assistant' and proof in x.get('content','') for x in events)
                        assert any(x.get('role')=='tool' and proof in x.get('content','') for x in events)
                    result['provider_sessions']=[x['session_id'] for x in runs]
                    passed('browser_switches_A_B_A_using_vault_keys_real_model_reads_and_verified_stops')
                sid=browser['session_id']
                record=api.call('GET','/terminal/sessions/'+sid)
                assert record['cleanup_verified'] and record['phase']=='closed'
                validate(record['stop'],record['process']['root']);result['terminal_record']=record
                output=api.call('GET',api.prefix+'/raw?path=kimi-output.jsonl',raw=True)
                stderr=api.call('GET',api.prefix+'/raw?path=kimi-stderr.txt',raw=True)
                assert configuration['key'].encode() not in output+stderr
                (api.root/'kimi-output.jsonl').write_bytes(output);(api.root/'kimi-stderr.txt').write_bytes(stderr)
                assert api.call('GET',api.prefix+'/raw?path=kimi-exit.txt',raw=True).strip()==b'0'
                events=[json.loads(x) for x in output.decode('utf-8').splitlines() if x.strip()]
                calls=[t for x in events for t in x.get('tool_calls',[])]
                assert len(calls)==1 and calls[0]['function']['name']=='Read'
                assert any(x.get('role')=='tool' and x.get('tool_call_id')==calls[0]['id'] and proof in x.get('content','') for x in events)
                assert any(x.get('role')=='assistant' and proof in x.get('content','') for x in events)
                if contracts:
                    assert any(x.get('role')=='assistant' and contract_label in x.get('content','') for x in events)
                    assert browser['contract_restore_verified']
                    for name,value in originals.items():assert api.call('GET',api.prefix+'/raw?path='+name,raw=True)==value.encode()
                    assert api.call('GET',api.prefix+'/cli-contract')['selected'] is None
                    passed('restored_project_contract_reaches_real_model_and_original_user_files_restored')
                assert not any(x.get('type')=='error' for x in events)
                assert api.call('GET',api.prefix+'/raw?path=proof.txt',raw=True)==(proof+'\n').encode()
                result['cli_retries']=[{k:x.get(k) for k in ['failed_attempt','error_name']} for x in events if x.get('type')=='turn.step.retrying']
                save();assert result['real_http_responses']>=2 and all(x['status']==200 for x in result['upstream'])
                passed('actual_browser_button_launches_selected_path_and_real_model_reads_unknown_proof')
                passed('actual_browser_close_has_verified_terminal_stop')
        finally:
            restore()
            if web is not None:
                if web.poll() is None:web.terminate()
                web.wait(timeout=15)
                result['web_exit']=probe(result['web_birth']);assert result['web_exit']['state']=='exited'
            if web_log:web_log.close()
            if hasattr(api,'birth'):
                deadline=time.monotonic()+10
                while probe(api.birth)['state']=='match' and time.monotonic()<deadline:time.sleep(.05)
                result['api_exit']={'birth':api.birth,'probe':probe(api.birth)}
                assert result['api_exit']['probe']['state']=='exited'
        for folder in [state,api.root/'cli-state']:
            for p in folder.rglob('*'):
                if p.is_file() and p.stat().st_size<16*1024*1024:assert configuration['key'].encode() not in p.read_bytes()
        result['passed']=True;save();print('LAUNCHER_OK '+str(api.root/'launcher-result.json'),flush=True);return 0
    except BaseException as error:
        result.update(failure_type=type(error).__name__,failure=str(error).replace(configuration['key'],'[REDACTED]')[:1200])
        save();print('LAUNCHER_FAILED '+result['failure_type'],flush=True);return 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--source-profile-id',type=int,required=True);parser.add_argument('--review',type=Path,required=True);parser.add_argument('--contracts',action='store_true');parser.add_argument('--provider',action='store_true')
    args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf-8'))
    assert review['entry']=='platform/server/live_llm_cli_launcher_acceptance.py' and review['classification']=='real_llm'
    for name,digest in review['sources'].items():assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
    assert hashlib.sha256((PACKAGE/'dist/main.mjs').read_bytes()).hexdigest()==review['kimi_main_sha256']
    assert not (args.contracts and args.provider),'Use separate acceptance runs'
    sys.exit(verify(asyncio.run(source(args.source_profile_id)),contracts=args.contracts,provider=args.provider))
