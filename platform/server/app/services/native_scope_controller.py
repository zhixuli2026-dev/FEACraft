"""Fixed stdlib WSL control: create, observe or revoke one bound cgroup.

Separate from the read-only identity service. No command, PID or filesystem
destination can be supplied. The native controller has an OS alarm and exits
on owner pipe closure; it never joins the target or launches another process.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SCHEMA = 'vibeflow.native-scope-control.v1'
_BASE_FILES = ('native_scope_controller.py', 'process_cgroup_native.py',
               'process_identity_native.py', 'control_identity_reader.py')
_COMPUTE = 'native_compute_v1'
FILES = _BASE_FILES + (_COMPUTE+'.py',)
LIMIT = 16384


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def load(name, request, *, check_hash=True):
    path = Path(__file__).with_name(name + '.py')
    source = path.read_bytes()
    if path.name not in FILES or (check_hash and hashlib.sha256(source).hexdigest() != request['helpers'][path.name]):
        raise ValueError('Native scope controller implementation changed')
    namespace = {'__name__': 'vibeflow_scope_' + name, '__file__': str(path)}
    exec(compile(source, str(path), 'exec'), namespace)
    return namespace


def validate(request, *, check_code=True):
    if (not isinstance(request, dict) or set(request) != {
            'schema', 'nonce', 'distribution', 'operation', 'arguments', 'helpers'}
            or request['schema'] != SCHEMA or not isinstance(request['nonce'], str)
            or re.fullmatch('[0-9a-f]{64}', request['nonce']) is None):
        raise ValueError('Invalid native scope control request')
    distro = request['distribution']
    if not isinstance(distro, str) or not 1 <= len(distro) <= 128 or any(ord(c) < 32 for c in distro):
        raise ValueError('Invalid native scope distribution')
    if request['operation'] not in {'create', 'observe', 'retire', 'allocate', 'abandon'}:
        raise ValueError('Unsupported native scope operation')
    helpers = request['helpers']
    allowed_files = (set(FILES),) if check_code else (set(FILES),set(_BASE_FILES))
    if not isinstance(helpers, dict) or set(helpers) not in allowed_files:
        raise ValueError('Invalid native scope helper selection')
    for name, expected in helpers.items():
        if (not isinstance(expected, str) or re.fullmatch('[0-9a-f]{64}', expected) is None
                or (check_code and hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() != expected)):
            raise ValueError('Native scope helper changed')
    args = request['arguments']
    if not isinstance(args, dict):
        raise ValueError('Invalid native scope arguments')
    if request['operation'] in {'create', 'allocate'}:
        key = 'seed' if request['operation'] == 'allocate' else 'launch_nonce'
        choices = ({key}, {key,'compute_policy'}) if request['operation']=='allocate' else ({key},)
        if set(args) not in choices or not isinstance(args[key], str) or re.fullmatch('[0-9a-f]{64}', args[key]) is None:
            raise ValueError('Invalid native scope launch nonce')
        if 'compute_policy' in args:
            if _COMPUTE+'.py' not in helpers or not any(load(_COMPUTE,request,check_hash=check_code)
                    ['validate_policy'](args['compute_policy']).values()):
                raise ValueError('Invalid native compute policy for allocation')
    elif request['operation'] == 'abandon':
        if set(args) != {'allocation_request', 'preparation'}:
            raise ValueError('Invalid abandoned allocation selection')
        original = args['allocation_request']
        if (not isinstance(original, dict) or original.get('operation') != 'allocate'
                or original.get('distribution') != distro):
            raise ValueError('Abandon requires an allocation in the same distribution')
        validate(original, check_code=False)
        validate_preparation(original, args['preparation'])
    else:
        if set(args) != {'scope'}:
            raise ValueError('Invalid bound scope operation')
        # Historical receipts use the current structural validator; do not
        # execute historical bytes or require the deployment to stay unchanged.
        selected=load('process_cgroup_native', request, check_hash=check_code)['validate'](args['scope'])
        if 'compute' in selected:
            if _COMPUTE+'.py' not in helpers:
                raise ValueError('Native compute helper was not selected')
            load(_COMPUTE,request,check_hash=check_code)['validate'](selected['compute'])
    if len(json.dumps(request).encode()) > LIMIT:
        raise ValueError('Native scope request exceeds its bound')
    return request


def allocation_nonce(request, birth):
    return digest({'domain': 'vibeflow.native-cgroup-allocation.v1',
                   'seed': request['arguments']['seed'], 'creator': birth})


def validate_preparation(request, value):
    if (request['operation'] != 'allocate' or not isinstance(value, dict) or set(value) != {
            'schema', 'stage', 'request_sha256', 'controller', 'membership', 'reservation'}
            or value['schema'] != SCHEMA or value['stage'] != 'prepared'
            or value['request_sha256'] != digest(request)):
        raise ValueError('Invalid prepared allocation binding')
    birth = load('process_identity_native', request, check_hash=False)['validate'](value['controller'])
    scope = load('process_cgroup_native', request, check_hash=False)
    reservation = scope['validate_reservation'](value['reservation'])
    if (birth['platform'] != 'linux' or birth['uid'] != 0 or reservation['boot_id'] != birth['boot_id']
            or reservation['nonce'] != allocation_nonce(request, birth)):
        raise ValueError('Allocation reservation differs from its actual creator')
    membership = value['membership']
    if not isinstance(membership, str) or len(membership.encode()) > 4096:
        raise ValueError('Invalid prepared controller membership')
    paths = [line[3:] for line in membership.splitlines() if line.startswith('0::')]
    target = '/' + scope['PARENT'] + '/a-' + reservation['nonce']
    if len(paths) != 1 or not paths[0].startswith('/') or paths[0] == target or paths[0].startswith(target + '/'):
        raise ValueError('Allocation controller must remain outside its target')
    return value


def permit(request, preparation):
    return {'action': 'perform', 'request_sha256': digest(request), 'preparation_sha256': digest(preparation)}


def emit(value):
    encoded = json.dumps(value, separators=(',', ':'))
    if len(encoded.encode()) > LIMIT:
        raise ValueError('Native scope response exceeds its bound')
    print(encoded, flush=True)


def response(request, *, controller=None, membership=None, result=None, error=None):
    return {'schema': SCHEMA, 'request_sha256': digest(request), 'controller': controller,
            'membership': membership, 'result': result, 'error': error}


def native(request):
    import signal
    import threading
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(40 if request.get('operation') == 'allocate' else 15)
    def disconnected():
        try:
            os.read(sys.stdin.fileno(), 1)
        finally:
            os._exit(125)
    validate(request)
    identity = load('process_identity_native', request)
    birth = identity['observe'](os.getpid())
    membership = Path('/proc/self/cgroup').read_text(encoding='ascii')
    if len(membership.encode()) > 4096:
        raise ValueError('Controller membership exceeds its bound')
    scope = load('process_cgroup_native', request)
    try:
        operation = request['operation']
        if operation == 'allocate':
            reservation = scope['reserve_allocation'](allocation_nonce(request, birth))
            preparation = validate_preparation(request, {'schema': SCHEMA, 'stage': 'prepared',
                'request_sha256': digest(request), 'controller': birth, 'membership': membership, 'reservation': reservation})
            emit(preparation)
            # Do not start the disconnect watcher until it cannot consume the permit.
            raw = sys.stdin.buffer.readline(LIMIT + 1)
            if len(raw) > LIMIT or not raw.endswith(b'\n') or json.loads(raw) != permit(request, preparation):
                raise ValueError('Allocation was not durably permitted')
            validate(request)
        threading.Thread(target=disconnected, daemon=True).start()
        signal.alarm(15)
        if operation == 'allocate':
            result = scope['allocate'](reservation)
            policy=request['arguments'].get('compute_policy')
            if policy is not None:
                result['compute']=load(_COMPUTE,request)['allocate'](result['nonce'],policy)
        elif operation == 'abandon':
            prepared = request['arguments']['preparation']
            exited = identity['probe'](prepared['controller'])
            if exited != {'state': 'exited', 'reason': 'process_exited'}:
                raise ValueError('Allocation creator has not verifiably exited')
            result = scope['discard_unadmitted'](prepared['reservation'])
            policy=request['arguments']['allocation_request']['arguments'].get('compute_policy')
            if policy is not None:
                load(_COMPUTE,request)['discard_unadmitted'](prepared['reservation']['nonce'],policy)
            result['creator_exit'] = exited
        elif operation == 'create':
            result = scope['create'](request['arguments']['launch_nonce'])
        else:
            selected=request['arguments']['scope']
            result = scope[operation](selected)
            if 'compute' in selected:
                result['compute_usage']=load(_COMPUTE,request)[operation](selected['compute'])
        return response(request, controller=birth, membership=membership, result=result)
    except (OSError, ValueError, TimeoutError) as error:
        return response(request, controller=birth, membership=membership, error=type(error).__name__)


async def host_permit():
    """A daemon stdin reader cannot keep asyncio.run alive after a timeout."""
    import threading
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    def deliver(value):
        if not future.done():
            future.set_result(value)
    def read():
        try:
            raw = sys.stdin.buffer.readline(LIMIT + 1)
        except OSError:
            raw = b''
        try:
            loop.call_soon_threadsafe(deliver, raw)
        except RuntimeError:
            pass  # Loop is already closed after its actual deadline.
    threading.Thread(target=read, daemon=True).start()
    return await asyncio.wait_for(future, 35)


async def invoke_allocation(argv, request, bounded):
    process = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW, limit=LIMIT + 1)
    stderr = asyncio.create_task(bounded(process.stderr, 4096))
    try:
        raw = await asyncio.wait_for(process.stdout.readline(), 15)
        if len(raw) > LIMIT or not raw.endswith(b'\n'):
            raise ValueError('Invalid native preparation frame')
        prepared = json.loads(raw)
        # Failures before preparation cannot permit a mutation.
        if prepared.get('stage') != 'prepared':
            await asyncio.wait_for(process.wait(), 20)
            return prepared
        validate_preparation(request, prepared)
        emit(prepared)
        raw = await host_permit()
        if len(raw) > LIMIT or not raw.endswith(b'\n') or json.loads(raw) != permit(request, prepared):
            raise ValueError('Host received no matching allocation permit')
        process.stdin.write(raw)
        await process.stdin.drain()
        stdout, _, code = await asyncio.wait_for(asyncio.gather(
            bounded(process.stdout, LIMIT), stderr, process.wait()), 20)
        if code:
            return response(request, error='native_controller_failed')
        return json.loads(stdout)
    finally:
        process.stdin.close()
        if process.returncode is None:
            process.kill()
        await process.wait()
        if not stderr.done():
            stderr.cancel()
        await asyncio.gather(stderr, return_exceptions=True)


async def host(request):
    validate(request)
    transport = load('control_identity_reader', request)
    code, stdout, _ = await transport['invoke'](['wsl.exe', '--list', '--running', '--quiet'], timeout=10)
    if code:
        return response(request, error='distribution_list_unavailable')
    encoding = 'utf-16-le' if b'\0' in stdout else 'utf-8'
    names = {line.strip().lstrip('\ufeff') for line in stdout.decode(encoding).splitlines()}
    if request['distribution'] not in names:
        return response(request, error='distribution_not_running')
    path = str(Path(__file__).resolve()).replace('\\', '/')
    if len(path) < 3 or path[1] != ':' or not path[0].isalpha():
        raise ValueError('Scope controller requires a Windows source path')
    path = '/mnt/' + path[0].lower() + path[2:]
    encoded = base64.b64encode(json.dumps(request, separators=(',', ':')).encode()).decode('ascii')
    argv = ['wsl.exe', '-d', request['distribution'], '--exec',
        '/usr/bin/python3', '-I', '-S', '-u', path, '--native', encoded]
    if request['operation'] == 'allocate':
        return await invoke_allocation(argv, request, transport['bounded'])
    code, stdout, _ = await transport['invoke'](argv, keep_input=True, timeout=20)
    if code:
        return response(request, error='native_controller_failed')
    return json.loads(stdout)


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in {'--host', '--native'} or len(sys.argv[2]) > LIMIT * 2:
        raise ValueError('Fixed bounded controller mode is required')
    raw = base64.b64decode(sys.argv[2], validate=True)
    if len(raw) > LIMIT:
        raise ValueError('Native scope request exceeds its bound')
    request = json.loads(raw)
    if sys.argv[1] == '--native':
        if not sys.platform.startswith('linux'):
            raise ValueError('Native scope controller requires Linux')
        value = native(request)
    else:
        if os.name != 'nt':
            raise ValueError('Scope host controller requires Windows')
        value = asyncio.run(host(request))
    emit(value)


if __name__ == '__main__':
    main()
