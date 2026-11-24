"""Real Edge and owned SyncTeX using E03's retained native TeX evidence."""
import argparse
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit
import uuid


MAIN = 'E-report/latex/main.tex'
PDF = 'E-report/latex/build/main.pdf'
SIDECAR = 'E-report/latex/build/main.synctex.gz'
CROPPED = 'E-report/latex/cropped-xe.pdf'
CROP_SIDECAR = 'E-report/latex/cropped-xe.synctex.gz'
HISTORY_NAME = 'E03 LaTeX recipe acceptance'
CROP_NAME = 'E10 cropped and rotated XeLaTeX'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def restore_history(source, api):
    assert source.parent == api.root.parent and source.name.startswith('e03-real-latex-')
    result = read_json(source / 'latex-recipes-result.json')
    assert result['passed'] and read_json(source / 'cleanup-independent.json')['passed']
    old, failed, current = result['browser']['jobs']
    assert [job['status'] for job in (old, failed, current)] == ['succeeded', 'failed', 'succeeded']
    files = source / 'files'
    assert not any(path.is_symlink() for path in files.rglob('*'))
    shutil.copytree(files, api.root / 'files')
    with closing(sqlite3.connect((source / 'test.sqlite').as_uri() + '?mode=ro', uri=True)) as database:
        assert database.execute('SELECT id,status FROM jobs ORDER BY id').fetchall() == [
            (job['job'], job['status']) for job in (old, failed, current)]
        case, workspace, account, email, key = database.execute(
            'SELECT req.case_id,cases.workspace_id,users.id,users.email,req.idempotency_key '
            'FROM job_submission_requests req JOIN users ON users.id=req.created_by '
            'JOIN cases ON cases.id=req.case_id WHERE req.job_id=? AND cases.name=?',
            (failed['job'], HISTORY_NAME)).fetchone()
        assert re.fullmatch(r'[0-9a-f]{32}@example\.com', email) and str(uuid.UUID(key)) == key
        with closing(sqlite3.connect(api.root / 'test.sqlite')) as destination:
            database.backup(destination)
    return {'case': case, 'workspace': workspace, 'account': account, 'email': email,
            'previous': source.name, 'jobs': [old, failed, current], 'hashes': result['documents'],
            'selection': {'key': key, 'main': MAIN, 'recipe': failed['recipe'], 'jobId': failed['job']}}


