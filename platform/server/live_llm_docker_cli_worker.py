"""Disposable actual Codex and DeepSeek relay processes for the Docker acceptance."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from select import select
import socket
from socketserver import BaseRequestHandler, ThreadingTCPServer, ThreadingUnixStreamServer
import subprocess
import sys
import tempfile
import threading
from uuid import UUID
from http.server import BaseHTTPRequestHandler

import httpx


RELAY_SOCKET = '/relay/provider.sock'


def cgroup_limit(unified, legacy):
    root = Path('/sys/fs/cgroup')
    path = root / unified
    return (path if path.is_file() else root / legacy).read_text().strip()


def inspect_isolation():
    status = dict(row.split(':', 1) for row in Path('/proc/self/status').read_text().splitlines() if ':' in row)
    limits = {'uid':os.getuid(), 'effective_capabilities':int(status['CapEff'], 16),
              'no_new_privileges':int(status['NoNewPrivs']), 'docker_socket_visible':Path('/var/run/docker.sock').exists(),
              'memory_bytes':int(cgroup_limit('memory.max','memory/memory.limit_in_bytes')),
              'pids':int(cgroup_limit('pids.max','pids/pids.max'))}
    quota = Path('/sys/fs/cgroup/cpu.max')
    if quota.is_file():
        cpu_quota, period = map(int, quota.read_text().split())
    else:
        cpu_quota = int(cgroup_limit('cpu.max','cpu/cpu.cfs_quota_us'))
        period = int(cgroup_limit('cpu.max','cpu/cpu.cfs_period_us'))
    limits['cpu_cores'] = cpu_quota / period
    routes = Path('/proc/net/route').read_text().splitlines()[1:]
    limits['default_route'] = any(row.split()[1] == '00000000' for row in routes)
    limits['interfaces'] = sorted(path.name for path in Path('/sys/class/net').iterdir())
    mounts = [line.split() for line in Path('/proc/self/mountinfo').read_text().splitlines()]
    limits['root_read_only'] = any(row[4] == '/' and 'ro' in row[5].split(',') for row in mounts)
    assert limits['uid'] == 65534 and limits['memory_bytes'] == 768 * 1024**2
    assert limits['pids'] == 64 and limits['cpu_cores'] == 1
    assert limits['root_read_only'] and limits['no_new_privileges'] == 1
    assert not limits['effective_capabilities'] and not limits['docker_socket_visible'] and not limits['default_route']
    assert limits['interfaces'] == ['lo']
    return limits


def relay(cfg):
    """Send the original request to the enabled, authorized DeepSeek endpoint."""
    lock = threading.Lock()
    calls = 0
    with httpx.Client(trust_env=False, timeout=95, follow_redirects=False) as client:
        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if self.path == '/health':
                    self.send_response(204)
                    self.end_headers()
                elif self.path == '/v1/models':
                    self.forward('GET')
                else:
                    self.send_error(404)

            def do_POST(self):
                if self.path not in ('/v1/responses', '/v1/chat/completions'):
                    return self.send_error(404)
                self.forward('POST')

            def forward(self, method):
                nonlocal calls
                if not hmac.compare_digest(self.headers.get('Authorization', '').encode(),
                                           ('Bearer ' + cfg['capability']).encode()):
                    return self.send_error(401)
                body = None
                if method == 'POST':
                    try:
                        size = int(self.headers.get('Content-Length', '0'))
                        if not 0 < size <= 2 * 1024**2:
                            return self.send_error(413)
                        body = self.rfile.read(size)
                        if json.loads(body).get('model') != cfg['model']:
                            return self.send_error(400)
                    except (ValueError, AttributeError):
                        return self.send_error(400)
                with lock:
                    if calls >= 10:
                        return self.send_error(429)
                    calls += 1
                replied = False
                receipt = {'method':method,'path':self.path,'provider':'api.deepseek.com','model':cfg['model']}
                try:
                    headers = {'Authorization':'Bearer '+cfg['provider_key'], 'Content-Type':'application/json'}
                    with client.stream(method, 'https://api.deepseek.com'+self.path, content=body, headers=headers) as response:
                        content_type = response.headers.get('Content-Type','application/json')
                        receipt.update(status=response.status_code, stream=content_type.startswith('text/event-stream'), parts=0)
                        self.send_response(response.status_code)
                        self.send_header('Content-Type', content_type)
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        replied = True
                        for chunk in response.iter_bytes():
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            receipt['parts'] += 1
                        receipt['complete'] = True
                except (httpx.HTTPError, OSError) as error:
                    receipt.update(complete=False, transport_error=type(error).__name__)
                    if not replied:
                        self.send_error(502)
                finally:
                    print(json.dumps(receipt), flush=True)

        with ThreadingUnixStreamServer(RELAY_SOCKET, Gateway) as server:
            Path(RELAY_SOCKET).chmod(0o600)
            print(json.dumps({'relay':'ready'}), flush=True)
            server.serve_forever()


def check_relay_access(target):
    with httpx.Client(trust_env=False, timeout=2) as client:
        assert client.get(target + '/health').status_code == 204
        assert client.get(target + '/v1/models').status_code == 401
        assert client.post(target + '/v1/files').status_code == 404
    # This socket only tests the absence of a route to the documentation address.
    # The address cannot receive application traffic from this network.
    try:
        with socket.create_connection(('192.0.2.1', 443), timeout=1):
            raise AssertionError('Sandbox unexpectedly has an external route')
    except OSError as error:
        return type(error).__name__


class LoopbackRelay(BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as relay:
            relay.connect(RELAY_SOCKET)
            streams = {relay:self.request, self.request:relay}
            while ready := select(list(streams), [], [], 100)[0]:
                for connection in ready:
                    data = connection.recv(65536)
                    if not data:
                        return
                    streams[connection].sendall(data)


def actual_cli(cfg, harness):
    limits = inspect_isolation()
    with ThreadingTCPServer(('127.0.0.1', 0), LoopbackRelay) as bridge:
        thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        thread.start()
        target = f'http://127.0.0.1:{bridge.server_address[1]}'
        try:
            external_route = check_relay_access(target)
            return {'limits':limits, 'external_route_probe':external_route, **run_cli(cfg, target, harness)}
        finally:
            bridge.shutdown()
            thread.join()


def run_cli(cfg, target, harness):
    persistent = harness == 'codex' and cfg.get('persistent') is True
    resume_id = str(UUID(cfg['resume_id'])) if persistent and 'resume_id' in cfg else None
    home = Path('/state' if persistent else '/tmp/cli-state')
    if persistent:
        assert home.stat().st_uid == os.getuid() and home.stat().st_mode & 0o777 == 0o700
    else:
        home.mkdir(mode=0o700)
    case = Path('/tmp/case')
    case.mkdir(mode=0o700)
    proof = None
    if resume_id is None:
        proof = cfg['proof']
        assert re.fullmatch(r'VIBE_CASE_[0-9a-f]{32}', proof)
        (case/'proof.txt').write_text(proof+'\n', encoding='utf8')
    env = {key:value for key,value in os.environ.items() if key in {'PATH','LANG','LC_ALL'}}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home/'config'), XDG_DATA_HOME=str(home/'data'),
               XDG_CACHE_HOME=str(home/'cache'), VIBEFLOW_RELAY_CAPABILITY=cfg['capability'], TERM='dumb')
    prompt = 'Read proof.txt in this case with the available reading tool. Reply with the exact contents. Do not modify files.'
    endpoint = target+'/v1'
    if harness == 'codex':
        env['CODEX_HOME'] = str(home)
        configuration = '''cli_auth_credentials_store="file"
model_provider="project_relay"
sandbox_mode="danger-full-access"
approval_policy="never"
[model_providers.project_relay]
name="Authorized DeepSeek relay"
base_url='''+json.dumps(endpoint)+'''
env_key="VIBEFLOW_RELAY_CAPABILITY"
wire_api="responses"
requires_openai_auth=false
supports_websockets=false
request_max_retries=0
stream_max_retries=0
'''
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf8', dir=home, delete=False) as temp:
            temp.write(configuration)
        Path(temp.name).replace(home/'config.toml')
        command = ['codex','exec']
        if resume_id is not None:
            command.append('resume')
        command.extend(('--json', '--skip-git-repo-check', '--model', cfg['model']))
        if not persistent:
            command.append('--ephemeral')
        if resume_id is not None:
            command.extend((resume_id, 'Repeat the exact VIBE_CASE marker from your previous reply. Use the conversation history without any tool calls.'))
        else:
            command.append(prompt)
    elif harness == 'opencode':
        model = cfg['model']
        config = {'providers':{'relay':{'name':'DeepSeek','env':['VIBEFLOW_RELAY_CAPABILITY'],
                   'package':'@opencode/ai/providers/openai-compatible','settings':{'baseURL':endpoint},
                   'models':{model:{'name':model}}}},
                  'agents':{'acceptance':{'mode':'primary','steps':4,'system':prompt,
                  'permissions':[{'action':'*','resource':'*','effect':'deny'},
                                 {'action':'read','resource':'*','effect':'allow'}]}}}
        (case/'opencode.json').write_text(json.dumps(config), encoding='utf8')
        command = ['opencode','run','--standalone','--format','json','--agent','acceptance','--model','relay/'+model,prompt]
    else:
        env.update(KIMI_CODE_HOME=str(home), KIMI_DISABLE_TELEMETRY='1', KIMI_MODEL_NAME=cfg['model'],
                   KIMI_MODEL_API_KEY=cfg['capability'], KIMI_MODEL_PROVIDER_TYPE='openai',
                   KIMI_MODEL_BASE_URL=endpoint, KIMI_MODEL_MAX_CONTEXT_SIZE='131072', KIMI_SHELL_PATH='/bin/bash')
        (home/'config.toml').write_text('builtin_product_skills=false\ndisable_feedback_survey=true\n[loop_control]\nmax_steps_per_turn=4\n', encoding='utf8')
        agent = home/'reader.md'
        agent.write_text('---\nname: file-acceptance\ndescription: Read one file\ntools:\n  - Read\nsubagents: []\n---\n'+prompt+'\n', encoding='utf8')
        skills = home/'skills'
        skills.mkdir()
        command = ['kimi','--agent-file',str(agent),'--skills-dir',str(skills),'--output-format','stream-json','-p',prompt]
    completed = subprocess.run(command, cwd=case, env=env, capture_output=True, text=True, timeout=180)
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith('{')]
    state = {}
    if harness == 'codex':
        items = [row['item'] for row in events if row.get('type') == 'item.completed']
        replies = [row.get('text','') for row in items if row.get('type') == 'agent_message']
        commands = [row for row in items if row.get('type') == 'command_execution']
        read = proof is not None and any(proof in row.get('aggregated_output','') for row in commands)
        returned = proof is not None and any(proof in text for text in replies)
        if persistent:
            marker = re.search(r'VIBE_CASE_[0-9a-f]{32}', replies[-1]) if replies else None
            native_id = next((row.get('thread_id') for row in events if row.get('type') == 'thread.started'), None)
            state = {'native_thread_id':str(UUID(native_id)) if native_id else None,
                     'reply_marker_sha256':hashlib.sha256(marker[0].encode('ascii')).hexdigest() if marker else None,
                     'resume_without_tools':resume_id is not None and not commands,
                     'prior_case_absent':resume_id is not None and not (case/'proof.txt').exists()}
    elif harness == 'kimi':
        read = any(row.get('role') == 'tool' and proof in str(row.get('content','')) for row in events)
        returned = any(row.get('role') == 'assistant' and proof in str(row.get('content','')) for row in events)
    else:
        read = any(row.get('type') == 'tool_use' and row.get('part',{}).get('tool') == 'read' for row in events)
        returned = any(row.get('type') == 'text' and proof in str(row.get('part',{}).get('text','')) for row in events)
    return {'exit_code':completed.returncode, **state,
            'tool_read':read, 'reply_contains_proof':returned,
            'input_preserved': proof is not None and (case/'proof.txt').read_text(encoding='utf8') == proof+'\n',
            'event_types':[row.get('type') or row.get('role') for row in events]}


if __name__ == '__main__':
    mode = sys.argv[1]
    if mode == 'probe':
        print(json.dumps(inspect_isolation()))
    elif mode == 'relay':
        relay(json.loads(sys.stdin.readline()))
    elif mode in {'codex','kimi','opencode'}:
        print(json.dumps(actual_cli(json.load(sys.stdin), mode)))
    else:
        raise SystemExit('Unknown Docker CLI acceptance mode')
