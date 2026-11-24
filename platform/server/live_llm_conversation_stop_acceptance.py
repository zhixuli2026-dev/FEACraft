"""Exercise conversation ownership and stopping with actual DeepSeek streams."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import socket
import tempfile
import time
import uuid

import httpx

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer, SERVER


CASE = 'Actual conversation controls'
START = 'CONTROL_RACE_ORIGINAL'
DUPLICATE = 'CONTROL_RACE_DUPLICATE'
HELD_MARKERS = ('CONTROL_BACKGROUND_A', 'CONTROL_BACKGROUND_B', 'CONTROL_FALLBACK')
REMOUNT_MARKERS = ('CONTROL_PANEL_REOPEN', 'CONTROL_PANEL_HIDDEN')
FIXTURE_CONTROLS = '''
import asyncio
from fastapi import Depends
from app.db import SessionLocal
from app.deps import get_current_user
from app.models import Conversation, ConversationExecution
from app.routers import agent
from app.routers.workspaces import get_owned_case

live_request_limit=8
original_run=agent._run_loop
async def held_run(cid,*args,**kwargs):
 ready=root/f'before-stream-{cid}'
 if (root/f'arm-stream-{cid}').is_file() and not ready.exists():
  ready.touch()
  async with asyncio.timeout(80):
   while not (root/f'release-stream-{cid}').is_file(): await asyncio.sleep(.03)
 from contextlib import aclosing
 async with aclosing(original_run(cid,*args,**kwargs)) as frames:
  async for frame in frames: yield frame
agent._run_loop=held_run

original_live_lines=httpx.Response.aiter_lines
async def hold_actual_text(self,*args,**kwargs):
 marker=None
 content_chunks=0
 if self.request.url.host=='api.deepseek.com' and self.request.url.path.endswith('/chat/completions'):
  prompt=next((row.get('content') for row in reversed(json.loads(self.request.content)['messages']) if row['role']=='user'),None)
  marker=next((value for value in held_markers if value in json.dumps(prompt)),None)
  if 'CONTROL_NEXT_TURN' in json.dumps(prompt):
   receipt({'kind':'next_turn_user_input','marker':'CONTROL_NEXT_TURN','latest_user_message':True})
 async for line in original_live_lines(self,*args,**kwargs):
  yield line
  if marker and line.startswith('data:') and line[5:].strip()!='[DONE]':
   choices=json.loads(line[5:]).get('choices') or []
   if choices and choices[0].get('delta',{}).get('content'):
    content_chunks+=1
    if content_chunks==1 or (marker=='CONTROL_PANEL_REOPEN' and content_chunks==4):
     stage=marker if content_chunks==1 else marker+'.next'
     (root/f'{stage}.real-text').touch()
     async with asyncio.timeout(95):
      while not (root/f'{stage}.release').exists():await asyncio.sleep(.03)
httpx.Response.aiter_lines=hold_actual_text

@app.get('/api/v1/fixture/conversation-owner/{cid}')
async def conversation_owner(cid:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  conv=await db.get(Conversation,cid)
  await get_owned_case(conv.case_id,user,db)
  owner=await db.get(ConversationExecution,cid)
  return {'phase':owner.phase if owner else None,'resource_id':owner.resource_id if owner else None,
          'registered':cid in agent._RUN_CANCELLATIONS}
'''


def events(api, cid):
    return api.call('GET', f'/conversations/{cid}')['events']


def messages(api, cid, role):
    return [event['payload'] for event in events(api, cid)
            if event['type'] == 'message' and event['payload'].get('role') == role]


def until(check, description, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.04)
    raise TimeoutError(description)


@contextmanager
def connect(api):
    with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                      trust_env=False, timeout=90) as client:
        yield client


def send(client, cid, profile, model, marker, reasoning='off'):
    return client.post(f'/conversations/{cid}/messages', json={
        'content': f'Reply exactly with {marker}.', 'provider_profile_id': profile,
        'model': model, 'reasoning_effort': reasoning})


def decoded(response):
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith('data:')]


def disconnect_before_stream(api, case, profile, model):
    cid = api.call('POST', '/conversations', {'case_id': case['id'], 'title': 'Before streaming',
                   'provider_profile_id': profile, 'model': model, 'permission_mode': 'ask'})['id']
    (api.root/f'arm-stream-{cid}').touch()
    route = f'/conversations/{cid}'
    owner_route = f'/fixture/conversation-owner/{cid}'
    with connect(api) as client:
        with client.stream('POST', route+'/messages', json={'content': 'Close before any model starts.',
                           'provider_profile_id': profile, 'model': model}) as response:
            assert response.status_code == 200
            until((api.root/f'before-stream-{cid}').is_file, 'The first frame was not held')
            before = api.call('GET', owner_route)
            assert before['phase'] == 'active' and not before['registered']
        until(lambda: api.call('GET', owner_route)['phase'] == 'idle', 'Early disconnect left an owner')
    assert [row['type'] for row in events(api, cid)] == ['message']
    changed = api.call('PATCH', route+'/permission', {'permission_mode': 'plan'})
    assert changed['permission_mode'] == 'plan'
    return {'owner_released': True, 'permission_change_accepted': True, 'model_attempted': False}


def race(api, cid, profile, alternate, model, baseline):
    root = api.root
    (root/f'arm-stream-{cid}').touch()
    release = root/f'release-stream-{cid}'
    with connect(api) as client, ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(send, client, cid, profile, model, START)
        try:
            until((root/f'before-stream-{cid}').is_file, 'First real request did not reach the stream boundary')
            before = api.call('GET', f'/fixture/conversation-owner/{cid}')
            assert before['phase'] == (None if baseline else 'active'), before
            second = pool.submit(send, client, cid, alternate, model, DUPLICATE, 'high')
            if baseline:
                until(lambda: len(messages(api, cid, 'user')) == 2, 'Old endpoint did not commit both user messages')
            else:
                blocked = second.result(timeout=10)
                assert blocked.status_code == 409, blocked.text
                denied = client.patch(f'/conversations/{cid}/permission', json={'permission_mode': 'full_access'})
                assert denied.status_code == 409, denied.text
                assert [row['content'] for row in messages(api, cid, 'user')] == [f'Reply exactly with {START}.']
        finally:
            release.touch()
        original, duplicate = first.result(), second.result()
        if baseline:
            replies = [decoded(response) for response in (original, duplicate)]
            assert original.status_code == duplicate.status_code == 200
            assert sum(any(event['type'] == 'run.completed' for event in reply) for reply in replies) == 1
            return {'pre_stream_phase': before['phase'], 'committed_user_messages': 2,
                    'http_statuses': [original.status_code, duplicate.status_code]}
        frames = decoded(original)
        answer = ''.join(frame.get('text', '') for frame in frames if frame['type'] == 'text.delta')
        assert original.status_code == 200 and START in answer and DUPLICATE not in answer, frames
        assert sum(frame['type'] == 'run.completed' for frame in frames) == 1
        after = api.call('GET', f'/fixture/conversation-owner/{cid}')
        assert after == {'phase': 'idle', 'resource_id': None, 'registered': False}, after
        return {'pre_stream_phase': before['phase'], 'committed_user_messages': 1,
                'http_statuses': [original.status_code, duplicate.status_code], 'permission_change_status': denied.status_code}


def browser_controls(api, case, profiles, model, baseline):
    from playwright.sync_api import expect, sync_playwright

    markers = HELD_MARKERS[-1:] if baseline else HELD_MARKERS
    ids = {marker: api.call('POST', '/conversations', {'case_id': case['id'], 'title': marker,
           'provider_profile_id': profiles[index % 2], 'model': model, 'permission_mode': 'ask'})['id']
           for index, marker in enumerate(markers)}
    result = {'browser_baseline': baseline, 'conversations': ids}
    stranger = api.call('POST', '/auth/register', {'name': 'Independent operator',
        'email': 'independent-f40@example.com', 'password': 'isolated-test-123'})

    def phase(marker):
        return api.call('GET', f'/fixture/conversation-owner/{ids[marker]}')['phase']

    def statuses(marker):
        return [e['payload']['status'] for e in events(api, ids[marker]) if e['type'] == 'run_state']

    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge')
        context = edge.new_context(viewport={'width': 1480, 'height': 1020}, locale='zh-CN')
        context.add_init_script('if(location.origin===' + json.dumps(api.base) + ')localStorage.setItem("vf_token",'
                                + json.dumps(api.token) + ');')
        page = context.new_page()
        page.set_default_timeout(20000)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        result['edge'] = edge.version
        stop = page.get_by_test_id('agent-stop')
        composer = page.get_by_test_id('agent-input')
        choose = page.get_by_test_id('agent-conversation')
        log = page.get_by_test_id('chat-message-list')
        cards = log.locator('[data-testid="chat-markdown"][data-authoritative="false"]')
        settings = page.locator('.agent-settings')

        def choose_marker(marker):
            choose.select_option(str(ids[marker]))
            expect(choose).to_have_value(str(ids[marker]))

        def submit(marker, blocked=True):
            choose_marker(marker)
            expect(composer).to_be_enabled()
            composer.fill('Reply with only this identifier: ' + marker)
            page.get_by_test_id('agent-send').click()
            if blocked:
                until((api.root/f'{marker}.real-text').exists, 'No real provider text for ' + marker, 45)
                expect(cards.first).to_be_visible()
                expect(stop).to_be_enabled()

        def open_case():
            case_button = page.locator('[data-case-name='+json.dumps(CASE)+']')
            expect(case_button).to_be_visible(timeout=20000)
            if case_button.get_attribute('aria-pressed') != 'true':
                case_button.click()
            if not choose.is_visible():
                page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()

        try:
            page.goto(api.base, wait_until='domcontentloaded')
            open_case()
            if not baseline:
                a, b, _ = HELD_MARKERS
                submit(a)
                if settings.get_attribute('open') is None:
                    settings.locator('summary').click()
                for lock in ['agent-profile', 'profile-edit', 'conv-model', 'agent-permission-mode', 'reasoning-effort', 'agent-backend']:
                    expect(page.get_by_test_id(lock)).to_be_disabled()
                expect(choose).to_be_enabled()
                page.screenshot(path=str(api.root/'active-controls.png'))
                submit(b)
                assert phase(a) == phase(b) == 'active'
                assert statuses(a)[-1] == statuses(b)[-1] == 'started'
                active_ids = [api.call('GET', f'/fixture/conversation-owner/{ids[key]}')['resource_id'] for key in (a,b)]
                assert active_ids[0] and active_ids[1] and active_ids[0]!=active_ids[1]
                wrong = api.call('POST', f'/conversations/{ids[a]}/messages:cancel', {'run_id':active_ids[1]})
                assert wrong['ok'] is False and phase(a) == phase(b) == 'active'
                foreign = {'Authorization': 'Bearer '+stranger['token']}
                api.call('POST', f'/conversations/{ids[a]}/messages:cancel', {}, expect=404, extra_headers=foreign)
                api.call('PATCH', f'/conversations/{ids[a]}/permission', {'permission_mode':'plan'}, expect=404, extra_headers=foreign)
                choose_marker(a)
                expect(stop).to_be_enabled()
                with page.expect_response(f'**/conversations/{ids[a]}/messages:cancel') as observed:
                    stop.click()
                assert observed.value.status == 200 and observed.value.json()['ok'] is True
                assert observed.value.request.post_data_json == {'run_id':active_ids[0]}
                expect(stop).to_be_disabled()
                until(lambda: phase(a) == 'idle', 'Stopped session retained its execution owner')
                assert statuses(a)[-1] == 'cancelled' and phase(b) == 'active'
                assert messages(api, ids[a], 'assistant')[-1]['cancelled'] is True
                assert not messages(api, ids[b], 'assistant')
                (api.root/f'{b}.release').touch()
                until(lambda: phase(b) == 'idle', 'Background session did not complete', 35)
                assert statuses(b)[-1] == 'completed'
                assert b in messages(api, ids[b], 'assistant')[-1]['content']
                choose_marker(b)
                expect(log).to_contain_text(b)
                result.update(concurrent_owners=True, unrelated_user_status=404, background_completed=True,
                              explicitly_stopped='cancelled', stopped_partial_persisted=True,
                              unmatched_run_preserved_both_owners=True)
            fallback = HELD_MARKERS[-1]
            submit(fallback)
            page.evaluate('''() => {
              const actual = window.fetch;
              window.fetch = (url, options) => {
                if (typeof url === 'string' && url.endsWith('/messages:cancel')) {
                  window.stopRequestWaiting = true;
                  return new Promise((resolve, reject) => options?.signal?.addEventListener('abort', () => {
                    window.stopRequestAborted = true;
                    reject(options.signal.reason);
                  }, {once: true}));
                }
                return actual(url, options);
              };
            }''')
            start = time.monotonic()
            stop.click()
            page.wait_for_function('window.stopRequestWaiting === true')
            if baseline:
                page.wait_for_timeout(2600)
                expect(stop).to_be_enabled()
                assert phase(fallback) == 'active'
                result['missed_two_second_fallback'] = True
                page.close()
            else:
                expect(stop).to_be_disabled(timeout=4000)
                result['fallback_seconds'] = round(time.monotonic()-start, 3)
                assert 1.8 <= result['fallback_seconds'] < 4.2
                assert page.evaluate('window.stopRequestAborted === true')
                expect(page.get_by_test_id('profile-status')).to_contain_text('已断开当前会话的输出连接')
                expect(log).not_to_contain_text('[发送失败]')
                expect(log).to_contain_text('已中断')
            until(lambda: phase(fallback) == 'idle', 'Disconnected session retained its owner', 15)
            interrupted = [row for row in events(api, ids[fallback]) if row['type'] == 'run_state'][-1]
            assert interrupted['payload']['status'] == 'interrupted'
            assert interrupted['recovered_checkpoint']['content'].strip()
            result['interrupted_checkpoint_retained'] = True
            if not baseline:
                page.reload(wait_until='domcontentloaded')
                open_case()
                choose_marker(fallback)
                expect(log).to_contain_text('已中断')
                for lock in ['agent-profile', 'profile-edit', 'conv-model', 'agent-permission-mode', 'reasoning-effort', 'agent-backend']:
                    expect(page.get_by_test_id(lock)).to_be_enabled()
                composer.fill('The previous request was stopped. Ignore the old identifier. Reply only with the new identifier CONTROL_NEXT_TURN.')
                page.get_by_test_id('agent-send').click()
                until(lambda: statuses(fallback)[-1] == 'completed', 'Next actual model turn did not complete', 40)
                replies = messages(api, ids[fallback], 'assistant')
                completed = [row for row in events(api, ids[fallback]) if row['type'] == 'run_state'][-1]
                assert len(replies) == 1 and replies[0]['content'].strip()
                assert replies[0]['run_id'] == completed['payload']['run_id'] and not replies[0].get('cancelled')
                receipt = [json.loads(line) for line in (api.root/'upstream.jsonl').read_text(encoding='utf8').splitlines()]
                assert sum(row['kind']=='next_turn_user_input' and row['latest_user_message'] for row in receipt) == 1
                expect(log.locator('[data-testid="chat-markdown"][data-authoritative="true"]').last).to_be_visible()
                result['reload_and_followup_completed'] = True
            assert not errors, errors
            result['passed'] = True
        finally:
            for marker in markers:
                (api.root/f'{marker}.release').touch()
            if not page.is_closed():
                page.screenshot(path=str(api.root/'browser-final.png'))
            edge.close()
            (api.root/'browser.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf8')
    return result


def browser_remount(api, case, profile, model, mode):
    from playwright.sync_api import expect, sync_playwright

    markers = REMOUNT_MARKERS[:1] if mode=='baseline' else REMOUNT_MARKERS
    ids={marker:api.call('POST','/conversations', {'case_id':case['id'],'title':marker,'provider_profile_id':profile,
                                                   'model':model,'permission_mode':'ask'})['id'] for marker in markers}
    stages=(*markers, REMOUNT_MARKERS[0]+'.next')
    result={'passed':False,'mode':mode,'conversations':ids}
    with sync_playwright() as playwright:
        edge=playwright.chromium.launch(channel='msedge')
        context=edge.new_context(viewport={'width':1480,'height':1020},locale='zh-CN')
        context.add_init_script('if(location.origin==='+json.dumps(api.base)+')localStorage.setItem("vf_token",'
                                +json.dumps(api.token)+');')
        page=context.new_page();page.set_default_timeout(12000)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        result['edge']=edge.version
        choose=page.get_by_test_id('agent-conversation');stop=page.get_by_test_id('agent-stop')
        composer=page.get_by_test_id('agent-input');log=page.get_by_test_id('chat-message-list')
        streaming=log.locator('[data-testid="chat-markdown"][data-authoritative="false"]')
        saved=log.locator('[data-testid="chat-markdown"][data-authoritative="true"]')
        selector=page.get_by_label('工作面板',exact=True)
        tab=page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$'))

        def owner(marker):
            return api.call('GET',f'/fixture/conversation-owner/{ids[marker]}')

        def submit(marker):
            choose.select_option(str(ids[marker]));expect(composer).to_be_enabled()
            composer.fill(f'Begin with {marker} on its own line. Then write a clear 140-word overview of mesh convergence.')
            page.get_by_test_id('agent-send').click()
            until((api.root/f'{marker}.real-text').is_file,'No actual provider text for '+marker,55)
            expect(streaming).to_be_visible();expect(stop).to_be_enabled()

        def close_panel():
            tab.locator('.dv-default-tab-action').click()
            expect(choose).not_to_be_attached()

        def reopen(marker):
            if mode=='baseline':
                page.set_viewport_size({'width':730,'height':970});selector.select_option('agent')
                choose.select_option(str(ids[marker]))
            else:
                page.get_by_role('button',name='打开 Agent').click()
                expect(choose).to_have_value(str(ids[marker]))

        try:
            page.goto(api.base,wait_until='domcontentloaded')
            case_button=page.locator('[data-case-name='+json.dumps(CASE)+']')
            expect(case_button).to_be_visible(timeout=20000)
            if case_button.get_attribute('aria-pressed')!='true':case_button.click()
            if not choose.is_visible():tab.click()
            live=markers[0];submit(live);before=owner(live)
            assert before['phase']=='active' and before['resource_id']
            close_panel();assert owner(live)==before
            reopen(live);expect(stop).to_be_enabled();expect(streaming).to_be_visible()
            first=streaming.inner_text()
            (api.root/f'{live}.release').touch()
            until((api.root/f'{live}.next.real-text').is_file,'No subsequent actual provider text',35)
            held_history=[]
            if mode=='fixed':
                expect(streaming).not_to_have_text(first)
                close_panel()
                def capture_old_history(route):
                    response=route.fetch()
                    assert response.status==200
                    old=response.json()['events']
                    assert not any(row['type']=='message' and row['payload'].get('role')=='assistant' for row in old)
                    held_history.append((route,response))
                page.route(f'**/api/v1/conversations/{ids[live]}',capture_old_history,times=1)
                reopen(live);expect(stop).to_be_enabled()
                page.wait_for_function('() => document.querySelector("[data-testid=agent-stop]") !== null')
                deadline=time.monotonic()+10
                while not held_history:
                    assert time.monotonic()<deadline,'The real pre-completion history response was not captured'
                    page.wait_for_timeout(40)
            (api.root/f'{live}.next.release').touch()
            until(lambda:owner(live)['phase']=='idle','Original live turn did not complete',45)
            answer=messages(api,ids[live],'assistant')
            assert len(answer)==1 and answer[0]['run_id']==before['resource_id'] and live in answer[0]['content']
            if mode=='baseline':
                expect(stop).to_be_enabled();expect(streaming).to_have_text(first);expect(saved).to_have_count(0)
                result['finished_original_run_but_panel_still_streaming']=True
            else:
                expect(stop).to_be_disabled();expect(saved).to_have_count(1)
                expect(saved).to_contain_text(live);expect(streaming).to_have_count(0)
                delayed,response=held_history[0]
                with page.expect_event('requestfinished',predicate=lambda req:req==delayed.request):
                    delayed.fulfill(response=response)
                expect(saved).to_have_count(1);expect(saved).to_contain_text(live);expect(stop).to_be_disabled()
                assert len(messages(api,ids[live],'user'))==1
                result['middle_and_terminal_frames_reached_reopened_panel']=True
                result['late_pre_completion_history_did_not_replace_committed_answer']=True
                hidden=markers[1];submit(hidden);hidden_before=owner(hidden)
                close_panel();assert owner(hidden)==hidden_before and hidden_before['phase']=='active'
                (api.root/f'{hidden}.release').touch()
                until(lambda:owner(hidden)['phase']=='idle','Hidden turn did not complete',45)
                answer=messages(api,ids[hidden],'assistant')
                assert len(answer)==1 and answer[0]['run_id']==hidden_before['resource_id'] and hidden in answer[0]['content']
                expect(choose).not_to_be_attached();reopen(hidden)
                expect(saved).to_have_count(1);expect(saved).to_contain_text(hidden);expect(stop).to_be_disabled()
                result['completed_off_panel_and_read_back_once']=True
            assert not errors,errors
            result['passed']=True
        finally:
            for stage in stages:(api.root/f'{stage}.release').touch()
            page.screenshot(path=str(api.root/'panel-final.png'))
            edge.close()
            (api.root/'browser.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    return result


def verify(args, configuration, *, database_url=None):
    api = IsolatedServer('ff-f39-panel' if args.panel_remount else 'ff-f40-control',
                         app='conversation_fixture:app', database_url=database_url)
    fixture = FIXTURE + '\nheld_markers=' + repr(HELD_MARKERS + REMOUNT_MARKERS) + '\n' + FIXTURE_CONTROLS
    if args.dist:
        dist = args.dist.resolve(strict=True)
        assert (dist/'index.html').is_file()
        fixture += '\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/",StaticFiles(directory='+repr(str(dist))+',html=True))\n'
    (api.root/'conversation_fixture.py').write_text(fixture, encoding='utf8')
    report = {'passed': False, 'before_fix': args.before_fix, 'provider': 'deepseek',
              'model': configuration['model'], 'simulated_llm_calls': 0,
              'database': 'postgresql' if database_url else 'sqlite'}
    print('F40_CONTROL_EVIDENCE', api.root, flush=True)
    try:
        with api:
            case = api.create_case(CASE)
            ids = []
            for name in ['Conversation profile', 'Other conversation profile']:
                row = api.call('POST', '/provider-profiles', {'provider': 'deepseek', 'name': name,
                    'base_url': configuration['base_url'], 'default_model': configuration['model'], 'api_key': configuration['key']})
                ids.append(row['id'])
            if not (args.browser_before_fix or args.panel_remount):
                cid = api.call('POST', '/conversations', {'case_id': case['id'], 'title': 'First message',
                    'provider_profile_id': ids[0], 'model': configuration['model'], 'permission_mode': 'ask'})['id']
                report['first_message_race'] = race(api, cid, *ids, configuration['model'], args.before_fix)
                if not args.before_fix:
                    report['disconnect_before_first_frame'] = disconnect_before_stream(api, case, ids[0], configuration['model'])
            if args.dist:
                report['browser'] = (browser_remount(api, case, ids[0], configuration['model'], args.panel_remount)
                                     if args.panel_remount else
                                     browser_controls(api, case, ids, configuration['model'], args.browser_before_fix))
        def port_closed():
            with socket.socket() as connection:
                connection.settimeout(1)
                return connection.connect_ex(('127.0.0.1', api.port)) != 0
        assert api.proc.poll() is not None
        until(port_closed, 'Finished isolated API still has a listening port', 12)
        report['isolated_api_closed'] = True
        log = (api.root/'upstream.jsonl').read_text(encoding='utf8').splitlines()
        observed = [row for line in log if (row := json.loads(line)).get('kind') == 'http']
        expected = (2 if args.panel_remount=='fixed' else 1) if args.panel_remount else (
            5 if args.dist and not args.browser_before_fix else 1)
        assert len(observed) == expected and all(row['host'] == 'api.deepseek.com' and row['status'] == 200
                                                for row in observed), observed
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__,
            'detail': str(error).replace(configuration['key'], '[REDACTED]')[:1200]}
        raise
    finally:
        if api.proc is not None and api.proc.poll() is not None:
            (api.root/'.vault_key').unlink(missing_ok=True)
        log = api.root/'upstream.jsonl'
        report['upstream'] = [json.loads(line) for line in log.read_text(encoding='utf8').splitlines()] if log.exists() else []
        report['sources'] = {name:hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
                             for name in ['app/routers/agent.py', 'app/services/conversation_execution.py']}
        (api.root/'result.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf8')
    return api.root


async def postgres(args, configuration):
    import asyncpg
    from sqlalchemy.engine import make_url
    from app.config import Settings
    from workflow_migration_smoke import alembic

    source_url = make_url(Settings(_env_file=SERVER/'.env').database_url)
    assert source_url.drivername == 'postgresql+asyncpg' and source_url.host in {'localhost', '127.0.0.1', '::1'}
    name = 'tmp_vf_controls_' + uuid.uuid4().hex
    assert re.fullmatch('tmp_vf_controls_[0-9a-f]{32}', name)
    folder = Path(tempfile.mkdtemp(prefix='ff-f40-postgres-', dir=SERVER.parents[1]/'tmp'))
    print('F40_POSTGRES_EVIDENCE', folder, flush=True)
    admin = await asyncpg.connect(host=source_url.host, port=source_url.port or 5432, user=source_url.username,
                                  password=source_url.password, database='postgres', timeout=10)
    report = {'passed': False, 'temporary_database_dropped': False, 'developer_database_modified': False}
    created = False
    try:
        await admin.execute('CREATE DATABASE "'+name+'"')
        created = True
        database_url = source_url.set(database=name).render_as_string(hide_password=False)
        await asyncio.to_thread(alembic, database_url, folder, 'upgrade', 'head')
        evidence = await asyncio.to_thread(verify, args, configuration, database_url=database_url)
        report.update(passed=True, isolated_evidence=str(evidence))
    finally:
        try:
            if created:
                await admin.execute('DROP DATABASE "'+name+'" WITH (FORCE)')
                report['temporary_database_dropped'] = not await admin.fetchval(
                    'SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname=$1)', name)
        finally:
            await admin.close()
            (folder/'result.json').write_text(json.dumps(report, indent=2)+'\n', encoding='utf8')
    assert report['temporary_database_dropped']


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--before-fix', action='store_true')
    parser.add_argument('--browser-before-fix', action='store_true')
    parser.add_argument('--dist', type=Path)
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--panel-remount', choices=['baseline','fixed'])
    arguments = parser.parse_args()
    if (arguments.browser_before_fix or arguments.panel_remount) and not arguments.dist:
        parser.error('Browser acceptance requires a built bundle')
    configuration = asyncio.run(source(arguments.source_profile_id))
    if arguments.postgres:
        asyncio.run(postgres(arguments, configuration))
    else:
        verify(arguments, configuration)
