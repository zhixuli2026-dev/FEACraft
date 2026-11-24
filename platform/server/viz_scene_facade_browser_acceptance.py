"""Verify all 12 VF scene functions through an isolated native VTK renderer in Edge."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

from testsupport import IsolatedServer, SERVER
from viz_information_browser_acceptance import upload, write_result


CASE = 'Native VF scene functions'
SERIES = 'Ref/facade.series'
SCRIPT = 'D-post/scenes.py'
LOG = 'D-post/logs/viz_executor.log'
NAMES = ('Contract surface', 'Contract section', 'Contract contour', 'Contract stream', 'Contract arrows')
AWAY = 'figures/vf-camera-away.png'
RESET = 'figures/vf-camera-reset.png'
FOAM = 'C-run/d10-cavity'
FOAM_INPUTS = {
    'system/controlDict': 'c6128a998d4d3624e41d09966b53323a63522d063b0274d430aabb9e84c34820',
    'constant/polyMesh/boundary': '4892e1ec8d62569cf2a6b23ec59316a4e21375a50b092e56f541eafb737417f2',
    'constant/polyMesh/faces': '5fc1d0e3cd48773b7c6e8fa4af3980892fed61561fe6340201b3645d631ac617',
    'constant/polyMesh/neighbour': 'e88ac5b4308a4f7417ea04254d183b2b03bcf312c5b09fc52f35fdce396cc53f',
    'constant/polyMesh/owner': '24a31f09634192eb136f96b3486f67083e372f5763faa99b9c0266fd521deabb',
    'constant/polyMesh/points': '88c0e985d240f6c9c1eb5321bb4b6e9aa3b4280d67bfb8c79b7775183902f5e6',
    '0.2/U': 'b7ff9bfdef215d83f640d7bbbd6154e91a5bf281651647d77c93d162d6221111',
    '0.2/p': '67ad22a56355d1c7859f5d40b9a8b2ab77f36e582deb1f300331fcebb2acc144',
    '0.5/U': '911f6cc07009414ca861907514e8dc3c711f267b4e4d24cdf07a4fa516517ea7',
    '0.5/p': 'b55d0a36f3e0fd97b7c9ef8883420433cc1c5adfe509b62ca5bae97d620f066f',
}

CREATE = """
assert VF_load() == 'VF_file'
assert VF_list() == []
VF_time(0.23)
assert VF_surface('Contract surface', 'Temperature') == 'Contract surface'
assert VF_slice('Contract section', 'Temperature', 'X', 0.4) == 'Contract section'
assert VF_contour('Contract contour', 'Temperature', 26) == 'Contract contour'
assert VF_stream('Contract stream', 'U', 7) == 'Contract stream'
assert VF_glyph('Contract arrows', 'U', 0.45) == 'Contract arrows'
assert VF_list() == sorted(['Contract surface', 'Contract section', 'Contract contour',
                            'Contract stream', 'Contract arrows'])
"""
ADVANCE = """
VF_color('Contract surface', 'Tracer')
VF_time(0.93)
assert len(VF_list()) == 5
"""
CAPTURE = f"""
assert VF_shot({AWAY!r}).replace(chr(92), '/').endswith('/D-post/{AWAY}')
VF_reset_camera()
assert VF_shot({RESET!r}).replace(chr(92), '/').endswith('/D-post/{RESET}')
"""
INVALID = "VF_load('D-post/missing-facade-data')\nVF_remove('Contract surface')\n"
RECOVER = """
assert VF_list() == sorted(['Contract surface', 'Contract section', 'Contract contour',
                            'Contract stream', 'Contract arrows'])
VF_remove('Contract section')
VF_remove('Contract arrows')
assert VF_list() == ['Contract contour', 'Contract stream', 'Contract surface']
assert VF_slice('Recovered section', 'Temperature', 'X', 0.65) == 'Recovered section'
assert 'Recovered section' in VF_list()
"""
FOAM_LOAD = f"""
for item in VF_list():
    VF_remove(item)
