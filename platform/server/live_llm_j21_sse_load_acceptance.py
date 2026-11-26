"""Measure first visible tokens from actual DeepSeek SSE under concurrent account load."""

import argparse
import asyncio
from collections import Counter
import json
import math
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer


async def prepare(client, semaphore, config, index):
    async with semaphore:
        start = await client.post('/auth/register', json={
            'email': f'live-sse-{index}-{uuid4().hex}@example.com',
            'password': 'isolated-live-sse-load-123', 'name': 'Live SSE participant',
        })
        start.raise_for_status()
        headers = {'Authorization': 'Bearer ' + start.json()['token']}

        async def create(route, body):
            response = await client.post(route, json=body, headers=headers)
            response.raise_for_status()
            return response.json()['id']

        workspace = await create('/workspaces', {'name': 'Live SSE test'})
        case = await create(f'/workspaces/{workspace}/cases', {'name': 'Live SSE test case'})
        profile = await create('/provider-profiles', {
            'name': 'Official DeepSeek for J21', 'provider': 'deepseek',
            'base_url': config['base_url'], 'default_model': config['model'], 'api_key': config['key'],
        })
        conversation = await create('/conversations', {
            'case_id': case, 'title': 'First token timing', 'permission_mode': 'plan',
            'provider_profile_id': profile, 'model': config['model'],
        })
        return headers, profile, conversation


async def measure_one(client, account, model):
    headers, profile, conversation = account
    started = time.perf_counter()
    result = {'conversation_id': conversation, 'first_text_ms': None, 'completed': False}
    try:
        async with asyncio.timeout(120), client.stream('POST', f'/conversations/{conversation}/messages',
                headers=headers, json={'content': '用不超过二十个汉字解释什么是固定时间步。不要调用工具。',
                                       'provider_profile_id': profile, 'model': model}) as response:
            result['http'] = response.status_code
            result['response_headers_ms'] = round((time.perf_counter() - started) * 1000, 2)
            if response.is_success:
                async for line in response.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    event = json.loads(line[5:])
                    kind = event.get('type')
                    if isinstance(kind, str) and re.fullmatch(r'[a-z_.]{1,60}', kind):
                        result['last_event'] = kind
                    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
                    if kind == 'ready':
                        result.setdefault('ready_ms', elapsed_ms)
                    elif kind == 'context.window':
                        result.setdefault('context_window_ms', elapsed_ms)
                    elif kind == 'reasoning.delta' and event.get('text', '').strip():
                        result.setdefault('first_reasoning_ms', elapsed_ms)
                    elif kind == 'text.delta' and event.get('text', '').strip() and result['first_text_ms'] is None:
                        result['first_text_ms'] = elapsed_ms
                    if kind == 'run.completed':
                        result['completed'] = True
                        result.setdefault('run_completed_ms', elapsed_ms)
                    elif kind in {'stream.error', 'request.failed', 'request.timed_out', 'run.failed'}:
                        result['error_event'] = kind
                        code = event.get('code')
                        if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,60}', code):
                            result['error_code'] = code
                    elif kind in {'run.cancelled', 'run.interrupted'}:
                        result['terminal_event'] = kind
                    elif kind == 'stream.done':
                        result.setdefault('stream_done_ms', elapsed_ms)
                        reason = event.get('reason')
                        if isinstance(reason, str) and re.fullmatch(r'[a-z_]{1,60}', reason):
                            result['end_reason'] = reason
    except (httpx.HTTPError, TimeoutError, ValueError) as error:
        result['error_type'] = type(error).__name__
    result['elapsed_ms'] = round((time.perf_counter() - started) * 1000, 2)
    return result


async def exercise(api, config, users):
    limits = httpx.Limits(max_connections=users, max_keepalive_connections=users)
    async with httpx.AsyncClient(base_url=api.base + '/api/v1', timeout=120, trust_env=False, limits=limits) as client:
        semaphore = asyncio.Semaphore(4)
        accounts = await asyncio.gather(*(prepare(client, semaphore, config, index) for index in range(users)))
        return await asyncio.gather(*(measure_one(client, account, config['model']) for account in accounts))


def run(config, users, database_url=None):
    endpoint = urlsplit(config['base_url'])
    if endpoint.scheme != 'https' or endpoint.hostname != 'api.deepseek.com':
        raise ValueError('Use the authorized official DeepSeek HTTPS profile')
    api = IsolatedServer('live-j21-sse', app='live_sse_fixture:app', database_url=database_url)
    fixture = f'live_request_limit={users + 2}\n' + FIXTURE + '''
from cryptography.fernet import Fernet
vault = Fernet(Fernet.generate_key())
llm._vault = lambda: vault
'''
    (api.root / 'live_sse_fixture.py').write_text(fixture, encoding='utf-8')
    result = {'users': users, 'provider': 'deepseek', 'host': endpoint.hostname,
              'database': 'postgresql' if database_url else 'sqlite',
              'model': config['model'], 'simulated_model_calls': 0, 'limits_ms': {'first_text_p95': 2000}}
    print('LIVE_J21_SSE_EVIDENCE', api.root, flush=True)
    try:
        with api:
            rows = asyncio.run(exercise(api, config, users))
        tokens = sorted(row['first_text_ms'] for row in rows if row['first_text_ms'] is not None)
        completed = sum(row['completed'] and row['first_text_ms'] is not None
                        and 'error_event' not in row and 'error_type' not in row for row in rows)
        p95 = tokens[math.ceil(len(tokens) * .95) - 1] if tokens else None
        result.update(first_text_p95_ms=p95, completed=completed, first_text_samples=len(tokens), rows=rows)
    except (AssertionError, httpx.HTTPError, OSError, RuntimeError, TimeoutError, ValueError) as error:
        result['fixture_error'] = type(error).__name__
    finally:
        trace = api.root / 'upstream.jsonl'
        events = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()] if trace.is_file() else []
        calls = [event for event in events if event.get('path', '').endswith('/chat/completions')]
        result['actual_upstream_requests'] = dict(Counter(str(event.get('status', event['kind'])) for event in calls))
        result['actual_stream_receipts'] = sum(event['kind'] == 'stream_identity' for event in events)
        result['functional_passed'] = result.get('completed') == users and result['actual_upstream_requests'].get('200', 0) >= users
        result['slo_passed'] = result['functional_passed'] and result['first_text_p95_ms'] < 2000
        result['isolated_api_exited'] = api.proc is not None and api.proc.poll() is not None
        encoded = json.dumps(result, indent=2)
        assert config['key'] not in encoded
        (api.root / 'live-result.json').write_text(encoded + '\n', encoding='utf-8')
    return 0 if result['slo_passed'] else 1


async def main(args):
    config = await source(args.source_profile_id)
    if not args.postgres:
        return await asyncio.to_thread(run, config, args.users)
    from live_llm_terminal_agent_acceptance import disposable_database

    async with disposable_database(True) as database_url:
        return await asyncio.to_thread(run, config, args.users, database_url)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id', required=True, type=int)
    parser.add_argument('--users', choices=(2, 50), default=50, type=int)
    parser.add_argument('--postgres', action='store_true', help='use a disposable database on the local PostgreSQL server')
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args)))
