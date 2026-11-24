"""Exercise FloTHERM in Edge against an isolated real API, runner and file store."""

import asyncio
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

from flotherm_ecxml_acceptance import NEUTRAL_ECXML, assert_case
from flotherm_paired_acceptance import CONFIG, XTXML, pdml_fixture, upload_binary


CASE = 'FloTHERM browser conversion'
EMPTY_CASE = 'FloTHERM independent case'
RAW_CASE = 'FloTHERM original ECXML'
PAIRED_CASE = 'FloTHERM paired PDML'
RAW = 'Ref/chip.ecxml'
PAIRED = 'Ref/chip.pdml'
BAD_PAIR = 'Ref/other.pdml'
IR = 'Ref/flotherm-ir.json'
SETTINGS = 'Ref/conversion-settings.json'
NEUTRAL = 'Ref/thermal.ecxml'
IMPORTED = 'Ref/flotherm-imported.json'
PDML = 'Ref/thermal.pdml'
XTXMLA = 'Ref/thermal.xtxmla'
DOMAIN_CONFIG = 'Ref/domain.json'
SOURCE = 'Ref/helper'
OUTPUT = 'C-run/flotherm'
CANCELLED = 'C-run/cancelled'
LIBRARY = 'UDF/flotherm-helper'
XML = '<ecxml><box bbox="0 0 0 1 1 1" /></ecxml>\n'