assert VF_load({FOAM!r}) == 'VF_case'
VF_time(0.24)
assert VF_surface('Actual solved pressure', 'p') == 'Actual solved pressure'
assert VF_list() == ['Actual solved pressure']
"""


def fixture(root):
    import vtk

    records = []
    series = root / Path(SERIES).name
    for value, offset in ((0.2, 0), (1.0, 2)):
        data = vtk.vtkImageData()
        data.SetDimensions(13, 9, 6)
        data.SetSpacing(.25, .25, .25)
        temperature = vtk.vtkFloatArray()
        temperature.SetName('Temperature')
        tracer = vtk.vtkFloatArray()
        tracer.SetName('Tracer')
        velocity = vtk.vtkFloatArray()
        velocity.SetName('U')
        velocity.SetNumberOfComponents(3)
        for index in range(data.GetNumberOfPoints()):
            x, y, z = data.GetPoint(index)
            temperature.InsertNextValue(20 + 4 * x + offset)
            tracer.InsertNextValue(100 + 10 * y)
            velocity.InsertNextTuple3(0.06, 1 + 0.2 * x, 0.04 * z)
        data.GetPointData().SetScalars(temperature)
        data.GetPointData().AddArray(tracer)
        data.GetPointData().SetVectors(velocity)
        topology = vtk.vtkAppendFilter()
        topology.AddInputData(data)
        topology.Update()
        path = root / f'facade-{value:g}.vtu'
        writer = vtk.vtkXMLUnstructuredGridWriter()
        writer.SetInputConnection(topology.GetOutputPort())
        writer.SetFileName(str(path))
        assert writer.Write() == 1
        records.append({'path': path, 'name': path.name, 'time': value, 'cells': data.GetNumberOfCells(),
                        'temperature': temperature.GetRange(), 'tracer': tracer.GetRange(),
                        'velocity': velocity.GetRange(-1)})
    write_result(series, {'file-series-version': '1.0',
                           'files': [{k: record[k] for k in ('name', 'time')} for record in records]})
    return series, records


def browser(settings):
    from PIL import Image, ImageChops
    from playwright.sync_api import expect, sync_playwright

    root, base = Path(settings['root']), settings['base']
    headers = {'Authorization': 'Bearer ' + settings['token']}
    case = base + f"/api/v1/cases/{settings['case']}"
    result = {'passed': False, 'checks': [], 'model_requests': [], 'page_errors': [], 'outputs': {}}
    session = None
    phase = 'opening'
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        result['edge'] = edge.version
        context = edge.new_context(viewport={'width': 1536, 'height': 1040}, locale='zh-CN')
        context.add_init_script(f"if (location.origin === {json.dumps(base)}) localStorage.setItem('vf_token', {json.dumps(settings['token'])});")
        page = context.new_page()
        page.set_default_timeout(28_000)
        expect.set_options(timeout=20_000)
        page.on('pageerror', lambda error: result['page_errors'].append(
            {'phase': phase, 'message': str(error), 'stack': error.stack}))
        page.on('request', lambda req: result['model_requests'].append(urlsplit(req.url).path)
                if req.method == 'POST' and urlsplit(req.url).path.endswith(('/messages', '/turns', '/goal:run')) else None)
        inner = page.frame_locator('iframe[title="可视化"]')
        drawer = inner.locator('.v-navigation-drawer')
        state = inner.get_by_test_id('scene-script-status')
        info = inner.get_by_test_id('viz-information')

        def node(name):
            return inner.get_by_role('treeitem', name=name, exact=True)

        def field(name):
            return info.locator('[data-info-key=' + json.dumps(name, ensure_ascii=False) + ']')

        def raw(path):
            response = page.request.get(case + '/raw', headers=headers, params={'path': path})
            assert response.ok, (path, response.status)
            return response.body()

        def save(content):
            deadline = time.monotonic() + 6
            while True:
                response = page.request.put(case + '/file', headers=headers, data={'path': SCRIPT, 'content': content})
                detail = response.json().get('detail') if response.status == 409 else None
                if not isinstance(detail, dict) or detail.get('code') != 'storage_quota_busy':
                    break
                assert time.monotonic() < deadline, response.text()
                page.wait_for_timeout(180)
            assert response.ok and response.json()['content_sha256'] == hashlib.sha256(content.encode()).hexdigest(), response.text()

        def wait_log(marker):
            page.wait_for_function("""async ([url, headers, path, marker]) => {
                const response = await fetch(url + '/raw?path=' + encodeURIComponent(path), {headers, cache:'no-store'});
                return response.ok && (await response.text()).includes(marker);
            }""", arg=[case, headers, LOG, marker], timeout=18_000)

        def choose(name):
            node(name).click()
            expect(inner.get_by_test_id('viz-info-selection')).to_have_text(name)

        def read_image(path):
            data = raw('D-post/' + path)
            image = Image.open(io.BytesIO(data))
            assert image.format == 'PNG' and image.width >= 300 and image.height >= 300
            (root / Path(path).name).write_bytes(data)
            rgb = image.convert('RGB')
            colors = rgb.getcolors(rgb.width * rgb.height)
            assert colors
            return rgb, {'size': rgb.size, 'colors': len(colors), 'sha256': hashlib.sha256(data).hexdigest()}

        try:
            page.goto(base, wait_until='domcontentloaded')
            page.locator('[data-case-name=' + json.dumps(CASE) + ']').click()
            ref = page.locator('[data-path="Ref"]')
            if ref.get_attribute('aria-expanded') == 'false':
                ref.click()
            with page.expect_response(lambda reply: reply.request.method == 'POST' and reply.url == case + '/viz:sessions',
                                      timeout=55_000) as started:
                page.locator('[data-path=' + json.dumps(SERIES) + ']').click()
            assert started.value.ok, started.value.text()
            session = started.value.json()['id']
            expect(page.locator('iframe[title="可视化"]')).to_have_attribute('src', re.compile(f'^/viz-proxy/{session}/index.html'))
            expect(state).to_have_text('监听中')
            if 'v-navigation-drawer--active' not in (drawer.get_attribute('class') or '').split():
                inner.locator('.v-app-bar-nav-icon').click()
            expect(inner.locator('main canvas:visible').first).to_be_visible()

            phase = 'creating_filters'
            save(CREATE)
            expect(state).to_have_text('已执行 · 5 个对象')
            for name in NAMES:
                expect(node(name).locator('..')).to_have_attribute('data-object-status', 'visible')
                choose(name)
                points = int(field('点').inner_text().replace(',', ''))
                cells = int(field('单元').inner_text().replace(',', ''))
                assert points > 0 and cells > 0, (name, points, cells)
                result['outputs'][name] = {'points': points, 'cells': cells}
            choose('Contract section')
            expect(field('范围 X')).to_have_text('[1.2, 1.2]')
            choose('Contract surface')
            expect(field('时刻')).to_have_text('0.2')
            arrays = inner.get_by_test_id('viz-info-arrays')
            arrays.locator('summary').click()
            temperature = arrays.get_by_text('Temperature（点）', exact=True).locator('..')
            expect(temperature).to_contain_text('[20, 32]')
            expect(drawer.get_by_role('combobox', name='染色场')).to_have_value('Temperature')
            result['checks'].append('load_list_surface_slice_contour_stream_glyph_produce_real_vtk_cells')

            phase = 'changing_scalar_and_time'
            save(ADVANCE)
            expect(field('时刻')).to_have_text('1')
            expect(temperature).to_contain_text('[22, 34]')
            choose('Contract stream')
            choose('Contract surface')
            expect(drawer.get_by_role('combobox', name='染色场')).to_have_value('Tracer')
            assert '~Contract surface color Tracer' in raw(LOG).decode('utf8')
            result['checks'].append('color_rebuild_and_nearest_time_change_real_scalar_data')

            phase = 'resetting_camera'
            camera = {'schema': 'vibeflow.animation.camera.v1', 'frames': [
                {'t': t, 'position': [25, 16, 6], 'focal': [25, 16, 0], 'up': [0, 1, 0]} for t in (0, 1)
            ]}
            drawer.get_by_role('textbox', name='关键帧(JSON)').fill(json.dumps(camera))
            drawer.get_by_role('button', name='应用轨道', exact=True).click()
            expect(drawer.get_by_text('已应用 2 个关键帧', exact=True)).to_be_visible()
            save(CAPTURE)
            wait_log('screenshot -> D-post/' + RESET)
            away, result['away'] = read_image(AWAY)
            reset, result['reset'] = read_image(RESET)
            difference = ImageChops.difference(away, reset)
            assert result['reset']['colors'] > 20 and difference.getbbox() is not None
            result['checks'].append('reset_camera_reframes_actual_geometry_and_shot_persists_png')

            phase = 'recovering_from_invalid_load'
            save(INVALID)
            wait_log('missing-facade-data')
            expect(state).to_contain_text('执行失败')
            prior = raw(LOG).decode('utf8')
            assert '不是有效 OpenFOAM 算例目录' in prior and 'exec FAILED' in prior
            expect(node('Contract surface')).to_be_visible()
            save(RECOVER)
            expect(state).to_have_text('已执行 · 4 个对象')
            expect(node('Contract section')).to_have_count(0)
            expect(node('Contract arrows')).to_have_count(0)
            expect(node('Recovered section')).to_be_visible()
            choose('Recovered section')
            expect(field('范围 X')).to_have_text('[1.95, 1.95]')
            log = raw(LOG).decode('utf8')
            assert log.startswith(prior) and 'exec ok' in log[len(prior):] and '-Contract section' in log[len(prior):]
            (root / 'native-facade.log').write_text(log, encoding='utf8')
            result['checks'].append('invalid_load_preserves_live_objects_then_remove_and_recreate_recovers')
            page.screenshot(path=str(root / 'vf-facade-workbench.png'))

            phase = 'loading_openfoam'
            save(FOAM_LOAD)
            expect(state).to_have_text('已执行 · 1 个对象')
            expect(node('Actual solved pressure').locator('..')).to_have_attribute('data-object-status', 'visible')
            choose('Actual solved pressure')
            expect(field('时刻')).to_have_text('0.2')
            expect(field('单元')).to_have_text(re.compile(r'^[1-9][\d,]*$'))
            loaded = raw(LOG).decode('utf8')
            assert 'times=[0.2, 0.5]' in loaded[len(log):] and 'time -> 0.2' in loaded[len(log):]
            (root / 'native-facade.log').write_text(loaded, encoding='utf8')
            result['checks'].append('explicit_openfoam_load_reads_pinned_original_icofoam_pressure')

            phase = 'closing'
            url = base + f'/api/v1/viz:sessions/{session}'
            with page.expect_response(lambda reply: reply.request.method == 'DELETE' and reply.url == url,
                                      timeout=45_000) as stopped:
                page.get_by_role('button', name=f'关闭可视化会话 {session}', exact=True).click()
            assert stopped.value.ok, stopped.value.text()
            record = page.request.get(url, headers=headers).json()
            assert record['phase'] == 'closed' and record['stop']['verified'] and record['stop']['scope_empty'], record
            result['stop'] = record['stop']
            session = None
            assert not result['page_errors'] and not result['model_requests'], result
            result['passed'] = True
        finally:
            if not result['passed']:
                page.screenshot(path=str(root / 'vf-facade-failure.png'))
            if session is not None:
                phase = 'failure_cleanup'
                page.request.delete(base + f'/api/v1/viz:sessions/{session}', headers=headers)
            write_result(root / 'vf-facade-browser.json', result)
            edge.close()


def verify(args):
    import httpx

    server = IsolatedServer('viz-facade-d10', app='vf_facade_fixture:app')
    root = server.root
    scratch = root / 'native-tmp'
    scratch.mkdir()
    dist = args.dist.resolve(strict=True)
    env = dict(os.environ, TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch), PYTHONIOENCODING='utf8')
    (root / 'vf_facade_fixture.py').write_text('import os\nfrom fastapi.staticfiles import StaticFiles\n'
        f'os.environ.update(TEMP={str(scratch)!r}, TMP={str(scratch)!r}, TMPDIR={str(scratch)!r})\nfrom app.main import app\n'
        f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n', encoding='utf8')
    target = root / 'vf-facade-result.json'
    result = {'passed': False, 'model_calls': 0}
    print('VIZ_FACADE_EVIDENCE', root, flush=True)
    try:
        series, records = fixture(root)
        result['native_fixture'] = [{key: value for key, value in item.items() if key != 'path'} for item in records]
        foam = args.foam_case.resolve(strict=True)
        inputs = {relative: foam / relative for relative in FOAM_INPUTS}
        for relative, source in inputs.items():
            with source.open('rb') as stream:
                assert hashlib.file_digest(stream, 'sha256').hexdigest() == FOAM_INPUTS[relative], relative
        result['openfoam_inputs'] = FOAM_INPUTS
        with server as api:
            case = api.create_case(CASE)
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                              trust_env=False, timeout=35) as client:
                for item in records:
                    upload(api, client, item['path'], 'Ref/' + item['name'])
                upload(api, client, series, SERIES)
                for relative, source in inputs.items():
                    upload(api, client, source, FOAM + '/' + relative)
            other = api.call('POST', '/auth/register', {'name': 'Other facade owner', 'email': 'other-facade@example.com',
                                                        'password': 'isolated-test-123'})
            foreign = {'Authorization': 'Bearer ' + other['token']}
            api.call('PUT', api.prefix + '/file', {'path': SCRIPT, 'content': CREATE}, expect=404, extra_headers=foreign)
            api.call('POST', api.prefix + '/viz:sessions', {'file_path': SERIES}, expect=404, extra_headers=foreign)
            result['other_account_http'] = [404, 404]
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', __file__, '--browser'],
                input=json.dumps({'root':str(root), 'case':case['id'], 'base':api.base, 'token':api.token}),
                text=True, encoding='utf8', check=True, timeout=220, cwd=root, env=env)
            result['browser'] = json.loads((root / 'vf-facade-browser.json').read_text(encoding='utf8'))
            assert result['browser']['passed']
        result['api_exit_code'] = server.proc.poll()
        assert result['api_exit_code'] is not None
        result['passed'] = True
    except BaseException as error:
        result['failure'] = {'type':type(error).__name__, 'message':str(error)[:1000]}
        raise
    finally:
        write_result(target, result)
    print('VIZ_FACADE_PASS', target, flush=True)


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
        parser.add_argument('--foam-case', type=Path, required=True)
        verify(parser.parse_args())
