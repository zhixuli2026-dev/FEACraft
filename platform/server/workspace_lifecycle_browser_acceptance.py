"""Exercise workspace and case administration against an actual FS API and Edge."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit


SOURCE = '源算例'
CASE = '算例移交流程'
DESTINATION = '同名目标'
CLONE = '独立副本'
COPY_WORKSPACE = '工作区副本'
FILE = 'lifecycle.txt'
PUBLISHED = 'C-run/lifecycle-result.bin'
PAYLOAD = b'committed output\x00\x8b'
DRAFT = 'Preserved workspace rename draft'


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root = Path(settings['root'])
    base = settings['base']
    case = settings['case']
    source = settings['source']
    target = settings['target']
    prefix = f'/cases/{case}'
    report = {}

    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel=settings['channel'])

        def sign_in(token):
            session = edge.new_context(viewport={'width': 1500, 'height': 930}, locale='zh-CN')
            session.add_init_script('if(location.origin===' + json.dumps(base) + ')localStorage.setItem("vf_token",'
                                    + json.dumps(token) + ');')
            return session

        context = sign_in(settings['owner'])
        page = context.new_page()
        page.set_default_timeout(22000)
        expect.set_options(timeout=22000)
        workspaces = page.get_by_label('当前工作区', exact=True)
        trash = page.get_by_test_id('trash-panel')
        alert = page.get_by_role('alert').filter(has_text='目标工作区已有同名算例')
        tab = page.locator(f'[data-editor-path="{FILE}"]')
        requests = []
        page.on('request', lambda request: requests.append(request.post_data_json)
                if request.method == 'POST' and urlsplit(request.url).path == '/api/v1' + prefix + ':move' else None)

        def locator(name):
            return page.locator('[data-case-name=' + json.dumps(name, ensure_ascii=False) + ']')

        def request(method, path, *, data=None, status=200):
            response = context.request.fetch(base + '/api/v1' + path, method=method, data=data,
                                              headers={'Authorization': 'Bearer ' + settings['owner']})
            assert response.status == status, (method, path, response.status, response.text()[:300])
            return response.json()

        def clicked(button, method, path, *, status=200, answer=None):
            if answer is not None:
                page.once('dialog', lambda dialog: dialog.accept(answer) if dialog.type == 'prompt' else dialog.accept())
            with page.expect_response(lambda response: response.request.method == method
                                      and urlsplit(response.url).path == '/api/v1' + path) as done:
                button.click()
            response = done.value
            assert response.status == status, (method, path, response.status, response.text()[:300])
            return response

        def fixture_snapshot():
            summary = request('GET', prefix)
            files = request('GET', prefix + '/file?' + urlencode({'path': FILE}))
            return {key: summary[key] for key in ('id', 'name', 'workspace_id', 'storage_prefix', 'dict_revision')} | {
                'file': files['content']}

        def chosen_workspace(workspace):
            expect(workspaces).to_have_value(str(workspace))

        try:
            page.goto(base, wait_until='domcontentloaded')
            workspaces.select_option(str(source))
            locator(SOURCE).click()
            page.locator(f'[data-path="{FILE}"]').click()
            expect(tab).to_have_attribute('data-active', 'true')
            page.wait_for_function('window.monaco?.editor.getModels().length')
            page.evaluate('(text) => window.monaco.editor.getModels().at(-1).setValue(text)', DRAFT)
            expect(tab).to_have_attribute('data-dirty', 'true')
            clicked(page.get_by_title('重命名工作区'), 'PATCH', f'/workspaces/{source}', answer='源工作区已改名')
            expect(workspaces.locator('option:checked')).to_have_text('源工作区已改名')
            expect(locator(SOURCE)).to_have_attribute('aria-pressed', 'true')
            expect(tab).to_have_attribute('data-dirty', 'true')
            assert page.evaluate('window.monaco.editor.getModels().at(-1).getValue()') == DRAFT
            assert fixture_snapshot()['file'] == 'original saved source'
            report['workspace_rename_kept_editor_and_exact_draft'] = True

            page.get_by_title('移动算例').click()
            form = page.get_by_role('form', name='移动算例')
            destination = form.get_by_label('目标工作区')
            expect(destination).to_be_visible()
            options = destination.locator('option').evaluate_all('(rows) => rows.map(row => [row.value,row.textContent.trim()])')
            candidates = {key: label for key, label in options if key}
            assert str(settings['foreign']) not in candidates and str(source) not in candidates
            assert {candidates[str(target)], candidates[str(settings['duplicate'])]} == {
                f"{DESTINATION}（编号 {target}）", f"{DESTINATION}（编号 {settings['duplicate']}）"}
            destination.select_option(str(target))
            page.once('dialog', lambda dialog: dialog.dismiss())
            form.get_by_role('button', name='确认移动', exact=True).click()
            expect(tab).to_have_attribute('data-dirty', 'true')
            assert requests == []
            report['move_offered_only_owned_ids_and_decline_kept_draft'] = True
            form.get_by_role('button', name='取消', exact=True).click()
            clicked(page.get_by_role('button', name='保存 (Ctrl+S)', exact=True), 'PUT', prefix + '/file')
            expect(tab).to_have_attribute('data-dirty', 'false')

            clicked(page.get_by_title('重命名算例'), 'PATCH', prefix, answer=CASE)
            expect(locator(CASE)).to_have_attribute('aria-pressed', 'true')
            before = fixture_snapshot()
            page.get_by_title('移动算例').click()
            destination.select_option(str(target))
            clicked(form.get_by_role('button', name='确认移动', exact=True), 'POST', prefix + ':move', status=409)
            expect(alert).to_be_visible()
            expect(destination).to_have_value(str(target))
            chosen_workspace(source)
            assert fixture_snapshot() == before
            report['duplicate_move_409_preserved_metadata_file_and_selected_target'] = True
            page.screenshot(path=str(root / 'move-conflict.png'))
            request('PATCH', f"/cases/{settings['blocker']}", data={'name': '已改名的目标占位'})
            moved = clicked(form.get_by_role('button', name='确认移动', exact=True), 'POST', prefix + ':move').json()
            chosen_workspace(target)
            expect(locator(CASE)).to_have_attribute('aria-pressed', 'true')
            expect(form).to_have_count(0)
            assert moved['id'] == case and moved['workspace_id'] == target
            assert fixture_snapshot() == before | {'workspace_id': target}
            assert requests == [{'workspace_id': target}, {'workspace_id': target}]
            report['move_retry_of_same_id'] = target

            copied = clicked(page.get_by_title('复制算例'), 'POST', prefix + ':copy', answer=CLONE).json()
            expect(locator(CLONE)).to_have_attribute('aria-pressed', 'true')
            copied_path = f"/cases/{copied['id']}"
            assert copied['id'] != case and request('GET', copied_path)['storage_prefix'] != before['storage_prefix']
            assert request('GET', copied_path + '/file?' + urlencode({'path': FILE}))['content'] == DRAFT
            request('PUT', copied_path + '/file', data={'path': FILE, 'content': 'copy only'})
            assert fixture_snapshot()['file'] == DRAFT
            copied_ws = clicked(page.get_by_title('复制工作区'), 'POST', f'/workspaces/{target}:copy',
                                answer=COPY_WORKSPACE).json()
            chosen_workspace(copied_ws['id'])
            expect(locator(CASE)).to_be_visible()
            expect(locator(CLONE)).to_be_visible()
            members = request('GET', f"/workspaces/{copied_ws['id']}/cases")
            originals = request('GET', f'/workspaces/{target}/cases')
            assert {member['name'] for member in members} == {member['name'] for member in originals}
            assert not {member['id'] for member in members} & {member['id'] for member in originals}
            report['copy'] = {'case': copied['id'], 'workspace': copied_ws['id'],
                              'members': {item['name']: item['id'] for item in members}}

            workspaces.select_option(str(target))
            locator(CASE).click()
            clicked(page.get_by_test_id('case-delete'), 'DELETE', prefix, answer='')
            expect(locator(CASE)).to_have_count(0)
            request('GET', prefix, status=404)
            page.get_by_test_id('trash-toggle').click()
            case_restore = trash.get_by_test_id(f'case-restore-{case}')
            expect(case_restore).to_be_visible()
            clicked(case_restore, 'POST', prefix + ':restore')
            expect(locator(CASE)).to_be_visible()
            expect(case_restore).to_have_count(0)
            assert fixture_snapshot() == before | {'workspace_id': target}
            clicked(page.get_by_test_id('workspace-delete'), 'DELETE', f'/workspaces/{target}', answer='')
            request('GET', prefix, status=404)
            workspace_restore = trash.get_by_test_id(f'workspace-restore-{target}')
            expect(workspace_restore).to_be_visible()
            page.screenshot(path=str(root / 'workspace-recycle-bin.png'))
            clicked(workspace_restore, 'POST', f'/workspaces/{target}:restore')
            chosen_workspace(target)
            expect(locator(CASE)).to_be_visible()
            expect(locator(CLONE)).to_be_visible()
            expect(workspace_restore).to_have_count(0)
            assert fixture_snapshot() == before | {'workspace_id': target}
            report['case_and_workspace_trash_restored_original_identity'] = True

            viewer = sign_in(settings['viewer'])
            guest = viewer.new_page()
            guest.goto(base, wait_until='domcontentloaded')
            guest.get_by_label('当前工作区', exact=True).select_option(str(target))
            guest.locator('[data-case-name=' + json.dumps(CASE, ensure_ascii=False) + ']').click()
            for tool in ('重命名工作区', '复制工作区', '工作区移入回收站', '重命名算例', '移动算例', '复制算例', '算例移入回收站'):
                expect(guest.get_by_title(tool)).to_be_disabled()
            report['viewer_admin_controls_disabled'] = True
            report['passed'] = True
        finally:
            (root / 'workspace-lifecycle-browser.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
            if not report.get('passed'):
                page.screenshot(path=str(root / 'workspace-lifecycle-failure.png'))
            edge.close()


def verify(args):
    from testsupport import IsolatedServer

    api = IsolatedServer('a23-project-admin', app='project_admin_fixture:app')
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    (api.root / 'project_admin_fixture.py').write_text('from app.main import app\n'
        'from fastapi.staticfiles import StaticFiles\n'
        + 'app.mount("/", StaticFiles(directory=' + repr(str(dist)) + ', html=True))\n', encoding='utf8')
    temporary = api.root / 'browser-tmp'
    temporary.mkdir()
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0, 'storage': 'fs', 'database': 'sqlite'}
    print('WORKSPACE_LIFECYCLE_EVIDENCE', api.root, flush=True)
    try:
        with api:
            email = 'lifecycle-viewer@example.com'
            account = api.call('POST', '/auth/register', {'email': email, 'name': 'Viewer',
                                'password': 'isolated-lifecycle-123'})
            viewer = {'Authorization': 'Bearer ' + account['token']}
            foreign = api.call('POST', '/workspaces', {'name': DESTINATION}, extra_headers=viewer)['id']
            foreign_org = api.call('GET', '/org/mine', extra_headers=viewer)['org']['id']
            case = api.create_case(SOURCE)
            owner = api.call('GET', '/auth/me')
            source = api.call('GET', api.prefix)['workspace_id']
            duplicate = api.call('POST', '/workspaces', {'name': DESTINATION})['id']
            target = api.call('POST', '/workspaces', {'name': DESTINATION})['id']
            api.call('POST', f'/org/{foreign_org}/members', {'email': owner['email'], 'role': 'viewer'}, expect=201, extra_headers=viewer)
            api.call('POST', f'/org/{foreign_org}/invitation:accept', {})
            api.call('PUT', f'/workspaces/{foreign}/sharing', {'org_id': foreign_org, 'role': 'viewer'}, extra_headers=viewer)
            blocker = api.call('POST', f'/workspaces/{target}/cases', {'name': CASE})['id']
            org = api.call('GET', '/org/mine')['org']['id']
            api.call('POST', f'/org/{org}/members', {'email': email, 'role': 'viewer'}, expect=201)
            api.call('POST', f'/org/{org}/invitation:accept', {}, extra_headers=viewer)
            api.call('PUT', f'/workspaces/{target}/sharing', {'org_id': org, 'role': 'viewer'})
            assert any(row['id'] == foreign and row['shared'] for row in api.call('GET', '/workspaces'))
            api.call('POST', api.prefix + ':move', {'workspace_id': foreign}, expect=404)
            api.call('PATCH', api.prefix, {'name': 'FORBIDDEN'}, expect=404, extra_headers=viewer)
            api.put(FILE, 'original saved source')
            dictionary = api.call('GET', api.prefix + '/caseDict')
            command = f"from pathlib import Path; Path({PUBLISHED!r}).write_bytes(bytes.fromhex({PAYLOAD.hex()!r}))"
            job = api.call('POST', api.prefix + '/jobs', {'type': 'convert', 'runtime': 'windows',
                'command': f'"{sys.executable}" -c "{command}"', 'timeout_seconds': 30})
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                finished = api.call('GET', f"/jobs/{job['id']}")
                if finished['status'] == 'succeeded' and not finished['completion_pending']:
                    break
                assert finished['status'] not in {'failed', 'cancelled', 'interrupted'}, finished['status']
                time.sleep(.2)
            assert finished['status'] == 'succeeded' and not finished['completion_pending']
            attempt = finished['attempt']
            publication = attempt['outputs']['publication']
            assert attempt['process_stop']['verified'] and publication['status'] == 'committed'
            physical = api.root / 'files' / api.call('GET', api.prefix)['storage_prefix']
            immutable = physical / '.vibeflow' / 'publications' / publication['id'] / 'files' / PUBLISHED
            assert immutable.read_bytes() == PAYLOAD
            report['source_publication'] = {'job': job['id'], 'attempt': attempt['id'],
                'id': publication['id'], 'sha256': hashlib.sha256(PAYLOAD).hexdigest()}
            settings = {'base': api.base, 'owner': api.token, 'viewer': account['token'], 'case': case['id'],
                'source': source, 'target': target, 'duplicate': duplicate, 'foreign': foreign, 'blocker': blocker,
                'channel': args.channel, 'root': str(api.root)}
            subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
                input=json.dumps(settings), text=True, check=True, timeout=170,
                env=dict(os.environ, TMP=str(temporary), TEMP=str(temporary)))
            view = json.loads((api.root / 'workspace-lifecycle-browser.json').read_text('utf8'))
            after = api.call('GET', f"/jobs/{job['id']}")['attempt']
            assert after['id'] == attempt['id'] and after['outputs']['publication'] == publication
            assert immutable.read_bytes() == PAYLOAD
            published_raw = '/raw?' + urlencode({'path': PUBLISHED})
            assert api.call('GET', api.prefix + published_raw, raw=True) == PAYLOAD
            assert api.call('GET', api.prefix + '/caseDict')['content_sha256'] == dictionary['content_sha256']
            for copied in (view['copy']['case'], view['copy']['members'][CASE]):
                copied_path = f'/cases/{copied}'
                copied_root = api.root / 'files' / api.call('GET', copied_path)['storage_prefix']
                assert api.call('GET', copied_path + published_raw, raw=True) == PAYLOAD
                assert not (copied_root / '.vibeflow' / 'publications' / publication['id']).exists()
                assert api.call('GET', copied_path + '/caseDict')['content_sha256'] == dictionary['content_sha256']
            api.call('DELETE', api.prefix, expect=404, extra_headers=viewer)
            api.call('POST', api.prefix + ':restore', {}, expect=404, extra_headers=viewer)
            api.call('DELETE', f'/workspaces/{target}', expect=404, extra_headers=viewer)
            assert api.call('GET', '/trash', extra_headers=viewer) == {'workspaces': [], 'cases': []}
            report['browser'] = view
            report['original_publication_and_case_dictionary_preserved'] = True
            report['copies_got_live_bytes_without_original_immutable_history'] = True
            report['viewer_lifecycle_mutation_denied'] = True
        report['passed'] = view['passed'] and api.proc.poll() is not None
    finally:
        report['api_exited'] = api.proc is not None and api.proc.poll() is not None
        (api.root / 'workspace-lifecycle-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    assert report['passed']
    print('WORKSPACE_LIFECYCLE_PASS', api.root / 'workspace-lifecycle-result.json', flush=True)


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