def original_xe(source, api):
    from pypdf import PdfReader, PdfWriter

    assert source.parent == api.root.parent and source.name.startswith('e03-real-latex-')
    receipt = read_json(source / 'xelatex-publication-independent.json')
    assert receipt['passed'] and read_json(source / 'cleanup-independent.json')['passed']
    publication, = (source / 'files').glob('workspaces/*/cases/*/.vibeflow/publications/' + receipt['publication'])
    pdf = (publication / 'files' / PDF).read_bytes()
    sidecar = (publication / 'files' / SIDECAR).read_bytes()
    original, = (publication / 'files' / 'E-report/latex-history').glob('*/sources/' + MAIN)
    tex = original.read_text(encoding='utf-8')
    assert sha(pdf) == receipt['artifacts']['main.pdf']['sha256']
    assert 'XeRecipeFixture' in tex.splitlines()[4]
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf)))
    page = writer.pages[0]
    top = float(page.mediabox.top)
    assert float(page.mediabox.left) == 0
    page.cropbox.lower_left = (40, 30)
    page.cropbox.upper_right = (float(page.mediabox.right) - 20, top - 90)
    page.rotate(90)
    output = io.BytesIO()
    writer.write(output)
    return pdf, output.getvalue(), sidecar, tex, top, receipt['publication']


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root = Path(settings['root'])
    base = settings['base']
    previous = settings['history']
    report = {'responses': [], 'passed': False}
    hint = f"vf-latex-build-v1:{previous['account']}:{previous['case']}"
    prefix = f"/api/v1/cases/{settings['crop_case']}"
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        context = edge.new_context(viewport={'width': 1530, 'height': 970}, locale='zh-CN')
        page = context.new_page()
        page.set_default_timeout(45000)
        context.add_init_script('if(location.origin===' + json.dumps(base) + '){'
            + 'localStorage.setItem("vf_token",' + json.dumps(settings['token']) + ');'
            + 'sessionStorage.setItem(' + json.dumps(hint) + ','
            + json.dumps(json.dumps(previous['selection'])) + ');}')
        runtime = {'started': 0, 'finished': 0, 'responses': []}

        def is_capability(request):
            return urlsplit(request.url).path == '/api/v1/runtime/capabilities'

        def capability_started(request):
            if is_capability(request):
                runtime['started'] += 1

        def capability_finished(request):
            if is_capability(request):
                runtime['finished'] += 1

        def capability_response(response):
            if is_capability(response.request):
                runtime['responses'].append(response.status)

        page.on('request', capability_started)
        page.on('requestfinished', capability_finished)
        page.on('requestfailed', capability_finished)
        page.on('response', capability_response)

        def wait_for_runtime_checks():
            deadline = time.monotonic() + 125
            while time.monotonic() < deadline:
                page.wait_for_timeout(200)
                if not runtime['responses'] or runtime['started'] != runtime['finished']:
                    continue
                with closing(sqlite3.connect((root / 'test.sqlite').as_uri() + '?mode=ro', uri=True)) as sql:
                    active = sql.execute("SELECT count(*) FROM native_requests WHERE phase='active' "
                                         "AND kind IN ('runtime.python_probe','runtime.wsl_probe')").fetchone()[0]
                if not active:
                    report['runtime_checks_before_jump'] = runtime
                    return
            raise AssertionError(('Visible workbench runtime checks did not finish', runtime))

        def choose_case(name):
            page.locator('[data-case-name=' + json.dumps(name) + ']').click()

        def tree_open(path):
            parts = path.split('/')
            for index in range(1, len(parts)):
                directory = page.locator('[data-path=' + json.dumps('/'.join(parts[:index])) + ']')
                expect(directory).to_be_visible()
                if directory.get_attribute('aria-expanded') == 'false':
                    directory.click()
            page.locator('[data-path=' + json.dumps(path) + ']').click()
            return page.get_by_test_id('pdf-viewer')

        def jump(x, y, status):
            wait_for_runtime_checks()
            viewer = page.get_by_test_id('pdf-viewer')
            scale = float(viewer.get_attribute('data-scale'))
            with page.expect_response(lambda response: response.request.method == 'POST'
                    and urlsplit(response.url).path.endswith('/synctex:query'), timeout=60000) as observed:
                viewer.locator('canvas').locator('..').dblclick(position={'x': x * scale, 'y': y * scale})
            response = observed.value
            body, sent = response.json(), response.request.post_data_json
            report['responses'].append({'status': response.status, 'point': sent, 'result': body})
            write_json(root / 'browser.json', report)
            assert response.status == status, (response.status, body, sent)
            assert sent['coordinate_space'] == 'pdf'
            return body, sent

        try:
            page.goto(base, wait_until='domcontentloaded')
            choose_case(HISTORY_NAME)
            page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^caseDict$')).click()
            diagnostic = page.get_by_test_id('latex-diagnostic').filter(has_text='Undefined control sequence').first
            expect(diagnostic).to_have_attribute('data-file', MAIN, timeout=60000)
            expect(diagnostic).to_have_attribute('data-line', '4')
            diagnostic.click()
            expect(page.get_by_test_id('editor-cursor-line')).to_contain_text('L4')
            page.screenshot(path=str(root / 'native-failure-monaco.png'))
            report['diagnostic'] = {'case': previous['case'], 'job': previous['jobs'][1]['job'], 'line': 4}

            viewer = tree_open(PDF)
            expect(viewer.locator('.textLayer')).to_contain_text('PdfRecipeFixture')
            page.get_by_test_id('report-history-toggle').click()
            oldest = previous['jobs'][0]['publication']
            page.locator(f'[data-publication-id="{oldest}"]').click()
            expect(viewer.locator('.textLayer')).to_contain_text('LuaRecipeFixture')
            body, sent = jump(187, 130, 200)
            assert sent['publication_id'] == body['publication_id'] == oldest
            assert body['line'] == 3 and body['file'] == MAIN and not body['source_current_matches']
            expect(page.get_by_test_id('pdf-frozen-source')).to_contain_text('LuaRecipeFixture')
            expect(page.locator('.pdf-source-current')).to_have_attribute('data-line', '3')
            page.screenshot(path=str(root / 'historical-source.png'))
            page.get_by_test_id('pdf-latest').click()
            expect(viewer.locator('.textLayer')).to_contain_text('PdfRecipeFixture')
            expect(page.get_by_test_id('pdf-frozen-source')).to_have_count(0)
            report['historical'] = {'old': oldest, 'latest': previous['jobs'][-1]['publication'], 'frozen_line': 3}

            choose_case(CROP_NAME)
            viewer = tree_open(CROPPED)
            expect(viewer.locator('.textLayer')).to_contain_text('XeRecipeFixture')
            for _ in range(3):
                page.get_by_test_id('pdf-zoom-out').click()
            expect(viewer).to_have_attribute('data-scale', re.compile(r'^0\.6(?:0*1)?$'))
            # Rotate 90 degrees clockwise after cropping, independently of PDF.js.
            click_x, click_y = settings['top'] - 131 - 30, 260 - 40
            canvas = viewer.locator('canvas')
            expect(canvas).to_have_attribute('width', str(int((settings['top'] - 120) * .6)))
            expect(viewer.locator('.textLayer')).to_contain_text('XeRecipeFixture')
            problem, sent = jump(click_x, click_y, 409)
            assert '没有 SyncTeX' in problem['detail'] and sent.get('expected_file_version')
            expect(page.get_by_test_id('pdf-notice')).to_contain_text('重新编译')
            page.screenshot(path=str(root / 'missing-sidecar.png'))
            upload = context.request.post(base + prefix + '/upload?' + urlencode({'path': CROP_SIDECAR}),
                headers={'Authorization': 'Bearer ' + settings['token']}, multipart={
                    'file': {'name': 'cropped-xe.synctex.gz', 'mimeType': 'application/gzip',
                             'buffer': (root / 'genuine.synctex.gz').read_bytes()}})
            assert upload.status == 200, (upload.status, upload.text())
            body, sent = jump(click_x, click_y, 200)
            assert body['file'] == MAIN and body['line'] == 5
            assert abs(sent['x'] - 260) < 3 and abs(sent['y'] - (settings['top'] - 131)) < 3, sent
            expect(page.get_by_test_id('editor-cursor-line')).to_contain_text('L5')
            page.screenshot(path=str(root / 'rotated-crop-monaco.png'))
            report['passed'] = True
        except BaseException:
            page.screenshot(path=str(root / 'browser-failure.png'), timeout=4000)
            raise
        finally:
            write_json(root / 'browser.json', report)
            edge.close()


