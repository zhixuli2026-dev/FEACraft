"""Verify persistent camera bookmarks and saved PNGs through Edge and native VTK."""
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


CASE = 'Camera bookmarks and retained captures'
WING = 'Ref/camera-wing.vtp'
BRACKET = 'Ref/camera-bracket.vtp'
X_VIEW = '侧面 X'
ISO_VIEW = '斜视角'
BRACKET_VIEW = '支架正视'
LATEST = 'D-post/figures/view-preset.png'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def view_file(source, name):
    return f"D-post/camera-views/{digest(source.encode())}/{digest(name.casefold().encode())}.json"


def native_fixture(path, scale):
    import vtk

    base = vtk.vtkCubeSource()
    base.SetXLength(3 * scale)
    base.SetYLength(1.2 * scale)
    base.SetZLength(0.4 * scale)
    feature = vtk.vtkSphereSource()
    feature.SetRadius(0.45 * scale)
    feature.SetThetaResolution(15)
    feature.SetPhiResolution(10)
    feature.SetCenter(0.9 * scale, 0.46 * scale, 0.38 * scale)
    shape = vtk.vtkAppendPolyData()
    shape.AddInputConnection(base.GetOutputPort())
    shape.AddInputConnection(feature.GetOutputPort())
    shape.Update()
    data = shape.GetOutput()
    field = vtk.vtkFloatArray()
    field.SetName('Temperature')
    for point in range(data.GetNumberOfPoints()):
        x, y, z = data.GetPoint(point)
        field.InsertNextValue(3 * x + y + z)
    data.GetPointData().SetScalars(field)
    writer = vtk.vtkXMLPolyDataWriter()
    writer.SetInputData(data)
    writer.SetFileName(str(path))
    assert writer.Write() == 1
    return {'points':data.GetNumberOfPoints(), 'cells':data.GetNumberOfCells(), 'bounds':data.GetBounds()}


