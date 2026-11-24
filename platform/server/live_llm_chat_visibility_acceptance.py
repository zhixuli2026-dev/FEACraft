"""FF-F09 browser acceptance against the authorized DeepSeek profile."""

import argparse
import asyncio
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer


CASE = 'Actual chat visibility'
FILE = 'Ref/chat-visibility.txt'
ORIGINAL = 'f09 example before\n'
REPLACEMENT = 'f09 example after\n'
SAMPLES = {
    'short': ('请只写一个字：好。不要使用工具，不要加引号、标点或任何说明。', '好', True),
    'placeholder': ('请只输出三个连续的半角句点，也就是 ... ，不要加引号、空格或其他字符。不要使用工具。', '...', False),
}
EMPTY_SAMPLE = {'empty': ('这是仅检查空白输出的测试。请只发送一个半角空格字符后结束。不要解释，不要加引号、标点、文字或工具。', '', False)}
HOLD_FIRST_REASONING = '''
import asyncio
observed_lines = httpx.Response.aiter_lines
async def hold_actual_reasoning(self, *args, **kwargs):
    paused = False
    async for line in observed_lines(self, *args, **kwargs):
        yield line
        if (paused or calls != 1 or self.request.url.host != 'api.deepseek.com'
                or not line.startswith('data:') or not line[5:].lstrip().startswith('{')):
            continue
        choices = json.loads(line[5:]).get('choices') or []
        if choices and choices[0].get('delta', {}).get('reasoning_content'):
            paused = True
            deadline = time.monotonic() + 25
            while not (root / 'first-reasoning-release').exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError('Browser did not observe the genuine reasoning fragment')
                await asyncio.sleep(.04)
httpx.Response.aiter_lines = hold_actual_reasoning
'''


def browser(api, conversations, samples, report):
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(api.root / 'edge-test-profile',
            channel='msedge', viewport={'width': 1450, 'height': 980})
        context.add_init_script('if (location.origin === ' + json.dumps(api.base) +
                                ') localStorage.setItem("vf_token", ' + json.dumps(api.token) + ');')
        page = context.new_page()
        expect.set_options(timeout=50_000)
        page.set_default_timeout(50_000)
        selector = page.get_by_test_id('agent-conversation')
        composer = page.get_by_test_id('agent-input')
        stop = page.get_by_test_id('agent-stop')
        history = page.get_by_test_id('chat-message-list')
        rendered = history.get_by_test_id('chat-message')
        request_paths = []
        page.on('request', lambda request: request_paths.append(urlsplit(request.url).path)
                if request.method == 'POST' else None)

        def open_conversation(cid):
            case = page.locator('[data-case-name=' + json.dumps(CASE) + ']')
            expect(case).to_be_visible()
            if case.get_attribute('aria-pressed') != 'true':
                case.click()
            if not selector.is_visible():
                page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
            selector.select_option(str(cid))
            expect(composer).to_be_enabled()

        def check_visibility(prompt, answer, shown, reasoning):
            expect(rendered.filter(has_text=prompt)).to_have_count(1)
            final = rendered.locator('[data-testid="chat-markdown"][data-authoritative="true"]')
            if shown:
                expect(final).to_have_count(1)
                expect(final).to_have_text(answer)
            elif reasoning:
                expect(rendered.get_by_test_id('reasoning-card')).to_have_count(1)
                assert not any(text.strip() for text in final.all_inner_texts())
            else:
                expect(final).to_have_count(0)

        page.goto(api.base, wait_until='domcontentloaded')
        if 'cancelled' in conversations:
            check_cancelled(api, page, conversations['cancelled'], open_conversation, report)
            check_edit(api, page, conversations['edit'], open_conversation, report)
            return

        for name, (prompt, answer, shown) in samples.items():
            cid = conversations[name]
            route = f'/conversations/{cid}'
            endpoint = '/api/v1' + route + '/messages'
            open_conversation(cid)
            composer.fill(prompt)
            with page.expect_response(lambda response: response.request.method == 'POST'
                                      and urlsplit(response.url).path == endpoint, timeout=100_000) as response:
                composer.press('Enter')
            result = response.value
            assert result.status == 200, result.status
            assert 'text/event-stream' in result.headers.get('content-type', '')
            frames = [json.loads(line[5:]) for line in result.text().splitlines() if line.startswith('data:')]
            event_types = [frame['type'] for frame in frames]
            assert event_types.count('run.completed') == 1, event_types
            output = ''.join(frame['text'] for frame in frames if frame['type'] == 'text.delta')
            reasoning = ''.join(frame['text'] for frame in frames if frame['type'] == 'reasoning.delta')
            report['checks'][name] = {'observed_answer_matches': output.strip() == answer,
                                      'reasoning_characters': len(reasoning), 'event_frames': len(frames)}
            assert output.strip() == answer and (name == 'empty' or not reasoning), report['checks'][name]
            expect(stop).to_be_disabled()
            check_visibility(prompt, answer, shown, reasoning)
            before_refresh = api.call('GET', route)['events']
            messages = [event for event in before_refresh if event['type'] == 'message'
                        and event['payload'].get('role') == 'assistant']
            assert len(messages) == 1 and messages[0]['payload']['content'] == output
            assert messages[0]['payload'].get('reasoning', '') == reasoning
            page.reload(wait_until='domcontentloaded')
            open_conversation(cid)
            check_visibility(prompt, answer, shown, reasoning)
            assert request_paths.count(endpoint) == 1
            assert api.call('GET', route)['events'] == before_refresh
            report['checks'][name].update(passed=True, retained_event_id=messages[0]['id'], visible=shown)


