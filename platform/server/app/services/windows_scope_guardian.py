"""Fixed Windows Job guardian, independent of the business API's lifetime.

Receives a duplicate of an already-created empty Job over its private pipe.
It never launches a payload or accepts a PID to terminate. It seals and drains
that exact Job before writing a bounded, birth-bound durable receipt.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import stat
import sys
import threading
import time
import types

SCHEMA = 'vibeflow.windows-scope-guardian.v1'
FILES = ('windows_scope_guardian.py', 'windows_process_scope.py',
         'windows_process_lifetime.py', 'process_identity_native.py')
LIMIT = 16384


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def modules(request):
    package = 'vibeflow_fixed_guardian'
    module = types.ModuleType(package)
    module.__path__ = [str(Path(__file__).parent)]
    sys.modules[package] = module
    sources = {}
    for name in FILES:
        path = Path(__file__).with_name(name)
        source = path.read_bytes()
        if hashlib.sha256(source).hexdigest() != request['helpers'][name]:
            raise ValueError('Guardian implementation changed')
        sources[path.stem] = source
    # Execute exactly the checked bytes, including fixed relative imports.
    loaded = {}
    for name in ('process_identity_native', 'windows_process_lifetime', 'windows_process_scope'):
        value = types.ModuleType(package + '.' + name)
        value.__package__ = package
        value.__file__ = str(Path(__file__).with_name(name + '.py'))
        sys.modules[value.__name__] = value
        exec(compile(sources[name], value.__file__, 'exec'), value.__dict__)
        loaded[name] = value
    return loaded['windows_process_scope'], loaded['windows_process_lifetime'], loaded['process_identity_native']


def directory(value):
    if not isinstance(value, dict) or set(value) != {'path', 'device', 'inode'}:
        raise ValueError('Invalid guardian evidence directory')
    path = Path(value['path'])
    if not path.is_absolute() or str(path.resolve()) != str(path):
        raise ValueError('Guardian evidence requires an absolute resolved directory')
    for parent in (path, *path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Guardian evidence cannot traverse a reparse point')
    info = path.stat()
    if type(value['device']) is not int or type(value['inode']) is not int or (
            info.st_dev, info.st_ino) != (value['device'], value['inode']):
        raise ValueError('Guardian evidence directory identity changed')
    return path


def validate(request, *, check_code=True, check_directory=True):
    if (not isinstance(request, dict) or set(request) != {'schema', 'nonce', 'owner', 'root', 'max_seconds', 'helpers'}
            or request['schema'] != SCHEMA or not isinstance(request['nonce'], str)
            or re.fullmatch('[0-9a-f]{64}', request['nonce']) is None
            or type(request['max_seconds']) is not int or not 1 <= request['max_seconds'] <= 86430
            or len(json.dumps(request).encode()) > LIMIT):
        raise ValueError('Invalid fixed guardian request')
    helpers = request['helpers']
    if not isinstance(helpers, dict) or set(helpers) != set(FILES):
        raise ValueError('Invalid guardian helper selection')
    for name, expected in helpers.items():
        if (not isinstance(expected, str) or re.fullmatch('[0-9a-f]{64}', expected) is None
                or check_code and hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() != expected):
            raise ValueError('Guardian implementation selection changed')
    # Importing this file through the application preserves the canonical identity validator.
    if __package__:
        from .process_identity_native import validate as identity_validate
    else:
        _, _, identities = modules(request)
        identity_validate = identities.validate
    if identity_validate(request['owner'])['platform'] != 'windows':
        raise ValueError('Guardian owner is not a Windows birth')
    if check_directory:
        directory(request['root'])
    return request


def write(request, name, value):
    if name not in {'prepared.json', 'receipt.json'}:
        raise ValueError('Invalid guardian evidence filename')
    raw = json.dumps(value, separators=(',', ':')).encode()
    if len(raw) > LIMIT:
        raise ValueError('Guardian evidence exceeds its bound')
    path = directory(request['root']) / name
    with path.open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    directory(request['root'])


def read(request, name):
    if name not in {'prepared.json', 'receipt.json'}:
        raise ValueError('Invalid guardian evidence filename')
    path = directory(request['root']) / name
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise ValueError('Guardian evidence is not an ordinary file')
    with path.open('rb') as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError('Guardian evidence exceeds its bound')
    return json.loads(raw)


def validate_prepared(request, value):
    if (not isinstance(value, dict) or set(value) != {'schema', 'request_sha256', 'guardian', 'job', 'empty_before_launch'}
            or value['schema'] != SCHEMA or value['request_sha256'] != digest(request)
            or value['job'] != 'Local\\VibeFlow-' + request['nonce'] or value['empty_before_launch'] is not True):
        raise ValueError('Guardian preparation differs from its selected Job')
    if __package__:
        from .process_identity_native import validate as identity_validate
    else:
        _, _, identities = modules(request)
        identity_validate = identities.validate
    birth = identity_validate(value['guardian'])
    if birth['platform'] != 'windows' or birth['host_id'] != request['owner']['host_id'] or birth == request['owner']:
        raise ValueError('Invalid independent guardian birth')
    return value


def validate_receipt(request, prepared, value, *, check_directory=True):
    validate(request, check_code=False, check_directory=check_directory)
    validate_prepared(request, prepared)
    if (not isinstance(value, dict) or set(value) != {'schema', 'request_sha256', 'preparation_sha256',
            'guardian', 'job', 'reason', 'forced', 'sealed', 'active_processes', 'membership', 'verified', 'error'}
            or value['schema'] != SCHEMA or value['request_sha256'] != digest(request)
            or value['preparation_sha256'] != digest(prepared) or value['guardian'] != prepared['guardian']
            or value['job'] != prepared['job'] or value['reason'] not in {'owner_exited', 'owner_pipe_closed', 'retire', 'deadline'}
            or value['verified'] is not True or value['error'] is not None or value['sealed'] is not True
            or type(value['forced']) is not bool or type(value['active_processes']) is not int or value['active_processes'] != 0):
        raise ValueError('Guardian has not proven scope closure')
    membership = value['membership']
    if (not isinstance(membership, dict) or set(membership) != {'schema', 'total', 'observed', 'exit_verified',
            'gone_before_open', 'pending', 'complete', 'error'} or membership['schema'] != 'vibeflow.windows-members.v1'
            or membership['complete'] is not True or membership['error'] is not None):
        raise ValueError('Guardian membership proof is incomplete')
    for key in ('total', 'observed', 'exit_verified', 'gone_before_open', 'pending'):
        if type(membership[key]) is not int or not 0 <= membership[key] <= 65536:
            raise ValueError('Invalid guardian membership accounting')
    if (membership['pending'] != 0 or membership['observed'] != membership['total']
            or membership['observed'] != membership['exit_verified'] + membership['gone_before_open']):
        raise ValueError('Guardian lifetime observations do not reconcile')
    return value


def run(request):
    validate(request)
    scopes, lifetimes, identity = modules(request)
    owner = scopes.ProcessHandle.query(request['owner'])
    messages = queue.Queue(maxsize=2)
    def input_loop():
        pending = bytearray()
        try:
            while True:
                # A blocking BufferedReader in a daemon thread can abort Python
                # during shutdown. Raw reads never hold that interpreter lock.
                raw = os.read(sys.stdin.fileno(), min(4096, LIMIT + 1 - len(pending)))
                if not raw:
                    messages.put_nowait(None)
                    return
                pending.extend(raw)
                while b'\n' in pending:
                    frame, _, rest = pending.partition(b'\n')
                    if len(frame) + 1 > LIMIT:
                        raise ValueError('Invalid guardian control frame')
                    messages.put_nowait(json.loads(frame))
                    pending = bytearray(rest)
                if len(pending) >= LIMIT:
                    raise ValueError('Invalid guardian control frame')
        except (OSError, ValueError, queue.Full):
            try: messages.put_nowait(None)
            except queue.Full: pass
    threading.Thread(target=input_loop, daemon=True).start()
    scope = None
    try:
        frame = messages.get(timeout=10)
        if (not isinstance(frame, dict) or set(frame) != {'action', 'handle', 'request_sha256'}
                or frame['action'] != 'attach' or frame['request_sha256'] != digest(request)
                or type(frame['handle']) is not int or not 0 < frame['handle'] < 2**64):
            raise ValueError('Guardian requires an actual transferred Job handle')
        scope = scopes.WindowsScope.__new__(scopes.WindowsScope)
        scope.kernel = scopes._kernel()
        scope.handle = frame['handle']
        scope.name = 'Local\\VibeFlow-' + request['nonce']
        scope.observer = None
        inside = wintypes.BOOL()
        scopes._require(scope.kernel.IsProcessInJob(scope.kernel.GetCurrentProcess(), scope.handle, ctypes.byref(inside)))
        if inside.value:
            raise ValueError('Guardian must remain outside its business Job')
        limits = scopes.ExtendedLimits()
        scopes._require(scope.kernel.QueryInformationJobObject(scope.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits), None))
        if not limits.basic.flags & 0x2000 or limits.basic.flags & (0x800 | 0x1000):
            raise ValueError('Guardian Job must kill on close and forbid breakaway')
        if scope.accounting().total_processes != 0 or scope.active_processes() != 0:
            raise ValueError('Guardian must attach before the first business process')
        scope.observer = lifetimes.LifetimeObserver(scope)
        prepared = {'schema': SCHEMA, 'request_sha256': digest(request), 'guardian': identity.observe(os.getpid()),
            'job': scope.name, 'empty_before_launch': True}
        validate_prepared(request, prepared)
        write(request, 'prepared.json', prepared)
        print(json.dumps(prepared), flush=True)
        deadline = time.monotonic() + request['max_seconds']
        while True:
            try:
                owner.wait_exit(0)
            except TimeoutError:
                pass
            else:
                reason = 'owner_exited'
                break
            if time.monotonic() >= deadline:
                reason = 'deadline'
                break
            try:
                frame = messages.get(timeout=.05)
            except queue.Empty:
                continue
            if frame is None:
                reason = 'owner_pipe_closed'
                break
            if frame != {'action': 'retire', 'nonce': request['nonce']}:
                raise ValueError('Invalid guardian retirement control')
            reason = 'retire'
            break
        receipt = {'schema': SCHEMA, 'request_sha256': digest(request), 'preparation_sha256': digest(prepared),
            'guardian': prepared['guardian'], 'job': scope.name, 'reason': reason, 'forced': False,
            'sealed': False, 'active_processes': None, 'membership': None, 'verified': False, 'error': None}
        try:
            receipt['forced'] = scope.retire(5)
            receipt['sealed'] = True
            receipt['active_processes'] = scope.active_processes()
            receipt['membership'] = scope.observer.summary()
            receipt['verified'] = True
            validate_receipt(request, prepared, receipt)
        except BaseException as error:
            receipt['verified'] = False
            receipt['error'] = type(error).__name__
            receipt['membership'] = scope.observer.summary()
        write(request, 'receipt.json', receipt)
        return 0 if receipt['verified'] else 1
    finally:
        try:
            if scope is not None:
                scope.close()
        finally:
            owner.close()


if __name__ == '__main__':
    if os.name != 'nt' or len(sys.argv) != 3 or sys.argv[1] != '--request':
        raise SystemExit('Fixed Windows guardian request is required')
    path = Path(sys.argv[2])
    if path.name != 'request.json' or not path.is_absolute():
        raise SystemExit('Fixed guardian request filename is required')
    with path.open('rb') as stream:
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise SystemExit('Guardian request exceeds bound')
    request = json.loads(raw)
    if directory(request['root']) != path.parent:
        raise SystemExit('Guardian request directory differs')
    try:
        raise SystemExit(run(request))
    except Exception as error:
        print('GUARDIAN_FAILED ' + type(error).__name__, file=sys.stderr)
        raise SystemExit(1)
