"""Fixed read-only recovery, executed inside an already registered outer gate.

The outer namespace can observe the original Linux births and read files owned
by the sandbox UID. It never executes recovered files or original user commands.
"""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid
import zipfile
import importlib.util

SCHEMA = 'vibeflow.sandbox-recovery.v1'
MAX_CONFIG = 256 * 1024
MAX_ARCHIVE = 128 * 1024 * 1024 + 8 * 1024 * 1024
CODE = ('sandbox_recovery_native.py', 'sandbox_disk_native.py', 'sandbox_volume.py',
        'sandbox_export_native.py', 'process_identity_native.py', 'process_stop.py', 'process_gate.py')


def module(name):
    spec = importlib.util.spec_from_file_location('recovery_' + name, Path(__file__).with_name(name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def code():
    return {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in CODE}


def read(path, limit):
    return module('sandbox_export_native').read(path, limit)


def write(root, name, value):
    module('sandbox_export_native').write(root, name, value)


def validate(value):
    fields = {'schema', 'root', 'image_root', 'disk', 'session_id', 'nonce', 'gate', 'processes', 'code'}
    if not isinstance(value, dict) or set(value) != fields or value['schema'] != SCHEMA or value['code'] != code():
        raise ValueError('Recovery settings or implementation changed')
    if str(uuid.UUID(value['session_id'])) != value['session_id']:
        raise ValueError('Invalid recovery session')
    if not isinstance(value['nonce'], str) or len(value['nonce']) != 64 or any(c not in '0123456789abcdef' for c in value['nonce']):
        raise ValueError('Invalid recovery nonce')
    root = Path(value['root'])
    disk = module('sandbox_disk_native')
    image_root = disk.root_path(value['image_root'])
    if (root.parent.name != '.terminal-recovery' or root.parent.parent != image_root.parent.parent
            or len(root.name) != 32 or uuid.UUID(root.name).hex != root.name or root.resolve() != root):
        raise ValueError('Recovery output is outside private node storage')
    for path in (root, *root.parents):
        disk.ordinary(path, directory=True)
    if json.loads(read(image_root / 'disk.json', 16384)) != value['disk']:
        raise ValueError('Original image receipt changed')
    identity = module('process_identity_native')
    identity.validate(value['gate'])
    if (not isinstance(value['processes'], list) or not 1 <= len(value['processes']) <= 65
            or value['gate'] not in value['processes']):
        raise ValueError('Missing original native process evidence')
    for birth in value['processes']:
        if identity.validate(birth)['platform'] != 'linux':
            raise ValueError('Recovery requires original Linux births')
    return root, image_root


def digest(path, limit, deadline):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise ValueError('Recovery file is not bounded and ordinary')
        result = hashlib.sha256()
        size = 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError('Recovery hashing exceeded its deadline')
            block = os.read(fd, min(1024 * 1024, limit - size + 1))
            if not block:
                break
            size += len(block)
            if size > limit:
                raise ValueError('Recovery file grew beyond its limit')
            result.update(block)
        def stamp(value):
            return value.st_dev,value.st_ino,value.st_mode,value.st_nlink,value.st_size,value.st_mtime_ns,value.st_ctime_ns
        if size != info.st_size or stamp(os.fstat(fd)) != stamp(info):
            raise ValueError('Recovery file changed while reading')
        return result.hexdigest(), size
    finally:
        os.close(fd)


def archive(root, manifest, deadline):
    directories = set(manifest['directories'])
    with zipfile.ZipFile(root / 'recovery.zip', 'x', compression=zipfile.ZIP_STORED, allowZip64=False) as output:
        for name in [*manifest['directories'], *manifest['files']]:
            if time.monotonic() >= deadline:
                raise TimeoutError('Recovery archive exceeded its deadline')
            directory = name in directories
            entry = zipfile.ZipInfo(name + '/' if directory else name)
            entry.create_system = 3
            entry.external_attr = ((stat.S_IFDIR if directory else stat.S_IFREG) | manifest['modes'][name]) << 16
            if directory:
                entry.external_attr |= 0x10
                output.writestr(entry, b'')
            else:
                with (root / 'files' / name).open('rb') as source, output.open(entry, 'w') as destination:
                    hashed = hashlib.sha256()
                    size = 0
                    while True:
                        if time.monotonic() >= deadline:
                            raise TimeoutError('Recovery archive exceeded its deadline')
                        block = source.read(1024 * 1024)
                        if not block:
                            break
                        size += len(block)
                        if size > manifest['files'][name]['size']:
                            raise ValueError('Recovery snapshot changed')
                        destination.write(block)
                        hashed.update(block)
                    if {'size': size, 'sha256': hashed.hexdigest()} != manifest['files'][name]:
                        raise ValueError('Recovery snapshot does not match manifest')
            if output.fp.tell() > MAX_ARCHIVE:
                raise ValueError('Recovery archive exceeds its bound')
    with (root / 'recovery.zip').open('rb') as completed:
        os.fsync(completed.fileno())
    return digest(root / 'recovery.zip', MAX_ARCHIVE, deadline)


def prepare(arguments):
    """Called by process_gate only after durable registration and release."""
    if (len(arguments) != 9 or arguments[:4] != ['python3', '-I', '-S', '-u']
            or Path(arguments[4]) != Path(__file__).resolve()
            or arguments[5] != '--config' or arguments[7] != '--sha256'):
        raise ValueError('Recovery requires the fixed native invocation')
    raw = read(arguments[6], MAX_CONFIG)
    if hashlib.sha256(raw).hexdigest() != arguments[8]:
        raise ValueError('Recovery configuration changed before release')
    value = json.loads(raw)
    root, image_root = validate(value)
    identity = module('process_identity_native')
    for birth in value['processes']:
        if identity.probe(birth)['state'] != 'exited':
            raise ValueError('Original native scope has not verifiably exited')
    disk = module('sandbox_disk_native')
    volume_module = module('sandbox_volume')
    image = disk.Image.reopen_readonly(image_root, value['disk'])
    try:
        mount = image.attach(readonly=True)
        descriptor = os.open(mount / 'case', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(descriptor)
            fs = os.fstatvfs(descriptor)
            binding = {'gate': value['gate'], 'device': info.st_dev, 'inode': info.st_ino,
                       'capacity': fs.f_frsize * fs.f_blocks, 'filesystem': 'ext4'}
            volume = volume_module.Volume.inherited(value['gate'], descriptor, binding)
        finally:
            os.close(descriptor)
        return volume, image, [*arguments, '--volume-fd', str(volume.fd), '--binding',
                              base64.b64encode(json.dumps(binding).encode()).decode('ascii')]
    except BaseException:
        image.close()
        raise


def run(value, descriptor, binding):
    root, image_root = validate(value)
    volume_module = module('sandbox_volume')
    deadline = time.monotonic() + 150
    before, _ = digest(image_root / 'volume.ext4', volume_module.MAX_BYTES, deadline)
    volume = volume_module.Volume.inherited(value['gate'], descriptor, binding)
    os.close(descriptor)
    with volume:
        # The registered outer gate checked the original Linux births before
        # opening this read-only image. Child PID namespace absence is not proof.
        manifest, encoded = volume.snapshot(root / 'files', timeout=120, scope_exit_verified=True, include_modes=True)
    after, _ = digest(image_root / 'volume.ext4', volume_module.MAX_BYTES, deadline)
    if before != after:
        raise ValueError('Read-only recovery altered its source image')
    write(root, 'manifest.json', encoded)
    archive_hash, size = archive(root, manifest, deadline)
    write(root, 'complete.json', json.dumps({'schema': SCHEMA, 'session_id': value['session_id'], 'nonce': value['nonce'],
        'manifest_sha256': hashlib.sha256(encoded).hexdigest(), 'archive_sha256': archive_hash,
        'archive_bytes': size, 'image_sha256': before}).encode())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--volume-fd', type=int, required=True)
    parser.add_argument('--binding', required=True)
    args = parser.parse_args()
    raw = read(args.config, MAX_CONFIG)
    if hashlib.sha256(raw).hexdigest() != args.sha256:
        raise ValueError('Recovery configuration changed')
    value = json.loads(raw)
    if len(args.binding) > MAX_CONFIG:
        raise ValueError('Recovery binding exceeds its bound')
    run(value, args.volume_fd, json.loads(base64.b64decode(args.binding, validate=True)))


if __name__ == '__main__':
    main()
