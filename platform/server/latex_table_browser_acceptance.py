"""E09: real Edge, current table inputs, conditional recovery, and a native PDF publication."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.parse import unquote, urlencode, urlsplit
import uuid


CASE = 'E09 LaTeX table acceptance'
MAIN = 'E-report/latex/main.tex'
PDF = 'E-report/latex/build/main.pdf'
LOG = 'E-report/latex/build/main.latex.log'
ORIGINAL = ('\\documentclass[UTF8,fontset=fandol]{ctexart}\n\\usepackage{booktabs}\n'
            '\\begin{document}\n% \\end{document} is an example in a comment\n'
            'Table input with 50\\% progress.\n\\end{document}\n')
DRAFT = '% Editor save survived\n'
COMPETING = '% Other editor saved this line\n'
ROWS = '"Quoted,cell",a_b,"%$#\\{}~^"\n"Line A\n\nLine B",42,100'
ESCAPES = (r'A\&B', r'a\_b', r'\%\$\#\textbackslash{}\{\}\textasciitilde{}\textasciicircum{}')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding='utf8'))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root = Path(settings['root'])
    base = settings['base']
    prefix = '/api/v1/cases/' + str(settings['case'])
    report = {'passed': False, 'backups': [], 'table_requests': []}
    with sync_playwright() as playwright:
        engine = playwright.chromium.launch(channel='msedge')

        def context_for(token):
            context = engine.new_context(viewport={'width': 1530, 'height': 970}, locale='zh-CN')
            context.add_init_script('if(location.origin===' + json.dumps(base) + ')localStorage.setItem("vf_token",'
                                    + json.dumps(token) + ');')
            return context

        def open_case(page, *, workspace=None):
            page.set_default_timeout(45000)
            page.goto(base, wait_until='domcontentloaded')
            if workspace is not None:
                page.get_by_label('当前工作区', exact=True).select_option(str(workspace))
            page.locator('[data-case-name=' + json.dumps(CASE) + ']').click()
            page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^caseDict$')).click()
            expect(page.get_by_test_id('latex-table-editor')).to_be_visible(timeout=30000)

        auth = {'Authorization': 'Bearer ' + settings['token']}
        context = context_for(settings['token'])
        page = context.new_page()
        report['edge'] = engine.version
        table = page.get_by_test_id('latex-table-editor')
        rows = table.get_by_test_id('latex-table-rows')
        preview = table.get_by_test_id('latex-table-preview')
        status = table.get_by_test_id('latex-table-status')
        generate = table.get_by_test_id('latex-table-generate')
        insert = table.get_by_test_id('latex-table-insert')
        open_file = page.locator('[data-editor-path=' + json.dumps(MAIN) + ']')
        registered_request = lambda response: (response.request.method == 'POST'
            and unquote(urlsplit(response.url).path) == prefix + '/latex:build')
        runtime = {'sent': 0, 'done': 0, 'statuses': []}

        def on_request(request):
            path = unquote(urlsplit(request.url).path)
            if path == '/api/v1/runtime/capabilities':
                runtime['sent'] += 1
            if request.method == 'POST' and path == prefix + '/latex:table':
                report['table_requests'].append(request.post_data_json)

        def runtime_finished(request):
            if unquote(urlsplit(request.url).path) == '/api/v1/runtime/capabilities':
                runtime['done'] += 1

        def runtime_response(response):
            if unquote(urlsplit(response.url).path) == '/api/v1/runtime/capabilities':
                runtime['statuses'].append(response.status)

        def saved_source(path=MAIN):
            response = context.request.get(base + prefix + '/file?' + urlencode({'path': path}), headers=auth)
            assert response.status == 200, (response.status, response.text())
            return response.json()

        def table_insert(expected):
            start = len(report['backups'])
            with page.expect_response(lambda response: response.request.method == 'PUT'
                    and unquote(urlsplit(response.url).path) == prefix + '/file'
                    and response.request.post_data_json['path'] == MAIN, timeout=65000) as observed:
                insert.click()
            response = observed.value
            assert response.status == expected, (response.status, response.text())
            assert len(report['backups']) == start + 1 and response.request.post_data_json['expected_sha256']
            return response.json()

        def capture_backup(request):
            if request.method != 'PUT' or unquote(urlsplit(request.url).path) != prefix + '/file':
                return
            data = request.post_data_json
            if data['path'].startswith(MAIN + '.table-backup-'):
                assert data['expected_sha256'] is None
                report['backups'].append(data['path'])

        def wait_capabilities():
            deadline = time.monotonic() + 130
            while time.monotonic() < deadline:
                page.wait_for_timeout(200)
                if not runtime['statuses'] or runtime['sent'] != runtime['done']:
                    continue
                with closing(sqlite3.connect((root / 'test.sqlite').as_uri() + '?mode=ro', uri=True)) as sql:
                    active = sql.execute("SELECT count(*) FROM native_requests WHERE phase='active' "
                        "AND kind IN ('runtime.python_probe','runtime.wsl_probe')").fetchone()[0]
                if not active:
                    report['capabilities_before_compile'] = runtime
                    return
            raise AssertionError(('Workbench capability checks did not finish', runtime))

        try:
            page.on('request', on_request)
            page.on('request', capture_backup)
            page.on('requestfinished', runtime_finished)
            page.on('requestfailed', runtime_finished)
            page.on('response', runtime_response)
            open_case(page)
            case_panel = page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^caseDict$'))
            latex_main = page.get_by_test_id('latex-main')
            directory = page.locator('[data-path="E-report/latex"]')
            expect(directory).to_be_visible()
            if directory.get_attribute('aria-expanded') == 'false':
                directory.click()
            page.locator('[data-path=' + json.dumps(MAIN) + ']').click()
            expect(open_file).to_have_attribute('data-dirty', 'false')
            case_panel.click()
            latex_main.select_option(MAIN)
            table.get_by_test_id('latex-table-headers').fill('A&B,值,量')
            table.get_by_test_id('latex-table-align').fill('lcr')
            rows.fill('StalePreview,1,2')
            generate.click()
            expect(preview).to_contain_text('StalePreview')
            assert saved_source()['content'] == ORIGINAL

            rows.fill('Short,1')
            expect(preview).to_have_count(0)
            insert.click()
            expect(status).to_contain_text('每行必须与表头列数一致')
            assert not report['backups'] and saved_source()['content'] == ORIGINAL

            rows.fill(ROWS)
            page.locator('.monaco-editor .view-lines:visible').click(position={'x': 60, 'y': 15})
            page.keyboard.press('Control+End')
            page.keyboard.insert_text(DRAFT)
            expect(open_file).to_have_attribute('data-dirty', 'true')
            insert.click()
            expect(status).to_contain_text('请先完成编辑器中目标文件的保存')
            expect(open_file).to_have_attribute('data-dirty', 'true')
            assert saved_source()['content'] == ORIGINAL and not report['backups']
            wait_capabilities()
            save = page.get_by_role('button', name='保存 (Ctrl+S)', exact=True)
            report['editor_save_attempts'] = []
            for attempt in range(2):
                with page.expect_response(lambda response: response.request.method == 'PUT'
                        and unquote(urlsplit(response.url).path) == prefix + '/file', timeout=65000) as receipt:
                    save.click()
                response = receipt.value
                payload = response.json()
                report['editor_save_attempts'].append({'status': response.status, 'body': payload})
                if response.status == 200:
                    break
                assert attempt == 0 and response.status == 409 and payload['detail']['code'] == 'storage_quota_busy', payload
                expect(page.get_by_test_id('editor-save-error')).to_contain_text('请稍后重试')
                expect(open_file).to_have_attribute('data-dirty', 'true')
                assert saved_source()['content'] == ORIGINAL
                page.screenshot(path=str(root / 'storage-busy-draft.png'))
                wait_capabilities()
            expect(open_file).to_have_attribute('data-dirty', 'false')
            expect(save).to_be_disabled(timeout=30000)
            saved = saved_source()
            assert DRAFT.strip() in saved['content']
            report['draft_and_short_line_preserved'] = True

            changed = saved['content'].replace('Table input', COMPETING + 'Table input', 1)
            raced = {'seen': False}

            def compete(route):
                request = route.request
                if request.method == 'PUT' and request.post_data_json['path'] == MAIN and not raced['seen']:
                    rival = context.request.put(base + prefix + '/file', headers=auth, data={
                        'path': MAIN, 'content': changed, 'expected_sha256': saved['content_sha256']})
                    raced.update(seen=True, status=rival.status)
                route.continue_()

            pattern = '**/api/v1/cases/' + str(settings['case']) + '/file'
            page.route(pattern, compete)
            try:
                conflict = table_insert(409)
            finally:
                page.unroute(pattern, compete)
            assert raced == {'seen': True, 'status': 200} and conflict['detail']['code'] == 'file_conflict'
            expect(status).to_contain_text('源文件已变化')
            expect(preview).to_contain_text('Quoted,cell')
            assert saved_source()['content'] == changed and saved_source(report['backups'][0])['content'] == saved['content']
            page.screenshot(path=str(root / 'table-conflict.png'))

            table_insert(200)
            expect(status).to_contain_text('表格已插入')
            expect(open_file).to_have_attribute('data-active', 'true')
            expect(open_file).to_have_attribute('data-dirty', 'false')
            first = saved_source()['content']
            assert saved_source(report['backups'][1])['content'] == changed
            assert all(value in first for value in ESCAPES)
            assert 'Line A  Line B' in first and 'Quoted,cell' in first and 'StalePreview' not in first
            assert r'\begin{tabular}{lcr}' in first and r'\toprule' in first and r'\bottomrule' in first
            expect(page.locator('.monaco-editor .view-lines:visible')).to_contain_text('Quoted,cell')

            case_panel.click()
            table.get_by_test_id('latex-table-booktabs').uncheck()
            table.get_by_test_id('latex-table-borders').check()
            table.get_by_test_id('latex-table-align').fill('rlc')
            rows.fill('Bordered,94,200')
            expect(preview).to_have_count(0)
            table_insert(200)
            expect(status).to_contain_text('表格已插入')
            expect(open_file).to_have_attribute('data-dirty', 'false')
            expect(page.locator('.monaco-editor .view-lines:visible')).to_contain_text('Bordered')
            cell = saved_source()
            assert saved_source(report['backups'][2])['content'] == first
            assert cell['content'].count(r'\begin{tabular}') == 2
            assert cell['content'].index(r'\begin{tabular}') > cell['content'].index('% \\end{document}')
            assert cell['content'].rindex(r'\end{tabular}') < cell['content'].rindex(r'\end{document}')
            assert r'\begin{tabular}{|r|l|c|}' in cell['content'] and cell['content'].count(r'\hline') == 3
            report['source_sha256'] = cell['content_sha256']
            page.screenshot(path=str(root / 'inserted-monaco.png'))

            guest = context_for(settings['viewer'])
            try:
                guest_page = guest.new_page()
                open_case(guest_page, workspace=settings['workspace'])
                expect(guest_page.get_by_test_id('latex-table-generate')).to_be_disabled()
                expect(guest_page.get_by_test_id('latex-table-insert')).to_be_disabled()
                expect(guest_page.get_by_test_id('latex-table-editor')).to_contain_text('此工作区仅可查看')
                guest_page.screenshot(path=str(root / 'table-readonly.png'))
            finally:
                guest.close()
            report['viewer_controls_disabled'] = True

            case_panel.click()
            wait_capabilities()
            expect(page.get_by_test_id('latex-recipe')).to_have_value('xelatex')
            with page.expect_response(registered_request, timeout=90000) as request:
                page.get_by_test_id('latex-build').click()
            response = request.value
            report['build_response'] = {'status': response.status, 'body': response.json()}
            assert response.status == 200, report['build_response']
            report['passed'] = True
        except BaseException:
            page.screenshot(path=str(root / 'table-browser-failure.png'), timeout=4000)
            raise
        finally:
            write_json(root / 'table-browser.json', report)
            engine.close()


def verify(args):
    from testsupport import IsolatedServer
    from app.services.process_identity_native import probe

    api = IsolatedServer('e09-latex-table', app='latex_table_fixture:app', access_log=True)
    print('E09_TABLE_EVIDENCE', api.root, flush=True)
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    (api.root / 'latex_table_fixture.py').write_text('from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        + 'from app.services.process_identity_native import observe\nfrom pathlib import Path\nimport json, os\n'
        + 'Path("api-birth.json").write_text(json.dumps(observe(os.getpid())), encoding="utf8")\n'
        + 'app.mount("/", StaticFiles(directory=' + repr(str(dist)) + ', html=True))\n', encoding='utf8')
    result = {'passed': False, 'model_calls': 0, 'database': 'sqlite', 'storage': 'fs'}
    try:
        with api:
            api.create_case(CASE)
            case = api.call('GET', api.prefix)
            api.put(MAIN, ORIGINAL)
            table_api = api.prefix + '/latex:table'
            sample = {'headers': ['left', 'right'], 'rows': [['A', 'B']], 'align': 'lx'}
            api.call('POST', table_api, sample, expect=422)
            guest_email = uuid.uuid4().hex + '@example.com'
            viewer = api.call('POST', '/auth/register', {'email': guest_email, 'name': 'E09 table viewer',
                                                        'password': 'isolated-test-123'})['token']
            auth = {'Authorization': 'Bearer ' + viewer}
            api.call('POST', table_api, {**sample, 'align': 'lr'}, expect=404, extra_headers=auth)
            org = api.call('GET', '/org/mine')['org']['id']
            api.call('POST', f'/org/{org}/members', {'email': guest_email, 'role': 'viewer'}, expect=201)
            api.call('POST', f'/org/{org}/invitation:accept', {}, extra_headers=auth)
            api.call('PUT', f"/workspaces/{case['workspace_id']}/sharing", {'org_id': org, 'role': 'viewer'})
            assert api.call('GET', api.prefix + '/file?' + urlencode({'path': MAIN}), extra_headers=auth)['content'] == ORIGINAL
            api.call('POST', table_api, {**sample, 'align': 'lr'}, expect=404, extra_headers=auth)
            api.call('PUT', api.prefix + '/file', {'path': MAIN, 'content': 'invalid write'}, expect=404, extra_headers=auth)
            result['api_acl'] = {'outsider_generate': 404, 'viewer_read': 200, 'viewer_generate': 404, 'viewer_write': 404}

            temp = api.root / 'browser-tmp'
            temp.mkdir()
            settings = {'root': str(api.root), 'base': api.base, 'case': case['id'], 'workspace': case['workspace_id'],
                        'token': api.token, 'viewer': viewer}
            env = dict(os.environ, PYTHONIOENCODING='utf8', TEMP=str(temp), TMP=str(temp))
            subprocess.run([str(args.browser_python), str(Path(__file__).resolve()), '--browser'], input=json.dumps(settings),
                           encoding='utf8', cwd=api.root, env=env, check=True, timeout=550)
            result['browser'] = read_json(api.root / 'table-browser.json')
            assert result['browser']['passed']
            submission = result['browser']['build_response']['body']
            job = api.wait_job(submission['job_id'], timeout=285)
            deadline = time.monotonic() + 90
            while job.get('completion_pending') and time.monotonic() < deadline:
                time.sleep(.25)
                job = api.call('GET', '/jobs/' + str(submission['job_id']))
            assert job['status'] == 'succeeded' and not job.get('completion_pending') and not job.get('completion_error'), job
            publication = submission['publication_id']
            outcome = api.call('GET', api.prefix + '/latex/results?' + urlencode({'publication_id': publication}))
            manifest = outcome['manifest']
            assert manifest['job_id'] == job['id'] and manifest['recipe'] == 'xelatex'
            engine = outcome['toolchain']['native']['runtime']['xelatex']['version'].splitlines()[0]
            assert 'XeTeX' in engine
            uri = api.prefix + '/latex/artifact?' + urlencode({'publication_id': publication, 'path': PDF})
            data = api.call('GET', uri, raw=True)
            assert data.startswith(b'%PDF') and sha(data) == manifest['artifacts'][PDF]['sha256']
            source_uri = api.prefix + '/latex/source?' + urlencode({'publication_id': publication, 'path': MAIN})
            source = api.call('GET', source_uri, raw=True)
            assert sha(source) == result['browser']['source_sha256']
            transcript = api.call('GET', api.prefix + '/latex/artifact?' + urlencode({'publication_id': publication, 'path': LOG}), raw=True)
            assert b'booktabs' in transcript and b'Undefined control sequence' not in transcript
            pdf = api.root / 'table.pdf'
            pdf.write_bytes(data)
            assert pdf.drive.upper() == 'D:'
            render = subprocess.run(['wsl.exe', '-d', 'vibeflow-linux', '--user', 'nobody', '--exec',
                                     'pdftotext', '-layout', '/mnt/d' + pdf.as_posix()[2:], '-'],
                                    capture_output=True, timeout=35, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            rendered = render.stdout.decode('utf8')
            (api.root / 'table-rendered.txt').write_text(rendered, encoding='utf8')
            flat = ' '.join(rendered.split())
            for marker in ('Quoted,cell', 'a_b', 'A&B', '值', '%$#', 'Bordered', 'Line A Line B'):
                assert marker in flat, (marker, flat)
            result['native_pdf'] = {'job': job['id'], 'publication': publication, 'bytes': len(data), 'engine': engine,
                                    'pages': manifest['pages'], 'sha256': sha(data), 'source_sha256': sha(source)}

        birth = read_json(api.root / 'api-birth.json')
        deadline = time.monotonic() + 15
        while probe(birth)['state'] != 'exited' and time.monotonic() < deadline:
            time.sleep(.2)
        assert probe(birth)['state'] == 'exited'
        with socket.socket() as connection:
            connection.settimeout(1)
            assert connection.connect_ex(('127.0.0.1', api.port)) != 0
        with closing(sqlite3.connect((api.root / 'test.sqlite').as_uri() + '?mode=ro', uri=True)) as sql:
            worker, envelope = sql.execute('SELECT worker_identity_json,child_processes_json FROM job_attempts WHERE job_id=?',
                                           (result['native_pdf']['job'],)).fetchone()
        identity = json.loads(worker)['process']
        children = json.loads(envelope)['children']
        assert probe(identity)['state'] == 'exited' and children
        assert all(child['stop']['verified'] and child['stop']['scope_empty'] for child in children.values())
        result['cleanup'] = {'api_exited': True, 'api_port_closed': True, 'worker_exited': True, 'stopped_children': len(children)}
        result['passed'] = True
    finally:
        write_json(api.root / 'latex-table-result.json', result)
    print('E09_TABLE_PASS', api.root / 'latex-table-result.json', flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
        from regression_llm_policy import enforce
        enforce([Path(__file__)])
        parser = argparse.ArgumentParser()
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        verify(parser.parse_args())
