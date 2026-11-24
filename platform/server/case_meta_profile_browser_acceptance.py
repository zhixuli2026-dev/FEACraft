"""Check metadata options, an imported older record, and actual coordinated edits."""

import argparse
import asyncio
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit
import urllib.request
import zipfile


NAME = 'Imported metadata profile'
OLD_TAG = 'legacy sparse record'
OTHER_SOURCE = 'customMetadata { ownerCode "preserve"; } // legacy project metadata'


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    root = Path(settings['root'])
    case = f"/api/v1/cases/{settings['case_id']}"
    base = settings['base']
    route = case + '/caseDict/meta'
    headers = {'Authorization': 'Bearer ' + settings['token']}
    proof = {}
    expect.set_options(timeout=25000)
    async with async_playwright() as driver:
        chromium = await driver.chromium.launch(channel=settings['channel'])
        context = await chromium.new_context(viewport={'width': 1450, 'height': 935})
        await context.add_init_script('if(location.origin===' + json.dumps(base) +
            ')localStorage.setItem("vf_token",' + json.dumps(settings['token']) + ');')
        page = await context.new_page()
        page.set_default_timeout(20000)
        form = page.get_by_test_id('case-meta-form')
        meta_heading = page.locator('summary').filter(has_text=re.compile(r'^算例元数据$'))

        async def open_editor(reload=False):
            await (page.reload() if reload else page.goto(base))
            if not await meta_heading.is_visible():
                await page.locator('[data-case-name=' + json.dumps(NAME) + ']').click()
                tab = page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^caseDict$'))
                if not await meta_heading.is_visible() and await tab.is_visible():
                    await tab.click()
            await expect(meta_heading).to_be_visible()
            if not await form.is_visible():
                await meta_heading.click()
            await expect(form).to_be_visible()

        async def current():
            response = await context.request.get(base + case + '/caseDict', headers=headers)
            assert response.status == 200
            return await response.json()

        async def submit(method, button_name, status=200):
            path = route + (':lint' if method == 'POST' else '')
            async with page.expect_response(lambda response: urlsplit(response.url).path == path
                and response.request.method == method) as caught:
                await form.get_by_role('button', name=button_name, exact=True).click()
            response = await caught.value
            assert response.status == status, await response.text()
            return await response.json()

        await open_editor()
        await expect(form.get_by_label('分析类型', exact=True)).to_have_value('steady')
        await expect(form.get_by_label('时间行为', exact=True)).to_have_value('steady')
        original = await current()
        lint = await submit('POST', '检查配置')
        assert lint['valid'] and lint['issues'][0]['code'] == 'remote_mode_local_profile'
        await expect(form.get_by_role('list', name='配置检查结果')).to_contain_text('execution.runMode')
        await form.get_by_label('分析类型', exact=True).select_option('harmonic')
        lint = await submit('POST', '检查配置')
        assert not lint['valid'] and any(item['code'] == 'analysis_mismatch' for item in lint['issues'])
        await expect(form.get_by_role('list', name='配置检查结果')).to_contain_text('physicalProblem.timeBehavior')
        assert await current() == original
        proof['lint_without_write'] = True

        await form.get_by_label('时间行为', exact=True).select_option('frequencyDomain')
        await form.get_by_label('执行方式', exact=True).select_option('remote')
        await form.get_by_label('学科', exact=True).select_option('acoustic')
        await form.get_by_label('维度', exact=True).select_option('2D-wedge')
        await form.get_by_label('精度层级', exact=True).select_option('validation')
        assert (await submit('POST', '检查配置'))['valid']
        await form.locator('summary').filter(has_text=re.compile(r'^名称、项目和标签$')).click()
        await expect(form.get_by_label('项目修订', exact=True)).to_have_value('legacy-R7')
        tags = form.get_by_label('标签（每行一个）', exact=True)
        await expect(tags).to_have_value(OLD_TAG)
        await tags.fill(OLD_TAG + '\n中文标签 (校验)')
        await form.locator('summary').filter(has_text=re.compile(r'^单位$')).click()
        await form.get_by_label('长度', exact=True).select_option('mm')
        pair = await submit('PATCH', '保存元数据')
        assert pair['dict_revision'] == original['dict_revision'] + 1
        await expect(form.get_by_role('status')).to_have_text('已保存算例元数据')
        saved = await current()
        assert saved['parsed']['caseMeta']['tags'] == [OLD_TAG, '中文标签 (校验)']
        assert saved['parsed']['physicalProblem']['timeBehavior'] == 'frequencyDomain'
        proof['paired_edit_one_revision'] = True

        name = form.get_by_label('显示名称', exact=True)
        await name.fill('本地更新的显示名称')
        changed = await context.request.patch(base + route, headers=headers,
            data={'expected_revision': saved['dict_revision'], 'expected_sha256': saved['content_sha256'],
                'changes': {'notes': '服务器增加的原始备注'}})
        assert changed.status == 200, await changed.text()
        await submit('PATCH', '保存元数据', 409)
        await expect(name).to_have_value('本地更新的显示名称')
        await page.get_by_role('button', name='查看服务器版本', exact=True).click()
        await expect(page.get_by_test_id('form-server-version')).to_contain_text('服务器增加的原始备注')
        await page.get_by_role('button', name='保留草稿，基于此版本继续编辑', exact=True).click()
        await expect(form.get_by_label('备注', exact=True)).to_have_value('服务器增加的原始备注')
        await submit('PATCH', '保存元数据')
        current_meta = (await current())['parsed']['caseMeta']
        assert current_meta['notes'] == '服务器增加的原始备注' and current_meta['displayName'] == '本地更新的显示名称'
        proof['rebase_preserved_unedited_server_field'] = True
        await open_editor(reload=True)
        await expect(form.get_by_label('分析类型', exact=True)).to_have_value('harmonic')
        await expect(form.get_by_label('时间行为', exact=True)).to_have_value('frequencyDomain')
        await form.get_by_label('分析类型', exact=True).scroll_into_view_if_needed()
        await page.screenshot(path=str(root / 'meta-profile-desktop.png'))
        await page.set_viewport_size({'width': 790, 'height': 900})
        await form.get_by_label('时间行为', exact=True).scroll_into_view_if_needed()
        await page.screenshot(path=str(root / 'meta-profile-compact.png'))
        (root / 'meta-browser.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf8')
        await chromium.close()