async def browser(data):
    from playwright.async_api import async_playwright, expect

    base = data['base']
    root = Path(data['root'])
    result = {'passed': False, 'model_requests': 0, 'page_errors': []}
    model_paths = []
    route_prefix = f"/api/v1/cases/{data['case_id']}/flotherm:"

    async with async_playwright() as playwright:
        chrome = await playwright.chromium.launch(channel='msedge')
        result['browser_version'] = chrome.version
        ctx = await chrome.new_context(locale='zh-CN', viewport={'width': 1536, 'height': 1060},
            storage_state={'cookies': [], 'origins': [{'origin': base, 'localStorage': [
                {'name': 'vf_token', 'value': data['token']}]}]})
        page = await ctx.new_page()

        def observe(request):
            parts = urlsplit(request.url)
            if parts.hostname not in ('127.0.0.1', 'localhost') or (request.method == 'POST' and re.search(
                    r'/conversations/[^/]+/(messages|goal:run)$|/codex/.*/turns$|/llm/', parts.path)):
                model_paths.append(parts.path)

        async def choose_case(name):
            await page.locator('[data-case-name='+json.dumps(name)+']').click(timeout=20000)

        async def show_studio():
            await page.locator('.dv-tab').filter(has_text=re.compile(r'^FloTHERM$')).click(timeout=20000)

        async def post(button, path, statuses=(200,), expected_key=None):
            async with page.expect_response(lambda response: response.request.method == 'POST' and
                    urlsplit(response.url).path == path, timeout=35000) as pending:
                await studio.get_by_role('button', name=button, exact=True).click()
            response = await pending.value
            assert response.status in statuses, (response.status, await response.text())
            if expected_key is not None:
                assert response.request.headers.get('idempotency-key') == expected_key
            return await response.json()

        async def detail(identifier):
            response = await ctx.request.get(base+f'/api/v1/jobs/{identifier}',
                headers={'Authorization': 'Bearer '+data['token']})
            assert response.ok, response.status
            return await response.json()

        page.on('request', observe)
        page.on('pageerror', lambda error: result['page_errors'].append(str(error)[:400]))
        studio = page.locator('.flotherm-studio:visible')
        job_section = studio.get_by_role('region', name='作业与输出')
        try:
            await page.goto(base, wait_until='domcontentloaded')
            await choose_case(CASE)
            await show_studio()
            await studio.get_by_label('FloTHERM 输入文件', exact=True).select_option(RAW)
            await studio.get_by_label('配套文件（可选）', exact=True).select_option(BAD_PAIR)
            bad = await post('检查输入', route_prefix+'inspect')
            assert not bad['ok'] and bad['blockers']
            await expect(studio.get_by_text(re.compile('预检未通过'))).to_be_visible()
            await studio.get_by_label('配套文件（可选）', exact=True).select_option(PAIRED)
            await expect(studio.get_by_text(re.compile('预检未通过'))).to_have_count(0)
            good = await post('检查输入', route_prefix+'inspect')
            assert good['ok'] and good['kind'] == 'ECXML+PDML'
            assert good['files'][0]['sha256'] == hashlib.sha256(XML.encode()).hexdigest()
            await expect(studio.get_by_text('预检通过 · ECXML+PDML')).to_be_visible()
            await expect(studio.get_by_text(re.compile('1 个 bbox'))).to_be_visible()
            result['input_check'] = {'accepted': good['kind'], 'blocked': bad['blockers'], 'files': good['files']}

            await studio.get_by_label('FloTHERM IR JSON', exact=True).select_option(IR)
            await studio.get_by_label('转换设置 JSON（可选）', exact=True).select_option(SETTINGS)
            await page.screenshot(path=root/'preflight-desktop.png')
            await studio.get_by_label('输出目录', exact=True).fill('../invalid')
            await expect(studio.get_by_role('button', name='提交转换')).to_be_disabled()
            await studio.get_by_label('输出目录', exact=True).fill(OUTPUT)
            intercepted = []
            convert_route = '**/flotherm:convert'

            async def drop_response(route):
                reply = await route.fetch(timeout=30000)
                assert reply.ok
                intercepted.append(((await reply.json())['job_id'], route.request.headers['idempotency-key']))
                await route.abort('connectionreset')

            await page.route(convert_route, drop_response, times=1)
            # The worker accepted the real request, but the first browser response is lost.
            await studio.get_by_role('button', name='提交转换', exact=True).click()
            await expect(studio.get_by_role('button', name='用原请求确认提交')).to_be_enabled(timeout=25000)
            await page.reload(wait_until='domcontentloaded')
            await choose_case(CASE)
            await show_studio()
            transient_codes = []
            for _ in range(12):
                retry = await post('用原请求确认提交', route_prefix+'convert', statuses=(200, 409),
                                   expected_key=intercepted[0][1])
                if 'job_id' in retry:
                    break
                code = retry.get('detail', {}).get('code')
                assert code in ('storage_quota_busy', 'case_write_busy'), retry
                transient_codes.append(code)
                await asyncio.sleep(.65)
            convert_id = retry['job_id']
            assert convert_id == intercepted[0][0]
            await expect(job_section.get_by_text('已完成', exact=True)).to_be_visible(timeout=45000)
            file = job_section.get_by_role('button', name='conversion-coverage.json', exact=True)
            await expect(file).to_be_visible(timeout=20000)
            conversion = await detail(convert_id)
            assert conversion['status'] == 'succeeded' and not conversion['completion_pending']
            await file.scroll_into_view_if_needed()
            await page.screenshot(path=root/'desktop.png')
            await file.click()
            await expect(page.locator(f'[data-editor-path="{OUTPUT}/conversion-coverage.json"][data-active="true"]')).to_be_visible()
            await show_studio()
            await job_section.get_by_role('button', name='打开作业日志').click()
            await expect(page.get_by_role('button', name=re.compile(fr'#{convert_id} FloTHERM IR conversion'))
                         ).to_have_attribute('aria-pressed', 'true', timeout=20000)
            result['convert_job'] = {'id': convert_id, 'state': conversion['status'],
                'same_job_after_replay': True, 'transient_replay_codes': transient_codes}

            await show_studio()
            await studio.get_by_text('辅助库', exact=True).click()
            await studio.get_by_label('辅助库源码目录', exact=True).select_option(SOURCE)
            library = await post('编译辅助库', route_prefix+'build-library')
            library_id = library['job_id']
            await expect(job_section.get_by_text(f'辅助库编译 #{library_id}')).to_be_visible()
            await expect(job_section.get_by_text('已完成', exact=True)).to_be_visible(timeout=65000)
            await expect(job_section.get_by_role('button', name='flotherm-load.log', exact=True)).to_be_visible()
            await expect(job_section.get_by_role('button', name='libflothermHelper.so', exact=True)).to_be_visible()
            result['library_job'] = {'id': library_id, 'state': (await detail(library_id))['status']}

            (root/'hold').touch()
            await studio.get_by_label('FloTHERM IR JSON', exact=True).select_option(IR)
            await studio.get_by_label('输出目录', exact=True).fill(CANCELLED)
            cancelled = await post('提交转换', route_prefix+'convert')
            cancel_id = cancelled['job_id']
            for _ in range(200):
                if (root/'ready').exists():
                    break
                await asyncio.sleep(.1)
            assert (root/'ready').exists(), 'Conversion did not reach real publication'
            await expect(job_section.get_by_text(f'IR 转换 #{cancel_id}')).to_be_visible()
            reply = await post('取消作业', f'/api/v1/jobs/{cancel_id}:cancel')
            assert reply['ok'], reply
            (root/'release').touch()
            await expect(job_section.get_by_text('已取消', exact=True)).to_be_visible(timeout=25000)
            await expect(job_section.get_by_text('项目输出文件', exact=True)).to_have_count(0)
            result['cancel_job'] = {'id': cancel_id, 'state': (await detail(cancel_id))['status']}

            await choose_case(EMPTY_CASE)
            await show_studio()
            await expect(studio.get_by_text(EMPTY_CASE, exact=True)).to_be_visible()
            await expect(studio.get_by_role('button', name='提交转换')).to_be_disabled()
            await expect(job_section).to_have_count(0)

            await choose_case(RAW_CASE)
            await show_studio()
            raw_case_id = data['raw_case_id']
            raw_endpoint = f'/api/v1/cases/{raw_case_id}/flotherm:'
            await studio.get_by_label('FloTHERM 输入文件', exact=True).select_option(NEUTRAL)
            inspected = await post('检查输入', raw_endpoint+'inspect')
            assert inspected['ok'] and inspected['kind'] == 'ECXML'
            imported = await post('导入并生成 IR', raw_endpoint+'import')
            assert imported['source']['path'] == NEUTRAL and imported['coverage']['power_w'] == 1200
            await expect(studio.get_by_text(f'已生成 {IMPORTED}', exact=True)).to_be_visible(timeout=18000)
            await expect(studio.get_by_label('FloTHERM IR JSON', exact=True)).to_have_value(IMPORTED, timeout=18000)
            await expect(studio.get_by_role('button', name='导入并生成 IR')).to_be_disabled()
            raw_job = await post('提交转换', raw_endpoint+'convert')
            await expect(job_section.get_by_text('已完成', exact=True)).to_be_visible(timeout=45000)
            await expect(job_section.get_by_role('button', name='preflight.json', exact=True)).to_be_visible(timeout=18000)
            result['raw_ecxml'] = {'job': raw_job['job_id'], 'sha256': imported['source']['sha256'],
                                   'power_w': imported['coverage']['power_w'], 'ir_path': IMPORTED}
            await job_section.get_by_role('heading', name='项目输出文件').scroll_into_view_if_needed()
            await page.screenshot(path=root/'import-desktop.png')

            await choose_case(PAIRED_CASE)
            await show_studio()
            pair_endpoint = f"/api/v1/cases/{data['pair_case_id']}/flotherm:"
            await studio.get_by_label('FloTHERM 输入文件', exact=True).select_option(XTXMLA)
            await studio.get_by_label('配套文件（可选）', exact=True).select_option(PDML)
            paired = await post('检查输入', pair_endpoint+'inspect')
            assert paired['ok'] and paired['kind'] == 'XTXMLA+PDML'
            await expect(studio.get_by_role('button', name='导入并生成 IR')).to_be_disabled()
            await studio.get_by_label('导入配置 JSON', exact=True).select_option(DOMAIN_CONFIG)
            graph = await post('导入并生成 IR', pair_endpoint+'import')
            assert len(graph['source']['inputs']) == 2
            await expect(studio.get_by_label('FloTHERM IR JSON', exact=True)).to_have_value(IMPORTED, timeout=18000)
            paired_job = await post('提交转换', pair_endpoint+'convert')
            await expect(job_section.get_by_text('已完成', exact=True)).to_be_visible(timeout=45000)
            result['paired'] = {'input_kind': paired['kind'], 'job': paired_job['job_id'],
                                'power_w': graph['coverage']['power_w']}
            await job_section.get_by_role('heading', name='项目输出文件').scroll_into_view_if_needed()
            await page.screenshot(path=root/'paired-desktop.png')

            await choose_case(CASE)
            await page.set_viewport_size({'width':390, 'height':844})
            await page.get_by_label('工作面板').select_option('flotherm')
            await expect(job_section.get_by_text('已取消', exact=True)).to_be_visible()
            await studio.get_by_label('FloTHERM IR JSON', exact=True).select_option(IR)
            await studio.get_by_label('FloTHERM 输入文件', exact=True).select_option(RAW)
            result['mobile_widths'] = await studio.evaluate('(panel) => [panel.clientWidth, panel.scrollWidth]')
            assert result['mobile_widths'][1] <= result['mobile_widths'][0] + 1, result['mobile_widths']
            await page.screenshot(path=root/'mobile.png')
            assert not model_paths and not result['page_errors'], (model_paths, result['page_errors'])
            result['passed'] = True
        except BaseException as error:
            result['failure'] = {'type': type(error).__name__, 'message': str(error)[:1600]}
            await page.screenshot(path=root/'failure.png')
            raise
        finally:
            result['model_requests'] = len(model_paths)
            (root/'browser.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
            await ctx.close()
            await chrome.close()


def run(browser_python):
    from app.services.flotherm_ir import build_ir
    from testsupport import IsolatedServer, SERVER

    bundle = (SERVER.parent/'web/dist').resolve(strict=True)
    api = IsolatedServer('flotherm-studio-browser', app='studio_browser_fixture:app')
    (api.root/'studio_browser_fixture.py').write_text(f'''import time
from pathlib import Path
from app.main import app
from app.services import artifact_publication
from fastapi.staticfiles import StaticFiles

root = Path(__file__).parent
prepare = artifact_publication.prepare_bundle

def held_bundle(*args, **kwargs):
    if (root/'hold').exists():
        (root/'ready').touch()
        deadline = time.monotonic()+60
        while not (root/'release').exists():
            if time.monotonic()>deadline:
                raise TimeoutError('FloTHERM cancellation fixture')
            time.sleep(.03)
    return prepare(*args, **kwargs)

artifact_publication.prepare_bundle = held_bundle
app.mount('/', StaticFiles(directory={str(bundle)!r}, html=True))
''', encoding='utf8')
    with api:
        case = api.create_case(CASE)
        ir = build_ir('chip.ecxml', XML.encode(), [{'id': 'chip', 'type': 'box', 'material': 'silicon',
            'mapping': 'converted', 'power_w': 5, 'shape': {'kind': 'box', 'origin': [0, 0, 0],
            'dimensions': [1, 1, 1]}}], {'silicon': {'conductivity_w_mk': 130, 'rho_cp': 1000}})
        raw_ir = json.dumps(ir)
        for path, content in {
            RAW: XML, PAIRED: '#FFFB V2310\n', BAD_PAIR: '#FFFB V2310\n', IR: raw_ir,
            SETTINGS: json.dumps({'power': {'chip': {'value': 7500, 'unit': 'mW'}}}),
            SOURCE+'/Make/files': 'flothermHelper.C\n\nLIB = $(PWD)/libflothermHelper\n',
            SOURCE+'/Make/options': 'EXE_INC =\nLIB_LIBS =\n',
            SOURCE+'/flothermHelper.C': 'extern "C" double vfFlothermPowerScale() { return 1.0; }\n',
        }.items():
            api.put(path, content)
        workspace = api.call('GET', '/workspaces')[0]
        api.call('POST', f"/workspaces/{workspace['id']}/cases", {'name': EMPTY_CASE})
        raw_case = api.call('POST', f"/workspaces/{workspace['id']}/cases", {'name': RAW_CASE})
        api.call('PUT', f"/cases/{raw_case['id']}/file", {'path': NEUTRAL, 'content': NEUTRAL_ECXML, 'expected_sha256': None})
        pair_case = api.call('POST', f"/workspaces/{workspace['id']}/cases", {'name': PAIRED_CASE})
        pair_prefix = f"/cases/{pair_case['id']}"
        api.call('PUT', pair_prefix+'/file', {'path': XTXMLA, 'content': XTXML, 'expected_sha256': None})
        api.call('PUT', pair_prefix+'/file', {'path': DOMAIN_CONFIG, 'content': json.dumps(CONFIG), 'expected_sha256': None})
        upload_binary(api, pair_case['id'], PDML, pdml_fixture())
        subprocess.run([str(browser_python), '-B', '-X', 'utf8', str(Path(__file__).resolve()), '--browser'],
            input=json.dumps({'base': api.base, 'case_id': case['id'], 'raw_case_id': raw_case['id'],
                              'pair_case_id': pair_case['id'],
                              'root': str(api.root), 'token': api.token}),
            text=True, timeout=220, check=True)
        case_root = api.root/'files'/'workspaces'/str(workspace['id'])/'cases'/str(case['id'])
        coverage = json.loads((case_root/OUTPUT/'conversion-coverage.json').read_text(encoding='utf8'))
        assert coverage['summary']['input_power_w'] == 7.5
        assert (case_root/OUTPUT/'system/blockMeshDict').is_file()
        assert (case_root/LIBRARY/'libflothermHelper.so').read_bytes().startswith(b'\x7fELF')
        assert (case_root/IR).read_text(encoding='utf8') == raw_ir
        assert not (case_root/CANCELLED).exists()
        jobs = api.call('GET', f"/cases/{case['id']}/jobs")
        assert len(jobs) == 3
        assert sorted(job['status'] for job in jobs) == ['cancelled', 'succeeded', 'succeeded']
        raw_root = case_root.parent/str(raw_case['id'])
        assert_case(raw_root/OUTPUT)
        assert (raw_root/NEUTRAL).read_bytes() == NEUTRAL_ECXML.encode()
        raw_original = (raw_root/IMPORTED).read_bytes()
        assert json.loads(raw_original)['source']['sha256'] == hashlib.sha256(NEUTRAL_ECXML.encode()).hexdigest()
        raw_jobs = api.call('GET', f"/cases/{raw_case['id']}/jobs")
        assert len(raw_jobs) == 1 and raw_jobs[0]['status'] == 'succeeded'
        pair_root = case_root.parent/str(pair_case['id'])
        pair_balance = json.loads((pair_root/OUTPUT/'power-balance.json').read_bytes())
        pair_ir = json.loads((pair_root/IMPORTED).read_bytes())
        assert pair_ir['assembly']['xtxml']['geometry_source'] == 'PDML'
        assert pair_balance['input_power_w'] == 1200 and (pair_root/PDML).read_bytes() == pdml_fixture()
        pair_jobs = api.call('GET', pair_prefix+'/jobs')
        assert len(pair_jobs) == 1 and pair_jobs[0]['status'] == 'succeeded'
    assert api.proc.poll() is not None
    index = (bundle/'index.html').read_bytes()
    script = bundle / re.search(rb'\bsrc="/([^"]+\.js)"', index).group(1).decode()
    (api.root/'attestation.json').write_text(json.dumps({
        'jobs': [{'id': job['id'], 'status': job['status']} for job in jobs], 'input_power_w': 7.5,
        'cancelled_output_published': False, 'isolated_api_exited': True,
        'main_js_sha256': hashlib.sha256(script.read_bytes()).hexdigest(),
        'raw_ecxml': {'job': raw_jobs[0]['id'], 'input_power_w': 1200, 'stored_ir': IMPORTED},
        'paired': {'job': pair_jobs[0]['id'], 'input_power_w': 1200, 'inputs': pair_ir['source']['inputs']},
    }, indent=2), encoding='utf8')
    print('FLOTHERM_STUDIO_BROWSER_PASS', api.root)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        run(Path(sys.argv[1]).resolve(strict=True))
