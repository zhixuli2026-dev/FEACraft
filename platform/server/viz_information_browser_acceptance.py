"""Verify actual VTK visualization information and ParaView server-manager data in Edge."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

TITLE = 'Native visualization data information'
SMALL = 'Ref/engineering.vtu'
SURFACE = 'Ref/large-surface.vtp'
SERIES = 'Ref/engineering.series'
COMPOSITE = 'Ref/assembly.vtm'
SOURCE = Path(__file__).resolve().parent / 'examples/paraview/naca0012-su2.vtu'


def write_result(path, values):
    path.write_text(json.dumps(values, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def actual_inputs(folder):
    import vtk

    folder.mkdir()
    standalone = folder / Path(SMALL).name
    shutil.copyfile(SOURCE, standalone)
    reader = vtk.vtkXMLUnstructuredGridReader()
    reader.SetFileName(str(standalone))
    reader.Update()
    small = reader.GetOutput()
    assert (small.GetNumberOfPoints(), small.GetNumberOfCells()) == (5233, 10216)
    large = vtk.vtkSphereSource()
    large.SetThetaResolution(512)
    large.SetPhiResolution(256)
    large.SetRadius(2)
    large.Update()
    surface = large.GetOutput()
    pressure = vtk.vtkFloatArray()
    pressure.SetName('p')
    pressure.SetNumberOfTuples(surface.GetNumberOfPoints())
    pressure.Fill(12.5)
    surface.GetPointData().AddArray(pressure)
    region = vtk.vtkUnsignedCharArray()
    region.SetName('zoneId')
    region.SetNumberOfTuples(surface.GetNumberOfCells())
    region.Fill(7)
    surface.GetCellData().AddArray(region)
    description = vtk.vtkStringArray()
    description.SetName('样本说明')
    description.InsertNextValue('真实生成的球面')
    surface.GetFieldData().AddArray(description)
    writer = vtk.vtkXMLPolyDataWriter()
    writer.SetFileName(str(folder / Path(SURFACE).name))
    writer.SetInputData(surface)
    assert writer.Write() == 1

    second = vtk.vtkSphereSource()
    second.SetThetaResolution(9)
    second.SetPhiResolution(7)
    second.Update()
    assembly = vtk.vtkMultiBlockDataSet()
    assembly.SetNumberOfBlocks(2)
    assembly.SetBlock(0, small)
    assembly.SetBlock(1, second.GetOutput())
    multi = vtk.vtkXMLMultiBlockDataWriter()
    multi.SetFileName(str(folder / Path(COMPOSITE).name))
    multi.SetInputData(assembly)
    assert multi.Write() == 1
    write_result(folder / Path(SERIES).name, {'file-series-version': '1.0', 'files': [
        {'name': standalone.name, 'time': 1.25}, {'name': Path(SURFACE).name, 'time': 8.0},
    ]})
    return {
        'small': {'points': small.GetNumberOfPoints(), 'cells': small.GetNumberOfCells(),
                  'memory_kib': small.GetActualMemorySize()},
        'large': {'points': surface.GetNumberOfPoints(), 'cells': surface.GetNumberOfCells(),
                  'file_bytes': (folder / Path(SURFACE).name).stat().st_size},
        'composite': {'points': small.GetNumberOfPoints() + second.GetOutput().GetNumberOfPoints(),
                      'cells': small.GetNumberOfCells() + second.GetOutput().GetNumberOfCells()},
    }


def upload(api, client, source, name):
    h = hashlib.sha256()
    with source.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            h.update(block)
        stream.seek(0)
        size = source.stat().st_size
        digest = h.hexdigest()
        path = api.prefix + '/uploads'
        session = api.call('POST', path, {'path': name, 'size': size, 'sha256': digest})['id']
        for offset in range(0, size, 8 * 1024 * 1024):
            response = client.put(path + '/' + session, params={'offset': offset}, content=stream.read(8 * 1024 * 1024))
            response.raise_for_status()
    assert api.call('POST', path + '/' + session + ':complete')['sha256'] == digest


def browser(args):
    from playwright.sync_api import expect, sync_playwright

    root, base, data = Path(args['root']), args['base'], args['data']
    report = {'passed': False, 'page_errors': [], 'inference_requests': [], 'stopped': []}
    with sync_playwright() as toolkit:
        edge = toolkit.chromium.launch(channel='msedge')
        report['edge'] = edge.version
        context = edge.new_context(viewport={'width': 1520, 'height': 1040}, locale='zh-CN')
        context.add_init_script(f"if(location.origin==={json.dumps(base)})localStorage.setItem('vf_token',{json.dumps(args['token'])});")
        page = context.new_page()
        page.set_default_timeout(30_000)
        expect.set_options(timeout=28_000)
        page.on('pageerror', lambda error: report['page_errors'].append(str(error)))
        page.on('request', lambda request: report['inference_requests'].append(urlsplit(request.url).path)
                if request.method == 'POST' and urlsplit(request.url).path.endswith(('/messages', '/turns', '/goal:run')) else None)
        inner = page.frame_locator('iframe[title="可视化"]')
        table = inner.get_by_test_id('viz-information')
        native_table = inner.get_by_test_id('viz-paraview-data')
        status = inner.get_by_test_id('viz-paraview-status')
        session = None

        def open_data(name):
            nonlocal session
            folder = page.locator('[data-path="Ref"]')
            if folder.get_attribute('aria-expanded') == 'false':
                folder.click()
            with page.expect_response(lambda resp: resp.request.method == 'POST'
                                      and urlsplit(resp.url).path.endswith('/viz:sessions')
                                      and resp.request.post_data_json.get('file_path') == name,
                                      timeout=60_000) as event:
                page.locator('[data-path=' + json.dumps(name) + ']').click()
            assert event.value.ok, (event.value.status, event.value.text())
            session = event.value.json()['id']
            assert session not in report['stopped']
            expect(page.locator('iframe[title="可视化"]')).to_have_attribute('src',
                re.compile(r'^/viz-proxy/' + str(session) + r'/index.html\?'))
            drawer = inner.locator('.v-navigation-drawer')
            expect(drawer).to_be_attached()
            if 'v-navigation-drawer--active' not in (drawer.get_attribute('class') or '').split():
                inner.locator('.v-app-bar-nav-icon').click()
            expect(table).to_be_visible()
            expect(inner.get_by_test_id('viz-info-selection')).to_have_text(Path(name).name)

        def close_data():
            nonlocal session
            if session is None:
                return
            headers = {'Authorization': 'Bearer ' + args['token']}
            route = base + f'/api/v1/viz:sessions/{session}'
            close_button = page.get_by_role('button', name=f'关闭可视化会话 {session}', exact=True)
            with page.expect_response(lambda resp: resp.request.method == 'DELETE' and resp.url == route,
                                      timeout=45_000) as response:
                close_button.click()
                expect(page.locator('iframe[title="可视化"]')).to_have_count(0)
            closed = response.value
            assert closed.ok, (closed.status, closed.text())
            read = page.request.get(route, headers=headers)
            assert read.ok, read.status
            record = read.json()
            assert record['phase'] == 'closed' and record['stop']['verified'] and record['stop']['scope_empty'], record
            report['stopped'].append(session)
            session = None

        def field(container, name, value):
            expect(container.locator('[data-info-key=' + json.dumps(name, ensure_ascii=False) + ']')).to_have_text(value)

        def counts(container, expected):
            field(container, '点', f"{expected['points']:,}")
            field(container, '单元', f"{expected['cells']:,}")
            field(container, 'VTK 内存', re.compile(r'^[\d,]+ KiB$'))

        def native(name):
            inner.get_by_test_id('viz-paraview-information').click()
            expect(status).to_have_text(re.compile(r'^ParaView [\d.]+ · ' + re.escape(Path(name).name) + '$'))
            return status.inner_text()

        try:
            page.goto(base, wait_until='domcontentloaded')
            page.locator('[data-case-name=' + json.dumps(TITLE) + ']').click()
            open_data(SMALL)
            counts(table, data['small'])
            field(table, '网格类型', 'vtkUnstructuredGrid')
            field(table, '单元类型', 'vtkTriangle')
            field(table, 'VTK 内存', f"{data['small']['memory_kib']:,} KiB")
            field(table, '范围 X', '[-20, 20]')
            report['native_version'] = native(SMALL)
            counts(native_table, data['small'])
            field(native_table, '数据块', '1')
            native_arrays = inner.get_by_test_id('viz-paraview-arrays')
            native_arrays.locator('summary').click()
            expect(native_arrays).to_contain_text('Velocity（点）')
            expect(native_arrays).to_contain_text('62,796 B')
            page.screenshot(path=str(root / 'information-unstructured.png'))
            close_data()

            open_data(SERIES)
            counts(table, data['large'])
            field(table, '网格类型', 'vtkPolyData')
            field(table, '时刻', '8')
            local_arrays = inner.get_by_test_id('viz-info-arrays')
            local_arrays.locator('summary').click()
            expect(local_arrays).to_contain_text('p（点）')
            expect(local_arrays).to_contain_text(f"{data['large']['points'] * 4:,} B")
            expect(local_arrays).to_contain_text('范围 [12.5, 12.5]')
            expect(local_arrays).to_contain_text('zoneId（单元）')
            expect(local_arrays).to_contain_text('样本说明（全局）')
            native(SERIES)
            counts(native_table, data['large'])
            field(native_table, '时间步', '2')
            field(native_table, '时刻', '8')
            page.screenshot(path=str(root / 'information-large-surface.png'))
            time_select = inner.get_by_role('combobox', name='时间步')
            time_select.focus()
            time_select.press('Enter')
            expect(time_select).to_have_attribute('aria-expanded', 'true')
            inner.get_by_role('option', name='t=1.25', exact=True).click()
            counts(table, data['small'])
            field(table, '时刻', '1.25')
            expect(native_table).to_have_count(0)
            native(SERIES)
            counts(native_table, data['small'])
            field(native_table, '时刻', '1.25')
            close_data()
            report['series_switched_from_large_polydata_to_vtu'] = True

            open_data(COMPOSITE)
            native(COMPOSITE)
            counts(native_table, data['composite'])
            field(native_table, '数据块', '2')
            field(native_table, '网格类型', 'vtkMultiBlockDataSet')
            page.screenshot(path=str(root / 'information-composite.png'))
            inner.get_by_role('button', name='切面', exact=True).click()
            expect(inner.get_by_test_id('viz-info-selection')).to_have_text('VF_slice_1')
            field(table, '网格类型', 'vtkPolyData')
            close_data()
            report['composite_native_and_selected_filter'] = True
            assert len(report['stopped']) == 3
            assert not report['page_errors'] and not report['inference_requests'], report
            report['passed'] = True
        finally:
            if not report['passed']:
                page.screenshot(path=str(root / 'viz-information-failure.png'))
            write_result(root / 'viz-information-browser.json', report)
            if session is not None:
                page.request.delete(base + f'/api/v1/viz:sessions/{session}',
                                    headers={'Authorization': 'Bearer ' + args['token']})
            edge.close()


def verify(args):
    import httpx
    from testsupport import IsolatedServer

    dist = args.dist.resolve(strict=True)
    api = IsolatedServer('viz-information-d04', app='viz_information_fixture:app')
    root = api.root
    run_tmp = root / 'native-tmp'
    run_tmp.mkdir()
    print('VIZ_INFORMATION_EVIDENCE', root, flush=True)
    fixture = ("import os\nfrom fastapi.staticfiles import StaticFiles\n"
               f"os.environ.update({{'TEMP': {str(run_tmp)!r}, 'TMP': {str(run_tmp)!r}}})\nfrom app.main import app\n"
               f"app.mount('/', StaticFiles(directory={str(dist)!r}, html=True))\n")
    (root / 'viz_information_fixture.py').write_text(fixture, encoding='utf8')
    folder = root / 'inputs'
    data = actual_inputs(folder)
    report = {'passed': False, 'fixtures': data, 'mock_llm_calls': 0}
    try:
        with api:
            api.create_case(TITLE)
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                              trust_env=False, timeout=40) as client:
                for source in sorted(folder.rglob('*')):
                    if source.is_file():
                        upload(api, client, source, 'Ref/' + source.relative_to(folder).as_posix())
            outsider = api.call('POST', '/auth/register', {'name': 'Other data user',
                                'email': 'viz-info-independent@example.com', 'password': 'isolated-test-123'})
            api.call('POST', api.prefix + '/viz:sessions', {'file_path': SMALL}, expect=404,
                     extra_headers={'Authorization': 'Bearer ' + outsider['token']})
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', __file__, '--browser'],
                           input=json.dumps({'root': str(root), 'base': api.base, 'data': data, 'token': api.token}),
                           text=True, encoding='utf8', check=True, timeout=360, cwd=root,
                           env=dict(os.environ, TEMP=str(run_tmp), TMP=str(run_tmp)))
            report['browser'] = json.loads((root / 'viz-information-browser.json').read_text(encoding='utf8'))
            assert report['browser']['passed']
            report['other_user_http'] = 404
        assert api.proc.poll() is not None
        report['passed'] = True
    finally:
        write_result(root / 'viz-information-result.json', report)
    print('VIZ_INFORMATION_PASS', root / 'viz-information-result.json', flush=True)


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
        verify(parser.parse_args())
