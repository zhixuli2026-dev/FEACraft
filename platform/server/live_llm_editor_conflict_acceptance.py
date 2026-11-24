"""Chromium conflict recovery, bounded reads, and actual DeepSeek evidence readback."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from urllib.parse import urlencode, urlsplit


FILE = 'Ref/editor-conflict.txt'
DICT = '0-caseDict/caseDict'
SLOW = 'Ref/editor-read.txt'
BYTES = b'x' * (64 * 1024 - 2) + '🌊中文'.encode() + b'\xff\x00\xf0\x9f'
BUSY = {'storage_quota_busy', 'validation_busy'}

FIXTURE = '''
from contextlib import asynccontextmanager, contextmanager
import time
from pathlib import Path
from app.services import case_read_capacity, case_tree
from app.services.file_read import FileRead
from app.services.request_deadline import scope

editor_test_root = Path(__file__).parent

def pause_for_test(key):
    if not (editor_test_root / (key + '-pause')).exists():
        return
    (editor_test_root / (key + '-entered')).touch()
    end = time.monotonic() + 25
    while not (editor_test_root / (key + '-release')).exists():
        if time.monotonic() >= end:
            raise RuntimeError('Test storage barrier was never released')
        time.sleep(.02)

original_open = FileRead._open
@contextmanager
def editor_open(self, start=None, end=None):
    name = self.location['Key'] if self.remote else self.path.as_posix()
    if name.endswith('/Ref/editor-read.txt'):
        pause_for_test('text')
    with original_open(self, start, end) as selected:
        yield selected
FileRead._open = editor_open

original_tree = case_tree.full_entries
def editor_tree(prefix):
    pause_for_test('tree')
    return original_tree(prefix)
case_tree.full_entries = editor_tree

original_budget = case_read_capacity.file_read_deadline
@asynccontextmanager
async def tree_budget(code, label):
    async with original_budget(code, label):
        if code == 'file_tree' and (editor_test_root / 'tree-pause').exists():
            async with scope(1.5):
                yield
        else:
            yield
case_read_capacity.file_read_deadline = tree_budget
'''


def browser_check(args):
    from playwright.sync_api import expect, sync_playwright

    root = Path(args['root'])
    prefix = '/api/v1' + args['prefix']
    checks = []
    writes = []
    busy_responses = []
    review_seconds = []

    with sync_playwright() as play:
        browser = play.chromium.launch()
        ctx = browser.new_context(viewport={'width':1280, 'height':840}, locale='zh-CN')
        ctx.add_init_script('if (location.origin === ' + json.dumps(args['base']) + ') localStorage.setItem(\'vf_token\', '
                            + json.dumps(args['token']) + ');')
        page = ctx.new_page()
        peer = play.request.new_context(base_url=args['peer'], extra_http_headers={'Authorization':'Bearer ' + args['token']})

        def button(name):
            return page.get_by_role('button', name=name, exact=True)

        def tab(path):
            return page.locator('[data-editor-path=' + json.dumps(path) + ']')

        def row(path):
            return page.locator('[data-path=' + json.dumps(path) + ']')

        def open_file(path):
            folder = row(path.split('/')[0])
            if folder.inner_text().startswith('▸'):
                folder.click()
            row(path).click()
            expect(tab(path)).to_have_attribute('data-active', 'true')

        def current_text():
            return page.evaluate('window.monaco.editor.getModels().at(-1).getValue()')

        def edit(text):
            page.evaluate('(text) => window.monaco.editor.getModels().at(-1).setValue(text)', text)

        def retry_if_busy(response, actor, route, deadline):
            if response.status != 409:
                return False
            detail = response.json().get('detail')
            if not isinstance(detail,dict) or detail.get('code') not in BUSY:
                return False
            busy_responses.append({'actor':actor,'path':route,'code':detail['code']})
            assert time.monotonic() < deadline, busy_responses
            page.wait_for_timeout(120)
            return True

        def save(route, status, *, code=None):
            deadline = time.monotonic() + 20
            while True:
                with page.expect_response(lambda r: r.request.method == 'PUT' and urlsplit(r.url).path == prefix + route) as pending:
                    button('保存 (Ctrl+S)').click()
                response = pending.value
                if not retry_if_busy(response,'editor',route,deadline):
                    break
            assert response.status == status, (route, response.status, response.text()[:400])
            if code:
                assert response.json()['detail']['code'] == code, response.json()
            if status >= 400:
                expect(page.get_by_test_id('editor-save-error')).to_be_visible()
            body = response.request.post_data_json
            writes.append({'path':route, 'status':status, 'condition':body.get('expected_sha256'),
                           'condition_sent':'expected_sha256' in body, 'revision':body.get('expected_revision')})
            return body

        def other(method, route, *, params=None, data=None, status=200):
            deadline = time.monotonic() + 20
            while True:
                response = peer.fetch(prefix + route, method=method, params=params, data=data)
                if not retry_if_busy(response,'peer',route,deadline):
                    break
            assert response.status == status, (method, route, response.status, response.text()[:400])
            return response.json()

        def review(text):
            button('查看服务器版本').click()
            started = time.monotonic()
            expect(page.get_by_test_id('editor-server-version')).to_contain_text(text,timeout=35000)
            review_seconds.append(round(time.monotonic()-started,3))

        try:
            page.goto(args['base'], wait_until='domcontentloaded')
            page.locator('[data-case-name=' + json.dumps(args['case_name']) + ']').click(timeout=30000)
            open_file(FILE)
            page.wait_for_function("window.monaco?.editor.getModels().at(-1)?.getValue() === 'BASE'")
            draft = '本地草稿🌊'
            edit(draft)
            other('PUT','/file',data={'path':FILE,'content':'PEER'})
            body = save('/file', 409, code='file_conflict')
            assert body['expected_sha256'] == hashlib.sha256(b'BASE').hexdigest()
            assert current_text() == draft and other('GET','/file',params={'path':FILE})['content'] == 'PEER'
            review('PEER')
            page.once('dialog', lambda dialog: dialog.dismiss())
            button('使用服务器版本').click()
            assert current_text() == draft
            button('保留草稿，基于此版本继续编辑').click()
            body = save('/file', 200)
            assert body['expected_sha256'] == hashlib.sha256(b'PEER').hexdigest()
            expect(tab(FILE)).to_have_attribute('data-dirty','false')
            assert other('GET','/file',params={'path':FILE})['content'] == draft
            checks.append('peer_same_length_update_keeps_draft_and_reviewed_save_uses_new_hash')

            new_draft = '删除后保留🌊'
            edit(new_draft)
            other('DELETE','/file',params={'path':FILE})
            save('/file', 409, code='file_conflict')
            review('服务器文件已删除。')
            page.screenshot(path=root/'editor-deleted.png')
            page.once('dialog', lambda dialog: dialog.dismiss())
            button('放弃草稿并关闭').click()
            assert current_text() == new_draft
            button('保留草稿，按新文件保存').click()
            other('PUT','/file',data={'path':FILE,'content':'CONCURRENT RECREATION'})
            body = save('/file', 409, code='file_conflict')
            assert body['expected_sha256'] is None
            assert current_text() == new_draft
            assert other('GET','/file',params={'path':FILE})['content'] == 'CONCURRENT RECREATION'
            other('DELETE','/file',params={'path':FILE})
            review('服务器文件已删除。')
            button('保留草稿，按新文件保存').click()
            assert save('/file', 200)['expected_sha256'] is None
            expect(tab(FILE)).to_have_attribute('data-dirty','false')
            assert other('GET','/file',params={'path':FILE})['content'] == new_draft
            checks.append('explicit_absence_precondition_refuses_peer_recreation_and_allows_safe_retry')

            edit('discarded draft')
            other('DELETE','/file',params={'path':FILE})
            save('/file', 409, code='file_conflict')
            review('服务器文件已删除。')
            page.once('dialog', lambda dialog: dialog.accept())
            button('放弃草稿并关闭').click()
            expect(tab(FILE)).to_have_count(0)
            assert not other('GET','/file:version',params={'path':FILE})['exists']
            checks.append('confirmed_discard_closes_editor_with_file_still_absent')

            original = other('GET','/caseDict')
            open_file(DICT)
            page.wait_for_function('(text) => window.monaco?.editor.getModels().at(-1)?.getValue() === text', arg=original['content'])
            revision, digest = original['dict_revision'], original['content_sha256']
            peer_text = original['content'] + '\n// EXTERNAL DICTIONARY\n'
            edit(original['content'] + '\n// LOCAL DICTIONARY\n')
            other('PUT','/caseDict',data={'content':peer_text,'expected_revision':revision,'expected_sha256':digest})
            body = save('/caseDict', 409, code='revision_conflict')
            assert (body['expected_revision'], body['expected_sha256']) == (revision, digest)
            assert 'LOCAL DICTIONARY' in current_text()
            review('EXTERNAL DICTIONARY')
            page.once('dialog', lambda dialog: dialog.accept())
            button('使用服务器版本').click()
            expect(tab(DICT)).to_have_attribute('data-dirty','false')
            assert current_text() == peer_text
            edit(original['content'])
            with page.expect_request(lambda r: r.method == 'GET' and urlsplit(r.url).path == prefix):
                body = save('/caseDict', 200)
            assert (body['expected_revision'],body['expected_sha256']) == (revision+1,hashlib.sha256(peer_text.encode()).hexdigest())
            expect(tab(DICT)).to_have_attribute('data-dirty','false')
            assert other('GET','')['dict_revision'] == revision+2
            restored = other('GET','/caseDict')
            assert restored['content'] == original['content'] and restored['parsed'] == original['parsed']
            assert (restored['dict_revision'], restored['content_sha256']) == (revision+2, digest)
            checks.append('dictionary_conflict_review_and_exact_text_restoration_with_revision_and_hash')
            edit(current_text().replace('workflowState A', 'workflowState B'))
            save('/caseDict', 409)
            expect(page.get_by_test_id('editor-save-error')).to_contain_text('不得修改 workflowState')
            assert 'workflowState B' in current_text() and other('GET','/caseDict') == restored
            checks.append('ordinary_dictionary_save_cannot_advance_workflow_and_keeps_rejected_draft')
        except Exception:
            page.screenshot(path=root/'editor-browser-failure.png',timeout=5000)
            raise
        finally:
            (root/'editor-browser.json').write_text(json.dumps({'checks':checks,'writes':writes,
                'busy_responses':busy_responses,'review_seconds':review_seconds,'chromium':browser.version},indent=2))
            peer.dispose()
            browser.close()


def capacity_checks(api, peer):
    import httpx
    from live_llm_failure_fallback_acceptance import wait_for
    from live_llm_job_repeat_acceptance import put

    path = api.prefix + '/file?' + urlencode({'path':SLOW})
    headers = {'Authorization':'Bearer ' + api.token}
    with httpx.Client(base_url=api.base+'/api/v1', headers=headers, trust_env=False, timeout=22) as client:
        upload = client.post(api.prefix+'/upload', params={'path':SLOW}, files={'file':('editor-read.txt',BYTES,'text/plain')})
        assert upload.status_code == 200, upload.status_code
        first = api.call('GET',path)
        assert (first['content'], first['size'], first['content_sha256']) == (
            BYTES.decode('utf8',errors='replace'),len(BYTES),hashlib.sha256(BYTES).hexdigest())

        def run_paused(key, url, during):
            (api.root/(key+'-pause')).touch()
            with ThreadPoolExecutor(1) as pool:
                reply = pool.submit(client.get,url)
                try:
                    wait_for((api.root/(key+'-entered')).exists, 'No storage worker reached the fixture barrier')
                    assert api.call('GET','/api/health',api_prefix=False)['ok']
                    during(reply)
                finally:
                    (api.root/(key+'-release')).touch()
                    (api.root/(key+'-pause')).unlink()
                return reply.result(timeout=22)

        changed = 'committed while the editor body worker was paused'
        stale = run_paused('text',path,lambda _: put(peer,SLOW,changed))
        assert stale.status_code == 409 and stale.json()['detail']['code'] == 'file_changed_during_read', stale.status_code
        assert api.call('GET',path)['content'] == changed

        def verify_draining(reply):
            time.sleep(1.65)
            assert not reply.done(), 'Tree reply escaped before its worker drained'
            assert api.call('GET','/api/health',api_prefix=False)['ok']

        expired = run_paused('tree',api.prefix+'/tree',verify_draining)
        assert expired.status_code == 504 and expired.json()['detail']['code'] == 'file_tree_timeout', expired.status_code
        tree = api.call('GET',api.prefix+'/tree')
        assert isinstance(tree['watermark'],int) and any(entry['path']==SLOW for entry in tree['entries'])
    return {'split_utf8_size':len(BYTES),'raw_sha256':first['content_sha256'],'stale_read_status':stale.status_code,
            'concurrent_same_case_commit':True,'worker_drained_before_tree_timeout':True,'tree_retry_watermark':tree['watermark']}


def scenario(api, *, store=None, **_):
    from live_llm_job_repeat_acceptance import put
    from live_llm_publication_restart_acceptance import worker_peer

    api.create_case('Editor conflict ' + uuid.uuid4().hex[:8])
    case = api.call('GET',api.prefix)
    put(api,FILE,'BASE')
    path = api.prefix + '/file:version?' + urlencode({'path':FILE})
    dictionary = api.prefix + '/caseDict'
    email = uuid.uuid4().hex + '@example.com'
    guest = api.call('POST','/auth/register',{'name':'Editor guest','email':email,'password':'isolated-editor-123'})
    viewer = {'Authorization':'Bearer '+guest['token']}
    for endpoint in path,dictionary:
        api.call('GET',endpoint,expect=404,extra_headers=viewer)
    organization = api.call('GET','/org/mine')['org']['id']
    api.call('POST',f'/org/{organization}/members',{'email':email,'role':'viewer'},expect=201)
    api.call('POST',f'/org/{organization}/invitation:accept',{},extra_headers=viewer)
    workspace = api.call('GET','/workspaces')[0]['id']
    share = {'org_id':organization,'role':'viewer','enabled':True}
    api.call('PUT',f'/workspaces/{workspace}/sharing',share)
    assert api.call('GET',path,extra_headers=viewer)['exists']
    original = api.call('GET',dictionary,extra_headers=viewer)
    assert original['parse_status'] == 'ok'
    api.call('PUT',api.prefix+'/file',{'path':FILE,'content':'forbidden','expected_sha256':None},expect=(403,404),extra_headers=viewer)
    api.call('PUT',dictionary,{'content':original['content']},expect=(403,404),extra_headers=viewer)
    api.call('PUT',f'/workspaces/{workspace}/sharing',{**share,'enabled':False})
    api.call('GET',path,expect=404,extra_headers=viewer)
    api.call('GET',api.prefix+'/file?path=../outside.txt',expect=422)
    api.call('PUT',api.prefix+'/file',{'path':DICT,'content':'forbidden','expected_sha256':None},expect=(400,409,422))

    with worker_peer(api,store) as peer:
        capacity = capacity_checks(api,peer)
        args = {'base':api.base,'peer':peer.base,'prefix':api.prefix,'case_name':case['name'],'root':str(api.root),'token':api.token}
        subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'],str(Path(__file__).resolve()),'--browser'],
                       input=json.dumps(args),text=True,timeout=210,check=True)
    result = {'browser':json.loads((api.root/'editor-browser.json').read_text()),'capacity':capacity,
              'foreign_account_denied':True,'viewer_cannot_write':True,'protected_dictionary_cannot_be_created_as_ordinary_file':True}
    (api.root/'editor-conflict.json').write_text(json.dumps(result,indent=2))
    return result


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser_check(json.load(sys.stdin))
    else:
        from live_llm_job_repeat_acceptance import main

        bundle = Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
        assert (bundle/'index.html').is_file()
        mount = '\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='+repr(str(bundle))+', html=True))\n'
        asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE+mount,store_volume_max=6))
