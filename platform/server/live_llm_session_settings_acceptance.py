"""Independent session configuration through the actual Edge workbench and DeepSeek."""

import argparse
import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import re
import time
from traceback import extract_tb
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer


CASE = 'Independent session configuration'
ALT_MODEL = 'deepseek-v4-flash'
OBSERVE = '''
import asyncio
session_send = httpx.AsyncClient.send
async def witness_session_config(self, request, *args, **kwargs):
    response = await session_send(self, request, *args, **kwargs)
    if request.url.host != 'api.deepseek.com' or not request.url.path.endswith(('/chat/completions','/messages')):
        return response
    payload = json.loads(request.content)
    receipt({'kind':'session_config','path':request.url.path,'model':payload['model'],
            'tool_count':len(payload.get('tools') or []),'status':response.status_code})
    armed = root / 'hold-model.arm'
    if armed.is_file():
        armed.unlink()
        (root / 'hold-model.ready').touch()
        deadline = time.monotonic() + 25
        while not (root / 'hold-model.release').exists():
            if time.monotonic() > deadline:
                raise TimeoutError('The settings check did not release the live response')
            await asyncio.sleep(.05)
    return response
httpx.AsyncClient.send = witness_session_config
'''


@contextmanager
def browser(api, *, width=1450):
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(api.root / f'edge-{width}',
            channel='msedge', viewport={'width':width,'height':980})
        try:
            context.add_init_script('if(location.origin===' + json.dumps(api.base) + ')localStorage.setItem("vf_token",'
                                    + json.dumps(api.token) + ');')
            page = context.new_page()
            expect.set_options(timeout=50_000)
            page.set_default_timeout(50_000)
            page.goto(api.base, wait_until='domcontentloaded')
            yield page
        finally:
            context.close()


def select_session(page, cid):
    case = page.locator('[data-case-name=' + json.dumps(CASE) + ']')
    expect(case).to_be_visible()
    if case.get_attribute('aria-pressed') != 'true':
        case.click()
    select = page.get_by_test_id('agent-conversation')
    if not select.is_visible():
        page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
    select.select_option(str(cid))
    expect(page.get_by_test_id('agent-input')).to_be_enabled()
    settings = page.locator('details.agent-settings')
    if settings.get_attribute('open') is None:
        settings.locator('summary').click()
    return settings


def setup(api, config):
    case = api.create_case(CASE)
    left, right = (api.call('POST','/provider-profiles',{'provider':provider,'name':name,
                   'base_url':base,'default_model':config['model'],'api_key':config['key']})['id']
        for provider,name,base in (
            ('deepseek','隔离配置 · 对话补全',config['base_url']),
            ('anthropic','隔离配置 · 消息','https://api.deepseek.com/anthropic')))
    profile = {'case_id':case['id'],'model':config['model'],'provider_profile_id':left,'permission_mode':'ask'}
    a = api.call('POST','/conversations',{**profile,'title':'算例复核'})['id']
    b = api.call('POST','/conversations',{**profile,'title':'独立汇报','permission_mode':'plan'})['id']
    return a, b, left, right


def baseline(api, config):
    a, _, left, right = setup(api,config)
    with browser(api) as page:
        panel = select_session(page,a)
        profile = page.get_by_test_id('agent-profile')
        expect(profile).to_have_value(str(left))
        panel.screenshot(path=str(api.root / 'settings-before.png'))
        profile.select_option(str(right))
        page.get_by_test_id('conv-model').fill(ALT_MODEL)
        page.reload(wait_until='domcontentloaded')
        select_session(page,a)
        expect(profile).to_have_value(str(left))
        expect(page.get_by_test_id('conv-model')).to_have_value(config['model'])
    latest = api.call('GET', f'/conversations/{a}')
    assert latest['provider_profile_id'] == left and latest['model'] == config['model']
    return {'baseline_reproduced':True,'model_calls':0,'render_capture':'settings-before.png'}


def saved_values(api, cid, profile, model, permission):
    data = api.call('GET', f'/conversations/{cid}')
    assert (data['provider_profile_id'],data['model'],data['permission_mode']) == (profile,model,permission)
    return data


def expect_response(page, method, path):
    return page.expect_response(lambda response: response.request.method == method
        and urlsplit(response.url).path == '/api/v1' + path, timeout=100_000)


