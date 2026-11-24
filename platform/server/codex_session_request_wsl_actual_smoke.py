"""Exercise the actual Linux CLI matrix through durable startup admission.

Reuses the installed-CLI scenarios, replacing their session factory at the
HTTP client boundary. No legacy POST /codex/sessions reaches the server.
All providers and private Linux state are disposable local fixtures.
"""
import asyncio
import inspect
import json
import uuid

from testsupport import IsolatedServer
from codex_session_smoke import wait_for
from codex_wsl_actual_smoke import DRIVER, exercise
from codex_turn_actual_smoke import Provider
from codex_wsl_test_support import NativeStateFixture
from app.services.codex_stdio import CodexStdioClient
from app.services.codex_environment import create_isolated_codex_environment
from app.services.owned_process import wsl_path


class StartupServer(IsolatedServer):
    def __init__(self, provider, loop):
        super().__init__('native-startup-wsl-actual', app='actual_turn:app')
        self.provider, self.loop = provider, loop
        self.admissions, self.final = [], []

    def calls(self):
        value = asyncio.run_coroutine_threadsafe(self.provider.request('status'), self.loop).result(15)
        return len(value['calls'])

    def call(self, method, path, body=None, **kwargs):
        if method != 'POST' or path != '/codex/sessions':
            return super().call(method, path, body, **kwargs)
        assert body['runtime'] == 'wsl' and body['case_id'] == self.case_id
        endpoint = f"/codex/conversations/{body['conversation_id']}/session-requests"
        request = {key: body[key] for key in ('provider_profile_id', 'provider_wire_api', 'runtime')}
        request.update(client_request_id=uuid.uuid4().hex, purpose='chat')
        before = self.calls()
        admitted = super().call('POST', endpoint, request)
        value = wait_for(lambda: self.call('GET', '/codex/session-requests/' + admitted['id']),
            lambda row: row['phase'] not in {'admitted', 'starting'}, seconds=60)
        assert value['phase'] == 'completed' and value['runtime'] == 'wsl', value
        assert value['session']['phase'] == 'active' and value['session']['thread_id'] is None, value
        assert self.calls() == before, 'Session initialization invoked inference'
        replay = super().call('POST', endpoint, request)
        found = self.call('GET', endpoint + '?client_request_id=' + request['client_request_id'])
        assert len(found) == 1 and replay['id'] == found[0]['id'] == value['id']
        assert replay['session']['id'] == value['session']['id'] and self.calls() == before
        self.admissions.append(value)
        return value['session']

    def __exit__(self, error_type, error, traceback):
        try:
            if error_type is None:
                self.final = [self.call('GET', '/codex/session-requests/' + row['id']) for row in self.admissions]
                assert len(self.final) == 6
                assert all(row['phase'] == 'completed' and row['cleanup_verified'] is True for row in self.final)
                assert all(row['session']['stop']['native_state'] == 'exited' for row in self.final)
        finally:
            (self.root / 'startup-receipts.json').write_text(json.dumps({
                'admitted': self.admissions, 'final': self.final}, indent=2), encoding='utf8')
            super().__exit__(error_type, error, traceback)


async def main():
    loop = asyncio.get_running_loop()
    api = StartupServer(None, loop)
    (api.root / 'actual_turn.py').write_text("from app.main import app\nfrom app.services import llm\nfrom pathlib import Path\nllm._VAULT_KEY_PATH=Path(__file__).parent/'test-vault-key'\n", encoding='utf8')
    script = api.root / 'provider.py'
    script.write_text('from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer\nimport json,threading,uuid,sys\n'
        + inspect.getsource(Provider) + '\n' + DRIVER, encoding='utf8')
    environment = create_isolated_codex_environment(api.root / 'provider-home')
    environment.update(HOME=str(api.root), TMPDIR=str(api.root))
    provider = CodexStdioClient(['/usr/bin/python3', '-I', '-S', '-u', wsl_path(script)],
        environment, cwd=api.root, distro='vibeflow-linux')
    api.provider = provider
    with NativeStateFixture() as native_state:
        try:
            await provider.start()
            await asyncio.to_thread(exercise, api, provider, loop, native_state)
        finally:
            await provider.close()
            assert provider.stop_result['verified']
            (api.root / 'provider-stop.json').write_text(json.dumps(provider.stop_result, indent=2), encoding='utf8')
    # The fixture context has now removed its strictly checked native directory.
    result = json.loads((api.root / 'actual-result.json').read_text(encoding='utf8'))
    result['checks'] += ['all_six_linux_sessions_use_idempotent_startup_api_without_inference',
        'all_six_startup_receipts_link_verified_native_scope_exit',
        'owned_linux_provider_closed_and_private_test_state_removed']
    result.update(count=len(result['checks']), startup_requests=len(api.admissions),
        legacy_session_posts=0, native_test_state_removed=True, provider_cleanup_verified=True)
    (api.root / 'startup-result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result))


if __name__ == '__main__':
    asyncio.run(main())
