"""Registered Linux volume holder, outside the interactive terminal scope."""
import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import time

SCHEMA = 'vibeflow.sandbox-export.v1'
MAX_CONFIG = 16384


def read(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError('Invalid sandbox control file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('Sandbox control file exceeds its bound')
        return data
    finally:
        os.close(fd)


def write(root, name, data):
    temporary = root / (name + '.pending')
    with temporary.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    # The private directory is owned by this request. Never replace a receipt.
    if (root / name).exists():
        raise ValueError('Sandbox receipt already exists')
    os.rename(temporary, root / name)


def load_module(name):
    path = Path(__file__).with_name(name + '.py')
    spec = importlib.util.spec_from_file_location('sandbox_export_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_volume():
    return load_module('sandbox_volume')


def validate(value):
    fields={'schema', 'root', 'gate', 'terminal_process', 'nonce', 'lifetime', 'code'}
    if (not isinstance(value, dict) or set(value) not in (fields,fields|{'filesystem'})
            or value.get('filesystem','tmpfs') not in {'tmpfs','ext4'}
            or value['schema'] != SCHEMA or type(value['lifetime']) is not int
            or not 10 <= value['lifetime'] <= 14400
            or not isinstance(value['nonce'], str) or not re.fullmatch('[0-9a-f]{64}', value['nonce'])):
        raise ValueError('Invalid sandbox export settings')
    root = Path(value['root'])
    if not root.is_absolute() or root.resolve() != root or not root.is_dir():
        raise ValueError('Sandbox export root is not an ordinary absolute directory')
    names = {'sandbox_export_native.py', 'sandbox_volume.py', 'process_identity_native.py', 'process_stop.py'}
    if not isinstance(value['code'], dict) or set(value['code']) != names:
        raise ValueError('Sandbox export implementation is not bound')
    for name, digest in value['code'].items():
        if hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() != digest:
            raise ValueError('Sandbox export implementation changed')
    load_volume().identity_module().validate(value['gate'])
    return root


def pin(value):
    validate(value)
    volume_module = load_volume()
    # The original case copy precedes pivot_root. Large Windows-backed cases
    # may still be copying after 15 seconds; keep the wait below the existing
    # 60-second owner startup boundary, without renewing it per observation.
    deadline = time.monotonic() + 45
    while True:
        try:
            return volume_module.Volume(value['gate'],filesystem=value.get('filesystem','tmpfs'))
        except (FileNotFoundError, ValueError):
            if (volume_module.identity_module().probe(value['gate'])['state'] != 'match'
                    or time.monotonic() >= deadline):
                raise
            time.sleep(.05)


def prepare_descriptor(arguments):
    """Called only by process_gate before entering its private PID namespace."""
    if (len(arguments) != 9 or arguments[:4] != ['python3', '-I', '-S', '-u']
            or Path(arguments[4]) != Path(__file__).resolve()
            or arguments[5] != '--config' or arguments[7] != '--sha256'):
        raise ValueError('Sandbox pinning requires the fixed exporter command')
    raw = read(arguments[6], MAX_CONFIG)
    if hashlib.sha256(raw).hexdigest() != arguments[8]:
        raise ValueError('Sandbox pin configuration changed')
    volume = pin(json.loads(raw))
    return volume, [*arguments, '--volume-fd', str(volume.fd), '--binding',
        base64.b64encode(json.dumps(volume.binding).encode()).decode('ascii')]


def run(value, descriptor, binding):
    root = validate(value)
    volume_module = load_volume()
    volume = volume_module.Volume.inherited(value['gate'], descriptor, binding)
    os.close(descriptor)
    with volume:
        initial, encoded = volume.snapshot(include_modes=True)
        write(root, 'initial.json', encoded)
        write(root, 'ready.json', json.dumps({'schema': SCHEMA, 'nonce': value['nonce'],
            'binding': initial['binding'], 'initial_sha256': hashlib.sha256(encoded).hexdigest()}).encode())
        deadline = time.monotonic() + value['lifetime']
        while time.monotonic() < deadline:
            try:
                command = json.loads(read(root / 'command.json', MAX_CONFIG))
            except FileNotFoundError:
                time.sleep(.05)
                continue
            if (not isinstance(command, dict) or set(command) != {'schema', 'nonce', 'action', 'stop'}
                    or command['schema'] != SCHEMA or command['nonce'] != value['nonce']
                    or command['action'] not in {'abort', 'export'}):
                raise ValueError('Sandbox export command differs from its owner')
            if command['action'] == 'abort':
                return
            # This namespace cannot observe the ancestor namespace's PIDs.
            # Validate the exact API-owned terminal exit receipt, never infer
            # exit from a missing PID in this exporter's namespace.
            stop = load_module('process_stop').validate(command['stop'], value['terminal_process'])
            if stop['verified'] is not True:
                raise ValueError('Sandbox export has no verified terminal scope exit')
            _, encoded = volume.snapshot(root / 'files', timeout=120, scope_exit_verified=True, include_modes=True)
            write(root, 'manifest.json', encoded)
            write(root, 'complete.json', json.dumps({'schema': SCHEMA, 'nonce': value['nonce'],
                'manifest_sha256': hashlib.sha256(encoded).hexdigest()}).encode())
            return
        raise TimeoutError('Sandbox volume holder lifetime expired without an export')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--volume-fd', type=int, required=True)
    parser.add_argument('--binding', required=True)
    args = parser.parse_args()
    raw = read(args.config, MAX_CONFIG)
    if hashlib.sha256(raw).hexdigest() != args.sha256:
        raise ValueError('Sandbox export settings changed before launch')
    if len(args.binding) > MAX_CONFIG:
        raise ValueError('Sandbox descriptor binding exceeds its limit')
    run(json.loads(raw), args.volume_fd, json.loads(base64.b64decode(args.binding, validate=True)))


if __name__ == '__main__':
    main()
