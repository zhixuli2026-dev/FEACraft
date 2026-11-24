"""Actual cmd and non-root WSL running budgets. No model request is involved."""
import argparse
import asyncio
from contextlib import nullcontext
import json
import shlex
import subprocess
import sys
import time

import aiohttp
from app.config import Settings
from app.services.owned_process import wsl_path
from live_llm_failure_fallback_acceptance import kill_owned_api
from live_llm_outbox_restart_acceptance import restart
from live_llm_terminal_agent_acceptance import disposable_database
from s3testsupport import ObjectStore
from terminal_execution_smoke import LEAF, SPAWN, connect, detached, detached_exited, exited, ready
from testsupport import IsolatedServer, SERVER


FIXTURE = '''
import os
from fastapi import Depends
from app.config import get_settings
from app.deps import get_current_user
from app.main import app
from app.routers import terminal

@app.post('/fixture/terminal-budgets')
def budgets(body:dict, user=Depends(get_current_user)):
    os.environ['VIBEFLOW_TERMINAL_SESSION_LIFETIME_SECONDS'] = str(body['lifetime'])
    os.environ['VIBEFLOW_TERMINAL_SESSION_MAX_OUTPUT_BYTES'] = str(body['output'])
    get_settings.cache_clear()
    settings = get_settings()
    return {'lifetime': settings.terminal_session_lifetime_seconds,
            'output': settings.terminal_session_max_output_bytes}

@app.get('/fixture/terminal-live')
def live(user=Depends(get_current_user)):
    return {'pid': os.getpid(), 'sessions': sorted(terminal._sessions)}
'''

EMIT = '''
import sys
for n in range(int(sys.argv[1])):
    print('X' * 256)
print('MARK_' + sys.argv[2] + '_END', flush=True)
'''

LIFETIME = 9
OUTPUT = 64 * 1024


def select_budget(api, lifetime=0, output=0):
    request = {'lifetime': lifetime, 'output': output}
    assert api.call('POST', '/fixture/terminal-budgets', request, api_prefix=False) == request


def row(api, identifier):
    return api.call('GET', '/terminal/sessions/' + identifier)


async def wait_for_text(ws, predicate, limit=12):
    async with asyncio.timeout(limit):
        result = b''
        while True:
            message = await ws.receive()
            if message.type == aiohttp.WSMsgType.BINARY:
                result += message.data
                if predicate(result):
                    return result
            elif message.type == aiohttp.WSMsgType.TEXT:
                raise AssertionError(json.loads(message.data))
            else:
                raise AssertionError(message)


async def terminal_status(ws, limit=20):
    async with asyncio.timeout(limit):
        while True:
            message = await ws.receive()
            if message.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(message.data)
                if data['type'] in {'exit', 'error'}:
                    return data
            elif message.type != aiohttp.WSMsgType.BINARY:
                raise AssertionError(message)


async def command(ws, shell, value):
    await ws.send_json({'type': 'input', 'data': value + ('\r\n' if shell == 'cmd' else '\n')})


async def emit(api, ws, shell, lines, marker):
    script = api.root / 'terminal_budget_emit.py'
    text = (subprocess.list2cmdline([sys._base_executable, '-I', '-S', '-u', str(script), str(lines), marker])
            if shell == 'cmd' else shlex.join(['python3', '-I', '-S', '-u', wsl_path(script), str(lines), marker]))
    await command(ws, shell, text)


def new_case(api, name):
    workspace = api.call('GET', '/workspaces')[0]
    return api.call('POST', f'/workspaces/{workspace["id"]}/cases', {'name': name})['id']


async def reconnect(client, api, shell, identifier, process):
    ws = await connect(client, api, shell, identifier=identifier)
    assert await ready(ws) == identifier
    assert row(api, identifier)['process'] == process
    return ws


async def default_session(api, client, shell):
    select_budget(api)
    case = new_case(api, f'{shell} original interactive semantics')
    ws = await connect(client, api, shell, case=case)
    identifier = await ready(ws)
    before = row(api, identifier)
    select_budget(api, lifetime=1, output=OUTPUT)
    await asyncio.sleep(1.2)
    await emit(api, ws, shell, 280, 'DEFAULT')
    visible = await wait_for_text(ws, lambda data: b'MARK_DEFAULT_END' in data, 18)
    assert len(visible) > OUTPUT and row(api, identifier)['phase'] == 'active'
    await ws.close()
    again = await reconnect(client, api, shell, identifier, before['process'])
    await wait_for_text(again, lambda data: b'MARK_DEFAULT_END' in data)
    await emit(api, again, shell, 1, 'REATTACHED')
    await wait_for_text(again, lambda data: b'MARK_REATTACHED_END' in data)
    await command(again, shell, 'exit /b 7' if shell == 'cmd' else 'exit 7')
    assert await terminal_status(again) == {'type': 'exit', 'code': 7}
    await again.close()
    final = row(api, identifier)
    assert final['phase'] == 'closed' and final['cleanup_verified'] and final['exit_code'] == 7
    await exited(final)
    return {'id': identifier, 'passed': True, 'continued_after_short_config_changed': True,
            'original_process_reconnected': True, 'bytes_read': len(visible), 'final': final}