def import_case(api, workspace, content):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('OldProject/0-caseDict/caseDict', content)
        output.writestr('OldProject/Ref/original.txt', 'Historical project input.')
    delimiter = 'cb01-metadata-fixture'
    name = f'--{delimiter}\r\nContent-Disposition: form-data; name="name"\r\n\r\n{NAME}\r\n'
    data = name.encode() + (f'--{delimiter}\r\nContent-Disposition: form-data; name="file"; filename="historical.zip"\r\n'
        'Content-Type: application/zip\r\n\r\n').encode() + archive.getvalue() + f'\r\n--{delimiter}--\r\n'.encode()
    request = urllib.request.Request(api.base + f'/api/v1/workspaces/{workspace}/cases:import',
        data=data, headers={'Authorization': 'Bearer ' + api.token, 'Content-Type': f'multipart/form-data; boundary={delimiter}'})
    with api.opener.open(request, timeout=30) as response:
        return json.load(response)


def verify(args):
    from app.services.casedict import set_block
    from testsupport import IsolatedServer, SERVER

    api = IsolatedServer('case-meta-profile-browser', app='cb01_fixture:app')
    bundle = args.dist.resolve(strict=True)
    (api.root / 'cb01_fixture.py').write_text('from app.main import app\n'
        'from fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='
        + repr(str(bundle)) + ', html=True))\n', encoding='utf8')
    temp = api.root / 'browser-tmp'
    temp.mkdir()
    result = {'passed': False, 'backend': 'fs/sqlite', 'model_calls': 0}
    try:
        with api:
            api.create_case('Metadata enums')
            matrix_case, token = api.prefix, api.token
            workspace = api.call('GET', matrix_case)['workspace_id']
            dictionary = api.call('GET', matrix_case + '/caseDict')
            schema = api.call('GET', matrix_case + '/caseDict/meta-schema')
            original_path = SERVER.parent.parent / 'extracted/share/vibeflow/StandardCase/0-caseDict/caseDict'
            source = original_path.read_text(encoding='utf8')
            for key, options in {**schema['enums'], **schema['units'], 'timeBehavior': schema['time_behaviors']}.items():
                contract = re.search(r'^\s*//\s*' + re.escape(key) + r'\s*;\s*//\s*([^\n]+)', source, re.M)
                assert contract is not None and {value.strip() for value in contract[1].split('|')} == set(options), key
            result['original_enums'] = schema['enums']
            api.advance('V')
            groups = (*schema['enums'].values(), *schema['units'].values())
            for index in range(max(len(group) for group in groups)):
                enums = {key: values[index % len(values)] for key, values in schema['enums'].items()}
                units = {key: values[index % len(values)] for key, values in schema['units'].items()}
                api.call('PATCH', matrix_case + '/caseDict/meta', {'changes': {**enums, 'units': units}})
                accepted = api.call('GET', matrix_case + '/caseDict')['parsed']['caseMeta']
                assert accepted == {**enums, 'units': units}
            result['actual_enum_values_accepted'] = sum(len(group) for group in schema['enums'].values())
            result['actual_unit_values_accepted'] = sum(len(group) for group in schema['units'].values())
            valid_time = {'steady': 'steady', 'quasiSteady': 'steady', 'transient': 'transient',
                          'periodic': 'harmonic', 'frequencyDomain': 'harmonic'}
            assert set(valid_time) == set(schema['time_behaviors'])
            for behavior, analysis in valid_time.items():
                api.call('PATCH', matrix_case + '/caseDict/meta',
                         {'changes': {'analysisType': analysis}, 'time_behavior': behavior})
                accepted = api.call('GET', matrix_case + '/caseDict')['parsed']
                assert accepted['caseMeta']['analysisType'] == analysis
                assert accepted['physicalProblem']['timeBehavior'] == behavior
            result['actual_time_behavior_values_accepted'] = len(valid_time)
            body = set_block(dictionary['content'], 'caseMeta', {'status': 'draft', 'analysisType': 'steady',
                'discipline': 'solid', 'dimensions': '3D', 'executionProfile': 'local',
                'revision': 'legacy-R7', 'project': 'archive', 'units': {'length': 'm', 'mass': 'kg'}})
            body = body.replace('caseMeta\n{', f'caseMeta\n{{\n    tags "{OLD_TAG}";\n    {OTHER_SOURCE}', 1)
            body = set_block(body, 'physicalProblem', {'timeBehavior': 'steady', 'problemClass': 'structural'})
            body = set_block(body, 'execution', {'runMode': 'remote'})
            imported = import_case(api, workspace, body)
            prefix = f"/cases/{imported['id']}"
            assert imported['name'] == NAME
            path = prefix + '/caseDict'
            restored = api.call('GET', path)
            assert restored['content'] == body and restored['parsed']['caseMeta']['tags'] == [OLD_TAG]
            assert 'fidelity' not in restored['parsed']['caseMeta']
            result['legacy_zip_read_preserved_exact_bytes'] = True
            checks = {'status': 'readyForRun', 'discipline': 'invalid', 'dimensions': '4D', 'executionProfile': 'alien'}
            for key, value in checks.items():
                proposal = {'changes': {key: value}, 'expected_revision': restored['dict_revision']}
                lint = api.call('POST', path + '/meta:lint', proposal)
                assert not lint['valid'] and any(item['path'] == 'caseMeta.' + key for item in lint['issues'])
                api.call('PATCH', path + '/meta', proposal, expect=422)
            assert api.call('GET', path) == restored
            settings = {'root': str(api.root), 'base': api.base, 'token': token,
                'case_id': imported['id'], 'channel': args.channel}
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(settings), text=True, check=True, timeout=170,
                env=dict(os.environ, TEMP=str(temp), TMP=str(temp)))
            result['browser'] = json.loads((api.root / 'meta-browser.json').read_text(encoding='utf8'))
            current = api.call('GET', path)
            chain = api.call('GET', prefix + '/provenance')
            assert chain['chain_valid'] and chain['events'][0]['source'] == 'case-meta'
            assert chain['events'][0]['casedict_sha256'] == current['content_sha256']
            assert OTHER_SOURCE in current['content'] and current['parsed']['caseMeta']['revision'] == 'legacy-R7'
            assert current['parsed']['caseMeta']['units'] == {'length': 'mm', 'mass': 'kg'}
            with sqlite3.connect(api.root / 'test.sqlite') as db:
                revision, mirror = db.execute('SELECT dict_revision, case_meta FROM cases WHERE id=?',
                    (imported['id'],)).fetchone()
            assert revision == current['dict_revision'] and json.loads(mirror) == current['parsed']['caseMeta']
            api.create_case('Unauthorized metadata user')
            api.call('GET', path + '/meta-schema', expect=404)
            api.call('POST', path + '/meta:lint', {}, expect=404)
            api.call('PATCH', path + '/meta', {'changes': {'status': 'draft'}}, expect=404)
            api.token = token
            assert api.call('GET', path) == current and api.call('GET', prefix + '/provenance') == chain
            result['atomic_invalid_422'] = len(checks)
            result['unauthorized_404'] = 3
            result['sql_mirror_provenance_and_legacy_fields'] = True
        result['passed'] = True
    finally:
        (api.root / 'case-meta-profile-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
    print('CASE_META_PROFILE_BROWSER_OK', api.root, flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        verify(parser.parse_args())
