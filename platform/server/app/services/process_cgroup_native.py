"""Linux cgroup v2 scope primitive for fixed, trusted native launch gates.

No PID lookup or arbitrary path is accepted. This module is not yet wired into
request recovery: callers must durably bind the returned scope before launch,
and must separately fence the owner and account for its Windows host scope.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import errno
import os
from pathlib import Path
import re
import stat
import sys
import time
import uuid

SCHEMA = 'vibeflow.native-cgroup.v1'
ALLOCATION_SCHEMA = 'vibeflow.native-cgroup-allocation.v1'
RESERVATION_SCHEMA = 'vibeflow.native-cgroup-reservation.v1'
ROOT = '/sys/fs/cgroup'
PARENT = 'vibeflow-native'
MAX_WAIT = 10


def validate(value):
    fields = {'schema', 'nonce', 'boot_id', 'device', 'parent_inode', 'inode'}
    if not isinstance(value, dict) or set(value) not in (fields,fields|{'compute'}):
        raise ValueError('Invalid native scope descriptor')
    if (value['schema'] not in {SCHEMA, ALLOCATION_SCHEMA} or not isinstance(value['nonce'], str)
            or re.fullmatch('[0-9a-f]{64}', value['nonce']) is None):
        raise ValueError('Invalid native scope binding')
    if not isinstance(value['boot_id'], str) or str(uuid.UUID(value['boot_id'])) != value['boot_id']:
        raise ValueError('Invalid native scope boot identity')
    if any(type(value[key]) is not int or not 0 < value[key] < 2**64
           for key in ('device', 'parent_inode', 'inode')):
        raise ValueError('Invalid native scope inode identity')
    if 'compute' in value and (not isinstance(value['compute'],dict) or value['compute'].get('nonce')!=value['nonce']):
        raise ValueError('Native compute lease belongs to a different scope')
    return value


def name(value):
    validate(value)
    return ('a-' if value['schema'] == ALLOCATION_SCHEMA else '') + value['nonce']


def membership_path(value):
    return '/' + PARENT + '/' + name(value)


def validate_reservation(value):
    if (not isinstance(value, dict) or set(value) != {'schema', 'nonce', 'boot_id', 'device', 'parent_inode', 'absent'}
            or value['schema'] != RESERVATION_SCHEMA or value['absent'] is not True):
        raise ValueError('Invalid empty allocation reservation')
    validate({**{key: value[key] for key in ('nonce', 'boot_id', 'device', 'parent_inode')},
              'schema': ALLOCATION_SCHEMA, 'inode': 1})
    return value


def reserve_allocation(nonce):
    """Observe an absent creator-derived name before permitting mutation."""
    if not isinstance(nonce, str) or re.fullmatch('[0-9a-f]{64}', nonce) is None:
        raise ValueError('Invalid allocation nonce')
    with _parent(create=True) as parent:
        try:
            os.stat('a-' + nonce, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            info = os.fstat(parent)
            return validate_reservation({'schema': RESERVATION_SCHEMA, 'nonce': nonce, 'boot_id': _boot(),
                'device': info.st_dev, 'parent_inode': info.st_ino, 'absent': True})
        raise FileExistsError('Allocation name is already present before preparation')


def _reserved_parent(parent, reservation):
    validate_reservation(reservation)
    info = os.fstat(parent)
    if reservation['boot_id'] != _boot() or (info.st_dev, info.st_ino) != (reservation['device'], reservation['parent_inode']):
        raise ValueError('Allocation namespace changed after reservation')


def _boot():
    return str(uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text(encoding='ascii').strip()))


def _directory(fd):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Native scope directory is not privately controlled by root')
    # Check the actual open filesystem, rather than trusting a mount pathname.
    library = ctypes.CDLL(None, use_errno=True)
    library.fstatfs.argtypes = [ctypes.c_int, ctypes.c_void_p]
    library.fstatfs.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(512)
    if library.fstatfs(fd, buffer) != 0:
        raise OSError(ctypes.get_errno(), 'Cannot inspect native scope filesystem')
    if ctypes.c_long.from_buffer(buffer).value != 0x63677270:
        raise ValueError('Native scope requires an actual cgroup v2 filesystem')
    return info


@contextmanager
def _parent(*, create=False):
    if not sys.platform.startswith('linux') or os.geteuid() != 0:
        raise OSError('Native scope control requires the trusted Linux root gate')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    root = os.open(ROOT, flags)
    parent = None
    try:
        _directory(root)
        if create:
            try:
                os.mkdir(PARENT, mode=0o755, dir_fd=root)
            except FileExistsError:
                pass
        parent = os.open(PARENT, flags, dir_fd=root)
        _directory(parent)
        yield parent
    finally:
        if parent is not None:
            os.close(parent)
        os.close(root)


def _read(fd, name):
    raw = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        data = os.read(raw, 4097)
        if len(data) > 4096:
            raise ValueError('Native scope observation exceeds its bound')
        return data.decode('ascii')
    finally:
        os.close(raw)


def _write(fd, name, value):
    raw = os.open(name, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
    try:
        if os.write(raw, value) != len(value):
            raise OSError('Incomplete native scope control write')
    finally:
        os.close(raw)


def _populated(fd):
    fields = dict(line.split() for line in _read(fd, 'cgroup.events').splitlines())
    if fields.get('populated') not in {'0', '1'}:
        raise ValueError('Invalid native scope population observation')
    return fields['populated'] == '1'


def create(nonce):
    """Create an exclusive scope; do not admit any process until it is persisted."""
    if not isinstance(nonce, str) or re.fullmatch('[0-9a-f]{64}', nonce) is None:
        raise ValueError('Invalid native scope nonce')
    with _parent(create=True) as parent:
        return _create(parent, nonce, SCHEMA)


def allocate(reservation):
    """Caller must persist the actual creator and this reservation first."""
    validate_reservation(reservation)
    with _parent() as parent:
        _reserved_parent(parent, reservation)
        return _create(parent, reservation['nonce'], ALLOCATION_SCHEMA)


def _create(parent, nonce, schema):
    selected = ('a-' if schema == ALLOCATION_SCHEMA else '') + nonce
    os.mkdir(selected, mode=0o755, dir_fd=parent)  # Never reuse an existing scope.
    fd = None
    try:
        fd = os.open(selected, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        info = _directory(fd)
        if _populated(fd):
            raise ValueError('New native scope is unexpectedly populated')
        # Capability check only: opening the file does not issue a kill.
        capability = os.open('cgroup.kill', os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
        os.close(capability)
        return validate({'schema': schema, 'nonce': nonce, 'boot_id': _boot(),
                         'device': info.st_dev, 'parent_inode': os.fstat(parent).st_ino,
                         'inode': info.st_ino})
    except BaseException:
        # Kernel rmdir refuses a populated scope. Do not kill on failed create.
        try:
            os.rmdir(selected, dir_fd=parent)
        except OSError:
            pass
        raise
    finally:
        if fd is not None:
            os.close(fd)


def discard_unadmitted(reservation):
    """Remove only an empty reserved allocation after its bound creator exited.

    The fixed controller verifies the actual creator and creator-derived name.
    Its caller must also fence the request and prove no admission was committed.
    This operation never kills a process or removes an ordinary legacy scope.
    """
    validate_reservation(reservation)
    with _parent() as parent:
        _reserved_parent(parent, reservation)
        selected = 'a-' + reservation['nonce']
        try:
            fd = os.open(selected, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        except FileNotFoundError:
            return {'reservation': reservation, 'resolved_scope': None, 'state': 'absent', 'removed_this_call': False}
        try:
            info = _directory(fd)
            descriptor = validate({'schema': ALLOCATION_SCHEMA, 'nonce': reservation['nonce'], 'boot_id': _boot(),
                'device': info.st_dev, 'parent_inode': os.fstat(parent).st_ino, 'inode': info.st_ino})
            if _populated(fd):
                raise ValueError('Unadmitted allocation unexpectedly has active processes')
            current = os.stat(selected, dir_fd=parent, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError('Reserved allocation changed before removal')
            os.rmdir(selected, dir_fd=parent)  # Kernel rejects a racing join or a nonempty subtree.
            return {'reservation': reservation, 'resolved_scope': descriptor, 'state': 'absent', 'removed_this_call': True}
        finally:
            os.close(fd)


@contextmanager
def _bound(value):
    validate(value)
    if value['boot_id'] != _boot():
        raise ValueError('Native scope belongs to another boot')
    with _parent() as parent:
        info = os.fstat(parent)
        if (info.st_dev, info.st_ino) != (value['device'], value['parent_inode']):
            raise ValueError('Native scope parent identity changed')
        fd = None
        try:
            try:
                fd = os.open(name(value), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                             dir_fd=parent)
            except FileNotFoundError:
                yield parent, None
                return
            info = _directory(fd)
            if (info.st_dev, info.st_ino) != (value['device'], value['inode']):
                raise ValueError('Native scope identity changed')
            yield parent, fd
        finally:
            if fd is not None:
                os.close(fd)


def observe(value):
    """Observe only this scope. Absence says nothing about the Windows helper."""
    with _bound(value) as (_, fd):
        return {'scope': dict(value), 'state': 'absent' if fd is None else
                'populated' if _populated(fd) else 'empty'}


def join(value):
    """Join the calling gate itself, before HELLO, forking or accepting release."""
    with _bound(value) as (_, fd):
        if fd is None:
            raise ValueError('Native scope has been revoked')
        _write(fd, 'cgroup.procs', b'0')
        expected = '0::' + membership_path(value)
        if expected not in Path('/proc/self/cgroup').read_text(encoding='ascii').splitlines():
            raise ValueError('Native gate did not enter its intended scope')


def retire(value, *, timeout=MAX_WAIT):
    """Kill this bound scope, observe emptiness, then revoke future admission.

    Never call this from a scope member. The request owner must be independently
    fenced by the caller. A timeout is unverified, not a successful cleanup.
    """
    if type(timeout) not in (float, int) or not 0 < timeout <= MAX_WAIT:
        raise ValueError('Invalid native scope retirement deadline')
    validate(value)
    membership = membership_path(value)
    for line in Path('/proc/self/cgroup').read_text(encoding='ascii').splitlines():
        if line.startswith('0::') and (line[3:] == membership or line[3:].startswith(membership + '/')):
            raise ValueError('Native scope cannot retire its own controller')
    deadline = time.monotonic() + timeout
    with _bound(value) as (parent, fd):
        if fd is None:
            return {'scope': dict(value), 'state': 'absent', 'removed_this_call': False}
        while time.monotonic() < deadline:
            _write(fd, 'cgroup.kill', b'1')
            if not _populated(fd):
                # rmdir and admission race in the kernel. A late join makes
                # removal fail; repeat kill/observation within the same bound.
                current = os.stat(name(value), dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (value['device'], value['inode']):
                    raise ValueError('Native scope changed before revocation')
                try:
                    os.rmdir(name(value), dir_fd=parent)
                    return {'scope': dict(value), 'state': 'absent', 'removed_this_call': True}
                except OSError as error:
                    if error.errno != errno.EBUSY:
                        raise
            time.sleep(.01)
        raise TimeoutError('Native scope retirement is unverified')