def verify(args):
    import httpx
    from app.services.process_identity_native import probe
    from testsupport import IsolatedServer

    api = IsolatedServer('e10-synctex', app='synctex_fixture:app', access_log=True)
    print('E10_SYNCTEX_EVIDENCE', api.root, flush=True)
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    prior = restore_history(args.history.resolve(strict=True), api)
    pdf, cropped, sidecar, tex, top, xe_id = original_xe(args.xe.resolve(strict=True), api)
    (api.root / 'genuine.synctex.gz').write_bytes(sidecar)
    (api.root / 'synctex_fixture.py').write_text('from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        + 'from app.services.process_identity_native import observe\nfrom pathlib import Path\nimport json, os\n'
        + 'Path("api-birth.json").write_text(json.dumps(observe(os.getpid())), encoding="utf-8")\n'
        + 'app.mount("/", StaticFiles(directory=' + repr(str(dist)) + ', html=True))\n', encoding='utf-8')
    result = {'passed': False, 'model_calls': 0, 'database': 'sqlite', 'storage': 'fs',
              'reused_jobs': prior['jobs'], 'prior_xelatex_publication': xe_id,
              'original_pdf_sha256': sha(pdf), 'cropped_pdf_sha256': sha(cropped), 'sidecar_sha256': sha(sidecar)}
    try:
        with api:
            login = api.call('POST', '/auth/login', {'email': prior['email'], 'password': 'isolated-test-123'})
            api.token = login['token']
            history = f"/cases/{prior['case']}"
            diagnostic = history + '/latex/errors?' + urlencode({'job_id': prior['jobs'][1]['job']})
            data = api.call('GET', diagnostic)
            assert data['status'] == 'failed' and any(item['file'] == MAIN and item['line'] == 4
                and item['severity'] == 'error' and 'Undefined control sequence' in item['message'] for item in data['items'])
            result['diagnostics'] = data
            for entry in prior['hashes'].values():
                raw = api.call('GET', history + '/latex/artifact?' + urlencode({
                    'publication_id': entry['publication'], 'path': PDF}), raw=True)
                assert sha(raw) == entry['sha256']
            crop = api.call('POST', f"/workspaces/{prior['workspace']}/cases", {'name': CROP_NAME})['id']
            crop_prefix = f'/cases/{crop}'
            api.call('PUT', crop_prefix + '/file', {'path': MAIN, 'content': tex})
            response = httpx.post(api.base + '/api/v1' + crop_prefix + '/upload', params={'path': CROPPED},
                files={'file': ('cropped-xe.pdf', cropped, 'application/pdf')},
                headers={'Authorization': 'Bearer ' + api.token}, timeout=35, trust_env=False)
            assert response.status_code == 200, (response.status_code, response.text)
            assert response.json()['content_sha256'] == sha(cropped)

            settings = {'root': str(api.root), 'base': api.base, 'token': api.token,
                        'history': {key: prior[key] for key in ('account', 'case', 'selection', 'jobs')},
                        'crop_case': crop, 'top': top}
            temporary = api.root / 'browser-tmp'
            temporary.mkdir()
            env = dict(os.environ, PYTHONIOENCODING='utf-8', TEMP=str(temporary), TMP=str(temporary))
            subprocess.run([str(args.browser_python), str(Path(__file__).resolve()), '--browser'],
                input=json.dumps(settings), encoding='utf-8', cwd=api.root, env=env, check=True, timeout=500)
            result['browser'] = read_json(api.root / 'browser.json')
            assert result['browser']['passed']
            query = {'pdf': CROPPED, 'page': 1, 'x': 260, 'y': 131}
            endpoint = crop_prefix + '/synctex:query'
            assert api.call('POST', endpoint, query)['line'] == 5
            assert api.call('POST', endpoint, {**query, 'x': 220, 'y': 41})['line'] == 7
            assert api.call('POST', endpoint, {**query, 'expected_file_version': '0' * 64}, expect=409
                )['detail']['code'] == 'file_changed_during_read'
            assert api.call('POST', endpoint, {**query, 'coordinate_space': 'pdf', 'page': 2}, expect=422)['detail']
            assert api.call('POST', endpoint, {**query, 'coordinate_space': 'pdf', 'x': -2}, expect=422)['detail']
            result['coordinates'] = {'old_crop_origin_wrong_line': 7, 'legacy_page_point_line': 5,
                                     'stale_version_status': 409, 'invalid_page_and_point_status': 422}
            outsider = api.call('POST', '/auth/register', {
                'email': uuid.uuid4().hex + '@example.com', 'name': 'E10 PDF visitor',
                'password': 'isolated-test-123'})['token']
            headers = {'Authorization': 'Bearer ' + outsider}
            api.call('POST', endpoint, query, expect=404, extra_headers=headers)
            api.call('GET', diagnostic, expect=404, extra_headers=headers)
            # E03's genuine invitation and acceptance already filled the second seat.
            guest = api.call('POST', '/auth/login', {
                'email': 'latex-reader@example.com', 'password': 'isolated-latex-e03-123'})['token']
            headers = {'Authorization': 'Bearer ' + guest}
            shared = api.call('GET', '/workspaces', extra_headers=headers)
            assert any(workspace['id'] == prior['workspace'] and workspace['access_role'] == 'viewer'
                       for workspace in shared)
            assert api.call('POST', endpoint, query, extra_headers=headers)['line'] == 5
            assert api.call('GET', diagnostic, extra_headers=headers) == data
            api.call('GET', crop_prefix + '/latex/errors?' + urlencode({'job_id': prior['jobs'][1]['job']}),
                expect=404, extra_headers=headers)
            api.call('PUT', crop_prefix + '/file', {'path': MAIN, 'content': 'invalid mutation'}, expect=404,
                extra_headers=headers)
            result['acl'] = {'outsider': 404, 'viewer_diagnostics': 200, 'viewer_reverse_lookup': 200,
                             'wrong_case_job': 404, 'viewer_write': 404}

        birth = read_json(api.root / 'api-birth.json')
        deadline = time.monotonic() + 15
        while probe(birth)['state'] != 'exited' and time.monotonic() < deadline:
            time.sleep(.2)
        assert probe(birth)['state'] == 'exited'
        with socket.socket() as connection:
            connection.settimeout(1)
            assert connection.connect_ex(('127.0.0.1', api.port)) != 0
        with closing(sqlite3.connect((api.root / 'test.sqlite').as_uri() + '?mode=ro', uri=True)) as sql:
            native = sql.execute("SELECT phase,children_json FROM native_requests WHERE kind='synctex.query'").fetchall()
        assert len(native) == 5, [(phase, len(json.loads(children))) for phase, children in native]
        for phase, envelope in native:
            children = json.loads(envelope)['children']
            assert phase == 'succeeded' and children
            assert all(child['stop']['verified'] and child['stop']['scope_empty'] for child in children.values())
        private = api.root / 'files' / '.vibeflow-native-queries'
        assert private.is_dir() and not list(private.iterdir())
        result['cleanup'] = {'api_pid': birth['pid'], 'api_process_exited': True, 'api_port_closed': True,
                             'actual_synctex_requests': len(native), 'all_native_scopes_empty': True,
                             'private_directories_empty': True}
        result['passed'] = True
    finally:
        write_json(api.root / 'synctex-result.json', result)
    print('E10_SYNCTEX_PASS', api.root / 'synctex-result.json', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
        from regression_llm_policy import enforce
        enforce([Path(__file__)])
        parser = argparse.ArgumentParser()
        for name in ('dist', 'history', 'xe', 'browser-python'):
            parser.add_argument('--' + name, type=Path, required=True)
        verify(parser.parse_args())