def browser(settings):
    from PIL import Image, ImageChops
    from playwright.sync_api import TimeoutError as PlaywrightTimeout, expect, sync_playwright

    root, base = Path(settings['root']), settings['base']
    headers = {'Authorization':'Bearer ' + settings['token']}
    foreign = {'Authorization':'Bearer ' + settings['foreign']}
    api = base + '/api/v1'
    case = api + f"/cases/{settings['case']}"
    opened = set()
    result = {'passed':False, 'page_errors':[], 'rejections':[], 'sockets':[], 'raw_timeouts':[],
              'model_requests':[], 'shots':{}, 'stops':{}, 'checks':[]}
    phase = 'start'
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        result['edge'] = edge.version
        context = edge.new_context(viewport={'width':1660, 'height':1110}, locale='zh-CN')
        context.add_init_script(f"if (location.origin === {json.dumps(base)}) localStorage.setItem('vf_token', {json.dumps(settings['token'])});")
        context.expose_binding('observeCameraRejection', lambda source, value:
            result['rejections'].append({'phase':phase, 'frame':urlsplit(source['frame'].url).path, **value}))
        context.add_init_script("""window.addEventListener('unhandledrejection', ({reason}) => {
            const summary = (value) => Object.fromEntries(['name', 'message', 'error', 'code', 'type']
                .filter(key => ['string', 'number'].includes(typeof value?.[key]))
                .map(key => [key, String(value[key]).slice(0, 200)]));
            window.observeCameraRejection({path:location.pathname, kind:Object.prototype.toString.call(reason),
                keys:reason && typeof reason === 'object' ? Object.keys(reason) : [],
                detail:summary(reason), response:summary(reason?.response)}).catch(() => {});
        });""")
        page = context.new_page()
        page.set_default_timeout(28_000)
        expect.set_options(timeout=20_000)
        page.on('pageerror', lambda error: result['page_errors'].append({'phase':phase, 'message':str(error), 'stack':error.stack}))
        page.on('request', lambda req: result['model_requests'].append(urlsplit(req.url).path)
                if req.method == 'POST' and urlsplit(req.url).path.endswith(('/messages','/turns','/goal:run')) else None)

        def socket_open(socket):
            endpoint = urlsplit(socket.url).path
            if endpoint.startswith('/viz-proxy/'):
                result['sockets'].append({'phase':phase, 'path':endpoint, 'event':'open'})
                socket.on('close', lambda _:result['sockets'].append({'phase':phase, 'path':endpoint, 'event':'close'}))

        page.on('websocket', socket_open)
        iframe = page.locator('iframe[title="可视化"]')
        inner = page.frame_locator('iframe[title="可视化"]')
        drawer = inner.locator('.v-navigation-drawer')
        status = inner.get_by_test_id('viz-camera-status')
        name_input = drawer.get_by_role('textbox', name='书签名称')
        bookmark = drawer.get_by_role('combobox', name='视角书签')

        def button(text):
            return drawer.get_by_role('button', name=text, exact=True)

        def raw(path, *, account=headers, expected=200):
            for _ in range(2):
                try:
                    response = page.request.get(case + '/raw', headers=account, params={'path':path}, timeout=14_000)
                except PlaywrightTimeout:
                    result['raw_timeouts'].append({'phase':phase, 'path':path})
                    continue
                assert response.status == expected, (path, response.status)
                return response.body()
            raise AssertionError(f'Raw file was unreachable during two read attempts: {path}')

        def write(path, content):
            deadline = time.monotonic() + 7
            while True:
                response = page.request.put(case + '/file', headers=headers, data={'path':path, 'content':content})
                detail = response.json().get('detail') if response.status == 409 else None
                if not isinstance(detail, dict) or detail.get('code') != 'storage_quota_busy':
                    break
                assert time.monotonic() < deadline
                page.wait_for_timeout(200)
            assert response.ok, response.text()

        def ready(sid, source):
            expect(iframe).to_have_attribute('src', re.compile(f'^/viz-proxy/{sid}/index.html'))
            expect(inner.locator('main canvas:visible').first).to_be_visible()
            if 'v-navigation-drawer--active' not in (drawer.get_attribute('class') or '').split():
                inner.locator('.v-app-bar-nav-icon').click()
            expect(inner.get_by_test_id('viz-info-selection')).to_have_text(Path(source).name)
            expect(name_input).to_be_visible()

        def start(source):
            folder = page.locator('[data-path="Ref"]')
            if folder.get_attribute('aria-expanded') == 'false':
                folder.click()
            with page.expect_response(lambda response: response.request.method == 'POST' and response.url == case + '/viz:sessions',
                                      timeout=65_000) as started:
                page.locator('[data-path=' + json.dumps(source) + ']').click()
            assert started.value.ok, started.value.text()
            sid = started.value.json()['id']
            opened.add(sid)
            ready(sid, source)
            return sid

        def session_button(sid):
            return page.get_by_role('button', name=f'关闭可视化会话 {sid}', exact=True)

        def activate(sid, source):
            session_button(sid).locator('..').get_by_role('button', name='打开', exact=True).click()
            ready(sid, source)

        def close(sid):
            route = api + f'/viz:sessions/{sid}'
            with page.expect_response(lambda response: response.request.method == 'DELETE' and response.url == route,
                                      timeout=50_000) as closed:
                session_button(sid).click()
            assert closed.value.ok, closed.value.text()
            record = page.request.get(route, headers=headers).json()
            assert record['phase'] == 'closed' and record['stop']['verified'] and record['stop']['scope_empty'], record
            result['stops'][str(sid)] = record['stop']
            opened.remove(sid)

        def save(name, keyboard=False):
            name_input.fill(name)
            if keyboard:
                name_input.press('Enter')
            else:
                button('保存书签').click()
            expect(status).to_have_text('已保存视角书签 ' + name)

        def choose(name):
            field = bookmark.locator('xpath=ancestor::*[contains(concat(" ", normalize-space(@class), " "), " v-select ")][1]')
            field.locator('.v-field').click()
            inner.locator('.v-overlay--active .v-list-item-title').filter(has_text=re.compile('^' + re.escape(name) + '$')).click()
            expect(bookmark).to_have_value(name)

        def apply(name):
            choose(name)
            button('应用书签').click()
            expect(status).to_have_text('已应用视角书签 ' + name)

        def screenshot(label, retained=True):
            previous = status.text_content() if status.count() else None
            button('保留截图' if retained else '截图').click()
            if previous and retained:
                expect(status).not_to_have_text(previous)
            pattern = (r'^已保留截图 (D-post/figures/camera-[\d-]+-[a-f0-9]{12}\.png)$' if retained
                       else rf'^已截图 ({re.escape(LATEST)})$')
            expect(status).to_have_text(re.compile(pattern))
            path = re.fullmatch(pattern, status.inner_text()).group(1)
            data = raw(path)
            png = Image.open(io.BytesIO(data))
            assert png.format == 'PNG' and png.width >= 300 and png.height >= 300
            pixels = png.convert('RGB')
            colors = len(pixels.getcolors(pixels.width * pixels.height))
            assert colors > 8, (label, colors)
            result['shots'][label] = {'path':path, 'sha256':digest(data), 'size':pixels.size, 'colors':colors}
            (root / (label + '.png')).write_bytes(data)
            return pixels

        def same_frame(first, second):
            a, b = result['shots'][first], result['shots'][second]
            assert a['size'] == b['size'] and a['sha256'] == b['sha256'], (a, b)

        try:
            page.goto(base, wait_until='domcontentloaded')
            page.locator('[data-case-name=' + json.dumps(CASE) + ']').click()
            wing = start(WING)
            assert page.request.get(api + f'/viz:sessions/{wing}', headers=foreign).status == 404
            expect(button('应用书签')).to_be_disabled()
            button('保存书签').click()
            expect(drawer.get_by_text('书签名称须为 1–64 个可显示字符', exact=True)).to_be_visible()
            result['checks'].append('empty_name_reports_inline_and_empty_collection_disables_apply')

            phase = 'presets_and_bookmarks'
            button('+X').click()
            expect(status).to_have_text('视角 +X')
            save(X_VIEW, keyboard=True)
            view = view_file(WING, X_VIEW)
            initial_view = raw(view)
            native = json.loads(initial_view)
            assert native['schema'] == 'vibeflow.viz.camera-view.v1' and native['name'] == X_VIEW
            assert len(native['position']) == len(native['focal']) == len(native['up']) == 3
            assert type(native['parallel']) is bool and native['scale'] > 0 and 0 < native['angle'] < 180
            front = screenshot('wing-x')
            screenshot('legacy-latest', retained=False)
            button('+Y').click()
            expect(status).to_have_text('视角 +Y')
            button('+Z').click()
            expect(status).to_have_text('视角 +Z')
            button('等轴测').click()
            expect(status).to_have_text('视角 iso')
            save(ISO_VIEW)
            iso = screenshot('wing-iso')
            assert iso.size == front.size and ImageChops.difference(front, iso).getbbox() is not None
            name_input.fill('侧面 x')
            button('保存书签').click()
            expect(drawer.get_by_text('同名书签已存在', exact=True)).to_be_visible()
            assert raw(view) == initial_view
            result['checks'].append('unique_named_pose_and_previous_captures_are_not_overwritten')

            apply(X_VIEW)
            screenshot('wing-x-restored')
            same_frame('wing-x', 'wing-x-restored')
            button('撤回视角').click()
            expect(status).to_have_text('已撤回视角操作')
            expect(button('撤回视角')).to_be_disabled()
            screenshot('wing-iso-after-undo')
            same_frame('wing-iso', 'wing-iso-after-undo')
            result['checks'].append('named_restore_and_undo_return_identical_native_pngs')

            phase = 'paraview'
            inner.get_by_test_id('viz-paraview-information').click()
            native_status = inner.get_by_test_id('viz-paraview-status')
            expect(native_status).to_have_text(re.compile(r'^ParaView [\d.]+ · camera-wing\.vtp$'), timeout=36_000)
            result['paraview'] = native_status.inner_text()

            phase = 'opening_second_source'
            bracket = start(BRACKET)
            assert bracket != wing
            expect(button('应用书签')).to_be_disabled()
            button('+Y').click()
            expect(status).to_have_text('视角 +Y')
            save(BRACKET_VIEW)
            screenshot('bracket-y')
            simultaneous = page.request.get(case + '/viz:sessions', headers=headers).json()
            assert {record['id'] for record in simultaneous if record['alive'] is True} == {wing, bracket}, simultaneous
            assert raw(view_file(BRACKET, BRACKET_VIEW)) != initial_view
            phase = 'reattaching_first_source'
            activate(wing, WING)
            choose(ISO_VIEW)
            assert bookmark.input_value() != BRACKET_VIEW
            assert raw(view) == initial_view
            phase = 'closing_detached_second_source'
            close(bracket)
            phase = 'closing_attached_first_source'
            close(wing)
            for label in ('wing-x', 'wing-iso', 'wing-x-restored', 'wing-iso-after-undo', 'bracket-y', 'legacy-latest'):
                capture = result['shots'][label]
                assert digest(raw(capture['path'])) == capture['sha256'], label
            assert len({capture['path'] for capture in result['shots'].values()}) == len(result['shots'])
            raw(view, account=foreign, expected=404)
            raw(result['shots']['wing-x']['path'], account=foreign, expected=404)
            result['checks'].append('two_owned_native_sessions_have_independent_books_and_retained_files')

            phase = 'reopen_and_recover'
            write(view, '{invalid bookmark')
            reopened = start(WING)
            assert reopened not in {wing, bracket}
            expect(status).to_have_text('有 1 个视角书签无法读取')
            apply(ISO_VIEW)
            screenshot('wing-iso-reopened')
            same_frame('wing-iso', 'wing-iso-reopened')
            write(view, initial_view.decode('utf8'))
            button('重读书签').click()
            expect(status).to_have_text('已读取 2 个视角书签')
            apply(X_VIEW)
            screenshot('wing-x-recovered')
            same_frame('wing-x', 'wing-x-recovered')
            page.screenshot(path=str(root / 'camera-bookmarks-workbench.png'))
            close(reopened)
            assert raw(view) == initial_view
            result['checks'].append('reopen_restores_saved_pose_and_recovers_corrupt_record_without_losing_other_bookmarks')
            assert not result['page_errors'] and not result['rejections'] and not result['model_requests'], result
            result['passed'] = True
        finally:
            if not result['passed']:
                page.screenshot(path=str(root / 'camera-bookmarks-failure.png'))
            phase = 'failure_cleanup'
            for sid in opened:
                page.request.delete(api + f'/viz:sessions/{sid}', headers=headers)
            write_result(root / 'camera-bookmarks-browser.json', result)
            edge.close()


