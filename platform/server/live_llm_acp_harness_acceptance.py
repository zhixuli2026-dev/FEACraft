"""Installed OpenCode and Kimi ACP, with official DeepSeek replies and real process scopes."""
import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import tempfile
from threading import Event, Lock, Thread

import httpx

from app.services import acp_runtime
from app.services.acp_session import AcpSession
from app.services.owned_stdio import OwnedStdio
from live_llm_acceptance import source
from testsupport import SERVER


class Relay:
    """Record actual upstream responses and optionally hold delivery to the CLI."""
    def __init__(self, config, directory, *, responses=False, limit=8):
        self.token = secrets.token_hex(32)
        self.records = []
        self.count = 0
        self.lock = Lock()
        self.held = Event()
        self.release = Event()
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                size = int(self.headers.get('Content-Length', '0'))
                routes = {'/v1/chat/completions', '/responses'} if responses else {'/v1/chat/completions'}
                if (self.path not in routes or self.headers.get('Authorization') != 'Bearer '+relay.token
                        or not 0 < size < 2*1024*1024):
                    self.send_error(403)
                    return
                try:
                    body = self.rfile.read(size)
                    request = json.loads(body)
                    assert request['model'] == config['model']
                    with relay.lock:
                        if relay.count >= limit:
                            raise ValueError('Live request budget exhausted')
                        relay.count += 1
                    with httpx.Client(timeout=85, follow_redirects=False) as client:
                        response = client.post('https://api.deepseek.com'+self.path, content=body,
                            headers={'Authorization': 'Bearer '+config['key'], 'Content-Type': 'application/json',
                                     'Accept-Encoding': 'identity'})
                    assert config['key'].encode() not in response.content
                    record = {'host': 'api.deepseek.com', 'path': self.path, 'status': response.status_code,
                              'model': request['model'], 'stream': request.get('stream'),
                              'advertised_tools': len(request.get('tools') or []),
                              'request_id': response.headers.get('x-request-id')}
                    with relay.lock:
                        relay.records.append(record)
                        (directory/'model-receipts.json').write_text(json.dumps(relay.records, indent=2), encoding='utf8')
                    if b'ACP_HOLD_ACTUAL_REPLY' in body and response.status_code == 200:
                        relay.held.set()
                        if not relay.release.wait(28):
                            raise TimeoutError('Cancellation observation did not finish')
                    self.send_response(response.status_code)
                    self.send_header('Content-Type', response.headers['Content-Type'])
                    self.send_header('Content-Length', str(len(response.content)))
                    self.end_headers()
                    self.wfile.write(response.content)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    pass
                except Exception as error:
                    with relay.lock:
                        relay.records.append({'failure': type(error).__name__})
                    self.send_error(502, 'Actual inference relay failed')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.base = f'http://127.0.0.1:{self.server.server_port}/v1'

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)


def settings(harness, home, config, relay, *, permission_test=False):
    return acp_runtime.command(harness), acp_runtime.environment(harness, home, endpoint=relay.base,
        model=config['model'], api_key=relay.token, allow_file_approval=permission_test)


async def verify(harness, config):
    root = Path(tempfile.mkdtemp(prefix='live-acp-'+harness+'-', dir=SERVER.parents[1]/'tmp'))
    case = root/'case'
    case.mkdir()
    print('LIVE_ACP_EVIDENCE', root, flush=True)
    report = {'passed': False, 'harness': harness, 'model': config['model'], 'simulated_llm': False, 'scopes': []}
    marker = 'ACP_SESSION_'+secrets.token_hex(6).upper()
    frames = []

    async def update(content):
        if content.get('sessionUpdate') == 'agent_message_chunk':
            item = content.get('content', {})
            if item.get('type') == 'text':
                frames.append(item['text'])

    async def open_client(argv, env, session_id=None):
        client = AcpSession(OwnedStdio(argv, cwd=case, env=env, lifetime_seconds=200,
                                      max_output_bytes=12*1024*1024), emit=update)
        async with asyncio.timeout(35):
            init, _ = await client.start(str(case), session_id=session_id)
        report['version'] = init['agentInfo']['version']
        return client

    async def close(client):
        if client is None:
            return
        value = await client.close()
        assert value['verified'] and value['scope_empty']
        report['scopes'].append({'verified': value['verified'], 'scope_empty': value['scope_empty']})

    try:
        with Relay(config, root) as relay:
            argv, env = settings(harness, root/'private', config, relay)
            active = None
            try:
                active = await open_client(argv, env)
                session = active.session_id
                async with asyncio.timeout(70):
                    done = await active.prompt('Remember this reference code for this conversation: '+marker+
                                               '. Reply with that exact code as text. Use no tools.')
                assert done['stopReason'] == 'end_turn' and marker in ''.join(frames)
                report['first_prompt'] = True
                await close(active)
                active = None
                frames.clear()
                active = await open_client(argv, env, session)
                frames.clear()
                async with asyncio.timeout(70):
                    done = await active.prompt('Repeat the exact reference code from my first message. Use no tools.')
                assert done['stopReason'] == 'end_turn' and marker in ''.join(frames)
                report['resume_same_session'] = True
                frames.clear()
                pending = asyncio.create_task(active.prompt(
                    'ACP_HOLD_ACTUAL_REPLY. Say a short sentence about mesh quality. Use no tools.'))
                try:
                    assert await asyncio.to_thread(relay.held.wait, 55), relay.records
                    await active.cancel()
                    async with asyncio.timeout(18):
                        cancelled = await pending
                    assert cancelled['stopReason'] == 'cancelled', cancelled
                    report['native_cancel'] = True
                finally:
                    relay.release.set()
                    if not pending.done():
                        pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                frames.clear()
                async with asyncio.timeout(65):
                    done = await active.prompt('Repeat the original reference code once more. No tools.')
                assert done['stopReason'] == 'end_turn' and marker in ''.join(frames)
                report['after_cancel'] = True
            finally:
                await close(active)
                report['upstream'] = relay.records
            assert sum(row.get('status') == 200 for row in report['upstream']) >= 4
        report['passed'] = True
    except Exception as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error).replace(config['key'], '[REDACTED]')[:300]}
        raise
    finally:
        (root/'live-result.json').write_text(json.dumps(report, indent=2), encoding='utf8')