def save_from_edge(page, cid, profile, model, permission):
    panel = select_session(page,cid)
    save = page.get_by_test_id('session-settings-save')
    expect(save).to_be_disabled()
    page.get_by_role('combobox',name='模型配置').select_option(str(profile))
    page.get_by_label('会话模型',exact=True).fill(model)
    page.get_by_role('combobox',name='Agent 权限').select_option(permission)
    expect(save).to_be_enabled()
    save.focus()
    expect(save).to_be_focused()
    with expect_response(page,'PATCH',f'/conversations/{cid}/settings') as request:
        save.press('Enter')
    assert request.value.status == 200, (request.value.status, request.value.text())
    expect(page.get_by_test_id('profile-status')).to_have_text('已保存当前会话设置')
    expect(save).to_be_disabled()
    return panel


def save_and_check_permissions(api, config, a, b, left, right, report):
    with browser(api) as page:
        panel = save_from_edge(page,a,right,ALT_MODEL,'plan')
        page.screenshot(path=str(api.root/'settings-saved-desktop.png'))
        provider = page.get_by_role('combobox',name='模型配置')
        assert provider.bounding_box()['width'] >= 112
        page.set_viewport_size({'width':960,'height':850})
        expect(provider).to_be_visible()
        assert provider.bounding_box()['width'] >= 112
        panel.screenshot(path=str(api.root/'settings-saved-narrow.png'))
        save_from_edge(page,b,left,config['model'],'ask')
        page.reload(wait_until='domcontentloaded')
        check_controls(page,a,right,ALT_MODEL,'plan')
        check_controls(page,b,left,config['model'],'ask')
    saved_values(api,a,right,ALT_MODEL,'plan')
    saved_values(api,b,left,config['model'],'ask')
    owner = api.call('GET','/auth/me')
    outsider = api.call('POST','/auth/register',{'name':'Other isolated user',
        'email':'session-settings-other@example.com','password':'isolated-test-123'})
    auth = {'Authorization':'Bearer '+outsider['token']}
    stranger_profile = api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Other profile',
        'base_url':config['base_url'],'default_model':config['model'],'api_key':config['key']},extra_headers=auth)['id']
    route = f'/conversations/{a}/settings'
    body = {'provider_profile_id':stranger_profile,'model':config['model'],'permission_mode':'ask'}
    api.call('PATCH',route,body,expect=404,extra_headers=auth)
    api.call('PATCH',route,body,expect=404)
    api.call('PATCH',route,{**body,'provider_profile_id':right,'model':'m'*201},expect=422)
    saved_values(api,a,right,ALT_MODEL,'plan')
    report.update(keyboard_save=True, browser_reload=True, configuration_isolation=True,
                  cross_account_status=404, foreign_profile_status=404, invalid_model_status=422)
    return owner['email']


def check_controls(page,cid,profile,model,permission):
    select_session(page,cid)
    expect(page.get_by_test_id('agent-profile')).to_have_value(str(profile))
    expect(page.get_by_test_id('conv-model')).to_have_value(model)
    expect(page.get_by_test_id('agent-permission-mode')).to_have_value(permission)
    expect(page.get_by_test_id('session-settings-save')).to_be_disabled()


def make_turn(api,page,cid,marker,locked_settings=None):
    route = f'/conversations/{cid}'
    if locked_settings is not None:
        (api.root/'hold-model.arm').touch()
    draft = page.get_by_test_id('agent-input')
    draft.fill(f'这是隔离测试。请仅在正文原样回复 {marker}，不调用工具。')
    with expect_response(page,'POST',route+'/messages') as event:
        draft.press('Enter')
        if locked_settings is not None:
            try:
                deadline = time.monotonic()+75
                while not (api.root/'hold-model.ready').is_file():
                    if time.monotonic() >= deadline:
                        raise TimeoutError('No real model response reached the execution boundary')
                    time.sleep(.05)
                api.call('PATCH',route+'/settings',locked_settings,expect=409)
                expect(page.get_by_test_id('session-settings-save')).to_be_disabled()
            finally:
                (api.root/'hold-model.release').touch()
    assert event.value.status==200,event.value.status
    frames = [json.loads(line[5:]) for line in event.value.text().splitlines() if line.startswith('data:')]
    assert any(frame['type']=='run.completed' for frame in frames)
    assert not any(frame['type']=='tool.result' for frame in frames)
    answer = ''.join(frame['text'] for frame in frames if frame['type']=='text.delta')
    assert marker in answer, answer
    expect(page.get_by_test_id('chat-markdown')).to_contain_text(marker)
    return answer


