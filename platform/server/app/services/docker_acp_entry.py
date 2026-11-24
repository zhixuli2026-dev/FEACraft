"""Fixed container entrypoints for ACP stdio and its isolated model relay."""

import hmac
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
from select import select
import socket
from socketserver import BaseRequestHandler, ThreadingTCPServer, ThreadingUnixStreamServer
import subprocess
import sys
import tempfile
import threading

import httpx

# The trusted launcher mounts only the two entrypoint files at this location.
sys.path.insert(0, '/vf-runtime')
from acp_configuration import HARNESSES, environment  # noqa: E402


RELAY_SOCKET = '/relay/provider.sock'
DEEPSEEK = 'https://api.deepseek.com'
MAX_REQUEST = 8 * 1024**2
MAX_RESPONSES_REQUEST = 24 * 1024**2


def relay(config):
    route = {'chat':'/v1/chat/completions','responses':'/v1/responses'}[config.get('wire','chat')]
    max_request = MAX_RESPONSES_REQUEST if route == '/v1/responses' else MAX_REQUEST
    token = ('Bearer '+config['capability']).encode()
    upstream = {'Authorization':'Bearer '+config['key'], 'Content-Type':'application/json'}
    lock = threading.Lock()
    calls = 0
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=100) as client:
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(110)
                super().setup()

            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path == '/v1/models':
                    self.forward('GET')
                else:
                    self.send_error(404)

            def do_POST(self):
                if self.path == route:
                    self.forward('POST')
                else:
                    self.send_error(404)

            def forward(self, method):
                nonlocal calls
                if not hmac.compare_digest(self.headers.get('Authorization','').encode(), token):
                    return self.send_error(401)
                body = None
                if method == 'POST':
                    try:
                        size = int(self.headers.get('Content-Length','0'))
                        if not 0 < size <= max_request:
                            return self.send_error(413)
                        body = self.rfile.read(size)
                        if json.loads(body).get('model') != config['model']:
                            return self.send_error(400)
                    except (ValueError, AttributeError):
                        return self.send_error(400)
                with lock:
                    if calls >= 24:
                        return self.send_error(429)
                    calls += 1
                status, complete, sent, parts = None, False, False, 0
                try:
                    with client.stream(method, DEEPSEEK+self.path, content=body, headers=upstream) as response:
                        status = response.status_code
                        self.send_response(status)
                        self.send_header('Content-Type', response.headers.get('Content-Type','application/json'))
                        self.send_header('Connection','close')
                        self.end_headers()
                        sent = True
                        for block in response.iter_bytes():
                            self.wfile.write(block)
                            self.wfile.flush()
                            parts += 1
                        complete = True
                except (httpx.HTTPError, OSError):
                    if not sent:
                        self.send_error(502)
                finally:
                    print(json.dumps({'method':method,'status':status,'complete':complete,'parts':parts}), flush=True)

        with ThreadingUnixStreamServer(RELAY_SOCKET, Handler) as server:
            Path(RELAY_SOCKET).chmod(0o600)
            print('VF_ACP_RELAY_READY', flush=True)
            server.serve_forever()


class LoopbackRelay(BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as remote:
            remote.connect(RELAY_SOCKET)
            streams = {remote:self.request, self.request:remote}
            while ready := select(list(streams), [], [], 110)[0]:
                for source in ready:
                    data = source.recv(65536)
                    if not data:
                        return
                    streams[source].sendall(data)


def write_config(directory, name, content):
    if directory.resolve() != directory:
        raise ValueError('ACP state configuration was redirected')
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as temporary:
        path = Path(temporary.name)
        temporary.write(content)
    try:
        path.replace(directory/name)
    finally:
        path.unlink(missing_ok=True)


def served_worker(config, command, ready):
    home = Path('/state')
    if os.getuid() != 65534 or home.stat().st_uid != os.getuid():
        raise ValueError('CLI container has invalid execution identity')
    with ThreadingTCPServer(('127.0.0.1', 0), LoopbackRelay) as bridge:
        bridge.daemon_threads = True
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        env = {key:value for key,value in os.environ.items() if key in {'PATH','LANG','LC_ALL'}}
        endpoint = f'http://127.0.0.1:{bridge.server_address[1]}/v1'
        try:
            argv = command(home, env, endpoint)
            print(ready, flush=True)
            return subprocess.call(argv, env=env, cwd='/workspace')
        finally:
            bridge.shutdown()
            thread.join()


def acp(config):
    harness = config['harness']
    if harness not in HARNESSES:
        raise ValueError('Unsupported ACP harness')
    if config.get('case_inputs') and not os.statvfs('/workspace/case').f_flag & os.ST_RDONLY:
        raise ValueError('ACP case input mount must be read-only')

    def command(home, env, endpoint):
        contract = Path('/workspace/AGENTS.md') if config['contract'] else None
        environment(harness, home, env, endpoint=endpoint,
                    model=config['model'], api_key=config['capability'], managed_text_only=True,
                    allow_case_inputs=config.get('case_inputs',False), allow_file_approval=config['drafts'],
                    contract_file=contract, write_config=write_config)
        return [harness,'acp']

    return served_worker(config,command,'VF_ACP_WORKER_READY')


def codex(config):
    readonly = bool(os.statvfs('/workspace').f_flag & os.ST_RDONLY)
    if readonly == config.get('writable',False):
        raise ValueError('Codex workspace permissions differ from the admitted mode')
    if config.get('inputs') and not os.statvfs('/inputs').f_flag & os.ST_RDONLY:
        raise ValueError('Codex input mount must be read-only')

    def command(home, env, endpoint):
        env.update(HOME=str(home), CODEX_HOME=str(home), TMPDIR='/tmp',
                   VIBEFLOW_CODEX_PROVIDER_TOKEN=config['capability'])
        provider = ('{name="VibeFlow API", base_url='+json.dumps(endpoint)+
                    ', env_key="VIBEFLOW_CODEX_PROVIDER_TOKEN", wire_api="responses", requires_openai_auth=false, '
                    'supports_websockets=false, request_max_retries=0, stream_max_retries=0}')
        return ['codex','-c','cli_auth_credentials_store="file"','-c','model_provider="vibeflow_api"',
                '-c','model_providers.vibeflow_api='+provider,'-c','web_search="disabled"',
                '-c','shell_environment_policy={inherit="core", filters={VIBEFLOW_CODEX_PROVIDER_TOKEN="exclude"}}',
                '-c','allow_login_shell=false','app-server','--listen','stdio://']

    return served_worker(config,command,'VF_CODEX_WORKER_READY')


if __name__ == '__main__':
    mode = sys.argv[1]
    raw = sys.stdin.buffer.readline(16385)
    if not raw.endswith(b'\n') or len(raw) > 16384:
        raise SystemExit('Invalid container startup input')
    settings = json.loads(raw)
    if mode == 'relay':
        relay(settings)
    elif mode == 'acp':
        raise SystemExit(acp(settings))
    elif mode == 'codex':
        raise SystemExit(codex(settings))
    else:
        raise SystemExit('Unknown container entrypoint')
