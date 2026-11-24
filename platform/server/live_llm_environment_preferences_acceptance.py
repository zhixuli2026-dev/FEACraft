"""Verify backend preferences in Edge and a real DeepSeek request after reload."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys


def browser(config):
    from playwright.sync_api import expect, sync_playwright

    root = Path(config['root'])
    with sync_playwright() as play:
        browser = play.chromium.launch(channel='msedge')
        page = browser.new_page(viewport={'width': 1580, 'height': 950}, locale='zh-CN')
        page.add_init_script('localStorage.setItem("vf_token",' + json.dumps(config['token']) + ');')

        def panel():
            page.locator('[data-case-name="Environment choices"]').click(timeout=25000)
            page.locator('.dv-tab:visible').filter(has_text=re.compile('^Agent$')).click()
            page.get_by_test_id('agent-conversation').select_option(str(config['cid']))
            page.get_by_text('会话设置与历史操作', exact=True).click()
            return page.get_by_test_id('agent-backend')

        page.goto(config['base'], wait_until='domcontentloaded')
        backend = panel()
        model = page.get_by_test_id('conv-model')
        save = page.get_by_test_id('session-settings-save')

        def persist(runtime):
            with page.expect_response(lambda r: r.request.method == 'PATCH' and r.url.endswith('/settings')
                                      and r.request.post_data_json.get('runtime_preferences', {}).get('runtime') == runtime) as receipt:
                save.click()
            assert receipt.value.ok, receipt.value.status
            assert receipt.value.json()['model_preference']
            expect(page.locator('.agent-panel')).to_contain_text('已保存当前会话设置')

        page.get_by_test_id('reasoning-effort').select_option('medium')
        persist('api')
        for runtime, requested, effort in [('windows', 'deepseek-v4-pro', 'high'), ('wsl', 'deepseek-flash', 'low')]:
            backend.select_option(runtime)
            model.fill(requested)
            native = page.get_by_test_id('native-model-settings')
            native.locator('summary').click()
            native.get_by_label('原生推理强度').fill(effort)
            persist(runtime)

        backend.select_option('api')
        expect(model).to_have_value('deepseek-flash')
        expect(page.get_by_test_id('reasoning-effort')).to_have_value('medium')
        page.reload(wait_until='domcontentloaded')
        backend = panel()
        expect(backend).to_have_value('api')
        expect(page.get_by_test_id('reasoning-effort')).to_have_value('medium')
        for runtime, requested, effort in [('windows', 'deepseek-v4-pro', 'high'), ('wsl', 'deepseek-flash', 'low')]:
            backend.select_option(runtime)
            expect(model).to_have_value(requested)
            native = page.get_by_test_id('native-model-settings')
            native.locator('summary').click()
            expect(native.get_by_label('原生推理强度')).to_have_value(effort)
        backend.select_option('api')
        marker = 'ENVIRONMENT_PREFS_VERIFIED'
        initial = {'settings_passed': True, 'backend_switches': 6, 'persisted_after_reload': True}
        (root / 'preferences-browser.json').write_text(json.dumps(initial, indent=2) + '\n', encoding='utf-8')
        page.get_by_test_id('agent-input').fill('Reply with only the exact text ' + marker + '. Do not call any tools.')
        page.get_by_test_id('agent-send').click()
        expect(page.get_by_test_id('chat-message-list').get_by_text(marker, exact=True)).to_be_visible(timeout=199000)
        (root / 'preferences-browser.json').write_text(json.dumps({**initial, 'passed': True,
            'real_model_reply': marker}, indent=2) + '\n', encoding='utf-8')
        browser.close()


def run(configuration, args):
    from live_llm_acceptance import FIXTURE
    from testsupport import IsolatedServer

    dist = args.dist.resolve(strict=True)
    api = IsolatedServer('ff-f12-environment-preferences', app='preference_fixture:app')
    hook = """
send_with_options = httpx.AsyncClient.send
async def observe_request(self, request, *args, **kwargs):
 if request.url.host == 'api.deepseek.com' and request.url.path.endswith('/chat/completions'):
  payload = json.loads(request.content)
  receipt({'kind':'model_options','model':payload['model'],'effort':payload.get('reasoning_effort')})
 return await send_with_options(self, request, *args, **kwargs)
httpx.AsyncClient.send=observe_request
"""
    (api.root / 'preference_fixture.py').write_text(FIXTURE + hook + '\nfrom fastapi.staticfiles import StaticFiles\n'
        f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n', encoding='utf-8')
    with api:
        api.create_case('Environment choices')
        profile = api.call('POST', '/provider-profiles', {'name': 'Authorized isolated environment', 'provider': 'deepseek',
            'default_model': configuration['model'], 'base_url': configuration['base_url'], 'api_key': configuration['key']})
        cid = api.call('POST', '/conversations', {'case_id': api.case_id, 'title': 'Persist each environment',
            'provider_profile_id': profile['id'], 'model': configuration['model'], 'permission_mode': 'ask'})['id']
        subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'], '-X', 'utf8', __file__, '--edge'], text=True,
            input=json.dumps({'token': api.token, 'base': api.base, 'cid': cid, 'root': str(api.root)}), timeout=295, check=True)
        paths = f'/conversations/{cid}/model-preferences'
        saved = api.call('GET', paths)
        assert saved['preferences']['api']['reasoning_effort'] == 'medium'
        assert saved['preferences']['windows']['native_options']['effort'] == 'high'
        assert saved['preferences']['wsl']['native_options']['effort'] == 'low'
        response = api.call('GET', f'/conversations/{cid}')
        assert any(item['type'] == 'message' and item['payload'].get('role') == 'assistant'
            and 'ENVIRONMENT_PREFS_VERIFIED' in item['payload'].get('content', '') for item in response['events'])
        api.call('PATCH', f'/provider-profiles/{profile["id"]}', {'name': 'Only a display rename'})
        assert api.call('GET', paths) == saved
        api.call('PATCH', f'/provider-profiles/{profile["id"]}', {'default_model': 'deepseek-v4-pro'})
        assert api.call('GET', paths) == {'preferences': {}, 'stale': ['api', 'windows', 'wsl']}
    trace = [json.loads(line) for line in (api.root / 'upstream.jsonl').read_text(encoding='utf-8').splitlines()]
    options = [event for event in trace if event['kind'] == 'model_options']
    assert options == [{'kind': 'model_options', 'model': 'deepseek-flash', 'effort': 'medium'}], options
    assert any(event['kind'] == 'http' and event['status'] == 200 for event in trace)
    proof = {'passed': True, 'preferences': saved, 'actual_model_calls': len(options), 'simulated_llm_calls': 0,
             'display_edit_preserved': True, 'provider_edit_invalidated': True, 'upstream_model_options': options}
    (api.root / 'preferences-acceptance.json').write_text(json.dumps(proof, indent=2) + '\n', encoding='utf-8')
    print('ENVIRONMENT_PREFERENCES', api.root / 'preferences-acceptance.json', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--edge']:
        browser(json.load(sys.stdin))
    else:
        from live_llm_acceptance import source
        parser = argparse.ArgumentParser()
        parser.add_argument('--source-profile-id', type=int, required=True)
        parser.add_argument('--dist', type=Path, required=True)
        args = parser.parse_args()
        run(asyncio.run(source(args.source_profile_id)), args)
