"""Recover an interrupted ZIP installation, then read the recovered skill in a live DeepSeek turn."""
import argparse
import asyncio
import json
from pathlib import Path
import time
import uuid

import httpx

from app.services import process_identity_native as identity
from live_llm_acceptance import FIXTURE as OBSERVE_UPSTREAM, source
from skill_archive_acceptance import API_FIXTURE, INTERRUPT, zipped
from testsupport import IsolatedServer


# Log only whether the tool message in an actual outgoing model request contains
# the checked contract and sample. Keep raw prompts and credentials out of proof.
OBSERVE_BUILTIN = r'''
observed_send = httpx.AsyncClient.send
async def observe_skill_forwarding(client, request, *args, **kwargs):
    if request.url.host == 'api.deepseek.com' and request.url.path.endswith('/chat/completions'):
        messages = json.loads(request.content).get('messages', [])
        tools = [str(message.get('content', '')) for message in messages if message.get('role') == 'tool']
        proof = {'matching_tool_body': any('OpenFOAM 求解契约' in text and 'case-Re1000' in text for text in tools),
                 'tool_messages': len(tools)}
        with (root/'skill-forwarding.jsonl').open('a', encoding='utf8') as stream:
            stream.write(json.dumps(proof)+'\n')
    return await observed_send(client, request, *args, **kwargs)
httpx.AsyncClient.send = observe_skill_forwarding
'''


def _turn(api, profile, case, instruction):
    conv = api.call('POST','/conversations',{'case_id':case['id'],'title':'Read skill',
        'provider_profile_id':profile['id'],'model':profile['default_model'],'permission_mode':'plan'})
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},
                      timeout=180,trust_env=False) as client:
        with client.stream('POST',f'/conversations/{conv["id"]}/messages',json={
            'content':instruction,'provider_profile_id':profile['id'],'model':profile['default_model']}) as response:
            response.raise_for_status()
            events = [json.loads(line[5:]) for line in response.iter_lines() if line.startswith('data:')]
    assert any(event.get('type')=='run.completed' for event in events)
    return events


def _profile(api, configuration):
    return api.call('POST','/provider-profiles',{
        'provider':'deepseek','name':'Skill acceptance real DeepSeek',
        'base_url':configuration['base_url'],'default_model':configuration['model'],
        'api_key':configuration['key']})


def _save_report(api, report):
    upstream = api.root/'upstream.jsonl'
    report['upstream'] = [json.loads(line) for line in upstream.read_text(encoding='utf8').splitlines()] if upstream.exists() else []
    report['actual_http_requests'] = sum(row.get('kind')=='http' for row in report['upstream'])
    report['api_exited'] = api.proc is not None and api.proc.poll() is not None
    report['passed'] = report['passed'] and report['actual_http_requests']>=2 and report['api_exited']
    (api.root/'live-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')


def builtin(configuration):
    api = IsolatedServer('live-builtin-skill',app='live_fixture:app')
    (api.root/'live_fixture.py').write_text(OBSERVE_UPSTREAM+OBSERVE_BUILTIN,encoding='utf8')
    report = {'passed':False,'provider':'deepseek','model':configuration['model'],'mock_llm_calls':0}
    print('LIVE_BUILTIN_SKILL_EVIDENCE',api.root,flush=True)
    try:
        with api:
            case = api.create_case('Actual built-in OpenFOAM skill')
            profile = _profile(api, configuration)
            events = _turn(api, profile, case,
                '请调用 get_skill 读取 $vibeflow-openfoam。只调用这一个工具。'
                '在返回的“目录与字典”部分找到按工况特征命名的示例，只输出该示例的算例目录名。')
            assert configuration['key'] not in json.dumps(events)
            (api.root/'skill-events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf8')
            tools = [event for event in events if event.get('type')=='tool.result' and event.get('ok')]
            assert len(tools)==1 and 'case-Re1000' in json.dumps(tools)
            assert ''.join(event.get('text','') for event in events if event.get('type')=='text.delta').strip()=='case-Re1000'
            proof = [json.loads(line) for line in (api.root/'skill-forwarding.jsonl').read_text(encoding='utf8').splitlines()]
            assert proof==[{'matching_tool_body':False,'tool_messages':0},
                           {'matching_tool_body':True,'tool_messages':1}], proof
            report.update(passed=True,tool='get_skill',forwarding=proof)
    finally:
        _save_report(api, report)
    assert report['passed']


