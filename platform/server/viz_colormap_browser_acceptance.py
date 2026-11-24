"""Exercise a real scalar field, VTK color/opacity and a ParaView XML preset."""
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


CASE = 'Actual scalar color map'
FIELD = 'Ref/scalar-plane.vtp'
JSON_PATH = 'D-post/colormap.json'
XML_PATH = 'D-post/colormap.xml'
IMAGE_PATH = 'D-post/figures/view-preset.png'
PRESET = 'VibeFlow D06 Lab exchange'
XML = f'''<ColorMaps><ColorMap name="{PRESET}" space="Lab" indexedLookup="false">
<Point x="2" r="1" g="0" b="0" o="0.2"/>
<Point x="8" r="0" g="1" b="0" o="0.8"/>
</ColorMap></ColorMaps>'''
POINTS = [{'position': 0, 'color': '#FF0000'}, {'position': 1, 'color': '#00FF00'}]


def fixture(path):
    import vtk

    plane = vtk.vtkPlaneSource()
    plane.SetXResolution(126)
    plane.SetYResolution(4)
    plane.Update()
    data = plane.GetOutput()
    gradient = vtk.vtkFloatArray()
    gradient.SetName('Temperature')
    for index in range(data.GetNumberOfPoints()):
        gradient.InsertNextValue(5 + data.GetPoint(index)[0] * 6)
    data.GetPointData().SetScalars(gradient)
    writer = vtk.vtkXMLPolyDataWriter()
    writer.SetInputData(data)
    writer.SetFileName(str(path))
    assert writer.Write() == 1
    return {'points': data.GetNumberOfPoints(), 'cells': data.GetNumberOfCells(), 'range': gradient.GetRange()}


