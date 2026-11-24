"""Actual installed CLI through product HTTP APIs with a local Responses fixture.

No personal CLI home, login, hosted inference or external provider is used.
Select this suite explicitly; installed binary availability is required.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
import time
import uuid

from testsupport import IsolatedServer
from codex_session_smoke import wait_for


class Provider(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    behavior = 'text'
    calls = []
    entered = threading.Event()
    release = threading.Event()
    question_sent = False
    secret = 'local-responses-test-' + uuid.uuid4().hex
    environment_sent = False
    environment_verified = False
    shell_command = "if (Test-Path Env:VIBEFLOW_CODEX_PROVIDER_TOKEN) { 'CREDENTIAL_LEAK' } else { 'CREDENTIAL_ISOLATED' }"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        size = int(self.headers.get('Content-Length', '0'))
        assert self.path == '/v1/responses' and 0 < size <= 4 * 1024 * 1024
        request = json.loads(self.rfile.read(size))
        assert self.headers.get('Authorization') == 'Bearer ' + self.secret, 'Bound API credential was not used'
        names = []
        question = None
        execution = None
        def inspect(values, namespace=None):
            nonlocal question, execution
            for value in values:
                if value.get('type') == 'namespace':
                    inspect(value.get('tools', []), value.get('name'))
                elif isinstance(value.get('name'), str):
                    names.append(value['name'])
                    if value['name'] == 'request_user_input':
                        question = (value, namespace)
                    if value['name'] == 'exec_command':
                        execution = (value, namespace)
        inspect(request.get('tools', []))
        self.calls.append({'model': request.get('model'), 'tool_names': names,
                           'bound_authorization_verified': True, 'behavior': self.behavior})
        if self.behavior == 'block':
            self.entered.set()
            self.release.wait(20)
            return
        if self.behavior == 'failure':
            payload = b'{"error":{"message":"actual-turn-fixture-rejected"}}'
            self.send_response(400)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(payload)
            return
        if ((self.behavior == 'question' and not type(self).question_sent)
                or (self.behavior == 'environment' and not type(self).environment_sent)):
            if self.behavior == 'environment':
                assert execution is not None, names
                type(self).environment_sent = True
                tool = execution
                arguments = {'cmd': self.shell_command,
                             'login': False, 'max_output_tokens': 100}
            else:
                assert question is not None, names
                type(self).question_sent = True
                tool = question
                arguments = {'questions': [{'id': 'fixture_choice', 'header': 'Choice',
                    'question': 'Choose the local fixture answer.',
                    'options': [{'label': 'A', 'description': 'First fixture choice.'},
                                {'label': 'B', 'description': 'Second fixture choice.'}]}]}
            item = {'id': 'fc_' + uuid.uuid4().hex, 'type': 'function_call', 'status': 'completed',
                    'call_id': 'call_' + uuid.uuid4().hex, 'name': tool[0]['name'],
                    'arguments': json.dumps(arguments)}
            if tool[1]:
                item['namespace'] = tool[1]
            events = [('response.output_item.added', {'output_index': 0, 'item': {**item, 'arguments': ''}}),
                      ('response.function_call_arguments.delta', {'item_id': item['id'], 'output_index': 0, 'delta': item['arguments']}),
                      ('response.function_call_arguments.done', {'item_id': item['id'], 'output_index': 0, 'arguments': item['arguments']}),
                      ('response.output_item.done', {'output_index': 0, 'item': item})]
        else:
            if self.behavior == 'environment':
                outputs = [value.get('output') for value in request.get('input', []) if value.get('type') == 'function_call_output']
                text = json.dumps(outputs)
                assert 'CREDENTIAL_ISOLATED' in text and 'CREDENTIAL_LEAK' not in text, 'Shell inherited provider credential or did not run'
                type(self).environment_verified = True
            text = 'ACTUAL_QUESTION_OK' if self.behavior == 'question' else 'ACTUAL_TURN_OK'
            item = {'id': 'msg_' + uuid.uuid4().hex, 'type': 'message', 'status': 'completed', 'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}
            events = [('response.output_item.added', {'output_index': 0, 'item': {**item, 'status': 'in_progress', 'content': []}}),
                      ('response.content_part.added', {'item_id': item['id'], 'output_index': 0, 'content_index': 0,
                          'part': {'type': 'output_text', 'text': '', 'annotations': []}}),
                      ('response.output_text.delta', {'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'delta': text}),
                      ('response.output_text.done', {'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'text': text}),
                      ('response.content_part.done', {'item_id': item['id'], 'output_index': 0, 'content_index': 0, 'part': item['content'][0]}),
                      ('response.output_item.done', {'output_index': 0, 'item': item})]
        response = {'id': 'resp_' + uuid.uuid4().hex, 'object': 'response', 'status': 'completed', 'output': [item],
                    'usage': {'input_tokens': 1, 'output_tokens': 5, 'total_tokens': 6,
                              'input_tokens_details': {'cached_tokens': 0}, 'output_tokens_details': {'reasoning_tokens': 0}}}
        events.insert(0, ('response.created', {'response': {**response, 'status': 'in_progress', 'output': []}}))
        events.append(('response.completed', {'response': response}))
        payload = b''.join(('event: ' + kind + '\ndata: ' + json.dumps({'type': kind, **params}) + '\n\n').encode()
                           for kind, params in events)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(payload)


def main():
    provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    api = IsolatedServer('cli-turn-actual', app='actual_turn:app')
    (api.root / 'actual_turn.py').write_text('''from app.main import app
from app.services import llm
from pathlib import Path
llm._VAULT_KEY_PATH=Path(__file__).parent/'test-vault-key'
''', encoding='utf8')
    receipts = []
    checks = []
    try:
        with api:
            case = api.create_case('Actual CLI turn API')
            profile = api.call('POST', '/provider-profiles', {'provider': 'custom', 'name': 'Local Responses fixture',
                'base_url': f'http://127.0.0.1:{provider.server_port}/v1', 'default_model': 'fixture-model', 'api_key': Provider.secret})
            conv = api.call('POST', '/conversations', {'case_id': case['id'], 'permission_mode': 'plan'})
            def session():
                return api.call('POST', '/codex/sessions', {'case_id': case['id'], 'conversation_id': conv['id'],
                    'provider_profile_id': profile['id'], 'provider_wire_api': 'responses'})
            def submit(current, **kwargs):
                return api.call('POST', '/codex/sessions/' + current['id'] + '/turns', {
                    'content': 'Run the local fixture.', 'client_message_id': uuid.uuid4().hex, **kwargs})
            def done(turn, expected='completed'):
                row = wait_for(lambda: api.call('GET', '/codex/turns/' + turn['id']),
                    lambda value: value['phase'] in {'completed', 'failed', 'cancelled', 'interrupted'}, seconds=40)
                assert row['phase'] == expected and row['cleanup_verified'], row
                parent = api.call('GET', '/codex/sessions/' + row['session_id'])
                stream = api.call('GET', '/codex/turns/' + row['id'] + '/events', raw=True).decode('utf8')
                assert 'stream.done' in stream
                receipts.append({'turn': row, 'session': parent, 'events': stream})
                return row, stream
            current = session()
            account = api.call('GET', '/codex/sessions/' + current['id'] + '/account')
            assert account['account'] is None and account['credential']['provider_profile_id'] == profile['id']
            first, stream = done(submit(current))
            assert 'ACTUAL_TURN_OK' in stream
            checks.append('actual_plan_mode_text_and_scope_exit')
            resumed, _ = done(submit(session(), resume=True))
            assert resumed['thread_id'] == first['thread_id'] and resumed['session_id'] != first['session_id']
            checks.append('explicit_resume_after_verified_previous_scope')
            Provider.behavior = 'question'
            question_turn = submit(session(), resume=True)
            path = '/codex/turns/' + question_turn['id']
            pending = wait_for(lambda: api.call('GET', path + '/approvals'), bool, seconds=25)[0]
            assert pending['method'] == 'item/tool/requestUserInput', pending
            api.call('POST', path + '/approvals/' + pending['id'] + ':respond', {'answers': {'fixture_choice': ['A']}})
            _, stream = done(question_turn)
            assert 'ACTUAL_QUESTION_OK' in stream
            checks.append('actual_plan_user_question_roundtrip')
            Provider.behavior = 'environment'
            conv = api.call('POST', '/conversations', {'case_id': case['id'], 'permission_mode': 'full_access'})
            done(submit(session()))
            assert Provider.environment_verified
            checks.append('actual_native_shell_does_not_inherit_provider_credential')
            Provider.behavior = 'failure'
            done(submit(session()), 'failed')
            checks.append('actual_failed_request_never_completed')
            Provider.behavior = 'block'
            blocked = submit(session())
            assert Provider.entered.wait(20), 'Actual request did not reach the local fixture'
            started = time.monotonic()
            api.call('POST', '/codex/turns/' + blocked['id'] + ':cancel')
            done(blocked, 'cancelled')
            assert time.monotonic() - started < 15
            checks.append('actual_cancel_waits_native_ack_and_scope_exit')
            assert Provider.calls and all(call['bound_authorization_verified'] for call in Provider.calls)
            assert Provider.secret not in json.dumps(receipts)
            for path in (api.root / 'cli-state').rglob('*'):
                if path.is_file():
                    assert Provider.secret.encode() not in path.read_bytes(), 'Credential persisted in private CLI state'
            checks.append('product_byok_credentials_only_in_process_environment_and_provider_header')
        result = {'passed': True, 'count': len(checks), 'checks': checks, 'actual_cli': True,
                  'local_fixture_provider': True, 'hosted_inference': False, 'evidence': str(api.root)}
        (api.root / 'actual-result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
        print(json.dumps(result))
    finally:
        Provider.release.set()
        provider.shutdown()
        provider.server_close()
        (api.root / 'turn-receipts.json').write_text(json.dumps(receipts, indent=2), encoding='utf8')
        (api.root / 'provider-calls.json').write_text(json.dumps(Provider.calls, indent=2), encoding='utf8')


if __name__ == '__main__':
    main()
