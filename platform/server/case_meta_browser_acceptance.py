"""Exercise caseMeta schema and unit conversions against a real API and browser."""
import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit


INITIAL_META = {'status': 'draft', 'displayName': 'Metadata baseline', 'discipline': 'thermal',
                'analysisType': 'steady', 'dimensions': '3D', 'fidelity': 'smoke',
                'executionProfile': 'local', 'tags': [], 'units': {'length': 'm'}}
BROWSER_UNITS = {'length': 'inch', 'mass': 'kg', 'time': 's', 'temperature': 'degC', 'angle': 'deg',
                 'force': 'N', 'pressure': 'Pa', 'energy': 'J'}
TAGS = ['持续校验', 'owner (reference)']


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    root = Path(settings['root'])
    result = {}
    path = f"/api/v1/cases/{settings['case_id']}/caseDict"
    headers = {'Authorization': 'Bearer ' + settings['token']}
    expect.set_options(timeout=25000)
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(channel=settings['channel'])
        context = await chromium.new_context(viewport={'width': 1500, 'height': 950})
        await context.add_init_script('if(location.origin===' + json.dumps(settings['base'])
            + ')localStorage.setItem("vf_token",' + json.dumps(settings['token']) + ');')
        page = await context.new_page()
        page.set_default_timeout(25000)
        editor = page.get_by_test_id('block-editor')
        keys = editor.get_by_placeholder('键', exact=True)
        values = editor.get_by_placeholder(re.compile(r'^值（'))
        block = editor.get_by_label('编辑的字典块', exact=True)

        async def open_case():
            await page.goto(settings['base'])
            await page.locator('[data-case-name=' + json.dumps(settings['case']) + ']').click()
            if not await editor.is_visible():
                tab = page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^caseDict$'))
                if await tab.is_visible():
                    await tab.click()
            await expect(editor).to_be_visible()

        async def value_cell(key, add=False):
            row_names = await keys.evaluate_all('(nodes) => nodes.map(node => node.value)')
            if key in row_names:
                return values.nth(row_names.index(key))
            assert add, (key, row_names)
            await editor.get_by_role('button', name='+ 键', exact=True).click()
            await keys.last.fill(key)
            return values.last

        async def edit(key, value, add=False):
            field = await value_cell(key, add=add)
            await field.fill(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))

        async def snapshot():
            response = await context.request.get(settings['base'] + path, headers=headers)
            assert response.status == 200
            return await response.json()

        async def save(status=200, field=None):
            old = await snapshot()
            async with page.expect_response(lambda response: urlsplit(response.url).path == path + '/block'
                and response.request.method == 'PUT') as received:
                await editor.get_by_role('button', name='应用该块', exact=True).click()
            response = await received.value
            assert response.status == status, (response.status, await response.text())
            body = response.request.post_data_json
            assert body['expected_revision'] == old['dict_revision'] and body['expected_sha256'] == old['content_sha256']
            if status == 200:
                await expect(editor.get_by_role('status')).to_contain_text('已保存')
                await expect(editor).to_have_attribute('data-dirty', 'false')
                updated = await snapshot()
                assert updated['dict_revision'] == old['dict_revision'] + 1
                return updated
            await expect(editor.get_by_role('alert')).to_contain_text(field)
            await expect(editor).to_have_attribute('data-dirty', 'true')
            assert await snapshot() == old

        try:
            await open_case()
            await expect(block).to_have_value('caseMeta')
            assert json.loads(await (await value_cell('tags')).input_value()) == []
            await edit('analysisType', {'unexpected': 'transient'})
            await save(422, 'caseMeta.analysisType')
            result['invalid_object_draft_preserved'] = True

            valid = {'analysisType': 'transient', 'discipline': 'multiphysics', 'dimensions': '2D-axisymmetric',
                     'executionProfile': 'remote', 'fidelity': 'engineering', 'displayName': '多物理场配置',
                     'tags': ['单标签'], 'units': BROWSER_UNITS}
            for key, value in valid.items():
                await edit(key, value)
            saved = await save()
            assert saved['parsed']['caseMeta'] == INITIAL_META | valid
            result['single_tag'] = saved['parsed']['caseMeta']['tags']

            await block.select_option('physicalProblem')
            await edit('timeBehavior', 'periodic', add=True)
            assert (await save())['parsed']['physicalProblem']['timeBehavior'] == 'periodic'

            await block.select_option('caseMeta')
            assert json.loads(await (await value_cell('tags')).input_value()) == ['单标签']
            await edit('analysisType', 'modal')
            await save(422, 'physicalProblem.timeBehavior')
            result['cross_field_draft_preserved'] = True
            await edit('analysisType', 'harmonic')
            assert (await save())['parsed']['caseMeta']['tags'] == ['单标签']

            await edit('tags', TAGS)
            assert (await save())['parsed']['caseMeta']['tags'] == TAGS
            await edit('displayName', '确认后的多物理场配置')
            final = await save()
            assert final['parsed']['caseMeta']['tags'] == TAGS
            await open_case()
            await expect(await value_cell('analysisType')).to_have_value('harmonic')
            await expect(await value_cell('displayName')).to_have_value('确认后的多物理场配置')
            assert json.loads(await (await value_cell('tags')).input_value()) == TAGS
            await editor.screenshot(path=str(root / 'case-meta-browser.png'))
            result['final_revision'] = final['dict_revision']
            result['tags_after_resave_and_reload'] = TAGS
            result['passed'] = True
        except Exception:
            await page.screenshot(path=str(root / 'case-meta-browser-failure.png'))
            raise
        finally:
            (root / 'case-meta-browser.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
            await chromium.close()


def verify(args):
    from app.services.casedict import find_block_span, set_block
    from testsupport import IsolatedServer

    api = IsolatedServer('case-meta-browser', app='case_meta_fixture:app')
    bundle = args.dist.resolve(strict=True)
    assert (bundle / 'index.html').is_file()
    (api.root / 'case_meta_fixture.py').write_text('from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        + 'app.mount("/", StaticFiles(directory=' + repr(str(bundle)) + ', html=True))\n', encoding='utf8')
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0}
    temporary = api.root / 'browser-tmp'
    temporary.mkdir()

    def convert(dimension, value, source, target, **options):
        return api.call('POST', dictionary + '/units:convert', {'dimension': dimension, 'value': value,
            'source': source, 'target': target}, **options)

    try:
        with api:
            case = api.create_case('Actual caseMeta schema')
            dictionary = api.prefix + '/caseDict'
            initial = api.call('GET', dictionary)
            seeded = set_block(initial['content'], 'caseMeta', INITIAL_META)
            api.call('PUT', dictionary, {'content': seeded, 'expected_revision': initial['dict_revision'],
                'expected_sha256': initial['content_sha256']})
            saved = api.call('GET', dictionary)
            mirror = api.call('GET', api.prefix)
            assert saved['parsed']['caseMeta'] == INITIAL_META
            invalid = {
                'workflowState': seeded.replace('workflowState A;', 'workflowState { unexpected A; }'),
                'caseMeta.analysisType': set_block(seeded, 'caseMeta', INITIAL_META | {'analysisType': {'type': 'steady'}}),
                'physicalProblem.timeBehavior': set_block(seeded, 'physicalProblem', {'timeBehavior': {'type': 'steady'}}),
                'caseMeta.discipline': set_block(seeded, 'caseMeta', INITIAL_META | {'discipline': 'unlisted'}),
                'caseMeta.dimensions': set_block(seeded, 'caseMeta', INITIAL_META | {'dimensions': '4D'}),
                'caseMeta.executionProfile': set_block(seeded, 'caseMeta', INITIAL_META | {'executionProfile': 'unlisted'}),
                'caseMeta.displayName': set_block(seeded, 'caseMeta', INITIAL_META | {'displayName': {'text': 'not a string'}}),
                'caseMeta.tags': set_block(seeded, 'caseMeta', INITIAL_META | {'tags': {'label': 'invalid'}}),
                'caseMeta.units.speed': set_block(seeded, 'caseMeta', INITIAL_META | {'units': {'speed': 'm/s'}}),
                'caseMeta.units.temperature': set_block(seeded, 'caseMeta', INITIAL_META | {'units': {'temperature': 'C'}}),
            }
            for field, content in invalid.items():
                result = api.call('PUT', dictionary, {'content': content, 'expected_revision': saved['dict_revision'],
                    'expected_sha256': saved['content_sha256']}, expect=422)
                assert field in result['detail'], (field, result)
            report['invalid_schema_fields'] = list(invalid)

            results = []
            for dimension, value, source, target, answer in (
                ('length', 2, 'inch', 'mm', 50.8), ('length', .0254, 'm', 'inch', 1),
                ('mass', 7, 'kg', 'kg', 7), ('time', 2.5, 's', 's', 2.5),
                ('temperature', 20, 'degC', 'K', 293.15), ('temperature', 273.15, 'K', 'degC', 0),
                ('angle', 180, 'deg', 'rad', math.pi), ('angle', math.pi/2, 'rad', 'deg', 90),
                ('force', -7, 'N', 'N', -7), ('pressure', 101325, 'Pa', 'Pa', 101325), ('energy', 50, 'J', 'J', 50),
            ):
                result = convert(dimension, value, source, target)
                assert math.isclose(result['value'], answer, rel_tol=1e-12, abs_tol=1e-14), result
                results.append(result | {'input': value})
            assert convert('length', 1e-320, 'mm', 'mm')['value'] == 1e-320
            assert convert('temperature', 1e-15, 'degC', 'degC')['value'] == 1e-15
            report['supported_conversions'] = results
            report['same_unit_retains_small_values'] = True
            for dimension, value, source, target in (
                ('length', 1e308, 'm', 'mm'), ('angle', 1e308, 'rad', 'deg'), ('length', 'NaN', 'm', 'mm'),
                ('temperature', 'Infinity', 'K', 'degC'), ('speed', 1, 'm/s', 'm/s'), ('length', 1, 'Pa', 'mm'),
            ):
                convert(dimension, value, source, target, expect=422)
            report['rejected_conversion_count'] = 6
            other = api.call('POST', '/auth/register', {'email': 'meta-outsider@example.com',
                'password': 'isolated-test-123', 'name': 'Other organization'})
            foreign = {'Authorization': 'Bearer ' + other['token']}
            convert('length', 2, 'inch', 'mm', expect=404, extra_headers=foreign)
            api.call('PUT', dictionary + '/block', {'block': 'caseMeta', 'entries': INITIAL_META}, expect=404, extra_headers=foreign)
            assert api.call('GET', dictionary) == saved and api.call('GET', api.prefix) == mirror
            report['foreign_access_denied_and_rejected_changes_unchanged'] = True
            print('CASE_META_API_REACHED_BROWSER', flush=True)

            settings = {'root': str(api.root), 'base': api.base, 'token': api.token, 'case': case['name'],
                'case_id': case['id'], 'channel': args.channel}
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(settings), text=True, check=True, timeout=220,
                env=dict(os.environ, TEMP=str(temporary), TMP=str(temporary)))
            accepted = api.call('GET', dictionary)
            mirrored = api.call('GET', api.prefix)
            assert accepted['dict_revision'] == mirrored['dict_revision'] == saved['dict_revision'] + 5
            assert accepted['workflow_state'] == mirrored['workflow_state'] == 'A'
            assert accepted['parsed']['caseMeta'] == json.loads(mirrored['case_meta'])
            source_start, source_end = find_block_span(saved['content'], 'intent')
            final_start, final_end = find_block_span(accepted['content'], 'intent')
            assert saved['content'][source_start:source_end] == accepted['content'][final_start:final_end]
            report['browser'] = json.loads((api.root / 'case-meta-browser.json').read_text(encoding='utf8'))
            report['published_case_meta'] = accepted['parsed']['caseMeta']
        report['passed'] = api.proc.poll() is not None
    finally:
        report['api_exited'] = api.proc is not None and api.proc.poll() is not None
        (api.root / 'case-meta-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print('CASE_META_BROWSER_OK', api.root, flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        verify(parser.parse_args())
