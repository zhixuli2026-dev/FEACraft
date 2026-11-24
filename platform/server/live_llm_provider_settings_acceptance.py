"""Verify isolated Provider Profile settings using the authorized real model catalog."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid


def browser(config):
    from playwright.sync_api import expect, sync_playwright

    root = Path(config['root'])
    proof = {'passed': False}
    with sync_playwright() as play:
        browser = play.chromium.launch(channel='msedge')
        context = browser.new_context(viewport={'width': 1470, 'height': 920}, locale='zh-CN')
        page = context.new_page()
        page.add_init_script('localStorage.setItem("vf_token",' + json.dumps(config['token']) + ');')
        page.goto(config['base'], wait_until='domcontentloaded')
        page.locator('.dv-tab:visible').filter(has_text=re.compile('^设置$')).click(timeout=25000)
        page.get_by_role('button', name='管理模型配置', exact=True).click()
        workspace = page.get_by_test_id('provider-workspace')
        workspace.get_by_role('button', name='新建配置', exact=True).click()
        workspace.get_by_label('配置名称').fill('Authorized isolated catalog')
        workspace.get_by_label('默认模型').fill(config['model'])
        workspace.get_by_label('API 地址').fill(config['endpoint'])
        key = workspace.get_by_label('API 密钥')
        key.fill(config['key'])
        with page.expect_response(lambda res: res.request.method == 'POST' and res.url.endswith('/provider-profiles')) as created:
            workspace.get_by_role('button', name='保存配置').click()
        assert created.value.ok and config['key'] not in created.value.text()
        identifier = created.value.json()['id']
        expect(workspace.get_by_role('status')).to_have_text('模型配置已保存')
        assert key.evaluate('node => node.value.length') == 0

        def check(label, success):
            button = workspace.get_by_role('button', name=label, exact=True)
            for _ in range(3):
                button.click()
                expect(button).to_be_enabled(timeout=25000)
                status = workspace.get_by_role('status')
                if status.count() and success in status.inner_text():
                    return
                expect(workspace.get_by_role('alert')).to_be_visible()
            expect(workspace.get_by_role('status')).to_contain_text(success)

        check('测试连接', '连接通过')
        check('读取模型', '已读取')
        workspace.get_by_text('可用模型', exact=True).click()
        expect(workspace.get_by_role('listitem').filter(has_text=config['model']).first).to_be_visible()
        workspace.get_by_label('配置名称').fill('Edited catalog name')
        workspace.get_by_label('API 地址').fill('http://public.example')
        workspace.get_by_role('button', name='保存配置').click()
        expect(workspace.get_by_role('alert')).to_contain_text('HTTPS')
        expect(workspace.get_by_label('配置名称')).to_have_value('Edited catalog name')
        workspace.get_by_label('API 地址').fill(config['endpoint'])
        workspace.get_by_role('button', name='保存配置').click()
        expect(workspace.get_by_role('status')).to_have_text('模型配置已保存')
        check('测试连接', '连接通过')
        workspace.screenshot(path=root / 'provider-desktop.png')
        page.set_viewport_size({'width': 390, 'height': 840})
        page.get_by_label('工作面板', exact=True).select_option('providers')
        expect(workspace.get_by_label('配置名称')).to_be_visible()
        assert workspace.evaluate('node => node.scrollWidth <= node.clientWidth + 3')
        workspace.screenshot(path=root / 'provider-mobile.png')
        proof.update(passed=True, identifier=identifier, actual_catalog_reads=3, failed_edits_kept=True,
                     key_cleared=True, actual_llm_calls=0, simulated_llm_calls=0)
        browser.close()
    (root / 'provider-browser.json').write_text(json.dumps(proof, indent=2) + '\n', encoding='utf-8')


def verify(configuration, args):
    from testsupport import IsolatedServer
    from live_llm_acceptance import FIXTURE

    dist = args.dist.resolve(strict=True)
    api = IsolatedServer('ff-f11-provider-settings', app='provider_fixture:app')
    # Leave the production transport untouched and record its actual catalog responses.
    prefix = FIXTURE + """
send_for_catalog = httpx.AsyncClient.send
async def observed_catalog(self, request, *args, **kwargs):
 response = await send_for_catalog(self, request, *args, **kwargs)
 if request.url.host == 'api.deepseek.com' and request.method == 'GET' and request.url.path.endswith('/models'):
  receipt({'kind': 'model_catalog', 'status': response.status_code, 'path': request.url.path})
 return response
httpx.AsyncClient.send = observed_catalog
"""
    (api.root / 'provider_fixture.py').write_text(prefix + '\nfrom fastapi.staticfiles import StaticFiles\n'
        f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n', encoding='utf-8')
    with api:
        api.create_case('Provider profile owner')
        owner = api.token
        visitor = api.call('POST', '/auth/register', {'name': 'Isolated visitor', 'email': uuid.uuid4().hex + '@example.com',
                                                     'password': 'isolated-f11-profile-123'})['token']
        subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'], '-X', 'utf8', __file__, '--edge'], text=True, check=True,
            input=json.dumps({'token': owner, 'base': api.base, 'root': str(api.root), 'model': configuration['model'],
                              'endpoint': configuration['base_url'], 'key': configuration['key']}), timeout=150)
        proof = json.loads((api.root / 'provider-browser.json').read_text(encoding='utf-8'))
        identifier = proof['identifier']
        assert configuration['key'] not in json.dumps(api.call('GET', '/provider-profiles'))
        api.token = visitor
        assert all(item['id'] != identifier for item in api.call('GET', '/provider-profiles'))
        api.call('GET', f'/llm/models?provider_profile_id={identifier}', expect=404)
        api.call('PATCH', f'/provider-profiles/{identifier}', {'name': 'Foreign edit'}, expect=404)
        api.call('POST', f'/provider-profiles/{identifier}:test', {}, expect=404)
        api.call('DELETE', f'/provider-profiles/{identifier}', expect=404)
        api.token = owner
        api.call('DELETE', f'/provider-profiles/{identifier}')
        assert all(item['id'] != identifier for item in api.call('GET', '/provider-profiles'))
    trace = [json.loads(line) for line in (api.root / 'upstream.jsonl').read_text(encoding='utf-8').splitlines()]
    statuses = [event['status'] for event in trace if event['kind'] == 'model_catalog']
    assert statuses == [200, 200, 200], statuses
    failures = [event['error_type'] for event in trace if event['kind'] == 'transport_error']
    proof.update(catalog_upstream_status=statuses, cross_account_denied=True, owned_delete_passed=True,
                 catalog_transport_failures=failures)
    (api.root / 'provider-settings.json').write_text(json.dumps(proof, indent=2) + '\n', encoding='utf-8')
    print('PROVIDER_SETTINGS', api.root / 'provider-settings.json', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--edge']:
        browser(json.load(sys.stdin))
    else:
        from live_llm_acceptance import source
        parser = argparse.ArgumentParser()
        parser.add_argument('--source-profile-id', type=int, required=True)
        parser.add_argument('--dist', type=Path, required=True)
        args = parser.parse_args()
        verify(asyncio.run(source(args.source_profile_id)), args)
