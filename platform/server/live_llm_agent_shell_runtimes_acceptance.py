"""Real DeepSeek tool selection, approval, execution and cancellation on Windows/WSL."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import time
import uuid

import httpx

from live_llm_acceptance import FIXTURE, source
from live_llm_chat_markdown_acceptance import wait_for_api_exit
from testsupport import IsolatedServer


PROOF = 'Ref/shell-proof.txt'
SHELL_COMMANDS = {
    'wsl': "printf 'VIBE_UID='; id -u; cat Ref/shell-proof.txt",
    'powershell': "Write-Output 'PowerShell 温度'; Get-Content -LiteralPath 'Ref/shell-proof.txt'",
    'windows': r'type Ref\shell-proof.txt',
}
SLEEP = "printf 'ready' > Ref/agent-shell-ready.txt; sleep 100"
PRELAUNCH = "printf 'must not run' > Ref/agent-shell-prelaunch.txt"


def verify(config, selected_runtime):
    api = IsolatedServer('live-f24-f25-agent-shell', app='shell_fixture:app')
    fixture = '''live_request_limit=10
''' + FIXTURE + '''
import os
from cryptography.fernet import Fernet
from app.services.process_identity_native import observe
from app.services.native_request_execution import RequestExecutor
vault = Fernet(Fernet.generate_key())
llm._vault = lambda: vault
(root/'api-birth.json').write_text(json.dumps(observe(os.getpid())), encoding='utf8')
original_prepare = RequestExecutor._prepare
async def prepare(self, entry, argv, env):
    actual = await original_prepare(self, entry, argv, env)
    if self.kind == 'agent.shell' and any('agent-shell-prelaunch.txt' in argument for argument in argv):
        # The actual root is registered behind its launch gate. Hold only that
        # gate so the cancellation request arrives before the command runs.
        (root/'prelaunch-ready').touch()
        await asyncio.sleep(45)
    return actual
RequestExecutor._prepare = prepare
'''
    (api.root / 'shell_fixture.py').write_text(fixture, encoding='utf8')
    result = {'passed': False, 'model': config['model'], 'mock_llm_calls': 0, 'checks': {}}
    print('LIVE_SHELL_RUNTIMES_EVIDENCE', api.root, flush=True)

    def upstream():
        receipts = api.root / 'upstream.jsonl'
        return [json.loads(line) for line in receipts.read_text(encoding='utf8').splitlines()] if receipts.exists() else []

    def execute_request(route, body):
        with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                          timeout=150, trust_env=False) as client:
            with client.stream('POST', route, json=body) as response:
                response.raise_for_status()
                return [json.loads(line[5:]) for line in response.iter_lines() if line.startswith('data:')]

    def pending(case, profile, runtime, command, *, route=None):
        if route is None:
            cid = api.call('POST', '/conversations', {
                'case_id': case['id'], 'title': f'Shell {runtime}', 'permission_mode': 'approve_writes',
                'provider_profile_id': profile['id'], 'model': config['model'],
            })['id']
            route = f'/conversations/{cid}'
        prompt = (f'仅调用一次 run_shell_command，runtime={runtime}，timeout_seconds=120。'
                  f'把下列命令逐字作为 command，无封装或其他命令：\n{command}\n'
                  '收到工具结果后只报告实际输出，不要再次调用任何工具。')
        frames = execute_request(route + '/messages', {'content': prompt, 'provider_profile_id': profile['id']})
        requested = [row['tool_call'] for row in frames if row['type']=='approval.requested']
        assert len(requested) == 1 and requested[0]['tool'] == 'run_shell_command', requested
        call = requested[0]
        args = call['args']
        assert args == {'runtime': runtime, 'command': command, 'timeout_seconds': 120} or (
            runtime=='windows' and args=={'command': command, 'timeout_seconds': 120}), args
        approval = route + '/tool-calls/' + call['id']
        api.call('GET', approval + '/execution', expect=404)
        return route, approval

    def verify_execution(approval, expected):
        row = api.call('GET', approval + '/execution')
        assert row['phase'] == expected and row['cleanup_verified'] is True, (row['phase'], row['error'])
        children = list(row['children']['children'].values())
        assert len(children)==1 and children[0]['stop']['scope_empty'] is True
        api.call('POST', approval + ':approve', {}, expect=409)
        return row

    def stop_command(case, profile, command, ready):
        route, approval = pending(case, profile, 'wsl', command)
        with ThreadPoolExecutor(max_workers=1) as pool:
            operation = pool.submit(execute_request, approval + ':approve', {})
            try:
                deadline = time.monotonic() + 35
                while not ready():
                    assert time.monotonic() < deadline and not operation.done(), 'Shell did not reach the observation point'
                    time.sleep(.15)
            finally:
                stopped = api.call('POST', route + '/messages:cancel', {})
            assert stopped['ok']
            frames = operation.result(timeout=35)
            assert frames[-1].get('reason')=='cancelled'
        return route, approval, verify_execution(approval, 'cancelled')

    try:
        with api:
            case = api.create_case('Real approved shell runtimes')
            marker = 'SHELL_PROOF_' + uuid.uuid4().hex
            api.call('PUT', api.prefix + '/file', {'path': PROOF, 'content': marker + '\n'})
            profile = api.call('POST', '/provider-profiles', {'name': 'Actual shell selection', 'provider': 'deepseek',
                               'base_url': config['base_url'], 'default_model': config['model'], 'api_key': config['key']})
            runtimes = SHELL_COMMANDS if selected_runtime is None else (selected_runtime,)
            for runtime in runtimes:
                command = SHELL_COMMANDS[runtime]
                route, approval = pending(case, profile, runtime, command)
                frames = execute_request(approval + ':approve', {})
                output = next(row for row in frames if row['type']=='tool.result')
                assert output['ok'] is True and output['approved'] is True and marker in output['result'], output
                assert any(row['type']=='run.completed' for row in frames)
                if runtime=='wsl':
                    assert 'VIBE_UID=65534' in output['result'], output
                elif runtime=='powershell':
                    assert 'PowerShell 温度' in output['result'] and 'CLIXML' not in output['result'], output
                witness = verify_execution(approval, 'succeeded')
                result['checks'][runtime] = {'execution': witness['id'], 'output': output['result'], 'scope_empty': True}
            if 'wsl' in runtimes:
                _, _, witness = stop_command(case, profile, PRELAUNCH, (api.root / 'prelaunch-ready').exists)
                assert not list((api.root / 'files').rglob('agent-shell-prelaunch.txt'))
                result['checks']['wsl_start_cancel'] = {'execution': witness['id'], 'scope_empty': True, 'payload_never_ran': True}
                route, approval, witness = stop_command(case, profile, SLEEP,
                    lambda: next((api.root / 'files').rglob('agent-shell-ready.txt'), None) is not None)
                result['checks']['wsl_cancel'] = {'execution': witness['id'], 'scope_empty': True}
                _, approval = pending(case, profile, 'wsl', SHELL_COMMANDS['wsl'], route=route)
                frames = execute_request(approval + ':approve', {})
                output = next(row for row in frames if row['type']=='tool.result')
                assert output['ok'] and 'VIBE_UID=65534' in output['result'] and marker in output['result'], output
                witness = verify_execution(approval, 'succeeded')
                result['checks']['wsl_recovery'] = {'execution': witness['id'], 'scope_empty': True}
            foreign = api.call('POST', '/auth/register', {'name': 'Unrelated reader',
                               'email': 'isolated-shell-other@example.com', 'password': 'isolated-test-123'})
            api.call('GET', approval + '/execution', expect=404,
                     extra_headers={'Authorization': 'Bearer ' + foreign['token']})
            result['cross_account_rejected'] = True
        result['cleanup'] = wait_for_api_exit(api.root, api.port)
        expected_requests = 2 * len(runtimes) + (4 if 'wsl' in runtimes else 0)
        assert len([row for row in upstream() if row['kind']=='http' and row['status']==200]) == expected_requests
        result['passed'] = True
    finally:
        result['upstream'] = upstream()
        text = json.dumps(result, ensure_ascii=False, indent=2)
        assert config['key'] not in text
        (api.root / 'live-result.json').write_text(text, encoding='utf8')
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--runtime', choices=SHELL_COMMANDS)
    args = parser.parse_args()
    verify(asyncio.run(source(args.source_profile_id)), args.runtime)
