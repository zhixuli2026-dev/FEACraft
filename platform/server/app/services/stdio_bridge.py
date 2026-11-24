"""Fixed stdlib worker: frame stdio without sharing RPC and diagnostic streams.

This worker only runs inside OwnedProcess. Its control input is separate from
the process gate's stop channel. Blocking native stdin cannot block scope stop.
"""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

CHUNK = 8192
MAX_FRAME = 32768


def packed(value):
    return (json.dumps(value, ensure_ascii=True, separators=(',', ':'), sort_keys=True) + '\n').encode('ascii')


def main():
    if sys.argv[2:] not in ([], ['--wsl-environment']):
        raise ValueError('Invalid stdio environment mode')
    if sys.argv[2:]:
        if not sys.platform.startswith('linux'):
            raise ValueError('Native WSL environment requires Linux')
        # WSLENV transports only explicitly selected values. Do not pass distro
        # startup environment, Windows PATH, proxy/auth configuration or WSL
        # interop sockets into the CLI or its tool subprocesses.
        selected = {name: os.environ.get('VIBEFLOW_CLI_' + name, '') for name in ('CODEX_HOME', 'HOME', 'TMPDIR')}
        if any(not selected.get(name, '').startswith('/') for name in ('CODEX_HOME', 'HOME', 'TMPDIR')):
            raise ValueError('Native WSL environment requires private directories')
        os.environ.clear()
        os.environ.update(selected, PATH='/usr/bin:/bin', LANG='C.UTF-8')
    settings = json.loads(base64.b64decode(sys.argv[1], validate=True))
    if (set(settings) not in ({'argv', 'cwd', 'nonce'}, {'argv', 'cwd', 'nonce', 'native_state'},
            {'argv','cwd','nonce','native_state','native_management'}) or not isinstance(settings['argv'], list)
            or not settings['argv'] or any(not isinstance(x, str) or not x or '\0' in x for x in settings['argv'])
            or not isinstance(settings['nonce'], str) or len(settings['nonce']) != 64):
        raise ValueError('Invalid stdio profile')
    if 'native_management' in settings:
        if sys.argv[2:]!=['--wsl-environment'] or os.getuid()!=65534 or os.getgid()!=65534 or os.getgroups():
            raise ValueError('Managed CLI bridge did not drop to its selected account')
        status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
        if status['NoNewPrivs'].strip()!='1' or any(int(status[key].strip(),16) for key in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb')):
            raise ValueError('Managed CLI bridge retained privilege')
    lock = threading.Lock()

    def emit(kind, **fields):
        data = packed({'type': kind, 'nonce': settings['nonce'], **fields})
        if len(data) > MAX_FRAME:
            raise ValueError('Oversized stdio frame')
        with lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    # Report the fixed bridge's birth before it is allowed to start the CLI.
    spec = importlib.util.spec_from_file_location('stdio_identity', Path(__file__).with_name('process_identity_native.py'))
    identity = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(identity)
    digest = hashlib.sha256(packed(settings)).hexdigest()
    emit('gated', identity=identity.observe(os.getpid()), profile_sha256=digest)

    def authorize(action, *, final):
        line = sys.stdin.buffer.readline(MAX_FRAME + 1)
        expected = {'action': action, 'nonce': settings['nonce']}
        if line == packed(expected):
            return
        if not final or not sys.argv[2:] or len(line) > MAX_FRAME or not line.endswith(b'\n'):
            raise ValueError('Stdio bridge release is invalid')
        try:
            envelope = json.loads(line)
        except ValueError:
            raise ValueError('Stdio bridge release is invalid') from None
        token = envelope.pop('provider_token', None) if isinstance(envelope, dict) else None
        if (envelope != expected or not isinstance(token, str) or not 1 <= len(token) <= 4096
                or any(not 33 <= ord(char) <= 126 for char in token)):
            raise ValueError('Stdio bridge release is invalid')
        os.environ['VIBEFLOW_CODEX_PROVIDER_TOKEN'] = token

    authorize('release', final='native_state' not in settings)
    if 'native_state' in settings:
        if sys.argv[2:] != ['--wsl-environment']:
            raise ValueError('Private native initialization requires the WSL bridge')
        spec = importlib.util.spec_from_file_location('stdio_native_state', Path(__file__).with_name('codex_wsl_state.py'))
        state = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(state)
        prepared = state.initialize(settings['native_state'])
        if prepared['home'] != os.environ['CODEX_HOME']:
            raise ValueError('Private state differs from the bridge environment')
        emit('prepared', **prepared)
        authorize('launch', final=True)
    options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    child = subprocess.Popen(settings['argv'], cwd=settings['cwd'], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    emit('started', pid=child.pid)

    def forward(stream, kind):
        while True:
            chunk = os.read(stream.fileno(), CHUNK)
            if not chunk:
                break
            emit(kind, data=base64.b64encode(chunk).decode('ascii'))

    def input_stream():
        previous = 0
        try:
            while True:
                line = sys.stdin.buffer.readline(MAX_FRAME + 1)
                if not line:
                    child.stdin.close()
                    return
                if len(line) > MAX_FRAME or not line.endswith(b'\n'):
                    raise ValueError('Oversized input frame')
                message = json.loads(line)
                if message == {'action':'end_input'}:
                    child.stdin.close()
                    return
                if (set(message) != {'action', 'id', 'data'} or message['action'] != 'input'
                        or type(message['id']) is not int or message['id'] != previous + 1):
                    raise ValueError('Invalid input sequence')
                chunk = base64.b64decode(message['data'], validate=True)
                if not chunk or len(chunk) > CHUNK:
                    raise ValueError('Oversized native input chunk')
                child.stdin.write(chunk)
                child.stdin.flush()
                previous = message['id']
                emit('ack', id=previous)
        except (BrokenPipeError, OSError, ValueError, TypeError, KeyError):
            emit('input_closed')

    outputs = [threading.Thread(target=forward, args=(stream, kind), daemon=True)
               for stream, kind in ((child.stdout, 'stdout'), (child.stderr, 'stderr'))]
    for thread in outputs:
        thread.start()
    threading.Thread(target=input_stream, daemon=True).start()
    code = child.wait()
    # Readers must flush before exit is published. A descendant holding an
    # inherited pipe is deliberately retired by the enclosing scope instead.
    for thread in outputs:
        thread.join()
    emit('exit', exit_code=code)
    return code


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError, KeyError):
        raise SystemExit(125)