async def verify_permissions(harness, config):
    root = Path(tempfile.mkdtemp(prefix='live-acp-permissions-'+harness+'-', dir=SERVER.parents[1]/'tmp'))
    case = root/'case'
    case.mkdir()
    report = {'passed': False, 'harness': harness, 'simulated_llm': False, 'decisions': []}
    print('LIVE_ACP_PERMISSIONS_EVIDENCE', root, flush=True)
    choice = None
    target = None
    client = None

    async def approve(params):
        call = params['tool_call']
        info = {'kind': call.get('kind'), 'expected_path': target.name in json.dumps(call),
                'options': [option['kind'] for option in params['options']],
                'tool_fields':sorted(call),
                'location_matches':any(Path(item.get('path','')).name == target.name
                    for item in call.get('locations') or [] if isinstance(item, dict)),
                'input_fields':sorted(call['rawInput']) if isinstance(call.get('rawInput'), dict) else []}
        report['decisions'].append(info)
        assert info['expected_path'] and info['kind'] not in {'execute', 'fetch'}
        assert not client.respond('unrelated-approval', 'unrelated-option')
        try:
            client.respond(params['id'], 'unrelated-option')
        except ValueError:
            pass
        else:
            raise AssertionError('Unrelated ACP permission choice was accepted')
        for option in params['options']:
            if option.get('kind') == choice:
                assert client.respond(params['id'], option['optionId'])
                assert not client.respond(params['id'], option['optionId'])
                info['selected'] = choice
                return
        raise AssertionError('Expected ACP permission option was missing')

    try:
        with Relay(config, root) as relay:
            argv, env = settings(harness, root/'private', config, relay, permission_test=True)
            client = AcpSession(OwnedStdio(argv, cwd=case, env=env, lifetime_seconds=180,
                                          max_output_bytes=12*1024*1024), request_permission=approve)
            try:
                async with asyncio.timeout(30):
                    await client.start(str(case))
                for choice, name in (('reject_once', 'refused.txt'), ('allow_once', 'approved.txt')):
                    target = case/name
                    code = 'ACP_APPROVAL_'+secrets.token_hex(6)
                    async with asyncio.timeout(65):
                        await client.prompt('Use exactly one file write tool to create '+name+
                                            ' in the current working directory containing only '+code+
                                            '. Use a file write tool, no shell. If it is declined, do not retry.')
                    assert any(item.get('selected') == choice for item in report['decisions']), report['decisions']
                    if choice == 'allow_once':
                        assert target.read_text(encoding='utf8').strip() == code
                    else:
                        assert not target.exists()
                report['passed'] = True
            finally:
                stopped = await client.close()
                report['scope_empty'] = bool(stopped and stopped['verified'] and stopped['scope_empty'])
                report['upstream'] = relay.records
    except Exception as error:
        report['passed'] = False
        report['failure'] = {'type': type(error).__name__, 'message': str(error).replace(config['key'], '[REDACTED]')[:300]}
        raise
    finally:
        (root/'live-permissions.json').write_text(json.dumps(report, indent=2), encoding='utf8')


async def main(args):
    config = await source(args.source_profile_id)
    if args.permissions:
        await verify_permissions(args.harness, config)
    else:
        await verify(args.harness, config)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--harness', choices=('opencode', 'kimi'), required=True)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--permissions', action='store_true')
    asyncio.run(main(parser.parse_args()))
