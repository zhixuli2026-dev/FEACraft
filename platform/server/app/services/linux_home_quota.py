"""Provision and verify ext4 project quotas for native Linux account homes."""

import argparse
from contextlib import ExitStack
import ctypes
import fcntl
import json
import os
from pathlib import Path
import stat
import struct


DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
ATTR = struct.Struct('=IIIII8x')
GET_XATTR, SET_XATTR = 0x801C581F, 0x401C5820
INHERIT_PROJECT = 0x200
SCHEMA = 'vibeflow.linux-home-hard-quota.v1'
MIB = 1024**2


def _project(descriptor):
    raw = fcntl.ioctl(descriptor, GET_XATTR, bytes(ATTR.size))
    return ATTR.unpack(raw)


def _trusted(info):
    return info.st_uid == 0 and not info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)


def _receipt(user_id, filesystem, home, limit):
    return {'schema': SCHEMA, 'project': user_id, 'filesystem': filesystem, 'inode': home.st_ino,
            'hard_bytes': limit}


def _read_receipt(volume, user_id):
    marker = os.open(f'.home-quota-{user_id}.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=volume)
    try:
        info = os.fstat(marker)
        if not _trusted(info) or not stat.S_ISREG(info.st_mode) or info.st_size > 512 or info.st_nlink != 1:
            raise ValueError('Linux HOME project quota receipt is invalid')
        return json.loads(os.read(marker, 513))
    finally:
        os.close(marker)


def measure(volume, home, user_id, limit):
    """Verify the host's hard-only provisioning and the live kernel projection."""
    root = os.fstat(volume)
    selected = os.fstat(home)
    flags, _, _, project, _ = _project(home)
    if not _trusted(root) or root.st_dev != selected.st_dev or project != user_id or not flags & INHERIT_PROJECT:
        raise ValueError('Linux HOME is outside its provisioned project')
    storage = os.fstatvfs(volume)
    if _read_receipt(volume, user_id) != _receipt(user_id, storage.f_fsid, selected, limit):
        raise ValueError('Linux HOME project quota receipt differs from its selected account')
    budget = os.fstatvfs(home)
    if budget.f_blocks * budget.f_frsize != limit or storage.f_blocks * storage.f_frsize <= limit:
        raise ValueError('Linux HOME project quota is missing or differs from policy')
    return (budget.f_blocks-budget.f_bfree) * budget.f_frsize, storage.f_bavail * storage.f_frsize


class _Quota(ctypes.Structure):
    # struct if_dqblk from linux/quota.h. Block limits use 1 KiB units.
    _fields_ = [(name, ctypes.c_uint64) for name in ('bhard', 'bsoft', 'space', 'ihard', 'isoft',
                                                   'inodes', 'btime', 'itime')]
    _fields_ += [('valid', ctypes.c_uint32)]


def _quotactl(fd, operation, project, value):
    # Linux quotactl_fd, on the supported 64-bit AMD and ARM architectures.
    if os.uname().machine not in {'x86_64', 'aarch64'}:
        raise ValueError('Linux HOME quota provisioning requires x86_64 or AArch64')
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(ctypes.c_long(443), ctypes.c_int(fd), ctypes.c_uint((operation << 8) | 2),
                          ctypes.c_uint(project), ctypes.byref(value))
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def provision(root, user_ids, uid, gid, limit):
    """Set and read back the kernel hard limit from a trusted Linux host."""
    if (os.geteuid() != 0 or root != root.resolve() or not root.is_mount() or root == Path('/')
            or limit < MIB or limit % MIB or min(uid, gid) < 0):
        raise ValueError('Use root on a dedicated mounted Linux HOME filesystem with a whole MiB limit')
    for user_id in user_ids:
        if not 0 < user_id < 2**32-1:
            raise ValueError('Linux HOME account ID is outside the kernel project range')

    with ExitStack() as stack:
        volume = os.open(root, DIRECTORY)
        stack.callback(os.close, volume)
        fcntl.flock(volume, fcntl.LOCK_EX)
        storage = os.fstatvfs(volume)
        if not _trusted(os.fstat(volume)) or storage.f_blocks * storage.f_frsize <= limit:
            raise ValueError('Linux HOME quota filesystem must be root-owned and larger than the account limit')
        # Verify project quota support before creating an account directory.
        _quotactl(volume, 0x800007, 0, _Quota())  # Q_GETQUOTA
        created = False
        try:
            os.mkdir('users', 0o755, dir_fd=volume)
            created = True
        except FileExistsError:
            pass
        users = os.open('users', DIRECTORY, dir_fd=volume)
        stack.callback(os.close, users)
        if created:
            os.fchmod(users, 0o755)
        if not _trusted(os.fstat(users)) or os.fstat(users).st_dev != os.fstat(volume).st_dev:
            raise ValueError('Linux HOME account directory must be on the quota filesystem and root-owned')
        for user_id in user_ids:
            _provision_home(volume, users, user_id, uid, gid, limit)


def _provision_home(volume, users, user_id, uid, gid, limit):
    name = str(user_id)
    try:
        os.mkdir(name, 0o700, dir_fd=users)
    except FileExistsError:
        pass
    home = os.open(name, DIRECTORY, dir_fd=users)
    try:
        selected = os.fstat(home)
        if selected.st_dev != os.fstat(volume).st_dev:
            raise ValueError('Linux HOME quota requires a directory on the selected filesystem')
        filesystem = os.fstatvfs(volume).f_fsid
        marker = f'.home-quota-{user_id}.json'
        flags, extent, entries, project, cow = _project(home)
        with os.scandir(home) as items:
            populated = next(items, None) is not None
        if populated:
            old = _read_receipt(volume, user_id)
            if (project != user_id or not flags & INHERIT_PROJECT or not isinstance(old, dict)
                    or type(old.get('hard_bytes')) is not int
                    or old != _receipt(user_id, filesystem, selected, old['hard_bytes'])):
                raise ValueError('Populate an existing HOME by copying into a provisioned empty directory')
        if project != user_id or not flags & INHERIT_PROJECT:
            if project not in {0, user_id}:
                raise ValueError('Linux HOME directory already belongs to another project')
            attributes = ATTR.pack(flags | INHERIT_PROJECT, extent, entries, user_id, cow)
            fcntl.ioctl(home, SET_XATTR, attributes)
        current = _Quota()
        _quotactl(volume, 0x800007, user_id, current)
        if current.space > limit:
            raise ValueError('Existing Linux HOME already exceeds the new hard limit')
        _quotactl(volume, 0x800008, user_id, _Quota(bhard=limit//1024, valid=1))  # Q_SETQUOTA / QIF_BLIMITS
        actual = _Quota()
        _quotactl(volume, 0x800007, user_id, actual)
        if actual.bhard != limit//1024 or actual.bsoft != 0:
            raise ValueError('The kernel did not retain the hard-only Linux HOME quota')
        if (selected.st_uid, selected.st_gid) != (uid, gid):
            os.fchown(home, uid, gid)
        os.fchmod(home, 0o700)
        # Rename atomically so a reader never observes a partial receipt.
        temporary = f'{marker}.{os.getpid()}'
        record = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=volume)
        try:
            with os.fdopen(record, 'w', encoding='utf-8') as stream:
                os.fchmod(stream.fileno(), 0o644)
                json.dump(_receipt(user_id, filesystem, selected, limit), stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, marker, src_dir_fd=volume, dst_dir_fd=volume)
            os.fsync(volume)
        finally:
            try:
                os.unlink(temporary, dir_fd=volume)
            except FileNotFoundError:
                pass
        measure(volume, home, user_id, limit)
    finally:
        os.close(home)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--uid', type=int, required=True)
    parser.add_argument('--gid', type=int, required=True)
    parser.add_argument('--limit-mib', type=int, required=True)
    parser.add_argument('accounts', type=int, nargs='+')
    args = parser.parse_args()
    provision(args.root, args.accounts, args.uid, args.gid, args.limit_mib*MIB)
