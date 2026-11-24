"""Read revision evidence after actual CasePanel saves, lifecycle actions and file faults."""
import argparse
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
import zipfile


DECISION = '核对实际温度数据后再修改网格'
ASSUMPTIONS = {'thermalMaterial': '材料在本工况内视为均匀'}
DECISIONS = {'meshReview': '人工复核原始网格检查记录'}
ERROR_ID = 'mesh-review'


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    root = Path(settings['root'])
    result = {}
    path = f"/api/v1/cases/{settings['id']}"
    headers = {'Authorization': 'Bearer ' + settings['token']}
    expect.set_options(timeout=25000)
    async with async_playwright() as driver:
        chromium = await driver.chromium.launch(channel=settings['channel'])
        context = await chromium.new_context(viewport={'width': 1500, 'height': 950})
        await context.add_init_script('if(location.origin===' + json.dumps(settings['base'])
            + ')localStorage.setItem("vf_token",' + json.dumps(settings['token']) + ');')
        page = await context.new_page()
        page.set_default_timeout(25000)
        editor = page.get_by_test_id('block-editor')
        errors = page.get_by_test_id('error-entry-' + ERROR_ID)

        async def open_case():
            await page.goto(settings['base'])
            await page.locator('[data-case-name=' + json.dumps(settings['case']) + ']').click()
            if not await editor.is_visible():
                tab = page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^caseDict$'))
                if await tab.is_visible():
                    await tab.click()
            await expect(editor).to_be_visible()

        async def chain():
            response = await context.request.get(settings['base'] + path + '/provenance', headers=headers)
            assert response.status == 200
            value = await response.json()
            assert value['chain_valid'] and not value['issues']
            return value

        async def update_block(name, entries):
            await editor.get_by_label('编辑的字典块', exact=True).select_option(name)
            keys = editor.get_by_placeholder('键', exact=True)
            names = await keys.evaluate_all('(nodes) => nodes.map(node => node.value)')
            for key, value in entries.items():
                await editor.get_by_placeholder(re.compile(r'^值（')).nth(names.index(key)).fill(json.dumps(value, ensure_ascii=False))
            async with page.expect_response(lambda response: urlsplit(response.url).path == path + '/caseDict/block'
                and response.request.method == 'PUT') as received:
                await editor.get_by_role('button', name='应用该块', exact=True).click()
            response = await received.value
            assert response.status == 200, await response.text()
            await expect(editor.get_by_role('status')).to_contain_text('已保存')
            await expect(editor).to_have_attribute('data-dirty', 'false')
            receipt = await response.json()
            head = (await chain())['events'][0]
            assert (head['revision'], head['casedict_sha256']) == (receipt['dict_revision'], receipt['content_sha256'])
            return head

        async def lifecycle(action, status, expected=200):
            before = await chain()
            async with page.expect_response(lambda response: urlsplit(response.url).path == path + '/caseDict/errors/' + ERROR_ID
                and response.request.method == 'PATCH') as received:
                await errors.get_by_test_id('error-' + action + '-' + ERROR_ID).click()
            response = await received.value
            assert response.status == expected, await response.text()
            if expected == 422:
                await expect(errors.get_by_role('alert')).to_contain_text('fixOptions')
                assert await chain() == before
                return
            await expect(errors).to_have_attribute('data-status', status)
            actual = await response.json()
            head = (await chain())['events'][0]
            assert head['source'] == 'error-lifecycle' and head['revision'] == actual['dict_revision']
            assert head['previous_sha256'] == before['events'][0]['event_sha256']
            assert head['casedict_sha256'] == actual['content_sha256']
            result.setdefault('error_lifecycle', []).append({'status': status, 'summary': actual['summary']})

        try:
            await open_case()
            assert (await update_block('intent', {'decisionLog': [DECISION]}))['intent']['decisionLog'] == [DECISION]
            head = await update_block('provenance', {'assumptions': ASSUMPTIONS, 'decisions': DECISIONS})
            assert head['assumptions'] == ASSUMPTIONS and head['decisions'] == DECISIONS
            result['browser_assumptions_and_decisions'] = head['revision']
            await lifecycle('investigate', 'investigating')
            await lifecycle('propose', 'investigating', 422)
            result['missing_evidence_rejected_atomically'] = True
            await errors.get_by_test_id('error-fix-' + ERROR_ID).fill('保留标记并检查现场记录')
            await lifecycle('propose', 'fixProposed')
            await errors.get_by_test_id('error-resolution-' + ERROR_ID).fill('已将原始检查单加入算例')
            await lifecycle('fixed', 'fixed')
            await errors.get_by_test_id('error-verification-' + ERROR_ID).fill('复核实际归档的检查单')
            await lifecycle('verified', 'verified')
            await open_case()
            await expect(errors).to_have_attribute('data-status', 'verified')
            await expect(errors.get_by_test_id('error-verification-' + ERROR_ID)).to_have_value('复核实际归档的检查单')
            await errors.screenshot(path=str(root / 'provenance-browser-verified.png'))
            await lifecycle('reopen', 'open')
            assert result['error_lifecycle'][-2]['summary']['blocking'] == 0
            assert result['error_lifecycle'][-1]['summary']['blocking'] == 1
            result['passed'] = True
        except Exception:
            await page.screenshot(path=str(root / 'provenance-browser-failure.png'))
            raise
        finally:
            (root / 'provenance-browser.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
            await chromium.close()


def verify(args):
    import httpx
    from app.services.casedict import CASE_DICT_PATH, set_block
    from app.services.provenance import PROVENANCE_PATH
    from testsupport import IsolatedServer

    api = IsolatedServer('case-provenance-browser', app='case_provenance_fixture:app')
    bundle = args.dist.resolve(strict=True)
    assert (bundle / 'index.html').is_file()
    (api.root / 'case_provenance_fixture.py').write_text('from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        + 'app.mount("/", StaticFiles(directory=' + repr(str(bundle)) + ', html=True))\n', encoding='utf8')
    temporary = api.root / 'browser-tmp'
    temporary.mkdir()
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0}

    try:
        with api:
            case = api.create_case('Actual case provenance')
            dictionary = api.prefix + '/caseDict'
            chain_url = api.prefix + '/provenance'
            created = api.call('GET', chain_url)
            assert created['chain_valid'] and created['total'] == 1
            assert created['events'][0]['source'] == 'case-created' and created['events'][0]['revision'] == 0
            initial = api.call('GET', dictionary)
            text = set_block(initial['content'], 'intent', {'decisionLog': ['先保存原始数据']})
            text = set_block(text, 'provenance', {'assumptions': {'initial': '待现场检查'}, 'decisions': {'initial': '保留旧版'}})
            text = set_block(text, 'errors', {'entries': {ERROR_ID: {'title': '补充网格检查单', 'stage': 'mesh',
                'category': 'meshQuality', 'severity': 'error', 'status': 'open', 'blocking': True}}})
            api.call('PUT', dictionary, {'content': text, 'expected_revision': initial['dict_revision']})
            settings = {'root': str(api.root), 'base': api.base, 'token': api.token, 'case': case['name'],
                'id': case['id'], 'channel': args.channel}
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(settings), text=True, timeout=220, check=True,
                env=dict(os.environ, TEMP=str(temporary), TMP=str(temporary)))
            report['browser'] = json.loads((api.root / 'provenance-browser.json').read_text(encoding='utf8'))
            current = api.call('GET', dictionary)
            namespace = dictionary + '/extensions/evidence.review'
            extension = {'version': '1.0', 'enabled': True, 'settings': {'review': 'original record'}}
            api.call('PUT', namespace, extension | {'version': ''}, expect=422)
            assert api.call('GET', dictionary) == current
            install = api.call('PUT', namespace, extension | {'expected_revision': current['dict_revision']})
            assert install['dict_revision'] == current['dict_revision'] + 1
            remove = api.call('DELETE', namespace)
            assert remove['dict_revision'] == install['dict_revision'] + 1
            api.call('DELETE', namespace, expect=404)
            foreign = api.call('POST', '/auth/register', {'name': 'Other organization', 'email': 'provenance-other@example.com',
                'password': 'isolated-test-123'})
            headers = {'Authorization': 'Bearer ' + foreign['token']}
            api.call('GET', chain_url, expect=404, extra_headers=headers)
            api.call('PUT', namespace, extension, expect=404, extra_headers=headers)
            api.call('PATCH', dictionary + '/errors/' + ERROR_ID, {'status': 'open'}, expect=404, extra_headers=headers)
            report['extension_revisions'] = [install['dict_revision'], remove['dict_revision']]

            latest = api.call('GET', dictionary)
            mirror = api.call('GET', api.prefix)
            chain = api.call('GET', chain_url)
            assert chain['chain_valid'] and chain['issues'] == [] and chain['total'] == latest['dict_revision'] + 1
            assert chain['events'][0]['source'] == chain['events'][1]['source'] == 'extensions'
            assert chain['events'][0]['assumptions'] == ASSUMPTIONS and chain['events'][0]['intent']['decisionLog'] == [DECISION]
            page = api.call('GET', chain_url + '?offset=1&limit=1')
            assert page['events'] == [chain['events'][1]] and page['has_more']
            case_root = (api.root / 'files' / mirror['storage_prefix']).resolve()
            assert case_root.is_relative_to(api.root.resolve())
            provenance_file, source_file = case_root / PROVENANCE_PATH, case_root / CASE_DICT_PATH
            provenance, source = provenance_file.read_bytes(), source_file.read_bytes()
            lines = provenance.splitlines(keepends=True)
            assert chain['events'][0]['casedict_sha256'] == hashlib.sha256(source).hexdigest()

            faults = {}
            try:
                for name, content, code in (
                    ('tail', b''.join(lines[:-1]), 'head_revision_mismatch'),
                    ('origin', b''.join(lines[1:]), 'broken_link'),
                    ('empty', b'', 'history_missing'),
                    ('malformed', lines[0] + b'{partial\n' + b''.join(lines[2:]), 'invalid_record'),
                    ('wrong_shape', lines[0] + b'[]\n' + b''.join(lines[2:]), 'invalid_record'),
                    ('hash', provenance.replace(DECISION.encode('utf8'), b'edited stored evidence', 1), 'record_hash_mismatch'),
                ):
                    assert content != provenance
                    provenance_file.write_bytes(content)
                    result = api.call('GET', chain_url + '?offset=1&limit=1')
                    codes = [issue['code'] for issue in result['issues']]
                    assert not result['chain_valid'] and code in codes, (name, result)
                    faults[name] = {'codes': codes, 'readable_records': result['total']}
                provenance_file.write_bytes(lines[0] + b'[]\n')
                api.call('PUT', dictionary, {'content': latest['content'], 'expected_revision': latest['dict_revision']}, expect=422)
                assert api.call('GET', dictionary) == latest and api.call('GET', api.prefix) == mirror
                provenance_file.write_bytes(provenance)
                source_file.write_bytes(source + b'\n// controlled physical change\n')
                result = api.call('GET', chain_url)
                assert result['issues'] == [{'code': 'head_content_mismatch'}]
                faults['dictionary'] = result['issues']
            finally:
                provenance_file.write_bytes(provenance)
                source_file.write_bytes(source)
            assert api.call('GET', chain_url) == chain
            report['physical_faults_and_recovery'] = faults

            workspace = mirror['workspace_id']
            backup = api.call('GET', f'/workspaces/{workspace}/backup', raw=True)
            copied = api.call('POST', api.prefix + ':copy', {'name': 'Copied provenance'})
            copy_history = api.call('GET', f"/cases/{copied['id']}/provenance")
            assert copy_history['chain_valid'] and copy_history['events'][1:] == chain['events']
            assert copy_history['events'][0]['revision'] == chain['events'][0]['revision']
            assert copy_history['events'][0]['source'] == 'case-copy'
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, 'w') as upload:
                upload.writestr(CASE_DICT_PATH, source)
                upload.writestr(PROVENANCE_PATH, provenance)
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token}, trust_env=False) as client:
                response = client.post(f'/workspaces/{workspace}/cases:import', data={'name': 'Imported provenance'},
                    files={'file': ('case.zip', archive.getvalue(), 'application/zip')})
                assert response.is_success, response.text
                imported = response.json()
                response = client.post('/workspaces:restore', files={'file': ('workspace.zip', backup, 'application/zip')})
                assert response.is_success, response.text
                restored = response.json()
            import_history = api.call('GET', f"/cases/{imported['id']}/provenance")
            assert import_history['chain_valid'] and import_history['events'][1:] == chain['events']
            assert import_history['events'][0]['revision'] == 1
            assert import_history['events'][0]['source'] == 'case-import'
            restored_case = api.call('GET', f"/workspaces/{restored['workspace_id']}/cases")[0]
            assert restored_case['id'] != case['id']
            assert api.call('GET', f"/cases/{restored_case['id']}/provenance") == chain
            report['copy_import_backup_compatible'] = True
        report['passed'] = api.proc.poll() is not None
    finally:
        report['api_exited'] = api.proc is not None and api.proc.poll() is not None
        (api.root / 'case-provenance-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print('CASE_PROVENANCE_BROWSER_OK', api.root, flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        verify(parser.parse_args())
