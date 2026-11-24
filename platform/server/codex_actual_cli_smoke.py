"""Installed Codex app-server with a local Responses fixture, no user login.

This is real CLI/protocol evidence, not evidence of hosted model inference.
It runs only when explicitly selected; the offline suite uses subprocess fixtures.
"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import uuid

from app.services.codex_environment import create_isolated_codex_environment
from app.services.codex_stdio import start_codex_stdio
from app.services.codex_turn import CodexTurnError, run_codex_turn


class Provider(BaseHTTPRequestHandler):
    calls = []
    behavior = 'success'
    entered = threading.Event()
    release = threading.Event()
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_args):
        pass

    def do_POST(self):
        size = int(self.headers.get('Content-Length', '0'))
        if self.path != '/v1/responses' or not 0 < size <= 4 * 1024 * 1024:
            self.send_error(400)
            return
        request = json.loads(self.rfile.read(size))
        self.calls.append({'model': request.get('model'), 'input_present': bool(request.get('input')),
                           'authorization_present': bool(self.headers.get('Authorization'))})
        assert not self.headers.get('Authorization'), 'Fixture must not receive user credentials'
        if self.behavior == 'failure':
            data = json.dumps({'error': {'message': 'fixture-rejected'}}).encode()
            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(data)
            return
        if self.behavior == 'block':
            self.entered.set()
            self.release.wait(15)
            return  # The cancelled request is intentionally never answered.
        response_id = 'resp_' + uuid.uuid4().hex
        message = {'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'status': 'completed', 'role': 'assistant',
                   'content': [{'type': 'output_text', 'text': 'OWNED_CLI_FIXTURE_OK', 'annotations': []}]}
        response = {'id': response_id, 'object': 'response', 'status': 'completed', 'output': [message],
                    'usage': {'input_tokens': 1, 'output_tokens': 5, 'total_tokens': 6,
                              'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}}}
        events = [
            ('response.created', {'response': {**response, 'status': 'in_progress', 'output': []}}),
            ('response.output_item.added', {'output_index': 0, 'item': {**message, 'status': 'in_progress', 'content': []}}),
            ('response.content_part.added', {'item_id': message['id'], 'output_index': 0, 'content_index': 0,
                                             'part': {'type': 'output_text', 'text': '', 'annotations': []}}),
            ('response.output_text.delta', {'item_id': message['id'], 'output_index': 0, 'content_index': 0, 'delta': 'OWNED_CLI_FIXTURE_OK'}),
            ('response.output_text.done', {'item_id': message['id'], 'output_index': 0, 'content_index': 0, 'text': 'OWNED_CLI_FIXTURE_OK'}),
            ('response.content_part.done', {'item_id': message['id'], 'output_index': 0, 'content_index': 0, 'part': message['content'][0]}),
            ('response.output_item.done', {'output_index': 0, 'item': message}),
            ('response.completed', {'response': response}),
        ]
        data = b''.join(('event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **params}) + '\n\n').encode('utf8')
                        for kind, params in events)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(data)


async def main():
    program = shutil.which('codex.exe') or shutil.which('codex')
    if program is None:
        raise RuntimeError('Install Codex CLI before selecting the actual CLI test')
    root = Path(tempfile.mkdtemp(prefix='codex-actual-evidence-', dir=Path(__file__).resolve().parents[2] / 'tmp'))
    work = root / 'workspace'
    work.mkdir()
    environment = create_isolated_codex_environment(root / 'home')
    environment['NO_PROXY'] = '127.0.0.1,localhost'
    environment['no_proxy'] = '127.0.0.1,localhost'
    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    config = root / 'home/config.toml'
    with config.open('a', encoding='utf8') as stream:
        stream.write('\nmodel = "fixture-model"\nmodel_provider = "vibeflow_fixture"\n'
                     '[model_providers.vibeflow_fixture]\nname = "Local evidence fixture"\n'
                     f'base_url = "http://127.0.0.1:{provider.server_port}/v1"\n'
                     'wire_api = "responses"\nrequires_openai_auth = false\nsupports_websockets = false\n'
                     'request_max_retries = 0\nstream_max_retries = 0\n')
    client = None
    events = []
    try:
        client = await start_codex_stdio([program, 'app-server', '--listen', 'stdio://'], environment, cwd=work)
        assert (await client.get_account())['account'] is None
        created = await client.new_thread(cwd=work)
        thread_id = created['thread']['id']
        async def capture(event):
            events.append(event)
        result = await run_codex_turn(client, thread_id, 'Return the fixture response.', 'actual-fixture-1',
                                      on_event=capture, turn_timeout_seconds=60)
        assert result['status'] == 'completed', result
        assert 'OWNED_CLI_FIXTURE_OK' in json.dumps(events), events[-2:]
        resumed = await client.resume_thread(thread_id, exclude_turns=True)
        assert resumed['thread']['id'] == thread_id
        forked = await client.fork_thread(thread_id, result['lastTurnId'], True)
        assert forked['thread']['id'] != thread_id
        Provider.behavior = 'failure'
        try:
            await run_codex_turn(client, thread_id, 'Return failure fixture.', 'actual-fixture-failure', turn_timeout_seconds=30)
            raise AssertionError('Failed actual turn must not report completion')
        except CodexTurnError as error:
            assert '400' in str(error) or 'fixture-rejected' in str(error), str(error)
        Provider.behavior = 'block'
        cancel = asyncio.Event()
        task = asyncio.create_task(run_codex_turn(client, thread_id, 'Wait for cancellation fixture.', 'actual-fixture-cancel',
                                                   cancel=cancel, turn_timeout_seconds=30))
        async with asyncio.timeout(10):
            while not Provider.entered.is_set():
                await asyncio.sleep(.02)
        cancel.set()
        try:
            await asyncio.wait_for(task, 10)
            raise AssertionError('Cancellation must not report completion')
        except asyncio.CancelledError:
            pass
        Provider.release.set()
        assert client.initialized, 'Actual interrupted acknowledgement should keep the CLI usable'
        assert Provider.calls and all(not row['authorization_present'] for row in Provider.calls)
        value = {'passed': True, 'actual_cli': str(program), 'local_fixture_provider': True,
                 'initialize_user_agent': client.initialize_result['userAgent'],
                 'hosted_inference': False, 'account_present': False, 'provider_calls': len(Provider.calls),
                 'turn_status': result['status'], 'resume': True, 'fork': True,
                 'failed_turn_not_completed': True, 'cancel_waited_terminal_ack': True, 'evidence': str(root)}
    finally:
        Provider.release.set()
        (root / 'provider-calls.json').write_text(json.dumps(Provider.calls), encoding='utf8')
        if client is not None:
            (root / 'diagnostic-tail.txt').write_bytes(client.transport.stderr_tail)
            stop = await client.close()
            (root / 'stop.json').write_text(json.dumps(stop), encoding='utf8')
        await asyncio.to_thread(provider.shutdown)
        provider.server_close()
    assert stop['verified']
    value['stop_verified'] = True
    (root / 'result.json').write_text(json.dumps(value, indent=2), encoding='utf8')
    print(json.dumps(value))


if __name__ == '__main__':
    asyncio.run(main())
