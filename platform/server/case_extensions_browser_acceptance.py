"""Verify isolated extension editing, serialization, and an actual cold API reload."""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit


NAMESPACE = 'acme.mesh'
LEGACY_NAME = 'legacy.heat'
LEGACY_ENTRY = '''        legacy.heat
        {
            version "v1"; // preserve original characters
            enabled True;
            settings
            {
                mode "(legacy expression)";
                inputs ( raw "name with space" );
            }
        }'''
SETTINGS = {'quoted': '"mesh target"', 'paths': ['Alpha'], 'empty': [], 'order': 2,
            '"order"': 'distinct', 'nested': {'paths': ['a b', 'c']}, 'literal': '(scalar)'}
PARSED_SETTINGS = {**SETTINGS, 'order': '2'}


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    root = Path(settings['root'])
    api = settings['base'] + f"/api/v1/cases/{settings['case_id']}"
    headers = {'Authorization': 'Bearer ' + settings['token']}
    route = api + '/caseDict/extensions/' + NAMESPACE
    checks = {}
    expect.set_options(timeout=20000)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel=settings['channel'])
        context = await browser.new_context(viewport={'width': 1380, 'height': 930})
        await context.add_init_script('if(location.origin===' + json.dumps(settings['base']) +
            ')localStorage.setItem("vf_token",' + json.dumps(settings['token']) + ');')
        page = await context.new_page()
        page.set_default_timeout(18000)
        region = page.get_by_role('region', name='算例扩展', exact=True)
        chooser = region.get_by_label('选择扩展', exact=True)
        version = region.get_by_label('版本', exact=True)
        body = region.get_by_label('设置（JSON 对象）', exact=True)
        active = region.get_by_label('启用扩展', exact=True)
        submitted = []

        def observe(request):
            if urlsplit(request.url).path == urlsplit(route).path and request.method in ('PUT', 'DELETE'):
                submitted.append(request.method)

        page.on('request', observe)

        async def open_case(reload=False):
            await (page.reload() if reload else page.goto(settings['base']))
            if not await region.is_visible():
                await page.locator('[data-case-name=' + json.dumps(settings['case']) + ']').click()
                tab = page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^caseDict$'))
                if not await region.is_visible() and await tab.is_visible():
                    await tab.click()
            await expect(region).to_be_visible()

        async def write(button, method, status=200):
            async with page.expect_response(lambda response: urlsplit(response.url).path == urlsplit(route).path
                and response.request.method == method) as caught:
                await button.click()
            response = await caught.value
            assert response.status == status, (response.status, await response.text())
            return await response.json()

        await open_case()
        await expect(chooser).to_have_value(LEGACY_NAME)
        await chooser.select_option('')
        await region.get_by_label('命名空间', exact=True).fill(NAMESPACE)
        await version.fill('"2.0"')
        await body.fill(json.dumps(SETTINGS))
        created = await write(region.get_by_role('button', name='注册扩展', exact=True), 'PUT')
        assert created['entry'] == {'version': '"2.0"', 'enabled': True, 'settings': SETTINGS}
        await expect(chooser).to_have_value(NAMESPACE)
        await open_case(reload=True)
        await expect(chooser).to_have_value(NAMESPACE)
        await expect(version).to_have_value('"2.0"')
        assert json.loads(await body.input_value()) == PARSED_SETTINGS
        checks['registration_and_reload'] = True

        await version.fill('2.1')
        await active.uncheck()
        await body.fill('{"unfinished":')
        writes_before = len(submitted)
        await region.get_by_role('button', name='保存扩展', exact=True).click()
        await expect(region.get_by_role('alert')).to_contain_text('设置不是有效的 JSON')
        assert len(submitted) == writes_before
        await body.fill(json.dumps({**PARSED_SETTINGS, 'status': 'reviewed'}))
        saved = await write(region.get_by_role('button', name='保存扩展', exact=True), 'PUT')
        assert saved['entry']['enabled'] is False
        await expect(region.get_by_role('status')).to_contain_text('扩展已保存')
        checks['invalid_json_stayed_local'] = True
        checks['edit_and_disable'] = True

        await version.fill('2.2 local')
        external = await context.request.put(route, headers=headers, data={'version': '3.0 external',
            'enabled': False, 'settings': saved['entry']['settings'], 'expected_revision': saved['dict_revision'],
            'expected_sha256': saved['content_sha256']})
        assert external.status == 200, await external.text()
        await write(region.get_by_role('button', name='保存扩展', exact=True), 'PUT', 409)
        await expect(version).to_have_value('2.2 local')
        await region.get_by_role('button', name='查看服务器版本', exact=True).click()
        await expect(region.get_by_test_id('form-server-version')).to_contain_text('3.0 external')
        await region.get_by_role('button', name='保留草稿，基于此版本继续编辑', exact=True).click()
        await write(region.get_by_role('button', name='保存扩展', exact=True), 'PUT')
        checks['conflict_preserved_draft'] = True
        await body.scroll_into_view_if_needed()
        await page.screenshot(path=str(root / 'extensions-desktop.png'))

        delete = region.get_by_role('button', name='删除扩展', exact=True)
        writes_before = len(submitted)
        page.once('dialog', lambda dialog: dialog.dismiss())
        await delete.click()
        assert len(submitted) == writes_before
        page.once('dialog', lambda dialog: dialog.accept())
        await write(delete, 'DELETE')
        await expect(chooser).to_have_value('')
        await expect(chooser.locator(f'option[value="{NAMESPACE}"]')).to_have_count(0)
        checks['explicit_delete'] = True
        await page.set_viewport_size({'width': 790, 'height': 910})
        await chooser.select_option(LEGACY_NAME)
        await expect(version).to_have_value('v1')
        await version.scroll_into_view_if_needed()
        await page.screenshot(path=str(root / 'extensions-compact.png'))
        (root / 'browser-result.json').write_text(json.dumps(checks, indent=2), encoding='utf8')
        await browser.close()