def check_cancelled(api, page, cid, open_conversation, report):
    open_conversation(cid)
    settings = page.locator('details.agent-settings')
    summary = settings.locator('summary')
    if settings.get_attribute('open') is None:
        summary.click()
    settings.get_by_test_id('reasoning-effort').select_option('low')
    summary.click()
    composer = page.get_by_test_id('agent-input')
    stop = page.get_by_test_id('agent-stop')
    history = page.get_by_test_id('chat-message-list')
    active = history.locator('[data-testid="reasoning-card"][data-streaming="true"]')
    route = f'/conversations/{cid}'
    release = api.root / 'first-reasoning-release'
    composer.fill('请计算 (27×14−18)÷6。用短句给出结果。不要使用工具。')
    composer.press('Enter')
    try:
        expect(active).to_have_count(1)
        reasoning = active.get_by_test_id('reasoning-content').text_content()
        assert reasoning
        expect(history.get_by_test_id('chat-markdown')).to_have_count(0)
        with page.expect_response(f'**{route}/messages:cancel') as observed:
            stop.click()
        assert observed.value.status == 200 and observed.value.json()['ok'] is True
        expect(stop).to_be_disabled()
    finally:
        release.touch()

    def check_card():
        card = history.get_by_test_id('chat-message').filter(has_text='已取消')
        expect(card).to_have_count(1)
        expect(card.get_by_test_id('reasoning-card')).to_have_count(1)
        expect(card.get_by_test_id('chat-markdown')).to_have_count(0)

    check_card()
    before = api.call('GET', route)['events']
    stored, = [item for item in before if item['type'] == 'message' and item['payload'].get('cancelled')]
    assert stored['payload']['content'] == '' and stored['payload']['reasoning'].startswith(reasoning)
    page.reload(wait_until='domcontentloaded')
    open_conversation(cid)
    check_card()
    assert api.call('GET', route)['events'] == before
    report['checks']['cancelled'] = {'passed': True, 'reasoning_characters': len(reasoning),
                                     'retained_event_id': stored['id']}


def check_edit(api, page, cid, open_conversation, report):
    open_conversation(cid)
    prompt = (f'请只调用一次 propose_text_replacement 修改隔离算例文本 {FILE}。'
              '把 f09 example before 精确替换为 f09 example after。保留换行，其余文件不动。等待人工批准。'
              '批准后停止调用工具，只输出一个字：好。')
    composer = page.get_by_test_id('agent-input')
    composer.fill(prompt)
    composer.press('Enter')
    approval = page.locator('[data-testid="approval-card"][data-tool="propose_text_replacement"]')
    approve = approval.get_by_role('button', name='批准执行', exact=True)
    route = f'/conversations/{cid}'
    expect(approve).to_be_enabled(timeout=75_000)
    pending, = api.call('GET', route)['pending_tools']
    assert pending['args']['path'] == FILE
    target = api.root / 'files' / api.call('GET', api.prefix)['storage_prefix'] / FILE
    assert target.read_text(encoding='utf-8') == ORIGINAL
    with page.expect_response(f'**{route}/tool-calls/{pending["id"]}:approve') as approved:
        approve.click()
    assert approved.value.status == 200, approved.value.status
    _ = approved.value.text()
    expect(approval).to_have_count(0)

    def check_summary():
        toggle = page.get_by_test_id('chat-changes')
        expect(toggle).to_have_text('修改摘要 1')
        item = page.get_by_test_id('chat-change-item')
        if not item.is_visible():
            toggle.click()
        expect(item).to_contain_text(FILE)
        expect(page.get_by_test_id('chat-tool-card').filter(has_text='人工批准')).to_have_count(1)

    check_summary()
    assert target.read_text(encoding='utf-8') == REPLACEMENT
    before = api.call('GET', route)['events']
    stored, = [item for item in before if item['type'] == 'tool_result' and item['payload'].get('approved')]
    assert stored['payload']['ok']
    page.reload(wait_until='domcontentloaded')
    open_conversation(cid)
    check_summary()
    assert api.call('GET', route)['events'] == before
    report['checks']['edit'] = {'passed': True, 'actual_replacement': True, 'retained_event_id': stored['id']}


