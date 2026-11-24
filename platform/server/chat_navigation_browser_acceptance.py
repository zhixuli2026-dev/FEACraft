"""FF-F08B. Exercise Edge actions on retained real chat and approved file history."""

import argparse
from contextlib import closing
import hashlib
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
from uuid import uuid4

from app.services.process_identity_native import probe
from testsupport import IsolatedServer


NOTE_COUNT = 320
TARGET_NOTE = 125
FIXTURE = '''
import json
import os
from pathlib import Path
import httpx
from fastapi.staticfiles import StaticFiles
from app.main import app
from app.services.process_identity_native import observe
root = Path(__file__).parent
(root/'api-birth.json').write_text(json.dumps(observe(os.getpid())), encoding='utf8')
original_send = httpx.AsyncClient.send
async def local_only(self, request, *args, **kwargs):
    if request.url.host not in {'localhost', '127.0.0.1', '::1'}:
        (root/'external-request-attempted').touch()
        raise RuntimeError('Retained history fixture permits loopback only')
    return await original_send(self, request, *args, **kwargs)
httpx.AsyncClient.send = local_only
'''


def note(number):
    return f'F08B 导航笔记 {number:03d}\n' + '用户分页测试记录。' * 12


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def seed_history(original, api):
    receipt = json.loads((original / 'live-result.json').read_text(encoding='utf8'))
    assert receipt['passed'] and receipt['provider']=='deepseek' and receipt['host']=='api.deepseek.com'
    assert receipt['mock_llm_calls']==0
    assert probe(json.loads((original / 'api-birth.json').read_text(encoding='utf8')))['state']=='exited'
    trusted = receipt['conversations']['new']
    cid = trusted['id']
    sibling = receipt['conversations']['long']['id']
    shutil.copytree(original / 'files', api.root / 'files', dirs_exist_ok=True)
    old = (original / 'test.sqlite').as_uri() + '?mode=ro'
    with closing(sqlite3.connect(old, uri=True)) as db, closing(sqlite3.connect(api.root / 'test.sqlite')) as clone:
        db.backup(clone)
        owner, case_id, case_name = db.execute('SELECT u.email,c.id,c.name FROM users u JOIN workspaces w '
            'ON w.user_id=u.id JOIN cases c ON c.workspace_id=w.id WHERE c.id='
            '(SELECT case_id FROM conversations WHERE id=?)',(cid,)).fetchone()
        events = [(event_id, kind, json.loads(payload)) for event_id, kind, payload in db.execute(
            'SELECT id,type,payload FROM agent_events WHERE conversation_id=? ORDER BY seq,id',(cid,))]
        user, = [p['content'] for _,kind,p in events if kind=='message' and p.get('role')=='user']
        answer_id, answer = next((event_id, p['content']) for event_id,kind,p in reversed(events)
                                 if kind=='message' and p.get('role')=='assistant' and p.get('content'))
        _, tool = next((event_id,p) for event_id,kind,p in events if kind=='tool_result' and p.get('ok'))
        assert tool['tool']=='propose_file_update' and tool['status']=='completed'
        assert tool['tool_call_id']==trusted['tool_id'].split(':',1)[1]
        clone.execute('UPDATE agent_events SET seq=seq+? WHERE conversation_id=?',(NOTE_COUNT,cid))
        clone.executemany('INSERT INTO agent_events (conversation_id,seq,event_id,type,payload,cwd,created_at) '
            'VALUES (?,?,?,?,?,?,?)',
            [(cid, n, 'f08b-note-'+uuid4().hex, 'message', json.dumps({'role':'user','content':note(n)},ensure_ascii=False),
              '', '2026-11-18 06:00:00') for n in range(1,NOTE_COUNT+1)])
        clone.commit()
    return {'cid': cid, 'sibling': sibling, 'case': case_id, 'case_name': case_name, 'user': user,
            'owner': owner, 'answer': answer, 'answer_id': answer_id,
            'path': trusted['path'], 'approved_content': trusted['actual_arguments']['content'],
            'tool_result': tool['result'], 'upstream_http_200': sum(item.get('status')==200 for item in receipt['upstream'])}


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root, base = Path(settings['root']), settings['base']
    result = {'passed': False, 'inference_requests': [], 'observations': {}}
    with sync_playwright() as manager:
        edge = manager.chromium.launch(channel='msedge')
        result['edge'] = edge.version
        context = edge.new_context(viewport={'width': 1460, 'height': 960}, locale='zh-CN',
                                   permissions=['clipboard-read','clipboard-write'])
        context.add_init_script('if(location.origin===' + json.dumps(base) + ')localStorage.setItem("vf_token",'
                                + json.dumps(settings['token']) + ');')
        context.add_init_script("""if (navigator.clipboard) {
          const nativeWrite = navigator.clipboard.writeText.bind(navigator.clipboard);
          window.f08bCopied = [];
          navigator.clipboard.writeText = async (plain) => {
            const pending = window.f08bClipboardHold;
            window.f08bClipboardHold = null;
            await nativeWrite(plain);
            window.f08bCopied.push(plain);
            if (pending) await pending;
          };
        }""")
        page = context.new_page()
        page.set_default_timeout(62000)
        page.on('request', lambda request: result['inference_requests'].append(urlsplit(request.url).path)
                if request.method=='POST' and urlsplit(request.url).path.endswith(('/messages','/turns','/goal:run'))
                else None)
        choose = page.get_by_test_id('agent-conversation')
        composer = page.get_by_test_id('agent-input')
        list_view = page.get_by_test_id('chat-message-list')
        nav = page.get_by_role('combobox', name='跳到指定用户消息', exact=True)
        older = list_view.get_by_test_id('chat-load-older')
        bottom = list_view.get_by_test_id('chat-bottom')
        changes = list_view.get_by_test_id('chat-changes')
        selected_note = list_view.get_by_test_id('chat-message').filter(has_text=note(TARGET_NOTE).split('\n')[0])
        actual_user = list_view.get_by_test_id('chat-message').filter(has_text=settings['user'].split('\n')[0])
        actual_assistant = list_view.get_by_test_id('chat-message').filter(has_text=settings['answer'])

        def scroll_state():
            return list_view.evaluate('(list) => ({top:list.scrollTop,bottom:list.scrollHeight-list.clientHeight,'
                                      'outer:document.scrollingElement.scrollTop})')

        def selected_offset():
            return selected_note.evaluate('(node) => node.getBoundingClientRect().top - '
                                          'node.closest("[data-testid=chat-message-list]").getBoundingClientRect().top')

        def assert_horizontal_fit(stage):
            sizes = list_view.evaluate("""(list) => {
                const pane = list.getBoundingClientRect();
                const controls = Array.from(list.querySelectorAll(
                    '[data-testid=chat-user-nav], [data-testid=chat-load-older], [data-testid=chat-changes], '
                    + '[data-testid=chat-bottom], [data-testid=chat-change-item]'
                )).map(node => {
                    const bounds = node.getBoundingClientRect();
                    return {id:node.dataset.testid, left:bounds.left-pane.left, right:pane.right-bounds.right};
                });
                return {controls, clientWidth:list.clientWidth, scrollWidth:list.scrollWidth, scrollLeft:list.scrollLeft};
            }""")
            assert sizes['scrollLeft']==0 and sizes['scrollWidth']<=sizes['clientWidth']+1, sizes
            assert all(min(control['left'],control['right']) >= -1 for control in sizes['controls']), sizes
            result['observations'].setdefault('narrow_panel_fit', {})[stage] = sizes

        def copy(message, raw):
            page.bring_to_front()
            control = message.get_by_test_id('chat-copy')
            control.click()
            expect(control).to_have_text('已复制')
            assert page.evaluate('window.f08bCopied.at(-1)') == raw
            normalized = page.evaluate('navigator.clipboard.readText()').replace('\r\n','\n')
            assert normalized == raw.replace('\r\n','\n')

        def hold_clipboard():
            page.evaluate("""() => {
                window.f08bClipboardHold = new Promise(resolve => {window.f08bReleaseClipboard = resolve;});
            }""")

        def release_clipboard():
            page.evaluate('() => window.f08bReleaseClipboard?.()')

        try:
            page.goto(base, wait_until='domcontentloaded')
            row = page.locator('[data-case-name=' + json.dumps(settings['case_name']) + ']')
            expect(row).to_be_visible()
            if row.get_attribute('aria-pressed') != 'true':
                row.click()
            if not choose.is_visible():
                page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
            choose.select_option(str(settings['sibling']))
            expect(composer).to_be_enabled()

            held = []
            wanted = re.compile(re.escape(base)+rf'/api/v1/conversations/{settings["cid"]}$')
            page.route(wanted, lambda route: held.append(route), times=1)
            choose.select_option(str(settings['cid']))
            expect(composer).to_be_disabled()
            expect(page.get_by_test_id('agent-send')).to_be_disabled()
            for _ in range(120):
                if held:
                    break
                page.wait_for_timeout(50)
            assert len(held)==1
            held[0].continue_()
            expect(composer).to_be_enabled()
            expect(actual_assistant).to_have_count(1)
            assert_horizontal_fit('initial')
            assert scroll_state()['bottom'] > 1000
            page.wait_for_function("""() => {
                const el=document.querySelector('[data-testid=chat-message-list]');
                return el.scrollHeight-el.clientHeight-el.scrollTop<=8;
            }""")
            result['observations']['first_history_blocks_send_and_starts_at_bottom'] = True

            copy(actual_user, settings['user'])
            copy(actual_assistant, settings['answer'])
            result['observations']['actual_user_and_assistant_clipboard'] = True

            old_index = selected_note.get_attribute('data-index')
            nav.select_option(old_index)
            expect(selected_note).to_have_attribute('data-highlight','true')
            page.wait_for_function("""() => {
                const el = document.querySelector('[data-testid=chat-message][data-highlight=true]');
                const list = el?.closest('[data-testid=chat-message-list]');
                if (!list) return false;
                const rect = el.getBoundingClientRect();
                const top = rect.top-list.getBoundingClientRect().top;
                return Math.abs(top - (list.clientHeight-rect.height)/2) <= 3;
            }""")
            selected_note.evaluate('(node) => {window.f08bSelectedNote = node;}')
            before_top = selected_offset()
            hold_clipboard()
            selected_note.get_by_test_id('chat-copy').click()
            page.wait_for_function('(plain) => window.f08bCopied.at(-1) === plain', arg=note(TARGET_NOTE))
            older.click()
            expect(older).to_have_count(0)
            assert selected_note.get_attribute('data-index') != old_index
            expect(selected_note).to_have_attribute('data-highlight', 'true')
            assert selected_note.evaluate('(node) => node === window.f08bSelectedNote')
            after_top = selected_offset()
            assert abs(after_top-before_top) <= 16, (before_top,after_top)
            release_clipboard()
            expect(selected_note.get_by_test_id('chat-copy')).to_have_text('已复制')
            result['observations']['pagination'] = {'kept_same_dom_and_highlight': True,
                'copy_feedback_stayed_on_same_message': True, 'before_top': before_top, 'after_top': after_top}
            assert_horizontal_fit('pagination')
            page.screenshot(path=str(root / 'chat-navigation-pagination.png'))

            state = scroll_state()
            assert state['bottom']-state['top'] > 300
            bottom.click()
            latest = scroll_state()
            assert latest['bottom']-latest['top'] <= 8 and latest['outer']==state['outer']
            result['observations']['bottom_moves_only_the_message_container'] = True
            changes.click()
            expect(changes).to_have_attribute('aria-expanded','true')
            item = list_view.get_by_test_id('chat-change-item')
            expect(item).to_have_count(1)
            expect(item).to_contain_text(settings['path'])
            item.click()
            tool = list_view.get_by_test_id('chat-tool-card').filter(has_text=settings['path'])
            expect(tool).to_have_attribute('data-highlight','true')
            expect(tool).to_have_attribute('data-status','completed')
            expect(tool).to_contain_text(settings['tool_result'])
            assert_horizontal_fit('summary')
            page.screenshot(path=str(root / 'chat-navigation-approved-summary.png'))
            result['observations']['approved_change_summary_targets_actual_tool'] = True

            hold_clipboard()
            actual_assistant.get_by_test_id('chat-copy').click()
            page.wait_for_function('(plain) => window.f08bCopied.at(-1) === plain', arg=settings['answer'])
            choose.select_option(str(settings['sibling']))
            expect(composer).to_be_enabled()
            expect(actual_assistant).to_have_count(0)
            release_clipboard()
            expect(list_view.locator('[data-highlight="true"]')).to_have_count(0)
            expect(list_view.get_by_role('button', name='已复制', exact=True)).to_have_count(0)
            result['observations']['conversation_switch_clears_highlight_and_late_copy'] = True
            assert not result['inference_requests']
            result['passed'] = True
        finally:
            release_clipboard()
            if not result['passed']:
                page.screenshot(path=str(root / 'chat-navigation-failed.png'))
            write_json(root / 'chat-navigation-browser.json', result)
            edge.close()