def verify(args):
    import httpx

    server = IsolatedServer('viz-camera-d16', app='camera_fixture:app')
    root = server.root
    scratch = root / 'native-tmp'
    scratch.mkdir()
    dist = args.dist.resolve(strict=True)
    env = dict(os.environ, TEMP=str(scratch), TMP=str(scratch), TMPDIR=str(scratch), PYTHONIOENCODING='utf8')
    (root / 'camera_fixture.py').write_text('import os\nfrom fastapi.staticfiles import StaticFiles\n'
        f'os.environ.update(TEMP={str(scratch)!r}, TMP={str(scratch)!r}, TMPDIR={str(scratch)!r})\nfrom app.main import app\n'
        f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n', encoding='utf8')
    target = root / 'camera-bookmarks-result.json'
    result = {'passed':False, 'model_calls':0}
    print('VIZ_CAMERA_EVIDENCE', root, flush=True)
    try:
        inputs = {relative: root / Path(relative).name for relative in (WING, BRACKET)}
        result['native_inputs'] = {relative:native_fixture(inputs[relative], scale)
                                   for relative, scale in ((WING, 1), (BRACKET, 1.45))}
        with server as api:
            case = api.create_case(CASE)
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization':'Bearer ' + api.token},
                              trust_env=False, timeout=36) as client:
                for relative, source in inputs.items():
                    upload(api, client, source, relative)
            foreign = api.call('POST', '/auth/register', {'name':'Other camera account', 'email':'other-camera@example.com',
                'password':'isolated-test-123'})
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', __file__, '--browser'],
                input=json.dumps({'root':str(root), 'case':case['id'], 'base':api.base, 'token':api.token, 'foreign':foreign['token']}),
                text=True, encoding='utf8', check=True, timeout=235, cwd=root, env=env)
            result['browser'] = json.loads((root / 'camera-bookmarks-browser.json').read_text(encoding='utf8'))
            assert result['browser']['passed']
        result['api_exit_code'] = server.proc.poll()
        assert result['api_exit_code'] is not None
        result['passed'] = True
    except BaseException as error:
        result['failure'] = {'type':type(error).__name__, 'message':str(error)[:1000]}
        raise
    finally:
        write_result(target, result)
    print('VIZ_CAMERA_PASS', target, flush=True)


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
        verify(parser.parse_args())