def verify(args, config):
    dist = args.dist.resolve(strict=True)
    api = IsolatedServer('live-f09-chat-visibility', app='visibility_fixture:app')
    api_fixture = '\nfrom fastapi.staticfiles import StaticFiles\n'
    api_fixture += 'app.mount("/", StaticFiles(directory=' + repr(str(dist)) + ', html=True))\n'
    samples = EMPTY_SAMPLE if args.empty else SAMPLES
    expected_calls = 3 if args.extended else len(samples)
    observed = HOLD_FIRST_REASONING if args.extended else ''
    (api.root / 'visibility_fixture.py').write_text(FIXTURE + f'\nlive_request_limit = {expected_calls}\n' + observed + api_fixture,
                                                     encoding='utf-8')
    report = {'passed': False, 'provider': 'deepseek', 'model': config['model'], 'checks': {}, 'mock_llm_calls': 0}
    print('LIVE_F09_EVIDENCE', api.root, flush=True)
    try:
        with api:
            case = api.create_case(CASE)
            profile = api.call('POST', '/provider-profiles', {
                'provider': 'deepseek', 'name': CASE, 'base_url': config['base_url'],
                'default_model': config['model'], 'api_key': config['key'],
            })
            permissions = {'cancelled': 'ask', 'edit': 'approve_writes'} if args.extended else dict.fromkeys(samples, 'ask')
            if args.extended:
                api.put(FILE, ORIGINAL)
            conversations = {name: api.call('POST', '/conversations', {
                'case_id': case['id'], 'title': name, 'model': config['model'],
                'provider_profile_id': profile['id'], 'permission_mode': permission,
            })['id'] for name, permission in permissions.items()}
            browser(api, conversations, samples, report)
            stranger = api.call('POST', '/auth/register', {
                'name': 'Independent visibility test account', 'email': 'f09-other@example.com',
                'password': 'isolated-test-123',
            })
            for cid in conversations.values():
                api.call('GET', f'/conversations/{cid}', expect=404,
                         extra_headers={'Authorization': 'Bearer ' + stranger['token']})
            report['cross_account_status'] = 404
        report['passed'] = True
    except Exception as error:
        report['failure'] = {'type': type(error).__name__,
                             'detail': str(error).replace(config['key'], '[REDACTED]')[:800]}
    finally:
        (api.root / '.vault_key').unlink(missing_ok=True)
        trace = api.root / 'upstream.jsonl'
        report['upstream'] = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()] if trace.exists() else []
        report['model_attempts'] = sum(row['kind'] in {'http', 'transport_error'} for row in report['upstream'])
        report['successful_model_calls'] = sum(row.get('status') == 200 for row in report['upstream'] if row['kind'] == 'http')
        report['passed'] = report['passed'] and report['model_attempts'] == report['successful_model_calls'] == expected_calls
        target = api.root / 'live-result.json'
        encoded = json.dumps(report, ensure_ascii=False, indent=2)
        assert config['key'] not in encoded
        target.write_text(encoded + '\n', encoding='utf-8')
    print('LIVE_F09_RESULT', target, 'passed=' + str(report['passed']), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--dist', type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--extended', action='store_true')
    mode.add_argument('--empty', action='store_true')
    arguments = parser.parse_args()
    raise SystemExit(verify(arguments, asyncio.run(source(arguments.source_profile_id))))
