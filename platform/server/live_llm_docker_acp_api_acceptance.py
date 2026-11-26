"""Exercise the platform ACP API with real DeepSeek and Docker-backed native sessions."""

import argparse
import asyncio
from contextlib import nullcontext
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import time
from uuid import UUID, uuid4, uuid5

from cryptography.fernet import Fernet

from app.services.docker_acp import command
from app.services.docker_cli_state import StateVolume
from live_llm_acceptance import source
from live_llm_acp_platform_acceptance import result
from live_llm_docker_cli_sandbox_acceptance import review
from testsupport import IsolatedServer


def verify(harnesses, model_config, *, storage_env=None, bundle=None, browser_python=None):
    api = IsolatedServer('docker-acp-api', app='docker_acp_fixture:app')
    deployment = uuid4()
    database = api.root/'test.sqlite'
    vault = api.root/'disposable-acp-vault.key'
    vault.write_bytes(Fernet.generate_key())
    settings = {'VIBEFLOW_ACP_DOCKER_IMAGE':'vibeflow/api:cli-discovery-20261118',
                'VIBEFLOW_ACP_DOCKER_DEPLOYMENT':str(deployment), 'VIBEFLOW_VAULT_KEY_PATH':str(vault),
                'VIBEFLOW_RATE_LIMIT_BACKEND':'memory', 'VIBEFLOW_JWT_SECRET':uuid4().hex+uuid4().hex}
    settings.update(storage_env or {})
    fixture = 'import os\nos.environ.update('+repr(settings)+')\nfrom app.main import app\n'
    if bundle is not None:
        fixture += ('from fastapi.staticfiles import StaticFiles\n'
                    f'app.mount("/", StaticFiles(directory={str(bundle.resolve(strict=True))!r}, html=True))\n')
    fixture_path = api.root/'docker_acp_fixture.py'
    fixture_path.write_text(fixture, encoding='utf8')
    initial = set(command('ps','--no-trunc','--format','{{.ID}} {{.Names}}').splitlines())
    report = {'passed':False,'model':model_config['model'],'provider':'api.deepseek.com','model_receipts':[],
              'scenarios':[],'simulated_model_calls':0,'storage':'s3' if storage_env else 'fs'}
    marker = 'VF_CONTAINER_' + secrets.token_hex(12)
    published = 'VF_PUBLISHED_' + secrets.token_hex(12)
    first_path, second_path = 'system/acp-input.txt', 'system/acp-other.txt'

    def input_path(turn, user):
        return (api.root/'cli-state'/'.private-cli'/'users'/str(user)/'docker'/'acp'/turn['thread_id']
                /'case-inputs'/turn['id'])

    def observe(turn):
        with sqlite3.connect(database) as conn:
            payload = conn.execute('SELECT process_json,stop_json FROM acp_turns WHERE id=?',(turn['id'],)).fetchone()
        origin, stop = map(json.loads, payload)
        assert origin['schema'] == 'vibeflow.acp.docker.v1' and stop['origin'] == origin
        assert stop['verified'] and stop['scope_empty']
        assert set(stop['containers']) == {'worker','relay'}
        if 'resource_usage' in stop:
            fetched = api.call('GET','/acp/turns/'+turn['id'])
            assert fetched['cleanup_verified'] and fetched['resource_usage'] == stop['resource_usage']
            report.setdefault('resource_samples', []).append(fetched['resource_usage'])
        requests = [row for row in stop['upstream'] if row['method']=='POST']
        report['model_receipts'].extend(requests)
        assert requests and all(r['status']==200 and (r['complete'] or r['parts']>0) for r in requests), requests
        assert not set(row['id'] for row in stop['containers'].values()).intersection(command('ps','-aq','--no-trunc').splitlines())
        return stop['containers']['worker']['id']

    try:
        selected = []
        with api:
            case = api.create_case('Docker ACP real models')
            token = api.token
            case_files = f"/cases/{case['id']}/file"
            for name in (first_path, second_path):
                api.call('PUT',case_files,{'path':name,'content':marker})
            user = api.call('GET','/auth/me')['id']
            profile = api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Isolated official DeepSeek',
                'base_url':'https://api.deepseek.com','default_model':model_config['model'],'api_key':model_config['key']})['id']
            for harness in harnesses:
                cid = api.call('POST','/conversations',{'case_id':case['id'],'title':harness,
                    'provider_profile_id':profile,'model':model_config['model'],'permission_mode':'ask'})['id']
                endpoint = f'/acp/conversations/{cid}/{harness}/turns'
                request = {'provider_profile_id':profile,'client_message_id':uuid4().hex,
                           'case_files':[first_path],
                           'content':f'Use the file read tool to read /workspace/case/{first_path}. '
                                     'Reply with the exact reference found in the file. Remember it for this conversation.'}
                api.call('POST',endpoint,{**request,'case_files':['../outside.txt']},expect=422)
                api.call('POST',endpoint,{**request,'case_files':['.vibeflow/attempts/secret.txt']},expect=422)
                api.call('POST',endpoint,{**request,'case_files':['absent-acp-input.txt']},expect=404)
                turn = api.call('POST',endpoint,request,expect=202)
                assert turn['runtime']=='linux' and turn['execution_backend']=='docker'
                assert api.call('POST',endpoint,request,expect=202)['id']==turn['id']
                api.call('POST',endpoint,{**request,'case_files':[second_path]},expect=409)
                _, events = result(api,turn,'completed')
                reply = ''.join(x['text'] for x in events if x['type']=='agent_message_chunk')
                assert marker in reply and model_config['key'] not in reply
                assert any(x['type']=='tool_call' and x.get('kind')=='read' for x in events), [
                    (x['type'],x.get('kind')) for x in events]
                assert any(x['type']=='case_inputs' and x['files'][0]['path']==first_path for x in events)
                assert not input_path(turn,user).exists()
                selected.append((endpoint, turn, observe(turn)))
            outsider = api.call('POST','/auth/register',{'name':'Outsider','email':uuid4().hex+'@example.com',
                'password':'isolated-test-123'})['token']
            foreign = {'Authorization':'Bearer '+outsider}
            for _endpoint, turn, _container in selected:
                api.call('GET','/acp/turns/'+turn['id'],expect=404,extra_headers=foreign)
                api.call('POST','/acp/turns/'+turn['id']+':cancel',expect=404,extra_headers=foreign)
            if 'kimi' in harnesses:
                conversation = api.call('POST','/conversations',{'case_id':case['id'],'title':'Docker draft approval',
                    'provider_profile_id':profile,'permission_mode':'ask'})['id']
                draft = api.call('POST',f'/acp/conversations/{conversation}/kimi/turns',{
                    'provider_profile_id':profile,'allow_drafts':True,'client_message_id':uuid4().hex,
                    'case_files':[first_path,second_path],
                    'content':f'Use one file write tool to create /workspace/docker-draft.txt with exactly {published} as its content. '
                              'Call the write tool now. Do not retry if the tool is declined.'}, expect=202)
                draft_route = '/acp/turns/'+draft['id']
                deadline = time.monotonic()+110
                permission = None
                cursor = 0
                while time.monotonic()<deadline:
                    page = api.call('GET',draft_route+f'/events?after={cursor}')
                    permission = next((event for event in page['events'] if event['type']=='permission'),None)
                    if permission:
                        break
                    if page['events']:
                        cursor = page['events'][-1]['seq']
                    if page['has_more']:
                        continue
                    assert page['turn']['phase'] in ('admitted','running'),page['turn']
                    time.sleep(.15)
                assert permission and permission['path']=='docker-draft.txt'
                approval = draft_route+'/approvals/'+permission['approval_id']
                api.call('POST',approval,{'decision':'approve'},expect=404,extra_headers=foreign)
                expected_draft = api.root/'cli-state'/'.private-cli'/'users'/str(user)/'docker'/'acp'/draft['thread_id']/'workspace'/'docker-draft.txt'
                assert not expected_draft.exists()
                answer = api.call('POST',approval,{'decision':'approve'})
                assert answer == api.call('POST',approval,{'decision':'approve'})
                _, events = result(api,draft,'completed')
                assert expected_draft.read_text(encoding='utf8').strip()==published
                assert any(event['type']=='draft' and event['path']=='docker-draft.txt'
                           and event['text'].strip()==published for event in events)
                def read_case(name):
                    from urllib.parse import quote
                    return api.call('GET',case_files+'?path='+quote(name,safe=''))['content']
                assert read_case(first_path)==marker
                assert not input_path(draft,user).exists()
                publish = draft_route+'/drafts:publish'
                proposal = {'draft':'docker-draft.txt','path':first_path}
                api.call('POST',publish,proposal,expect=404,extra_headers=foreign)
                api.call('POST',publish,{**proposal,'path':'unselected.txt'},expect=422)
                api.call('PUT',case_files,{'path':second_path,'content':'concurrent editor revision'})
                api.call('POST',publish,{**proposal,'path':second_path},expect=409)
                assert read_case(second_path)=='concurrent editor revision'
                receipt = api.call('POST',publish,proposal)
                assert receipt == api.call('POST',publish,proposal)
                assert read_case(first_path).strip()==published
                _, evidence = result(api,draft,'completed')
                assert sum(row['type']=='draft_published' for row in evidence)==1
                observe(draft)
                report['draft_approval'] = {'file_present_after_approval':True,'foreign_access_denied':True,
                    'same_decision_replayed':True, 'case_writeback':True,'stale_file_preserved':True,
                    'unchanged_until_publication':True,'publication_replayed':True}
        with api:
            api.token = token
            for endpoint, first, old_container in selected:
                turn = api.call('POST',endpoint,{'provider_profile_id':profile,'client_message_id':uuid4().hex,
                    'thread_id':first['thread_id'],'content':'Repeat the exact first reference from our history. Use no tools.'},expect=202)
                _, events = result(api,turn,'completed')
                reply = ''.join(x['text'] for x in events if x['type']=='agent_message_chunk')
                assert marker in reply and model_config['key'] not in reply
                assert observe(turn)!=old_container and turn['thread_id']==first['thread_id']
                report['scenarios'].append({'tool':first['harness'],'native_thread_resumed':True,'read_case_file':True,
                    'fresh_container':True,'foreign_access_denied':True,'cleanup_verified':True})
            endpoint, first, _ = selected[0]
            interrupted = api.call('POST',endpoint,{'provider_profile_id':profile,'client_message_id':uuid4().hex,
                'thread_id':first['thread_id'],'case_files':[second_path],
                'content':'Write fifty numbered one-word suggestions for engineering report section names. Use no tools.'},expect=202)
            deadline = time.monotonic()+40
            while time.monotonic()<deadline:
                current = api.call('GET','/acp/turns/'+interrupted['id'])
                if current['phase']=='running':
                    break
                assert current['phase']=='admitted',current
                time.sleep(.08)
            else:
                raise TimeoutError('Docker ACP run did not reach its active phase')
            selected_label = 'label=vibeflow.acp.execution='+deployment.hex+':'+UUID(interrupted['id']).hex
            running = command('ps','--no-trunc','--filter',selected_label,'--format','{{.ID}}').splitlines()
            assert len(running)==2
            assert input_path(interrupted,user).is_dir()
            api.proc.kill()
            api.proc.wait(timeout=10)
        with api:
            api.token = token
            returned, _ = result(api,interrupted,'cancelled')
            assert returned['error']=='OwnerExited'
            assert not set(running).intersection(command('ps','-aq','--no-trunc').splitlines())
            assert not input_path(interrupted,user).exists()
            with sqlite3.connect(database) as conn:
                stop = json.loads(conn.execute('SELECT stop_json FROM acp_turns WHERE id=?',(interrupted['id'],)).fetchone()[0])
            assert stop['reason']=='owner_exited' and stop['scope_empty']
            report['model_receipts'].extend(row for row in stop['upstream'] if row['method']=='POST')
            following = api.call('POST',endpoint,{'provider_profile_id':profile,'client_message_id':uuid4().hex,
                'thread_id':first['thread_id'],'content':'Repeat the exact first reference from our history. Use no tools.'},expect=202)
            _, events = result(api,following,'completed')
            assert marker in ''.join(x['text'] for x in events if x['type']=='agent_message_chunk')
            observe(following)
            report['api_exit'] = {'original_owner_killed':True,'containers_removed':2,
                                  'verified_stop':True,'same_thread_reused':True,'case_snapshot_removed':True}
            if bundle is not None:
                browser_case = 'Docker ACP workbench file publication'
                browser_proof = secrets.token_hex(12)
                browser_path = 'acp-browser-proof.txt'
                api.call('PUT',case_files,{'path':browser_path,'content':browser_proof})
                api.call('PATCH',f"/cases/{case['id']}",{'name':browser_case})
                browser_cid = api.call('POST','/conversations',{'case_id':case['id'],'title':'Workbench publication',
                    'provider_profile_id':profile,'permission_mode':'ask'})['id']
                browser_data = {'base':api.base,'case':browser_case,'conversation':browser_cid,'profile':profile,
                                'token':api.token,'proof':browser_proof,'artifacts':str(api.root)}
                subprocess.run([str(browser_python.resolve(strict=True)), '-B','-X','utf8',
                                str(Path(__file__).with_name('docker_acp_case_browser.py'))],
                    input=json.dumps(browser_data), text=True, encoding='utf8', check=True, timeout=300,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                browser_result = json.loads((api.root/'workbench-browser.json').read_text(encoding='utf8'))
                assert browser_result['passed']
                assert api.call('GET',case_files+'?path='+browser_path)['content'].strip()==browser_proof+'-checked'
                observe(api.call('GET','/acp/turns/'+browser_result['turn_id']))
                report['browser'] = browser_result
        report['passed'] = True
    finally:
        if database.is_file():
            with sqlite3.connect(database) as conn:
                tables = conn.execute("SELECT 1 FROM sqlite_master WHERE name='acp_threads'").fetchone()
                threads = conn.execute("SELECT id,created_by FROM acp_threads WHERE host_id LIKE 'docker:%'").fetchall() if tables else []
                interrupted = conn.execute(
                    "SELECT id FROM acp_turns WHERE phase IN ('admitted','running','recovery_required')"
                ).fetchall() if tables else []
            if interrupted and not report['passed']:
                with api:
                    api.token = token
                    for identifier, in interrupted:
                        current = api.call('GET','/acp/turns/'+identifier)
                        if current['phase'] in ('admitted','running'):
                            result(api,current,'cancelled')
            volumes = set(command('volume','ls','--format','{{.Name}}').splitlines())
            for identifier,user in threads:
                thread = UUID(identifier)
                state = StateVolume(deployment,user,thread,uuid5(deployment,thread.hex))
                if state.name in volumes:
                    state.remove(command)
        vault.unlink(missing_ok=True)
        fixture_path.unlink(missing_ok=True)
        report['original_containers_match'] = initial == set(command('ps','--no-trunc','--format','{{.ID}} {{.Names}}').splitlines())
        report['passed'] = report['passed'] and report['original_containers_match']
        encoded = json.dumps(report,indent=2)+'\n'
        assert model_config['key'] not in encoded and marker not in encoded and published not in encoded
        (api.root/'docker-acp-api-result.json').write_text(encoded,encoding='utf8')
        print('DOCKER_ACP_API_EVIDENCE='+str(api.root/'docker-acp-api-result.json'), flush=True)
    assert report['passed']


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('review',type=Path)
    parser.add_argument('--harness',action='append',choices=('kimi','opencode'))
    parser.add_argument('--rustfs',type=Path)
    parser.add_argument('--bundle',type=Path)
    parser.add_argument('--browser-python',type=Path)
    args = parser.parse_args()
    if bool(args.bundle) != bool(args.browser_python):
        parser.error('--bundle and --browser-python are used together')
    review(args.review)
    store = nullcontext()
    if args.rustfs:
        from live_llm_s3_publication_maintenance_acceptance import RustFS
        store = RustFS(args.rustfs)
    config = asyncio.run(source(26))
    with store as selected:
        verify(args.harness or ('kimi','opencode'), config, storage_env=selected.settings() if selected else None,
               bundle=args.bundle, browser_python=args.browser_python)
