"""Discard the private image of a sandbox whose shell was never released."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid


spec = importlib.util.spec_from_file_location('unused_sandbox_disk', Path(__file__).with_name('sandbox_disk_native.py'))
disk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(disk)


def check_contents(root):
    with os.scandir(root) as entries:
        for entry in entries:
            if entry.name not in {'disk', 'disk.json', 'volume.ext4'}:
                raise ValueError('Unused sandbox directory contains other evidence')
    mount = root / 'disk'
    if os.path.lexists(mount):
        disk.ordinary(mount, directory=True)
        with os.scandir(mount) as entries:
            if os.path.ismount(mount) or next(entries, None) is not None:
                raise ValueError('Unused sandbox directory is not an empty mount point')


def forget_image(root):
    target = root / 'volume.ext4'
    manifest = root / 'disk.json'
    expected = None
    if os.path.lexists(manifest):
        info = disk.ordinary(manifest)
        if info.st_size > 16384:
            raise ValueError('Invalid unused sandbox image manifest')
        with manifest.open('rb') as stream:
            value = json.loads(stream.read(16385))
        if (not isinstance(value, dict) or set(value) != {'schema', 'filesystem', 'image', 'uuid'}
                or value['schema'] != disk.SCHEMA or value['filesystem'] != 'ext4'
                or not isinstance(value['uuid'], str) or str(uuid.UUID(value['uuid'])) != value['uuid']
                or not isinstance(value['image'], dict) or set(value['image']) != {'device','inode','bytes'}
                or any(type(number) is not int or number < 0 for number in value['image'].values())):
            raise ValueError('Invalid unused sandbox image manifest')
        expected = value['image']
    if not os.path.lexists(target):
        return  # The native gate may have stopped before creating an image.
    disk.ordinary(target)
    descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        info = os.fstat(descriptor)
        if not 0 <= info.st_size <= disk.IMAGE_BYTES:
            raise ValueError('Unused sandbox image exceeds its original allocation')
        if expected is not None:
            disk.inspect_image(target, descriptor, expected)
        if disk.command(['losetup', '-j', str(target)]).strip():
            raise ValueError('Unused sandbox image still has a loop attachment')
        check_contents(root)
        current = disk.ordinary(target)
        if (current.st_dev, current.st_ino, current.st_size) != (info.st_dev, info.st_ino, info.st_size):
            raise ValueError('Unused sandbox image changed during cleanup')
        mount = root / 'disk'
        if os.path.lexists(mount):
            mount.rmdir()
        target.unlink()
    finally:
        os.close(descriptor)


def discard(value):
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise ValueError('Unused sandbox cleanup requires the trusted WSL owner')
    root = disk.root_path(value)
    check_contents(root)
    forget_image(root)
    mount = root / 'disk'
    if os.path.lexists(mount):
        mount.rmdir()
    (root / 'disk.json').unlink(missing_ok=True)
    root.rmdir()


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(2)
    discard(sys.argv[1])
