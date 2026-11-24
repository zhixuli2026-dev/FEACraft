"""Check the actual generic result readers and composite selection through Edge and VTK."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

from testsupport import IsolatedServer, SERVER
from viz_information_browser_acceptance import upload, write_result


CASE = 'Generic engineering result readers'
RESULTS = 'Ref/reader-matrix/'
TEMPORAL = ('temporal.xdmf', 'temporal.xmf', 'temporal.exo', 'temporal.e', 'temporal.exodus', 'reference.series')
STATIC = {
    'reference.vtk': ('vtkUnstructuredGrid', 1, True),
    'step-start.vtu': ('vtkUnstructuredGrid', 1, True),
    'surface.vtp': ('vtkPolyData', 4, True),
    'surface.stl': ('vtkPolyData', 4, False),
}
BLOCKS = {'Core · #2': ('[0, 1]', '[1, 4]'), 'Bracket · #3': ('[3, 4]', '[101, 104]'),
          'Mount · #4': ('[8, 9]', '[201, 204]')}
LARGE_NAME = 'large-surface.vtp'
LARGE_DIGEST = '30440cc52232027743e99b39cba4eb835d725ccbf378efd5164631245e4b4292'


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root, base = Path(settings['root']), settings['base']
    headers = {'Authorization':'Bearer ' + settings['token']}
    api = base + '/api/v1'
    case = api + f"/cases/{settings['case']}"
    report = {'passed':False, 'page_errors':[], 'model_requests':[], 'formats':{},
              'composite':{}, 'closed_sessions':[]}
    session = None
    source = None
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        report['edge'] = edge.version
        context = edge.new_context(viewport={'width':1620, 'height':1060}, locale='zh-CN')
        context.add_init_script(f"if (location.origin === {json.dumps(base)}) localStorage.setItem('vf_token', {json.dumps(settings['token'])});")
        page = context.new_page()
        page.set_default_timeout(27_000)
        expect.set_options(timeout=18_000)
        page.on('pageerror', lambda error:report['page_errors'].append(
            {'source':source, 'message':str(error), 'stack':error.stack}))
        page.on('request', lambda req:report['model_requests'].append(urlsplit(req.url).path)
                if req.method == 'POST' and urlsplit(req.url).path.endswith(('/messages','/turns','/goal:run')) else None)
        iframe = page.locator('iframe[title="可视化"]')
        inner = page.frame_locator('iframe[title="可视化"]')
        drawer = inner.locator('.v-navigation-drawer')
        canvas = inner.locator('main canvas:visible').first
        info = inner.get_by_test_id('viz-information')
        native = inner.get_by_test_id('viz-paraview-data')
        arrays = inner.get_by_test_id('viz-info-arrays')

        def readout(container, key):
            return container.locator('[data-info-key=' + json.dumps(key, ensure_ascii=False) + ']')

        def field(container, key, value):
            expect(readout(container, key)).to_have_text(str(value))

        def array(name, value):
            expect(arrays.get_by_text(name, exact=True).locator('..')).to_contain_text('范围 ' + value)

        def open_arrays():
            if arrays.get_attribute('open') is None:
                arrays.locator('summary').click()

        def choose(label, value):
            field = inner.get_by_role('combobox', name=label, exact=True)
            field.press('Enter')
            expect(field).to_have_attribute('aria-expanded', 'true')
            inner.get_by_role('option', name=value, exact=True).click()
            expect(field).to_have_value(value)

        def open_result(name):
            nonlocal session, source
            source = name
            relative = RESULTS + name
            with page.expect_response(lambda response: response.request.method == 'POST' and response.url == case + '/viz:sessions',
                                      timeout=55_000) as opened:
                page.locator('[data-path=' + json.dumps(relative) + ']').click()
            assert opened.value.ok, opened.value.text()
            value = opened.value.json()
            assert value['file_path'] == relative and value['alive'] is True
            session = value['id']
            expect(iframe).to_have_attribute('src', re.compile(f'^/viz-proxy/{session}/index.html'))
            expect(canvas).to_be_visible()
            if 'v-navigation-drawer--active' not in (drawer.get_attribute('class') or '').split():
                inner.locator('.v-app-bar-nav-icon').click()
            expect(inner.get_by_test_id('viz-info-selection')).to_have_text(name)

        def close_result():
            nonlocal session
            url = api + f'/viz:sessions/{session}'
            with page.expect_response(lambda response: response.request.method == 'DELETE' and response.url == url,
                                      timeout=45_000) as stopped:
                page.get_by_role('button', name=f'关闭可视化会话 {session}', exact=True).click()
            assert stopped.value.ok, stopped.value.text()
            record = page.request.get(url, headers=headers).json()
            assert record['phase'] == 'closed' and record['stop']['verified'] and record['stop']['scope_empty'], record
            report['closed_sessions'].append(session)
            session = None

        def read_paraview(name):
            inner.get_by_test_id('viz-paraview-information').click()
            status = inner.get_by_test_id('viz-paraview-status')
            expect(status).to_have_text(re.compile(r'^ParaView [\d.]+ · ' + re.escape(name) + '$'), timeout=32_000)
            return status.inner_text()

        try:
            page.goto(base, wait_until='domcontentloaded')
            page.locator('[data-case-name=' + json.dumps(CASE) + ']').click()
            for folder in ('Ref', RESULTS.rstrip('/')):
                node = page.locator('[data-path=' + json.dumps(folder) + ']')
                if node.get_attribute('aria-expanded') == 'false':
                    node.click()
            for name, (kind, cells, pressure) in STATIC.items():
                open_result(name)
                field(info, '网格类型', kind)
                field(info, '点', 4)
                field(info, '单元', cells)
                field(info, '时刻', '静态数据')
                expect(inner.get_by_role('combobox', name='时间步', exact=True)).to_have_count(0)
                if pressure:
                    open_arrays()
                    array('Pressure（点）', '[1, 4]')
                report['formats'][name] = {'dataset':kind, 'points':4, 'cells':cells, 'times':[]}
                close_result()
            for name in TEMPORAL:
                open_result(name)
                field(info, '点', 4)
                field(info, '时刻', '1.75')
                open_arrays()
                array('Pressure（点）', '[11, 14]')
                array('CellTemperature（单元）', '[30, 30]')
                choose('时间步', 't=0.25')
                field(info, '时刻', '0.25')
                array('Pressure（点）', '[1, 4]')
                array('CellTemperature（单元）', '[20, 20]')
                report['formats'][name] = {'times':[0.25,1.75], 'point_ranges':[[1,4],[11,14]], 'cell_values':[20,30]}
                if name in {'temporal.exo','temporal.xdmf'}:
                    report['formats'][name]['paraview'] = read_paraview(name)
                    field(native, '时刻', '0.25')
                if name == 'temporal.exodus':
                    page.screenshot(path=str(root / 'result-exodus-time.png'))
                close_result()

            open_result('assembly.vtm')
            expect(inner.get_by_role('combobox', name='结果数据块', exact=True)).to_have_value('全部数据块')
            field(info, '点', 12)
            field(info, '单元', 12)
            field(info, '范围 X', '[0, 9]')
            overview = canvas.screenshot(path=str(root / 'result-composite-all.png'))
            assert read_paraview('assembly.vtm').startswith('ParaView ')
            field(native, '数据块', 3)
            for label, (x_span, pressure) in BLOCKS.items():
                choose('结果数据块', label)
                field(info, '网格类型', 'vtkUnstructuredGrid')
                field(info, '范围 X', x_span)
                field(info, '点', 4)
                field(info, '单元', 1)
                open_arrays()
                array('Pressure（点）', pressure)
                name = label.split()[0]
                frame = canvas.screenshot(path=str(root / f'result-composite-{name.lower()}.png'))
                assert frame != overview
                report['composite'][name] = {'range_x':x_span, 'pressure':pressure, 'png_sha256':hashlib.sha256(frame).hexdigest()}
            choose('结果数据块', '全部数据块')
            field(info, '单元', 12)
            field(info, '范围 X', '[0, 9]')
            page.screenshot(path=str(root / 'result-composite-matrix.png'))
            close_result()

            open_result('composite.series')
            choose('时间步', 't=0.25')
            field(info, '单元', 12)
            choose('结果数据块', 'Bracket · #3')
            field(info, '范围 X', '[3, 4]')
            choose('时间步', 't=1.75')
            expect(inner.get_by_test_id('viz-result-block-status')).to_have_text('该时刻没有所选数据块，已显示全部数据块')
            field(info, '单元', 1)
            field(info, '范围 X', '[0, 1]')
            report['time_with_missing_block_restores_complete_source'] = True
            close_result()
            if settings['large']:
                open_result(LARGE_NAME)
                field(info, '网格类型', 'vtkPolyData')
                field(info, '点', '522,242')
                field(info, '单元', '1,044,480')
                expect(readout(info, 'VTK 内存')).to_have_text(re.compile(r'^[\d,]+ KiB$'))
                frame = canvas.screenshot(path=str(root / 'result-large-surface.png'))
                report['large_surface'] = {'points':522242, 'cells':1044480,
                    'memory':readout(info, 'VTK 内存').inner_text(), 'png_sha256':hashlib.sha256(frame).hexdigest()}
                close_result()
            assert not report['page_errors'] and not report['model_requests'], report
            report['passed'] = True
        finally:
            if not report['passed']:
                page.screenshot(path=str(root / 'result-formats-failure.png'))
            write_result(root / 'result-formats-browser.json', report)
            if session is not None:
                page.request.delete(api + f'/viz:sessions/{session}', headers=headers)
            edge.close()


def verify(args):
    import httpx

    from app.services.result_readers import ResultReaderError, load_result_file
    from viz_result_formats_fixture import fixtures

    server = IsolatedServer('viz-formats-d25', app='result_formats_fixture:app')
    root = server.root
    scratch = root / 'native-tmp'
    scratch.mkdir()
    dist = args.dist.resolve(strict=True)
    env = dict(os.environ, TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch), PYTHONIOENCODING='utf8')
    (root / 'result_formats_fixture.py').write_text('import os\nfrom fastapi.staticfiles import StaticFiles\n'
        f'os.environ.update(TEMP={str(scratch)!r}, TMP={str(scratch)!r}, TMPDIR={str(scratch)!r})\nfrom app.main import app\n'
        f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n', encoding='utf8')
    target = root / 'result-formats-result.json'
    result = {'passed':False, 'model_calls':0}
    print('VIZ_RESULT_FORMATS_EVIDENCE', root, flush=True)
    try:
        inputs = root / 'inputs'
        files = fixtures(inputs)
        result['fixture_hashes'] = {path.relative_to(inputs).as_posix():hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        large = args.large_vtp.resolve(strict=True) if args.large_vtp else None
        if large is not None:
            with large.open('rb') as stream:
                observed = hashlib.file_digest(stream, 'sha256').hexdigest()
            assert observed == LARGE_DIGEST and large.stat().st_size == 45960682
            result['large_source'] = {'sha256':observed, 'bytes':large.stat().st_size}
        bad_series = inputs / 'invalid.series'
        bad_series.write_text(json.dumps({'files':[{'name':'step-start.vtu', 'time':0.25},
                                                    {'name':'step-end.vtu', 'time':0.25}]}), encoding='utf8')
        try:
            load_result_file(bad_series)
        except ResultReaderError as error:
            assert '时刻不能重复' in str(error)
            result['rejected_invalid_series'] = True
        else:
            raise AssertionError('Duplicate time steps were accepted')
        try:
            load_result_file(inputs / 'temporal.exo', 9.75)
        except ResultReaderError as error:
            assert '文件不含时刻' in str(error)
            result['rejected_absent_native_time'] = True
        else:
            raise AssertionError('A nonexistent native time step was accepted')
        with server as api:
            case = api.create_case(CASE)
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization':'Bearer ' + api.token},
                              trust_env=False, timeout=36) as client:
                for file in files:
                    upload(api, client, file, RESULTS + file.relative_to(inputs).as_posix())
                if large is not None:
                    upload(api, client, large, RESULTS + LARGE_NAME)
            api.call('POST', api.prefix + '/viz:sessions', {'file_path':RESULTS + 'missing.vtu'}, expect=404)
            api.call('POST', api.prefix + '/viz:sessions', {'file_path':RESULTS + 'not-a-result.txt'}, expect=400)
            foreign = api.call('POST', '/auth/register', {'name':'Other reader owner', 'email':'other-d25@example.com',
                'password':'isolated-test-123'})
            api.call('POST', api.prefix + '/viz:sessions', {'file_path':RESULTS + 'temporal.xmf'}, expect=404,
                     extra_headers={'Authorization':'Bearer ' + foreign['token']})
            result['rejected_http'] = [404,400,404]
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', __file__, '--browser'],
                input=json.dumps({'root':str(root), 'case':case['id'], 'base':api.base, 'token':api.token,
                                  'large':large is not None}),
                text=True, encoding='utf8', check=True, timeout=325, cwd=root, env=env)
            result['browser'] = json.loads((root / 'result-formats-browser.json').read_text(encoding='utf8'))
            assert result['browser']['passed']
        result['api_exit_code'] = server.proc.poll()
        assert result['api_exit_code'] is not None
        result['passed'] = True
    except BaseException as error:
        result['failure'] = {'type':type(error).__name__, 'message':str(error)[:1000]}
        raise
    finally:
        write_result(target, result)
    print('VIZ_RESULT_FORMATS_PASS', target, flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        sys.path.insert(0, str(SERVER.parent / 'tools'))
        from regression_llm_policy import enforce

        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--large-vtp', type=Path, help='Reuse the exact 522,242-point D15 evidence asset')
        verify(parser.parse_args())
