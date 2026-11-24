"""Browser macro workflow against actual ParaView tools and isolated FS storage."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit
from xml.etree import ElementTree


CASE = 'ParaView 宏验收'
MACRO = '.macros/球面.py'
FAIL = '.macros/failure.py'
CANCEL = '.macros/cancel.py'
OUTPUT = 'D-post/macro-sphere.vtp'
READY = 'D-post/macro-native-ready'
NON_MACRO = 'D-post/ordinary.py'
SCENE = '''from pathlib import Path
from paraview.simple import Sphere, SaveData

sphere = Sphere(Radius={radius}, ThetaResolution=16, PhiResolution=10)
sphere.UpdatePipeline()
SaveData(str(Path({output!r}).resolve()), proxy=sphere, DataMode='Ascii')
'''
CANCEL_SCRIPT = '''from pathlib import Path
import time
from paraview.simple import Sphere

source = Sphere()
source.UpdatePipeline()
Path('D-post/macro-native-ready').write_text('native ParaView ready')
time.sleep(180)
'''


def model(radius):
    return SCENE.format(radius=radius, output=OUTPUT)


def verify_vtp(data, expected_radius):
    piece = ElementTree.fromstring(data).find('.//Piece')
    assert piece.attrib['NumberOfPoints'] == '130'
    array = piece.find('./Points/DataArray')
    assert array.attrib['format'] == 'ascii'
    coords = list(map(float, array.text.split()))
    assert len(coords) == 390
    error = max(abs(math.hypot(*coords[index:index + 3]) - expected_radius)
                for index in range(0, len(coords), 3))
    assert error < 2e-6
    return {'points': 130, 'radius': expected_radius, 'max_radius_error': error,
            'sha256': hashlib.sha256(data).hexdigest()}


def browser(settings):
    from playwright.sync_api import Error as BrowserError, expect, sync_playwright

    base = settings['base']
    root = Path(settings['root'])
    path = settings['prefix']
    run = path + '/paraview:run'
    case_files = root / 'files' / settings['storage_prefix']
    result = {'jobs': []}
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel=settings['channel'])

        def account(token):
            context = edge.new_context(viewport={'width': 1500, 'height': 950}, locale='zh-CN')
            context.add_init_script('if(location.origin===' + json.dumps(base) + ')localStorage.setItem("vf_token",'
                                    + json.dumps(token) + ');')
            return context

        session = account(settings['owner'])
        page = session.new_page()
        page.set_default_timeout(25000)
        expect.set_options(timeout=25000)
        panel = page.get_by_test_id('paraview-macros')
        status = panel.get_by_test_id('paraview-macro-job')
        selector = panel.get_by_label('宏脚本', exact=True)
        backend = panel.get_by_label('执行后端', exact=True)

        def request(method, endpoint, *, data=None, raw=False, status_code=200):
            response = session.request.fetch(base + '/api/v1' + endpoint, method=method, data=data,
                headers={'Authorization': 'Bearer ' + settings['owner']}, timeout=25000)
            assert response.status == status_code, (method, endpoint, response.status, response.text()[:200])
            return response.body() if raw else response.json()

        def click_response(button, method, endpoint, *, code=200):
            deadline = time.monotonic() + 25
            while True:
                with page.expect_response(lambda response: response.request.method == method
                        and urlsplit(response.url).path == '/api/v1' + endpoint) as observed:
                    button.click()
                response = observed.value
                detail = response.json().get('detail') if response.status == 409 else None
                if (method != 'PUT' and endpoint != run or not isinstance(detail, dict)
                        or detail.get('code') != 'storage_quota_busy'):
                    break
                assert time.monotonic() < deadline, detail
                result.setdefault('storage_admission_retries', []).append(endpoint)
                page.wait_for_timeout(250)
            assert response.status == code, (response.status, response.text()[:250])
            return response

        def open_macro_panel():
            page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^可视化$')).click()
            if panel.get_attribute('open') is None:
                panel.locator('summary').click()
            expect(selector).to_be_visible()

        def select_case(browser_page):
            browser_page.locator('[data-case-name=' + json.dumps(CASE, ensure_ascii=False) + ']').click()

        def edit(radius):
            editor = page.locator('[data-editor-path=' + json.dumps(MACRO, ensure_ascii=False) + ']')
            expect(editor).to_have_attribute('data-active', 'true')
            page.wait_for_function('window.monaco?.editor.getModels().length')
            page.evaluate('(text) => window.monaco.editor.getModels().at(-1).setValue(text)', model(radius))
            expect(editor).to_have_attribute('data-dirty', 'true')
            open_macro_panel()
            expect(panel.get_by_role('button', name='运行宏', exact=True)).to_be_disabled()
            page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^编辑器$')).click()
            click_response(page.get_by_role('button', name='保存 (Ctrl+S)', exact=True), 'PUT', path + '/file')
            expect(editor).to_have_attribute('data-dirty', 'false')
            open_macro_panel()

        def finished(job_id, phase, label):
            expect(status).to_contain_text(label, timeout=105000)
            job = request('GET', f'/jobs/{job_id}')
            assert job['status'] == phase and not job['completion_pending']
            assert job['attempt']['process_stop']['verified'] and job['attempt']['process_stop']['scope_empty']
            children = job['attempt']['child_processes']['children']
            assert children and all(child['stop']['verified'] and child['stop']['scope_empty'] for child in children.values())
            result['jobs'].append({'id': job_id, 'status': phase, 'attempt': job['attempt']['id'],
                'children_stopped': len(children)})
            return job

        def raw(name=OUTPUT):
            return request('GET', path + '/raw?' + urlencode({'path': name}), raw=True)

        def launch():
            button = panel.get_by_role('button', name=re.compile(r'^(运行宏|确认原提交)$'))
            return click_response(button, 'POST', run).json()['job_id']

        try:
            page.goto(base, wait_until='domcontentloaded')
            select_case(page)
            open_macro_panel()
            expect(selector.locator('option')).to_have_count(3)
            assert NON_MACRO not in (selector.locator('option').evaluate_all('(items) => items.map(item => item.value)'))
            panel.get_by_label('新宏文件名').fill('球面')
            click_response(panel.get_by_role('button', name='新建宏', exact=True), 'PUT', path + '/file')
            edit(1.25)
            expect(selector).to_have_value(MACRO)
            expect(backend).to_have_value('pvpython')
            original = {}

            def lose_committed_response(route):
                deadline = time.monotonic() + 30
                while True:
                    accepted = route.fetch()
                    if accepted.status == 200:
                        original.update(job=accepted.json()['job_id'], key=route.request.headers['idempotency-key'])
                        route.abort('connectionfailed')
                        return
                    detail = accepted.json().get('detail') if accepted.status == 409 else None
                    result.setdefault('intercepted_admission_statuses', []).append({'status': accepted.status,
                        'code': detail.get('code') if isinstance(detail, dict) else None})
                    if not isinstance(detail, dict) or detail.get('code') != 'storage_quota_busy' or time.monotonic() >= deadline:
                        route.fulfill(response=accepted)
                        return
                    time.sleep(.25)

            page.route('**/paraview:run', lose_committed_response, times=1)
            panel.get_by_role('button', name='运行宏', exact=True).click()
            expect(panel.get_by_role('alert')).to_contain_text('提交结果尚未确认')
            assert original, result.get('intercepted_admission_statuses')
            retried = click_response(panel.get_by_role('button', name='确认原提交', exact=True), 'POST', run)
            assert retried.json()['job_id'] == original['job']
            assert retried.request.headers['idempotency-key'] == original['key']
            first = finished(original['job'], 'succeeded', '执行成功')
            first_bytes = raw()
            result['initial_scene'] = verify_vtp(first_bytes, 1.25)
            result['lost_response_reused_original_job'] = True

            vtk_row = page.locator('[data-path=' + json.dumps(OUTPUT) + ']')
            if not vtk_row.is_visible():
                folder = page.locator('[data-path="D-post"]')
                if folder.get_attribute('aria-expanded') == 'false':
                    folder.click()
            with page.expect_response(lambda response: '/viz-proxy/' in urlsplit(response.url).path
                    and urlsplit(response.url).path.endswith('/index.html') and response.status == 200,
                    timeout=75000) as preview:
                vtk_row.click()
            preview_id = int(urlsplit(preview.value.url).path.split('/')[2])
            frame = page.locator('iframe[title="可视化"]')
            expect(frame).to_be_visible()
            selected = next(row for row in request('GET', path + '/viz:sessions') if row['id'] == preview_id)
            assert selected['file_path'] == OUTPUT and selected['alive']
            page.screenshot(path=str(root / 'macro-preview.png'))
            request('DELETE', f'/viz:sessions/{preview_id}')
            result['actual_viz_preview'] = {'id': preview_id, 'file': OUTPUT, 'proxy_status': 200}

            open_macro_panel()
            panel.get_by_role('button', name='打开脚本', exact=True).click()
            edit(0.75)
            backend.select_option('pvbatch')
            second_id = launch()
            second = finished(second_id, 'succeeded', '执行成功')
            second_bytes = raw()
            result['updated_scene'] = verify_vtp(second_bytes, 0.75)
            assert first_bytes != second_bytes
            publications = [job['attempt']['outputs']['publication'] for job in (first, second)]
            for publication, data in zip(publications, (first_bytes, second_bytes)):
                assert publication['status'] == 'committed'
                assert (case_files / '.vibeflow' / 'publications' / publication['id'] / 'files' / OUTPUT).read_bytes() == data
            result['published_ids'] = [publication['id'] for publication in publications]
            result['original_scene_version_preserved'] = True

            selector.select_option(FAIL)
            failed = finished(launch(), 'failed', '执行失败')
            failed_private = case_files / failed['attempt']['workspace']['run_path']
            assert b'vf macro failure marker' in (failed_private / '.vibeflow/paraview/native.log').read_bytes()
            assert raw() == second_bytes
            result['native_error_preserved_output'] = True

            selector.select_option(CANCEL)
            backend.select_option('pvpython')
            running = launch()
            deadline = time.monotonic() + 105
            while time.monotonic() < deadline:
                started = request('GET', f'/jobs/{running}')
                marker = case_files / started['attempt']['workspace']['run_path'] / READY
                if marker.is_file() and marker.read_text() == 'native ParaView ready':
                    break
                assert started['status'] in {'queued', 'running'}
                time.sleep(.25)
            else:
                raise AssertionError('No physical native macro readiness marker')
            page.reload(wait_until='domcontentloaded')
            select_case(page)
            open_macro_panel()
            expect(selector).to_have_value(CANCEL)
            expect(backend).to_have_value('pvpython')
            expect(status).to_contain_text(f'作业 #{running}')
            cancel_button = panel.get_by_role('button', name='取消宏', exact=True)
            click_response(cancel_button, 'POST', f'/jobs/{running}:cancel')
            cancelled = finished(running, 'cancelled', '已取消')
            assert cancelled['attempt']['outputs']['publication']['status'] != 'committed'
            assert raw() == second_bytes
            request('GET', path + '/raw?' + urlencode({'path': READY}), status_code=404)
            result['browser_reload_reattached_native_cancel'] = True

            guest = account(settings['viewer']).new_page()
            guest.goto(base, wait_until='domcontentloaded')
            guest.get_by_label('当前工作区', exact=True).select_option(str(settings['workspace']))
            select_case(guest)
            guest.locator('.dv-tab:visible').filter(has_text=re.compile(r'^可视化$')).click()
            guest_panel = guest.get_by_test_id('paraview-macros')
            guest_panel.locator('summary').click()
            guest_picker = guest_panel.get_by_label('宏脚本', exact=True)
            expect(guest_picker).to_have_value('')
            guest_picker.select_option(MACRO)
            expect(guest_panel.get_by_role('button', name='打开脚本', exact=True)).to_be_enabled()
            expect(guest_panel.get_by_role('button', name='运行宏', exact=True)).to_be_disabled()
            expect(guest_panel.get_by_role('button', name='新建宏', exact=True)).to_have_count(0)
            result['viewer_browses_and_uses_no_owner_preferences'] = True
            result['passed'] = True
        finally:
            (root / 'macro-browser.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
            if not result.get('passed'):
                try:
                    page.screenshot(path=str(root / 'macro-browser-failure.png'))
                except BrowserError:
                    pass
            edge.close()


def verify(args):
    from testsupport import IsolatedServer

    api = IsolatedServer('a15-paraview-macros', app='macro_fixture:app')
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    (api.root / 'macro_fixture.py').write_text('from app.main import app\n'
        'from fastapi.staticfiles import StaticFiles\n'
        + 'app.mount("/", StaticFiles(directory=' + repr(str(dist)) + ', html=True))\n', encoding='utf8')
    temporary = api.root / 'browser-tmp'
    temporary.mkdir()
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0, 'storage': 'fs', 'database': 'sqlite'}
    print('PARAVIEW_MACRO_EVIDENCE', api.root, flush=True)
    try:
        with api:
            api.create_case(CASE)
            case = api.call('GET', api.prefix)
            api.put(FAIL, 'raise RuntimeError("vf macro failure marker")\n')
            api.put(CANCEL, CANCEL_SCRIPT)
            api.put(NON_MACRO, '# not under the macro directory')
            email = 'macro-viewer@example.com'
            viewer = api.call('POST', '/auth/register', {'email': email, 'name': 'Macro viewer',
                               'password': 'isolated-paraview-123'})
            headers = {'Authorization': 'Bearer ' + viewer['token']}
            endpoint = api.prefix + '/paraview:run'
            api.call('POST', endpoint, {'script_path': CANCEL}, expect=404, extra_headers=headers)
            org = api.call('GET', '/org/mine')['org']['id']
            api.call('POST', f'/org/{org}/members', {'email': email, 'role': 'viewer'}, expect=201)
            api.call('POST', f'/org/{org}/invitation:accept', {}, extra_headers=headers)
            api.call('PUT', f"/workspaces/{case['workspace_id']}/sharing", {'org_id': org, 'role': 'viewer'})
            api.call('POST', endpoint, {'script_path': CANCEL}, expect=404, extra_headers=headers)
            api.call('PUT', api.prefix + '/file', {'path': CANCEL, 'content': 'forbidden'}, expect=404, extra_headers=headers)
            api.call('POST', endpoint, {'script_path': '.macros/../.vibeflow/state.py'}, expect=400)
            settings = {'base': api.base, 'root': str(api.root), 'prefix': api.prefix,
                'storage_prefix': case['storage_prefix'], 'workspace': case['workspace_id'], 'channel': args.channel,
                'owner': api.token, 'viewer': viewer['token']}
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(settings), text=True, check=True, timeout=490,
                env=dict(os.environ, TMP=str(temporary), TEMP=str(temporary)))
            view = json.loads((api.root / 'macro-browser.json').read_text(encoding='utf8'))
            jobs = api.call('GET', api.prefix + '/jobs')
            assert len(jobs) == 4 and {job['id'] for job in jobs} == {job['id'] for job in view['jobs']}
            api.call('POST', f"/jobs/{view['jobs'][-1]['id']}:cancel", {}, expect=404, extra_headers=headers)
            report['browser'] = view
            report['job_count'] = 4
            report['case_acl_checked'] = True
        report['passed'] = view['passed'] and api.proc.poll() is not None
    finally:
        report['api_exited'] = api.proc is not None and api.proc.poll() is not None
        (api.root / 'macro-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    assert report['passed']
    print('PARAVIEW_MACRO_PASS', api.root / 'macro-result.json', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
        from regression_llm_policy import enforce

        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        verify(parser.parse_args())