def verify(args):
    original = args.retained_history.resolve(strict=True)
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    api = IsolatedServer('chat-navigation-f08b', app='chat_navigation_fixture:app')
    root = api.root
    fixture = FIXTURE + '\napp.mount("/", StaticFiles(directory=' + repr(str(dist)) + ', html=True))\n'
    (root / 'chat_navigation_fixture.py').write_text(fixture, encoding='utf8')
    report = {'passed': False, 'model_calls': 0, 'mock_llm_calls': 0, 'added_user_notes': NOTE_COUNT}
    print('CHAT_NAVIGATION_EVIDENCE', root, flush=True)
    try:
        retained = seed_history(original, api)
        report['source'] = {'actual_model_receipt': str(original / 'live-result.json'),
            'approved_event_id': retained['answer_id'], 'historical_http_200': retained['upstream_http_200']}
        with api:
            account = api.call('POST','/auth/login',{'email':retained['owner'],'password':'isolated-test-123'})
            api.token = account['token']
            target = f"/conversations/{retained['cid']}"
            path = f"/cases/{retained['case']}/file?" + urlencode({'path': retained['path']})
            assert api.call('GET',path)['content']==retained['approved_content']
            first = api.call('GET', target)
            assert first['event_window']['has_more']
            assert any(e['id']==retained['answer_id'] for e in first['events'])
            sandbox = root / 'edge-tmp'
            sandbox.mkdir()
            settings = {name:value for name,value in retained.items() if name!='owner'}
            settings.update(root=str(root), base=api.base, token=api.token)
            subprocess.run([str(args.browser_python.resolve(strict=True)), str(Path(__file__).resolve()), '--browser'],
                           input=json.dumps(settings), cwd=root, env=dict(os.environ, PYTHONIOENCODING='utf8',
                           TEMP=str(sandbox), TMP=str(sandbox)), encoding='utf8', timeout=320, check=True)
            report['browser'] = json.loads((root / 'chat-navigation-browser.json').read_text(encoding='utf8'))
            assert report['browser']['passed']
            assert api.call('GET',path)['content']==retained['approved_content']
            other = api.call('POST','/auth/register',{'email':'f08b-independent@example.com',
                             'name':'Independent viewer','password':'isolated-test-123'})
            external_auth = {'Authorization': 'Bearer ' + other['token']}
            api.call('GET',target,expect=404,extra_headers=external_auth)
            api.call('GET',path,expect=404,extra_headers=external_auth)
            assert not (root/'external-request-attempted').exists()
            report['real_file_sha256'] = hashlib.sha256(retained['approved_content'].encode('utf8')).hexdigest()
            report['cross_account_http'] = 404
        identity = json.loads((root/'api-birth.json').read_text(encoding='utf8'))
        until = time.monotonic()+15
        while probe(identity)['state']!='exited' and time.monotonic()<until:
            time.sleep(.2)
        assert probe(identity)['state']=='exited'
        with socket.socket() as connection:
            connection.settimeout(1)
            assert connection.connect_ex(('127.0.0.1',api.port))!=0
        assert not (root/'.vault_key').exists()
        report['cleanup'] = {'api_pid':identity['pid'],'api_exited':True,'port_closed':True}
        report['passed'] = True
    finally:
        write_json(root/'chat-navigation-result.json', report)
    print('CHAT_NAVIGATION_PASS', root/'chat-navigation-result.json', flush=True)


if __name__=='__main__':
    if sys.argv[1:]==['--browser']:
        browser(json.load(sys.stdin))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
        from regression_llm_policy import enforce

        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--retained-history', type=Path, required=True)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        verify(parser.parse_args())