def verify(args):
    from testsupport import IsolatedServer

    api = IsolatedServer('case-extensions-browser', app='cb16_fixture:app')
    bundle = args.dist.resolve(strict=True)
    (api.root / 'cb16_fixture.py').write_text('from app.main import app\n'
        'from fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='
        + repr(str(bundle)) + ', html=True))\n', encoding='utf8')
    temp = api.root / 'browser-tmp'
    temp.mkdir()
    report = {'passed': False, 'backend': 'fs/sqlite', 'model_calls': 0}
    try:
        with api:
            case = api.create_case('Extension browser acceptance')
            prefix, token = api.prefix, api.token
            email = api.call('GET', '/auth/me')['email']
            dictionary_path = prefix + '/caseDict'
            before = api.call('GET', dictionary_path)
            assert before['parsed']['extensions'] == {}
            legacy = 'extensions\n{\n    schemaVersion 1;\n    entries\n    {\n' + LEGACY_ENTRY + '\n    }\n}'
            source = before['content'].replace('extensions\n{\n}', legacy, 1)
            assert source != before['content']
            api.call('PUT', dictionary_path, {'content': source, 'expected_revision': before['dict_revision']})
            settings = {'root': str(api.root), 'base': api.base, 'token': token, 'case': case['name'],
                        'case_id': case['id'], 'channel': args.channel}
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(settings), text=True, check=True, timeout=160,
                env=dict(os.environ, TEMP=str(temp), TMP=str(temp)))
            report['browser'] = json.loads((api.root / 'browser-result.json').read_text(encoding='utf8'))
            after = api.call('GET', dictionary_path)
            assert LEGACY_ENTRY in after['content']
            assert set(after['parsed']['extensions']['entries']) == {LEGACY_NAME}
            current = api.call('GET', prefix + '/provenance')
            assert current['chain_valid'] and not current['issues']
            events = current['events']
            assert NAMESPACE not in events[0]['extensions']['entries'] and events[0]['source'] == 'extensions'
            assert events[0]['revision'] == after['dict_revision'] and events[0]['casedict_sha256'] == after['content_sha256']
            assert any(event.get('extensions', {}).get('entries', {}).get(NAMESPACE, {}).get('settings') ==
                       PARSED_SETTINGS for event in events)
            assert any(event.get('extensions', {}).get('entries', {}).get(NAMESPACE, {}).get('version') ==
                       '2.2 local' for event in events)
            raw = api.call('GET', prefix + '/raw?path=0-caseDict/provenance.jsonl', raw=True)
            assert json.loads(raw.splitlines()[-1]) == events[0]
            report['provenance'] = {'raw_sha256': hashlib.sha256(raw).hexdigest(),
                'extension_sha256': events[0]['extensions_sha256'], 'revision': events[0]['revision']}
            for setting in ([{'field': 'p'}], [[1, 2]], None):
                api.call('PUT', dictionary_path + '/extensions/' + NAMESPACE, {'version': '3', 'enabled': True,
                    'settings': {'unsupported': setting}}, expect=422)
            api.call('DELETE', dictionary_path + '/extensions/' + NAMESPACE, expect=404)
            api.create_case('Extensions unrelated account')
            api.call('PUT', dictionary_path + '/extensions/' + NAMESPACE,
                     {'version': '3', 'enabled': True, 'settings': {}}, expect=404)
            api.call('DELETE', dictionary_path + '/extensions/' + LEGACY_NAME, expect=404)
            api.token = token
            assert api.call('GET', dictionary_path) == after and api.call('GET', prefix + '/provenance') == current
            report['atomic_rejections'] = {'unsupported_settings_422': 3, 'missing_404': 1, 'cross_account_404': 2}
            block = after['parsed']['extensions']
            block['entries']['block.editor'] = {'version': '"4"', 'enabled': False, 'settings': SETTINGS}
            api.call('PUT', dictionary_path + '/block', {'block': 'extensions', 'entries': block,
                'expected_revision': after['dict_revision'], 'expected_sha256': after['content_sha256']})
            saved = api.call('GET', dictionary_path)
            assert LEGACY_ENTRY in saved['content']
            assert saved['parsed']['extensions']['entries']['block.editor']['settings'] == PARSED_SETTINGS
            final_chain = api.call('GET', prefix + '/provenance')
            assert final_chain['chain_valid']
            report['valid_legacy_source_byte_preservation_and_block_edit'] = True
        (api.root / 'api-before-restart.log').write_bytes((api.root / 'api.log').read_bytes())
        initial_pid = api.proc.pid
        with api:
            assert api.proc.pid != initial_pid
            api.token = api.call('POST', '/auth/login', {'email': email, 'password': 'isolated-test-123'})['token']
            assert api.call('GET', dictionary_path) == saved
            assert api.call('GET', prefix + '/provenance') == final_chain
            report['cold_api_reloaded_exact_dictionary_and_history'] = True
        report['passed'] = True
    finally:
        (api.root / 'case-extensions-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print('CASE_EXTENSIONS_BROWSER_OK', api.root, flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        verify(parser.parse_args())