def real_settings_replay(api, config, report):
    with api:
        a,b,left,right = setup(api,config)
        email = save_and_check_permissions(api,config,a,b,left,right,report)
    assert api.proc.poll() is not None
    api.token = None
    with api:
        api.token = api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
        saved_values(api,a,right,ALT_MODEL,'plan')
        saved_values(api,b,left,config['model'],'ask')
        with browser(api) as page:
            markers = {a:'FF35_LEFT_CASE', b:'FF35_RIGHT_CASE'}
            check_controls(page,a,right,ALT_MODEL,'plan')
            first = make_turn(api,page,a,markers[a],{'provider_profile_id':left,'model':config['model'],'permission_mode':'ask'})
            saved_values(api,a,right,ALT_MODEL,'plan')
            check_controls(page,b,left,config['model'],'ask')
            second = make_turn(api,page,b,markers[b])
            expect(page.get_by_test_id('chat-message-list')).not_to_contain_text(markers[a])
            check_controls(page,a,right,ALT_MODEL,'plan')
            expect(page.get_by_test_id('chat-markdown')).to_contain_text(markers[a])
            expect(page.get_by_test_id('chat-message-list')).not_to_contain_text(markers[b])
        for cid,answer in ((a,first),(b,second)):
            records = api.call('GET',f'/conversations/{cid}')['events']
            assert any(row['type']=='message' and row['payload'].get('role')=='assistant'
                       and row['payload']['content']==answer for row in records)
    assert api.proc.poll() is not None
    report.update(cold_restart=True, active_settings_status=409, both_sessions_called_model=True,
                  completed_replies_isolated=True, both_apis_exited=True)


def verify(args, config):
    api = IsolatedServer('live-f35-session-settings',app='settings_fixture:app')
    dist = args.dist.resolve(strict=True)
    mount = '\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory=' + repr(str(dist)) + ', html=True))\n'
    (api.root/'settings_fixture.py').write_text(FIXTURE + '\nlive_request_limit=2\n' + OBSERVE + mount,encoding='utf8')
    print('LIVE_F35_EVIDENCE',api.root,flush=True)
    result = {'passed':False,'mock_llm_calls':0,'provider':'DeepSeek'}
    try:
        if args.baseline:
            with api:
                result.update(baseline(api,config))
        else:
            real_settings_replay(api,config,result)
            receipts = [json.loads(line) for line in (api.root/'upstream.jsonl').read_text(encoding='utf8').splitlines()]
            first, second = [row for row in receipts if row['kind']=='session_config']
            assert first['status']==200 and first['path']=='/anthropic/v1/messages' and first['model']==ALT_MODEL and first['tool_count']>0
            assert second['status']==200 and second['path']=='/v1/chat/completions' and second['model']==config['model'] and second['tool_count']==0
            result['actual_dispatch'] = [first,second]
        result['passed'] = True
    except Exception as error:
        frame = extract_tb(error.__traceback__)[-1]
        result['failure'] = {'location':f'{Path(frame.filename).name}:{frame.lineno}',
            'type':type(error).__name__,'detail':str(error).replace(config['key'],'[REDACTED]')[:1300]}
    finally:
        (api.root/'.vault_key').unlink(missing_ok=True)
        trace = api.root/'upstream.jsonl'
        result['upstream'] = [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.is_file() else []
        result['successful_model_calls'] = sum(row['kind']=='http' and row['status']==200 for row in result['upstream'])
        result['passed'] = result['passed'] and result['successful_model_calls'] == (0 if args.baseline else 2)
        encoded = json.dumps(result,ensure_ascii=False,indent=2)
        assert config['key'] not in encoded
        (api.root/'settings-result.json').write_text(encoded+'\n',encoding='utf8')
    print('LIVE_F35_RESULT',api.root/'settings-result.json','passed='+str(result['passed']),flush=True)
    return 0 if result['passed'] else 1


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--dist',type=Path,required=True)
    parser.add_argument('--baseline',action='store_true')
    arguments=parser.parse_args()
    raise SystemExit(verify(arguments,asyncio.run(source(arguments.source_profile_id))))