async def limited_session(api, client, shell, reason):
    select_budget(api, lifetime=LIFETIME if reason == 'timeout' else 0,
                  output=OUTPUT if reason == 'log_limit' else 0)
    case = new_case(api, f'{shell} configured {reason}')
    ws = await connect(client, api, shell, case=case)
    identifier = await ready(ws)
    begin = time.monotonic()
    before = row(api, identifier)
    member = await detached(api, ws, shell, identifier)
    await emit(api, ws, shell, 158 if reason == 'log_limit' else 1, 'FIRST')
    await wait_for_text(ws, lambda data: b'MARK_FIRST_END' in data)
    assert row(api, identifier)['phase'] == 'active'
    await ws.close()
    # Changing settings must affect subsequent starts, not the existing limit.
    select_budget(api)
    again = await reconnect(client, api, shell, identifier, before['process'])
    await wait_for_text(again, lambda data: b'MARK_FIRST_END' in data)
    if reason == 'log_limit':
        # Each burst fits individually, only the combined process output exceeds the cap.
        await emit(api, again, shell, 158, 'SECOND')
    else:
        await emit(api, again, shell, 1, 'REATTACHED')
        await wait_for_text(again, lambda data: b'MARK_REATTACHED_END' in data)
    status = await terminal_status(again)
    elapsed = time.monotonic() - begin
    assert (status.get('type'), status.get('code'), status.get('reason')) == ('error', 'terminal_resource_limit', reason), status
    final = row(api, identifier)
    assert final['phase'] == 'failed' and final['error'] == 'TerminalResourceLimit' and final['ended_at']
    assert final['cleanup_verified'] is True and final['stop']['reason'] == reason
    assert before['process'] == final['process']
    assert identifier not in api.call('GET', '/fixture/terminal-live', api_prefix=False)['sessions']
    await exited(final)
    await detached_exited(member)
    await again.close()
    if reason == 'log_limit':
        assert 60 * 1024 <= len(final['tail'].encode()) <= OUTPUT + 3
        assert 'MARK_FIRST_END' in final['tail'] and 'MARK_SECOND_END' not in final['tail']
    else:
        assert LIFETIME - 2 <= elapsed < LIFETIME + 9, elapsed
    return {'id': identifier, 'reason': reason, 'seconds_from_ready_to_terminal_status': round(elapsed, 3),
            'continued_after_reconnect_before_stop': True, 'actual_detached_child': member, 'final': final}


async def verify(database_url, store):
    api = IsolatedServer('terminal-running-budget', app='terminal_budget_fixture:app', database_url=database_url)
    settings = {'VIBEFLOW_TERMINAL_SESSION_LIFETIME_SECONDS': '0', 'VIBEFLOW_TERMINAL_SESSION_MAX_OUTPUT_BYTES': '0'}
    if store is not None:
        settings.update(store.settings())
    (api.root / 'terminal_budget_fixture.py').write_text('import os\nos.environ.update(' + repr(settings) + ')\n' + FIXTURE, 'utf8')
    (api.root / 'terminal_budget_emit.py').write_text(EMIT, 'utf8')
    (api.root / 'terminal_leaf.py').write_text(LEAF, 'utf8')
    (api.root / 'terminal_spawn.py').write_text(SPAWN, 'utf8')
    results = {}
    assert Settings.model_fields['terminal_session_lifetime_seconds'].default == 0
    assert Settings.model_fields['terminal_session_max_output_bytes'].default == 0
    with api:
        api.create_case('Terminal running budget bootstrap')
        async with aiohttp.ClientSession() as client:
            for shell in ('cmd', 'wsl'):
                results[shell] = [await default_session(api, client, shell)]
                for reason in ('timeout', 'log_limit'):
                    results[shell].append(await limited_session(api, client, shell, reason))
                print('TERMINAL_RUNNING_BUDGET_SHELL_OK', shell, str(api.root), flush=True)
            pid = api.call('GET', '/fixture/terminal-live', api_prefix=False)['pid']
            await asyncio.to_thread(kill_owned_api, api, pid)
            await asyncio.to_thread(restart, api)
            assert api.call('GET', '/fixture/terminal-live', api_prefix=False)['sessions'] == []
            for shell, cases in results.items():
                for case in cases:
                    current = row(api, case['id'])
                    assert all(current[key] == case['final'][key] for key in ('phase', 'tail', 'error', 'ended_at', 'process', 'stop'))
                    history = await connect(client, api, shell, identifier=case['id'])
                    assert await ready(history) == case['id']
                    status = await terminal_status(history)
                    if 'reason' in case:
                        assert status.get('code') == 'terminal_resource_limit' and status.get('reason') == case['reason']
                    else:
                        assert status == {'type': 'exit', 'code': 7}
                    await history.close()
            pid = api.call('GET', '/fixture/terminal-live', api_prefix=False)['pid']
            await asyncio.to_thread(kill_owned_api, api, pid)
    report = {'passed': True, 'database': 'postgresql' if database_url else 'sqlite', 'storage': 's3' if store else 'fs',
              'sessions': results, 'actual_api_cold_restart': True, 'resource_limit_sessions_reaped': True,
              'api_exited': api.proc.poll() is not None, 'real_model_requests': 0, 'mock_llm_calls': 0}
    (api.root / 'terminal-running-budget.json').write_text(json.dumps(report, indent=2, ensure_ascii=False), 'utf8')
    return api.root


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--s3', action='store_true')
    args = parser.parse_args()
    store = (ObjectStore(executable=SERVER.parents[1] / 'third_party/seaweed-4.52-runtime/weed', volume_max=6)
             if args.s3 else nullcontext(None))
    async with disposable_database(args.postgres) as url:
        with store as selected:
            root = await verify(url, selected)
        if args.s3:
            assert selected.proc.poll() is not None
            (root / 's3-cleanup.json').write_text(json.dumps({'stopped': True, 'root': str(selected.root)}), 'utf8')
    print('TERMINAL_RUNNING_BUDGET_OK', str(root), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
