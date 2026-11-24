"""Verify the workbench and run a real DeepSeek tool turn in Windows read-only mode."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import tomllib
import uuid


CASE = 'Windows sandbox acceptance'
OPTIONS = '''
from app.services import codex_sessions
native_command = codex_sessions.command
def test_command(credential=None):
    argv = native_command(credential)
    return [argv[0], '--disable', 'multi_agent', '--disable', 'multi_agent_v2',
            '-c', 'web_search="disabled"', *argv[1:]]
codex_sessions.command = test_command
'''


def browser(config):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as play:
        edge = play.chromium.launch(channel='msedge')
        context = edge.new_context(viewport={'width': 1440, 'height': 920}, locale='zh-CN')
        context.add_init_script('if (location.origin === ' + json.dumps(config['base']) +
                                ') localStorage.setItem("vf_token", ' + json.dumps(config['token']) + ');')
        page = context.new_page()
        page.goto(config['base'], wait_until='domcontentloaded')
        page.locator('[data-case-name=' + json.dumps(CASE) + ']').click(timeout=25000)
        page.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
        page.get_by_test_id('agent-conversation').select_option(str(config['conversation']))
        page.get_by_test_id('agent-backend').select_option('windows')
        button = page.get_by_role('button', name='检查运行环境', exact=True)
        with page.expect_response(lambda response: response.url.endswith('/sandbox'), timeout=95000) as readiness:
            button.click(timeout=40000)
        response = readiness.value
        assert response.ok, response.status
        result = response.json()
        assert result['runtime'] == 'windows' and result['legacy_readiness'] == 'ready', result
        assert result['windows_override'] == 'unelevated' and result['setup_authority'] == 'deployment_operator', result
        panel = page.get_by_role('dialog', name='运行环境检查')
        expect(panel).to_be_visible()
        expect(panel.get_by_text('已准备', exact=True)).to_be_visible()
        expect(panel.get_by_text('非提权运行', exact=True)).to_be_visible()
        panel.screenshot(path=str(Path(config['root']) / 'windows-sandbox-workbench.png'))
        page.set_viewport_size({'width': 390, 'height': 844})
        bounds = panel.bounding_box()
        assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= 390
        panel.screenshot(path=str(Path(config['root']) / 'windows-sandbox-workbench-390.png'))
        page.keyboard.press('Escape')
        expect(panel).not_to_be_visible()
        expect(button).to_be_enabled(timeout=55000)
        result['inspection_session'] = response.url.split('/')[-2]
        (Path(config['root']) / 'browser.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        edge.close()


def verify(args, configuration):
    from app.services.process_identity_native import probe
    from live_llm_codex_terminal_provider_acceptance import EXE, Forwarder
    from live_llm_shared_index_acceptance import SharedIndexServer, configure

    assert configuration['model'] == 'deepseek-flash'
    dist = args.dist.resolve(strict=True)
    assert (dist / 'index.html').is_file()
    api = SharedIndexServer('ff-f56-live', app='live_fixture:app')
    configure(api, {'VIBEFLOW_CODEX': str(EXE), 'VIBEFLOW_CODEX_WINDOWS_SANDBOX': 'unelevated'}, False)
    fixture = api.root / 'live_fixture.py'
    with fixture.open('a', encoding='utf8') as stream:
        stream.write(OPTIONS + '\nfrom fastapi.staticfiles import StaticFiles\n'
                     + f'app.mount("/", StaticFiles(directory={str(dist)!r}, html=True))\n')
    evidence = {'passed': False, 'simulated_llm_calls': 0, 'runtime': 'windows',
                'binary_sha256': hashlib.sha256(EXE.read_bytes()).hexdigest()}
    print('FF_F56_LIVE_EVIDENCE', api.root, flush=True)
    nonce = 'SANDBOX_' + uuid.uuid4().hex
    with Forwarder(configuration, api.root) as upstream:
        try:
            with api:
                api.create_case(CASE)
                api.put('proof.txt', nonce + '\n')
                profile = api.call('POST', '/provider-profiles', {'provider': 'custom', 'name': 'DeepSeek Responses',
                    'base_url': upstream.base, 'default_model': configuration['model'], 'api_key': configuration['key']})
                conv = api.call('POST', '/conversations', {'case_id': api.case_id, 'title': 'Windows read-only',
                    'permission_mode': 'ask', 'provider_profile_id': profile['id'], 'model': configuration['model']})
                subprocess.run([str(args.browser_python.resolve(strict=True)), '-B', '-X', 'utf8', __file__, '--browser'],
                    input=json.dumps({'base': api.base, 'root': str(api.root), 'token': api.token, 'conversation': conv['id']}),
                    text=True, check=True, timeout=150, env=dict(os.environ, TMP=str(api.root), TEMP=str(api.root)))
                evidence['browser'] = json.loads((api.root / 'browser.json').read_text('utf8'))
                inspect = api.call('GET', '/codex/sessions/' + evidence['browser']['inspection_session'])
                assert inspect['cleanup_verified'] and inspect['stop']['scope_empty']
                root = api.root / 'cli-state' / '.private-cli' / 'users'
                home, = root.glob(f'*/windows/providers/{profile["id"]}/*/home')
                config = home / 'config.toml'
                before = tomllib.loads(config.read_text('utf8'))
                setup = Path(__file__).resolve().parents[1] / 'scripts' / 'configure-windows-codex-sandbox.py'
                completion = subprocess.run([sys.executable, str(setup), 'setup', '--mode', 'unelevated',
                    '--home', str(home), '--codex', str(EXE)], capture_output=True, check=True, timeout=50)
                evidence['deployment_setup'] = json.loads(completion.stdout)
                assert evidence['deployment_setup']['completion'] == 'succeeded'
                assert evidence['deployment_setup']['transport_stopped']
                after = tomllib.loads(config.read_text('utf8'))
                assert after.pop('windows') == {'sandbox': 'unelevated'} and before == after
                evidence['provider_configuration_preserved'] = True
                session = api.call('POST', '/codex/sessions', {'case_id': api.case_id, 'conversation_id': conv['id'],
                    'provider_profile_id': profile['id'], 'provider_wire_api': 'responses', 'runtime': 'windows'})
                turn = api.call('POST', '/codex/sessions/' + session['id'] + '/turns', {
                    'content': 'Read proof.txt once using PowerShell Get-Content. Reply with its verification string only. Do not edit files.',
                    'client_message_id': uuid.uuid4().hex, 'timeout_seconds': 120, 'model_options': {'effort': 'low'}})
                deadline = time.monotonic() + 130
                settled = {'completed', 'failed', 'cancelled', 'interrupted', 'recovery_required', 'evidence_invalid'}
                while turn['phase'] not in settled:
                    assert turn['phase'] != 'awaiting_input', 'Expected a read-only tool inside the declared policy'
                    assert time.monotonic() < deadline, 'Real DeepSeek turn timed out'
                    time.sleep(.25)
                    turn = api.call('GET', '/codex/turns/' + turn['id'])
                evidence['turn'] = {'id': turn['id'], 'phase': turn['phase'], 'cleanup_verified': turn['cleanup_verified']}
                assert turn['phase'] == 'completed' and turn['cleanup_verified'], (turn['phase'], turn.get('error'))
                events = api.call('GET', '/codex/turns/' + turn['id'] + '/events', raw=True).decode('utf-8')
                trail = [json.loads(line[5:]) for line in events.splitlines() if line.startswith('data:')]
                items = [event['payload'] for event in trail if event['type'] == 'item.completed']
                answer = [item for item in items if item.get('type') == 'agentMessage'][-1]
                streamed = ''.join(event['payload']['text'] for event in trail
                                   if event['type'] == 'text.delta' and event['payload']['item_id'] == answer['id'])
                assert answer['text'].strip() == streamed.strip() == nonce, 'Final answer did not match the file'
                commands = [item for item in items if item.get('type') == 'commandExecution']
                assert len(commands) == 1 and commands[0]['exitCode'] == 0 and nonce in commands[0]['aggregatedOutput']
                final_session = api.call('GET', '/codex/sessions/' + session['id'])
                assert final_session['cleanup_verified'] and final_session['stop']['scope_empty']
                assert api.call('GET', api.prefix + '/file?path=proof.txt')['content'] == nonce + '\n'
                evidence.update(model_tool_read_verified=True, original_preserved=True, inspection_exit_verified=True,
                                turn_exit_verified=True)
            deadline = time.monotonic() + 10
            evidence['api_exit'] = probe(api.birth)['state']
            while evidence['api_exit'] == 'match' and time.monotonic() < deadline:
                time.sleep(.05)
                evidence['api_exit'] = probe(api.birth)['state']
            assert evidence['api_exit'] == 'exited'
            assert len(upstream.receipts) >= 2 and all(receipt.get('status') == 200 for receipt in upstream.receipts)
            evidence['passed'] = True
        except Exception as error:
            evidence['failure_type'] = type(error).__name__
            evidence['failure'] = str(error).replace(configuration['key'], '[REDACTED]')[:700]
        finally:
            evidence['actual_model_calls'] = upstream.count
            evidence['upstream'] = upstream.receipts
            (api.root / 'windows-sandbox-live.json').write_text(json.dumps(evidence, indent=2), encoding='utf8')
    print('FF_F56_LIVE_PASS', evidence['passed'], flush=True)
    return 0 if evidence['passed'] else 1


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        from live_llm_acceptance import source

        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--source-profile-id', type=int, required=True)
        parser.add_argument('--dist', type=Path, required=True)
        parser.add_argument('--browser-python', type=Path, required=True)
        args = parser.parse_args()
        sys.exit(verify(args, asyncio.run(source(args.source_profile_id))))