def browser(settings):
    from PIL import Image
    from playwright.sync_api import expect, sync_playwright

    root, base = Path(settings['root']), settings['base']
    headers = {'Authorization': 'Bearer ' + settings['token']}
    api = base + '/api/v1'
    case = api + f"/cases/{settings['case']}"
    report = {'passed': False, 'model_requests': [], 'page_errors': [], 'native_frames': {}}
    session = None
    with sync_playwright() as driver:
        edge = driver.chromium.launch(channel='msedge')
        report['edge'] = edge.version
        context = edge.new_context(viewport={'width': 1480, 'height': 1020}, locale='zh-CN')
        context.add_init_script(f"if (location.origin === {json.dumps(base)}) localStorage.setItem('vf_token', {json.dumps(settings['token'])});")
        page = context.new_page()
        page.set_default_timeout(28_000)
        page.on('pageerror', lambda error: report['page_errors'].append(str(error)))
        page.on('request', lambda req: report['model_requests'].append(urlsplit(req.url).path)
                if req.method == 'POST' and urlsplit(req.url).path.endswith(('/messages', '/turns', '/goal:run')) else None)
        frame = page.frame_locator('iframe[title="可视化"]')
        preview = frame.get_by_test_id('colormap-preview')
        status = frame.get_by_test_id('colormap-status')
        points = frame.get_by_role('textbox', name='控制点(JSON)', exact=True)

        def select(label, value):
            control = frame.get_by_role('combobox', name=label, exact=True)
            field = control.locator('xpath=ancestor::*[contains(concat(" ", normalize-space(@class), " "), " v-select ")][1]')
            field.locator('.v-field').click()
            frame.locator('.v-overlay--active .v-list-item-title').filter(
                has_text=re.compile('^' + re.escape(value) + '$')).click()

        def gradient():
            return preview.evaluate('(node) => node.style.backgroundImage')

        def assert_three_bands():
            rendered = gradient()
            assert len(set(re.findall(r'rgba\([^)]*\)', rendered))) == 3, rendered
            assert all(mark in rendered for mark in ('33.33%', '66.67%')), rendered

        def choose_button(label):
            frame.get_by_role('button', name=label, exact=True).click()

        def raw(path):
            response = page.request.get(case + '/raw', headers=headers, params={'path': path})
            assert response.ok, (path, response.status)
            return response.body()

        def capture(label):
            previous = page.request.get(case + '/raw', headers=headers, params={'path': IMAGE_PATH})
            prior_data = previous.body() if previous.ok else None
            choose_button('截图')
            expect(frame.get_by_text('已截图 ' + IMAGE_PATH, exact=True)).to_be_visible()
            until = time.monotonic() + 10
            while True:
                data = raw(IMAGE_PATH)
                if data != prior_data:
                    break
                assert time.monotonic() < until, 'Native screenshot did not change after applying the color map'
                page.wait_for_timeout(150)
            (root / (label + '.png')).write_bytes(data)
            image = Image.open(io.BytesIO(data)).convert('RGB')
            y = image.height // 2
            samples = [image.getpixel((round((.35 + index * .05) * image.width), y)) for index in range(7)]
            report['native_frames'][label] = {'sha256': hashlib.sha256(data).hexdigest(), 'center_row': samples}
            return samples

        def check_displayed_bands(reference):
            canvas = frame.locator('main canvas:visible').first
            expect(canvas).to_be_visible()
            deadline = time.monotonic() + 12
            while True:
                image = Image.open(io.BytesIO(canvas.screenshot())).convert('RGB')
                samples = [image.getpixel((round(x * image.width), image.height // 2)) for x in (.35, .50, .65)]
                delta = max(abs(actual - expected) for pixel, target in zip(samples, reference)
                            for actual, expected in zip(pixel, target))
                if delta <= 12:
                    report['displayed_rgb_delta'] = delta
                    return
                assert time.monotonic() < deadline, (samples, reference)
                page.wait_for_timeout(180)

        try:
            page.goto(base, wait_until='domcontentloaded')
            row = page.locator('[data-case-name=' + json.dumps(CASE) + ']')
            if row.get_attribute('aria-pressed') != 'true':
                row.click()
            folder = page.locator('[data-path="Ref"]')
            if folder.get_attribute('aria-expanded') == 'false':
                folder.click()
            with page.expect_response(lambda reply: reply.request.method == 'POST' and reply.url == case + '/viz:sessions',
                                      timeout=50_000) as opened:
                page.locator('[data-path=' + json.dumps(FIELD) + ']').click()
            assert opened.value.ok, opened.value.text()
            session = opened.value.json()['id']
            expect(page.locator('iframe[title="可视化"]')).to_have_attribute('src', re.compile(f'^/viz-proxy/{session}/index.html'))
            expect(preview).to_have_attribute('style', re.compile('linear-gradient'), timeout=22000)
            drawer = frame.locator('.v-navigation-drawer')
            drawer_open = re.compile(r'\bv-navigation-drawer--active\b')
            if not drawer_open.search(drawer.get_attribute('class') or ''):
                frame.locator('.v-app-bar-nav-icon').click()
            expect(drawer).to_have_class(drawer_open)
            select('预设', 'custom')
            points.fill(json.dumps(POINTS))
            choose_button('应用控制点')
            expect(status).to_have_text('已应用 2 个控制点')
            choose_button('导出')
            expect(status).to_have_text('已导出 ' + JSON_PATH)
            assert json.loads(raw(JSON_PATH)) == {'schema': 'vibeflow.colormap.v1', 'points': POINTS}
            v1 = capture('native-v1-rgb')

            advanced = {'schema': 'vibeflow.colormap.v2', 'points': POINTS, 'space': 'RGB', 'levels': 0,
                        'opacity_points': [{'position': 0, 'opacity': 0.2}, {'position': 1, 'opacity': 0.8}]}
            points.fill(json.dumps(advanced))
            choose_button('应用控制点')
            expect(status).to_have_text('已应用 2 个控制点')
            alpha = capture('native-opacity-ramp')
            background = (15, 18, 23)
            errors = []
            for opaque, blended in zip(v1, alpha):
                opacity = 0.2 + 0.6 * opaque[1] / 255
                expected = [round(original * opacity + back * (1 - opacity)) for original, back in zip(opaque, background)]
                errors.extend(abs(actual - estimate) for actual, estimate in zip(blended, expected))
            assert max(errors) <= 3, (v1, alpha, errors)
            report['max_opacity_blending_error'] = max(errors)
            assert '0.2)' in gradient() and '0.8)' in gradient(), gradient()

            select('颜色空间', 'HSV')
            frame.get_by_role('spinbutton', name='色阶（0 连续）', exact=True).fill('3')
            choose_button('应用映射设置')
            expect(status).to_have_text('已应用映射设置')
            assert_three_bands()
            discrete = capture('native-hsv-three-bands')
            assert discrete != alpha and 3 <= len(set(discrete)) <= 4, discrete
            frame.get_by_role('button', name='应用映射设置', exact=True).scroll_into_view_if_needed()
            check_displayed_bands((discrete[0], discrete[3], discrete[-1]))
            page.screenshot(path=str(root / 'colormap-workbench.png'))
            choose_button('导出')
            expect(status).to_have_text('已导出 ' + JSON_PATH)
            exported = json.loads(raw(JSON_PATH))
            assert exported == {**advanced, 'space': 'HSV', 'levels': 3}, exported
            choose_button('导出 ParaView XML')
            expect(status).to_have_text('已导出 ' + XML_PATH)
            rgba_export = raw(XML_PATH)
            assert b'o="0.2"' in rgba_export and b'o="0.8"' in rgba_export
            restored = page.request.put(case + '/file', headers=headers, data={'path': XML_PATH, 'content': XML})
            assert restored.ok, restored.text()
            choose_button('默认')
            choose_button('导入')
            expect(status).to_have_text('已导入 ' + JSON_PATH)
            assert_three_bands()
            current = gradient()
            points.fill('{}')
            choose_button('应用控制点')
            expect(status).to_contain_text('控制点错误')
            assert gradient() == current
            page.screenshot(path=str(root / 'colormap-recovery.png'))

            choose_button('导入 ParaView XML')
            expect(status).to_have_text('已导入 ' + XML_PATH)
            imported = json.loads(points.input_value())
            assert imported['name'] == PRESET and imported['space'] == 'Lab'
            assert [point['position'] for point in imported['points']] == [0, 1]
            assert [point['opacity'] for point in imported['points']] == [0.2, 0.8]
            assert capture('native-paraview-lab') != discrete
            choose_button('导出 ParaView XML')
            expect(status).to_have_text('已导出 ' + XML_PATH)
            (root / 'export.xml').write_bytes(raw(XML_PATH))
            current = gradient()
            invalid = page.request.put(case + '/file', headers=headers, data={'path': XML_PATH, 'content': '<broken>'})
            assert invalid.ok, invalid.text()
            choose_button('导入 ParaView XML')
            expect(status).to_contain_text('导入失败')
            assert gradient() == current
            assert json.loads(points.input_value()) == imported

            endpoint = api + f'/viz:sessions/{session}'
            with page.expect_response(lambda reply: reply.request.method == 'DELETE' and reply.url == endpoint,
                                      timeout=45_000) as stopped:
                page.get_by_role('button', name=f'关闭可视化会话 {session}', exact=True).click()
            assert stopped.value.ok, stopped.value.text()
            record = page.request.get(endpoint, headers=headers).json()
            assert record['phase'] == 'closed' and record['stop']['verified'] and record['stop']['scope_empty'], record
            report['stopped'] = record['stop']
            session = None
            assert not report['page_errors'] and not report['model_requests'], report
            report['passed'] = True
        finally:
            if not report['passed']:
                page.screenshot(path=str(root / 'colormap-failure.png'))
            if session is not None:
                page.request.delete(api + f'/viz:sessions/{session}', headers=headers)
            (root / 'colormap-browser.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
            edge.close()


def verify(args):
    import httpx
    from app.services.colormap import normalize_colormap, sample_colormap
    from testsupport import IsolatedServer
    from viz_information_browser_acceptance import upload, write_result

    api = IsolatedServer('viz-colormap-d06', app='colormap_fixture:app')
    root = api.root
    scratch = root / 'native-tmp'
    scratch.mkdir()
    env = dict(os.environ, TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch), PYTHONIOENCODING='utf8')
    print('VIZ_COLORMAP_EVIDENCE', root, flush=True)
    (root / 'colormap_fixture.py').write_text(
        'import os\nfrom fastapi.staticfiles import StaticFiles\n'
        f"os.environ.update(TEMP={str(scratch)!r}, TMP={str(scratch)!r})\nfrom app.main import app\n"
        f"app.mount('/', StaticFiles(directory={str(args.dist.resolve(strict=True))!r}, html=True))\n", encoding='utf8')
    source = root / Path(FIELD).name
    metadata = {'passed': False, 'native_fixture': fixture(source), 'model_calls': 0}
    try:
        with api:
            case = api.create_case(CASE)
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                              trust_env=False, timeout=30) as client:
                upload(api, client, source, FIELD)
            api.put(XML_PATH, XML)
            outsider = api.call('POST', '/auth/register', {'name': 'Other viewer', 'email': 'd06-other@example.com',
                                                          'password': 'isolated-test-123'})
            foreign = {'Authorization': 'Bearer ' + outsider['token']}
            api.call('POST', api.prefix + '/viz:sessions', {'file_path': FIELD}, expect=404, extra_headers=foreign)
            metadata['other_account_http'] = 404
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', __file__, '--browser'],
                           input=json.dumps({'root': str(root), 'case': case['id'], 'base': api.base, 'token': api.token}),
                           text=True, encoding='utf8', check=True, timeout=260, cwd=root, env=env)
            metadata['browser'] = json.loads((root / 'colormap-browser.json').read_text(encoding='utf8'))
            assert metadata['browser']['passed']
        assert api.proc.poll() is not None
        pv_check = """
import json, sys
from paraview.simple import GetColorTransferFunction, ImportPresets
from vtkmodules.vtkRenderingCore import vtkColorTransferFunction
assert ImportPresets(sys.argv[1])
lut = GetColorTransferFunction('VibeFlowD06Verification')
assert lut.ApplyPreset(sys.argv[2], False)
native = lut.GetClientSideObject()
reference = vtkColorTransferFunction()
reference.SetColorSpaceToLab()
assert native.GetColorSpace() == reference.GetColorSpace()
middle = [0.0, 0.0, 0.0]
native.GetColor(0.5, middle)
print(json.dumps({'space': 'Lab', 'points': list(lut.RGBPoints), 'middle': middle}))
"""
        pv_entry = root / 'colormap-paraview-check.py'
        pv_entry.write_text(pv_check, encoding='utf8')
        pv_result = subprocess.run([str(args.pvpython.resolve(strict=True)), str(pv_entry), str(root / 'export.xml'), PRESET],
                                   capture_output=True, text=True, check=True, timeout=80, cwd=root, env=env)
        metadata['paraview'] = json.loads(pv_result.stdout.strip().splitlines()[-1])
        assert metadata['paraview']['space'] == 'Lab' and len(metadata['paraview']['points']) == 8
        expected = sample_colormap(normalize_colormap({'schema': 'vibeflow.colormap.v2', 'points': POINTS,
                                                       'space': 'Lab', 'levels': 3}))[1][:3]
        assert max(abs(a-b) for a, b in zip(metadata['paraview']['middle'], expected)) < .012, metadata['paraview']
        metadata['isolated_api_exited'] = True
        metadata['passed'] = True
    finally:
        write_result(root / 'colormap-result.json', metadata)
    print('VIZ_COLORMAP_PASS', root / 'colormap-result.json', flush=True)


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
        parser.add_argument('--pvpython', type=Path, required=True)
        verify(parser.parse_args())
