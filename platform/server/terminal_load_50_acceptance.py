"""Hold 50 real Linux PTY sessions open, run one command per session and verify exit."""

import asyncio
from collections import Counter
import json
import math
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException


USERS = int(os.environ.get('VIBEFLOW_TEST_PTY_COUNT', '50'))
assert USERS in (1, 50)


async def account(client, semaphore, sequence):
    async with semaphore:
        registered = await client.post('/auth/register', json={
            'email': f'pty-load-{sequence}-{uuid4().hex}@example.com',
            'name': 'PTY load account', 'password': 'isolated-pty-load-123',
        })
        registered.raise_for_status()
        headers = {'Authorization': 'Bearer ' + registered.json()['token']}
        workspace = await client.post('/workspaces', headers=headers, json={'name': 'PTY load workspace'})
        workspace.raise_for_status()
        case = await client.post(f"/workspaces/{workspace.json()['id']}/cases", headers=headers,
                                 json={'name': 'PTY load case'})
        case.raise_for_status()
        return headers, case.json()['id']


async def open_session(ws_root, headers, case):
    measured = {}
    ws = None
    session = None
    started = time.perf_counter()
    try:
        async with asyncio.timeout(90):
            ws = await connect(f'{ws_root}/ws/terminal?case={case}&shell=cmd',
                               additional_headers=headers, open_timeout=30, close_timeout=3)
            tag = uuid4().hex
            expected = ('J21_PTY_' + tag).encode()
            seen = b''
            async for message in ws:
                if isinstance(message, str):
                    control = json.loads(message)
                    if control.get('type') == 'error':
                        code = control.get('code')
                        if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,60}', code):
                            measured['error'] = code
                        break
                    if control.get('type') == 'ready':
                        session = control['session']
                        measured['ready_ms'] = round((time.perf_counter() - started) * 1000, 2)
                        # The terminal echo contains separated fragments. Only
                        # stdout from printf contains the complete proof string.
                        sent = time.perf_counter()
                        await ws.send(json.dumps({'type': 'input', 'data': f"printf '\\n%s%s\\n' 'J21_PTY_' '{tag}'\r"}))
                elif session:
                    seen = (seen + message)[-8192:]
                    if expected in seen:
                        measured['command_ms'] = round((time.perf_counter() - sent) * 1000, 2)
                        break
    except (OSError, ValueError, WebSocketException) as error:
        measured['error'] = type(error).__name__
    return ws, session, measured


async def main():
    base = os.environ['VIBEFLOW_TEST_BASE']
    endpoint = urlsplit(base)
    assert endpoint.scheme == 'http' and endpoint.path == '/api/v1'
    assert endpoint.hostname == '127.0.0.1' or re.fullmatch(r'vf-j21-linux-[a-f0-9]{32}-api', endpoint.hostname or '')
    ws_root = 'ws://' + endpoint.netloc
    limits = httpx.Limits(max_connections=USERS, max_keepalive_connections=USERS)
    async with httpx.AsyncClient(base_url=base, timeout=40, trust_env=False, limits=limits) as client:
        setup_slots = asyncio.Semaphore(4)
        accounts = await asyncio.gather(*(account(client, setup_slots, index) for index in range(USERS)))
        sessions = await asyncio.gather(*(open_session(ws_root, headers, case) for headers, case in accounts))
        sockets_open_after_commands = sum(ws is not None and ws.close_code is None for ws, _, row in sessions
                                          if 'command_ms' in row)
        cleanup_slots = asyncio.Semaphore(6)

        async def close_session(state, headers):
            ws, session, measured = state
            async with cleanup_slots:
                try:
                    if session:
                        path = '/terminal/sessions/' + session
                        response = await client.delete(path, headers=headers)
                        detail = await client.get(path, headers=headers)
                        if detail.is_success:
                            public = detail.json()
                            measured['closed_verified'] = (response.is_success and public.get('phase') == 'closed'
                                                           and public.get('cleanup_verified') is True)
                except (httpx.HTTPError, ValueError):
                    measured['closed_verified'] = False
                finally:
                    if ws is not None:
                        await ws.close()

        await asyncio.gather(*(close_session(state, account[0]) for state, account in zip(sessions, accounts)))

    rows = [row for _, _, row in sessions]
    echo = sorted(row['command_ms'] for row in rows if 'command_ms' in row)
    result = {'users': USERS, 'actual_model_calls': 0, 'ready': sum('ready_ms' in row for row in rows),
              'commands_verified': len(echo), 'concurrent_sockets': sockets_open_after_commands,
              'closed_verified': sum(row.get('closed_verified') is True for row in rows),
              'errors': dict(Counter(row['error'] for row in rows if 'error' in row)),
              'command_p95_ms': echo[math.ceil(len(echo) * .95) - 1] if echo else None}
    Path(os.environ['VIBEFLOW_TEST_LOAD_REPORT']).write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    assert all(result[field] == USERS for field in ('ready', 'commands_verified', 'concurrent_sockets', 'closed_verified'))


if __name__ == '__main__':
    asyncio.run(main())
