"""Exercise selection and durable browser preferences in the real VTK pipeline."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

CASE = 'Native pipeline selection'
PRIMARY = 'Ref/pipeline.vtp'
SECONDARY = 'Ref/different-source.vtp'
SCENE = """VF_surface('Shell')
VF_slice('Section A', axis='X', pos=0.35)
VF_slice('Section B', axis='Y', pos=0.65)
VF_slice('Outside section', axis='X', pos=1.5)
try:
    VF_slice('Failed cut', axis='Z', pos='invalid')
except ValueError:
    pass
render()
"""


def create_source(path):
    import vtk

    sphere = vtk.vtkSphereSource()
    sphere.SetThetaResolution(28)
    sphere.SetPhiResolution(18)
    sphere.SetRadius(2)
    sphere.Update()
    writer = vtk.vtkXMLPolyDataWriter()
    writer.SetInputData(sphere.GetOutput())
    writer.SetFileName(str(path))
    assert writer.Write() == 1
    return {'points': sphere.GetOutput().GetNumberOfPoints(), 'cells': sphere.GetOutput().GetNumberOfCells()}


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root, base = Path(settings['root']), settings['base']
    auth = {'Authorization': 'Bearer ' + settings['token']}
    api = base + '/api/v1'
    case = api + f"/cases/{settings['case']}"
    report = {'passed': False, 'page_errors': [], 'model_requests': [], 'stopped': []}
    session = None

    with sync_playwright() as driver:
        edge = driver.chromium.launch(channel='msedge')
        report['edge'] = edge.version
        context = edge.new_context(viewport={'width': 1520, 'height': 1020}, locale='zh-CN')
        context.add_init_script(f"if (location.origin === {json.dumps(base)}) localStorage.setItem('vf_token', {json.dumps(settings['token'])});")
        page = context.new_page()
        page.set_default_timeout(28_000)
        expect.set_options(timeout=20_000)
        page.on('pageerror', lambda error: report['page_errors'].append(str(error)))
        page.on('request', lambda req: report['model_requests'].append(urlsplit(req.url).path)
                if req.method == 'POST' and urlsplit(req.url).path.endswith(('/messages', '/turns', '/goal:run')) else None)
        frame = page.frame_locator('iframe[title="可视化"]')
        tree = frame.get_by_role('tree', name='场景对象')
        menu = frame.get_by_role('menu', name='所选对象操作')
        count = frame.get_by_test_id('vf-selection-count')
        choice = frame.get_by_test_id('viz-info-selection')

        def node(name):
            return tree.get_by_role('treeitem', name=name, exact=True)

        def status(name, value):
            row = node(name).locator('xpath=ancestor::*[@data-testid="vf-pipeline-row"][1]')
            expect(row).to_have_attribute('data-object-status', value)

        def attached():
            expect(page.locator('iframe[title="可视化"]')).to_have_attribute(
                'src', re.compile(r'^/viz-proxy/' + str(session) + r'/index.html\?'))
            drawer = frame.locator('.v-navigation-drawer')
            expect(drawer).to_be_attached()
            if 'v-navigation-drawer--active' not in (drawer.get_attribute('class') or '').split():
                frame.locator('.v-app-bar-nav-icon').click()
            expect(tree).to_be_attached()

        def launch(name):
            nonlocal session
            folder = page.locator('[data-path="Ref"]')
            if folder.get_attribute('aria-expanded') == 'false':
                folder.click()
            with page.expect_response(lambda response: response.request.method == 'POST'
                                      and response.url == case + '/viz:sessions'
                                      and response.request.post_data_json.get('file_path') == name,
                                      timeout=50_000) as event:
                page.locator('[data-path=' + json.dumps(name) + ']').click()
            response = event.value
            assert response.ok, (response.status, response.text())
            session = response.json()['id']
            assert session not in report['stopped']
            attached()

        def close():
            nonlocal session
            endpoint = api + f'/viz:sessions/{session}'
            toggle = page.get_by_role('button', name=f'关闭可视化会话 {session}', exact=True)
            with page.expect_response(lambda response: response.request.method == 'DELETE' and response.url == endpoint,
                                      timeout=45_000) as stopped:
                toggle.click()
            assert stopped.value.ok, stopped.value.status
            expect(page.locator('iframe[title="可视化"]')).to_have_count(0)
            record = page.request.get(endpoint, headers=auth).json()
            assert record['phase'] == 'closed' and record['stop']['verified'] and record['stop']['scope_empty'], record
            report['stopped'].append(session)
            session = None

        def scene(revision):
            reply = page.request.put(case + '/file', headers=auth,
                                     data={'path': 'D-post/scenes.py', 'content': SCENE + f'\n# version {revision}\n'})
            assert reply.ok, reply.text()
            expect(node('Shell')).to_be_visible()
            expect(node('Section A')).to_be_visible()
            expect(node('Section B')).to_be_visible()
            expect(node('Outside section')).to_be_visible()
            expect(node('Failed cut')).to_be_visible()

        def selection(names, active):
            expect(tree.locator('[role="treeitem"][aria-selected="true"]')).to_have_count(len(names))
            for name in names:
                expect(node(name)).to_have_attribute('aria-selected', 'true')
            if active:
                expect(node(active)).to_have_attribute('aria-current', 'true')
                expect(choice).to_have_text(active)
                expect(frame.get_by_text(f'属性 · {active}', exact=True)).to_be_visible()

        def activate_case():
            case_row = page.locator('[data-case-name=' + json.dumps(CASE) + ']')
            if case_row.get_attribute('aria-pressed') != 'true':
                case_row.click()

        def action(label):
            expect(menu).to_be_visible()
            menu.get_by_role('menuitem', name=label, exact=True).click()
            expect(menu).to_be_hidden()

        try:
            page.goto(base)
            activate_case()
            launch(PRIMARY)
            scene(1)
            status('Shell', 'visible')
            status('Section A', 'visible')
            status('Outside section', 'empty')
            status('Failed cut', 'error')
            report['native_tree_states'] = True

            node('Section A').click()
            node('Section B').click(modifiers=['Control'])
            selection(['Section A', 'Section B'], 'Section B')
            expect(count).to_have_text('已选 2 项')
            node('Section A').click(button='right')
            action('隐藏所选')
            status('Section A', 'hidden')
            status('Section B', 'hidden')
            status('Shell', 'visible')
            selection(['Section A', 'Section B'], 'Section A')
            node('Section B').focus()
            node('Section B').press('Shift+F10')
            action('显示所选')
            status('Section A', 'visible')
            status('Section B', 'visible')
            node('Section A').click()
            node('Section B').click(modifiers=['Shift'])
            selection(['Section A', 'Section B'], 'Section B')
            report['modifier_and_context_actions'] = True
            page.screenshot(path=str(root / 'pipeline-multiselect.png'))

            keys = page.evaluate("Object.keys(localStorage).filter(key => key.startsWith('vf-pipeline-v1:'))")
            assert len(keys) == 1, keys
            preference = page.evaluate('(key) => JSON.parse(localStorage.getItem(key))', keys[0])
            assert preference['active'] == 'Section B' and len(preference['items']) == 2, preference
            page.reload(wait_until='domcontentloaded')
            activate_case()
            page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^可视化$')).click()
            owner_chip = page.get_by_role('button', name=f'关闭可视化会话 {session}', exact=True).locator('..')
            owner_chip.get_by_role('button', name='打开', exact=True).click()
            attached()
            selection(['Section A', 'Section B'], 'Section B')
            close()

            launch(PRIMARY)
            scene(2)
            selection(['Section A', 'Section B'], 'Section B')
            report['selection_survives_reload_and_new_kernel'] = True
            node('Section B').click(button='right')
            action('删除所选')
            expect(node('Section A')).to_have_count(0)
            expect(node('Section B')).to_have_count(0)
            expect(count).to_have_text('')
            status('Shell', 'visible')
            node('Shell').click()
            selection(['Shell'], 'Shell')
            close()

            launch(SECONDARY)
            scene(3)
            selection([], '')
            status('Shell', 'visible')
            close()
            report['batch_remove_and_per_source_isolation'] = True
            assert len(report['stopped']) == 3 and not report['page_errors'] and not report['model_requests'], report
            report['passed'] = True
        finally:
            if not report['passed']:
                page.screenshot(path=str(root / 'pipeline-failure.png'))
            if session is not None:
                page.request.delete(api + f'/viz:sessions/{session}', headers=auth)
            (root / 'pipeline-browser.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
            edge.close()


def verify(args):
    import httpx
    from testsupport import IsolatedServer
    from viz_information_browser_acceptance import upload, write_result

    dist = args.dist.resolve(strict=True)
    api = IsolatedServer('viz-pipeline-d02', app='viz_pipeline_fixture:app')
    root = api.root
    scratch = root / 'native-tmp'
    scratch.mkdir()
    print('VIZ_PIPELINE_EVIDENCE', root, flush=True)
    fixture = ("import os\nfrom fastapi.staticfiles import StaticFiles\n"
               f"os.environ.update({{'TEMP': {str(scratch)!r}, 'TMP': {str(scratch)!r}}})\nfrom app.main import app\n"
               f"app.mount('/', StaticFiles(directory={str(dist)!r}, html=True))\n")
    (root / 'viz_pipeline_fixture.py').write_text(fixture, encoding='utf8')
    source = root / 'sphere.vtp'
    report = {'passed': False, 'mock_llm_calls': 0, 'native_fixture': create_source(source)}
    try:
        with api:
            case = api.create_case(CASE)
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                              trust_env=False, timeout=30) as client:
                upload(api, client, source, PRIMARY)
                upload(api, client, source, SECONDARY)
            outsider = api.call('POST', '/auth/register', {'name': 'Unrelated viewer',
                                'email': 'viz-pipeline-restricted@example.com', 'password': 'isolated-test-123'})
            api.call('POST', api.prefix + '/viz:sessions', {'file_path': PRIMARY}, expect=404,
                     extra_headers={'Authorization': 'Bearer ' + outsider['token']})
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', __file__, '--browser'],
                           input=json.dumps({'root': str(root), 'case': case['id'], 'base': api.base, 'token': api.token}),
                           text=True, encoding='utf8', check=True, timeout=250, cwd=root,
                           env=dict(os.environ, TEMP=str(scratch), TMP=str(scratch)))
            report['browser'] = json.loads((root / 'pipeline-browser.json').read_text(encoding='utf8'))
            assert report['browser']['passed']
            report['other_account_http'] = 404
        assert api.proc.poll() is not None
        report['passed'] = True
    finally:
        write_result(root / 'pipeline-result.json', report)
    print('VIZ_PIPELINE_PASS', root / 'pipeline-result.json', flush=True)


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
