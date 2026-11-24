"""Real browser and cold API acceptance for the caseDict file and SQL stage mirror."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.parse import urlencode, urlsplit


CONCURRENT_LINE = b'\n// isolated concurrent source change\n'
FAULT_APP = '''from pathlib import Path
from app.main import app
from app.services import engineering_stage

original_reason = engineering_stage.evidence_reason

async def change_after_read(db, case, target):
    reason = await original_reason(db, case, target)
    marker = Path.cwd() / 'change-before-stage-commit'
    if reason is None and target == 'V' and marker.is_file():
        assert marker.read_text(encoding='utf8') == str(case.id)
        target_file = (Path.cwd() / 'files' / case.s3_prefix / '0-caseDict/caseDict').resolve()
        assert target_file.is_relative_to(Path.cwd().resolve())
        marker.unlink()
        with target_file.open('ab') as file:
            file.write(%r)
    return reason

engineering_stage.evidence_reason = change_after_read
''' % CONCURRENT_LINE


async def browser(settings):
    from playwright.async_api import async_playwright, expect

    root = Path(settings['root'])
    stage = settings['stage']
    result = {}
    expect.set_options(timeout=25000)
    async with async_playwright() as driver:
        chromium = await driver.chromium.launch(channel=settings['channel'])

        async def open_case(token, shared=False):
            context = await chromium.new_context(viewport={'width': 1500, 'height': 930})
            await context.add_init_script('if(location.origin===' + json.dumps(settings['base'])
                + ')localStorage.setItem("vf_token",' + json.dumps(token) + ');')
            page = await context.new_page()
            page.set_default_timeout(25000)
            await page.goto(settings['base'])
            if shared:
                await page.get_by_label('当前工作区', exact=True).select_option(str(settings['workspace_id']))
            await page.locator('[data-case-name=' + json.dumps(settings['case']) + ']').click()
            heading = page.get_by_role('heading', name=re.compile(r'^当前阶段'))
            if not await heading.is_visible():
                tab = page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^caseDict$'))
                if await tab.is_visible():
                    await tab.click()
            await expect(heading).to_be_visible()
            return context, page, heading

        owner = page = None
        try:
            owner, page, heading = await open_case(settings['owner_token'])
            names = {'A': 'A 几何', 'V': 'V 开发'}
            await expect(heading).to_have_text('当前阶段 ' + names[settings['previous']])
            target = page.get_by_label('目标阶段', exact=True)
            prepare = page.get_by_role('button', name='核对阶段确认', exact=True)
            if stage == 'A':
                await target.select_option('B')
                await expect(prepare).to_be_disabled()
                await expect(page.get_by_text('阶段只能逐级推进；请先完成当前阶段。', exact=True)).to_be_visible()
                viewer, viewer_page, viewer_heading = await open_case(settings['viewer_token'], shared=True)
                try:
                    await expect(viewer_heading).to_have_text('当前阶段 V 开发')
                    await viewer_page.get_by_label('目标阶段', exact=True).select_option('A')
                    await expect(viewer_page.get_by_role('button', name='核对阶段确认', exact=True)).to_be_disabled()
                    await expect(viewer_page.get_by_text(re.compile(r'^当前工作区为只读'))).to_be_visible()
                    result['shared_viewer_read_only'] = True
                finally:
                    await viewer.close()
            await target.select_option(stage)
            await prepare.click()
            await expect(heading).to_have_text('当前阶段 ' + names[settings['previous']])
            confirm = page.get_by_role('button', name=f'明确确认{stage}阶段', exact=True)
            await expect(confirm).to_be_enabled()
            path = '/api/v1/cases/' + str(settings['case_id']) + '/caseDict:advance'
            async with page.expect_response(lambda response: urlsplit(response.url).path == path
                and response.request.method == 'POST') as received:
                await confirm.click()
            response = await received.value
            assert response.status == 200, (response.status, await response.text())
            assert response.request.post_data_json == {'state': stage, 'confirmed': True, 'expected_revision': settings['revision']}
            receipt = await response.json()
            assert receipt['workflow_state'] == stage and receipt['dict_revision'] == settings['revision'] + 1
            await expect(heading).to_have_text('当前阶段 ' + names[stage])
            await heading.screenshot(path=str(root / f'case-dict-stage-{stage}.png'))
            result['confirmed'] = receipt
        except Exception:
            if page is not None:
                await page.screenshot(path=str(root / f'case-dict-stage-{stage}-failure.png'))
            raise
        finally:
            if owner is not None:
                await owner.close()
            await chromium.close()
            (root / f'case-dict-stage-{stage}.json').write_text(json.dumps(result, indent=2), encoding='utf8')


def verify(args):
    import httpx
    from app.services.casedict import CASE_DICT_PATH, set_block, set_workflow_state
    from app.services.case_events import CASE_EVENTS_PATH
    from app.services.provenance import PROVENANCE_PATH
    from live_llm_outbox_restart_acceptance import restart
    from testsupport import IsolatedServer

    api = IsolatedServer('casedict-authority-browser', app='casedict_authority_fixture:app')
    bundle = args.dist.resolve(strict=True)
    assert (bundle / 'index.html').is_file()
    (api.root / 'casedict_authority_fixture.py').write_text(FAULT_APP
        + 'from fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='
        + repr(str(bundle)) + ', html=True))\n', encoding='utf8')
    browser_tmp = api.root / 'browser-tmp'
    browser_tmp.mkdir()
    report = {'passed': False, 'real_model_requests': 0, 'mock_llm_calls': 0}
    reference_files = [CASE_DICT_PATH, CASE_EVENTS_PATH, PROVENANCE_PATH]

    def digest(data):
        return hashlib.sha256(data).hexdigest()

    def snapshot():
        return {path: digest((folder / path).read_bytes()) for path in reference_files}

    def history(state, revision, status):
        info = api.call('GET', api.prefix + '/caseDict')
        mirror = api.call('GET', api.prefix)
        raw = (folder / CASE_DICT_PATH).read_bytes()
        assert api.call('GET', api.prefix + '/raw?' + urlencode({'path': CASE_DICT_PATH}), raw=True) == raw
        assert info['content'].encode('utf8') == raw and info['content_sha256'] == digest(raw)
        assert info['workflow_state'] == mirror['workflow_state'] == state
        assert info['dict_revision'] == mirror['dict_revision'] == revision
        assert info['parse_status'] == mirror['parse_status'] == 'ok'
        assert info['parsed']['caseMeta']['status'] == status
        with sqlite3.connect('file:' + (api.root / 'test.sqlite').as_posix() + '?mode=ro', uri=True) as db:
            sql_stage, sql_revision, sql_parse, metadata = db.execute(
                'SELECT workflow_state,dict_revision,parse_status,case_meta FROM cases WHERE id=?', (case['id'],)).fetchone()
        assert (sql_stage, sql_revision, sql_parse) == (state, revision, 'ok')
        assert json.loads(metadata) == json.loads(mirror['case_meta']) == info['parsed']['caseMeta']
        events = [json.loads(line) for line in (folder / CASE_EVENTS_PATH).read_bytes().splitlines()]
        chain = api.call('GET', api.prefix + '/provenance')
        latest = chain['events'][0]
        assert chain['chain_valid'] and latest['casedict_sha256'] == digest(raw)
        assert (latest['workflow_state'], latest['revision']) == (state, revision)
        assert (events[-1]['workflow_state'], events[-1]['revision']) == (state, revision)
        return {'workflow_state': state, 'dict_revision': revision, 'case_meta_status': status,
                'sha256': snapshot(), 'event_count': len(events), 'provenance_count': chain['total']}

    def advance(target, revision, *, confirmed=True, expect=200, extra_headers=None):
        return api.call('POST', api.prefix + '/caseDict:advance', {'state': target, 'confirmed': confirmed,
            'expected_revision': revision}, expect=expect, extra_headers=extra_headers)

    def browser_stage(target, previous, revision, viewer_token):
        settings = {'base': api.base, 'root': str(api.root), 'case': case['name'], 'case_id': case['id'],
            'workspace_id': workspace_id, 'channel': args.channel, 'stage': target, 'previous': previous,
            'revision': revision, 'owner_token': api.token, 'viewer_token': viewer_token}
        subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', str(Path(__file__)), '--browser'],
            input=json.dumps(settings), text=True, check=True, timeout=130,
            env=dict(os.environ, TEMP=str(browser_tmp), TMP=str(browser_tmp)))
        report['browser_' + target] = json.loads((api.root / f'case-dict-stage-{target}.json').read_text(encoding='utf8'))

    password = 'isolated-test-123'
    viewer_login = {'email': 'dictionary-viewer@example.com', 'password': password}
    try:
        with api:
            case = api.create_case('Actual caseDict authority')
            mirror = api.call('GET', api.prefix)
            folder = (api.root / 'files' / mirror['storage_prefix']).resolve()
            assert folder.is_relative_to(api.root.resolve()) and folder.is_dir()
            workspace_id = mirror['workspace_id']
            initial = api.call('GET', api.prefix + '/caseDict')
            saved = api.call('PUT', api.prefix + '/caseDict', {'content': set_block(initial['content'], 'caseMeta',
                {'status': 'draft', 'displayName': 'Authority test'}),
                'expected_revision': initial['dict_revision'], 'expected_sha256': initial['content_sha256']})
            revision = saved['dict_revision']
            report['initial'] = history('A', revision, 'draft')
            canonical = folder / CASE_DICT_PATH
            original = canonical.read_bytes()
            divergent = set_workflow_state(original.decode('utf8'), 'V').encode('utf8')
            try:
                canonical.write_bytes(divergent)
                unmatched = snapshot()
                assert api.call('GET', api.prefix + '/caseDict')['workflow_state'] == 'V'
                assert api.call('GET', api.prefix)['workflow_state'] == 'A'
                results = {target: advance(target, revision, expect=409)['detail'] for target in ('A', 'V')}
                assert all(info['code'] == 'case_dict_state_conflict' for info in results.values()), results
                assert snapshot() == unmatched and api.call('GET', api.prefix)['dict_revision'] == revision
                report['disagreement'] = results
            finally:
                canonical.write_bytes(original)
            trigger = api.root / 'change-before-stage-commit'
            trigger.write_text(str(case['id']), encoding='utf8')
            try:
                collision = advance('V', revision, expect=409)['detail']
                assert collision['code'] == 'file_conflict' and canonical.read_bytes() == original + CONCURRENT_LINE
                assert not trigger.exists()
                assert {key: digest((folder / key).read_bytes()) for key in reference_files[1:]} == {
                    key: report['initial']['sha256'][key] for key in reference_files[1:]}
                assert api.call('GET', api.prefix)['dict_revision'] == revision
                report['concurrent_source_edit_preserved'] = True
            finally:
                canonical.write_bytes(original)

            outsider = api.call('POST', '/auth/register', viewer_login | {'name': 'Dictionary viewer'})
            viewer_headers = {'Authorization': 'Bearer ' + outsider['token']}
            api.call('GET', api.prefix + '/caseDict', expect=404, extra_headers=viewer_headers)
            advance('V', revision, expect=404, extra_headers=viewer_headers)
            org_id = api.call('GET', '/org/mine')['org']['id']
            api.call('POST', f'/org/{org_id}/members', {'email': viewer_login['email'], 'role': 'viewer'}, expect=201)
            api.call('POST', f'/org/{org_id}/invitation:accept', {}, extra_headers=viewer_headers)
            api.call('PUT', f'/workspaces/{workspace_id}/sharing', {'org_id': org_id, 'role': 'viewer', 'enabled': True})
            api.call('GET', api.prefix + '/caseDict', extra_headers=viewer_headers)
            advance('V', revision, expect=404, extra_headers=viewer_headers)
            api.call('PUT', api.prefix + '/caseDict', {'content': original.decode('utf8')}, expect=404, extra_headers=viewer_headers)
            report['foreign_and_viewer_denials'] = True

            aliases = [CASE_DICT_PATH, './' + CASE_DICT_PATH, '0-caseDict//caseDict']
            for variant in aliases:
                api.call('PUT', api.prefix + '/file', {'path': variant, 'content': divergent.decode('utf8')}, expect=400)
                api.call('DELETE', api.prefix + '/file?' + urlencode({'path': variant}), expect=400)
            with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token}, trust_env=False) as client:
                upload = client.post(api.prefix + '/upload', params={'path': aliases[1]},
                    files={'file': ('caseDict', divergent, 'text/plain')})
                assert upload.status_code == 400, upload.text
            api.call('PUT', api.prefix + '/caseDict', {'content': divergent.decode('utf8'),
                'expected_revision': revision, 'expected_sha256': digest(original)}, expect=409)
            advance('V', revision, confirmed=False, expect=409)
            assert history('A', revision, 'draft') == report['initial']
            report['protected_writer_denials'] = len(aliases) * 2 + 3
            browser_stage('V', 'A', revision, outsider['token'])
            report['entered_v'] = history('V', revision + 1, 'draft')

            first_api = api.proc
            first_api.terminate()
            first_api.wait(timeout=15)
            restart(api)
            assert first_api.poll() is not None and api.proc.pid != first_api.pid
            report['restarted_v'] = history('V', revision + 1, 'draft')
            assert report['restarted_v'] == report['entered_v']
            for target in ('B', 'D', 'E'):
                advance(target, revision + 1, expect=409)
            advance('A', revision, expect=409)
            assert advance('V', revision + 1) == {'ok': True, 'workflow_state': 'V', 'dict_revision': revision + 1}
            assert history('V', revision + 1, 'draft') == report['entered_v']
            browser_stage('A', 'V', revision + 1, outsider['token'])
            report['returned_a'] = history('A', revision + 2, 'readyForGeometry')
            for previous, next_state in ((report['initial'], report['entered_v']), (report['entered_v'], report['returned_a'])):
                assert next_state['event_count'] == previous['event_count'] + 1
                assert next_state['provenance_count'] == previous['provenance_count'] + 1
            report['isolated_api_restarted'] = True
        report['passed'] = api.proc.poll() is not None
    finally:
        report['api_exited'] = api.proc is not None and api.proc.poll() is not None
        (api.root / 'casedict-authority-result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print('CASEDICT_AUTHORITY_BROWSER_OK', api.root, flush=True)


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        parser.add_argument('--channel', default='msedge')
        verify(parser.parse_args())