def run(configuration):
    app = 'live_fixture:app'
    origin = IsolatedServer('live-skill-install-origin', app=app)
    (origin.root/'live_fixture.py').write_text(API_FIXTURE+INTERRUPT,encoding='utf8')
    nonce = 'SKILL_' + uuid.uuid4().hex
    report = {'passed':False,'provider':'deepseek','model':configuration['model'],
              'checks':[],'mock_llm_calls':0}

    def package(body):
        return zipped([('recover-skill/SKILL.md',
                        f'---\nname: recover-skill\ndescription: Isolated recovery check\n---\n\n{body}\n'.encode())])

    def install(server, body, overwrite):
        with httpx.Client(base_url=server.base+'/api/v1',timeout=30,trust_env=False) as client:
            response = client.post('/skills/install',headers={'Authorization':'Bearer '+server.token},
                data={'overwrite':str(overwrite).lower()},
                files={'file':('skill.zip',package(body),'application/zip')})
        response.raise_for_status()

    with origin:
        case = origin.create_case('Recovered skill live verification')
        install(origin,nonce,False)
        old = origin.call('GET','/skills/recover-skill')
        target = Path(old['path']).parent
        owner = identity.observe(origin.call('GET','/testing/skill-state')['pid'])
        (origin.root/'stop').write_text('before')
        try:
            install(origin,'replacement after publication',True)
        except httpx.TransportError:
            pass
        else:
            raise AssertionError('Installer did not exit during publication')
        deadline = time.monotonic()+10
        while identity.probe(owner)['state']!='exited' and time.monotonic()<deadline:
            time.sleep(.05)
        assert identity.probe(owner)['state']=='exited' and not target.exists()
        assert (target.parent/'.skill-install.json').is_file()
        report['checks'].append('original_api_exited_before_publication')

    cold = IsolatedServer('live-skill-install-recovery',app=app)
    env = {key:origin.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_JWT_SECRET',
                                          'VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_SKILLS_STORAGE_ROOT')}
    (cold.root/'live_fixture.py').write_text(
        'import os\nos.environ.update('+repr(env)+')\n'+OBSERVE_UPSTREAM,encoding='utf8')
    print('LIVE_SKILL_RECOVERY_EVIDENCE',cold.root,flush=True)
    try:
        with cold:
            cold.token = origin.token
            profile = _profile(cold, configuration)
            events = _turn(cold, profile, case,
                '请明确调用 get_skill 工具读取 $recover-skill，只使用这个工具。读取后原样输出技能正文中的验证字符串。')
            assert configuration['key'] not in json.dumps(events)
            (cold.root/'skill-events.json').write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf8')
            tool = [event for event in events if event.get('type')=='tool.result' and event.get('ok')]
            assert len(tool)==1 and nonce in json.dumps(tool) and 'get_skill' in json.dumps(events)
            assert ''.join(event.get('text','') for event in events if event.get('type')=='text.delta').strip()==nonce
            assert target.is_dir() and not (target.parent/'.skill-install.json').exists()
            report['checks'].extend(('skill_recovered_by_live_agent','live_get_skill_used_restored_contents'))
            report['passed'] = True
    finally:
        _save_report(cold, report)
    assert report['passed']


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--builtin-only',action='store_true')
    arguments = parser.parse_args()
    verify = builtin if arguments.builtin_only else run
    verify(asyncio.run(source(arguments.source_profile_id)))
