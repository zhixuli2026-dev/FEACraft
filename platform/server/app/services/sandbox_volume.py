"""Pin and export a bounded Linux sandbox tmpfs without following user links.

This stdlib-only primitive does not stop processes or publish project files.
The owner must pin before accepting terminal input, retain this process outside
the terminal's scope, prove that entire scope has stopped, then request export.
"""
from contextlib import contextmanager
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PureWindowsPath
import stat
import sys
import time

MAX_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_DEPTH = 32
MAX_MANIFEST = 4 * 1024 * 1024
TMPFS_MAGIC = 0x01021994
EXT4_MAGIC = 0xEF53
CHUNK = 1024 * 1024


def identity_module():
    spec = importlib.util.spec_from_file_location('sandbox_process_identity', Path(__file__).with_name('process_identity_native.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def component(name):
    if (not isinstance(name, str) or not name or name in {'.', '..'} or len(name.encode('utf8')) > 255
            or any(ord(c) < 32 or c in '<>:"/\\|?*' for c in name) or name.endswith((' ', '.'))
            or PureWindowsPath(name).is_reserved()):
        raise ValueError('Sandbox export needs portable ordinary file names')
    return name


def stamp(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def ordinary(info):
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        raise ValueError('Sandbox export refuses links and special files')
    if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
        raise ValueError('Sandbox export refuses hard links')


@contextmanager
def opened(parent, name, *, directory=False):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    # O_NONBLOCK keeps a raced FIFO replacement from blocking open before stat.
    flags |= os.O_DIRECTORY if directory else os.O_NONBLOCK
    fd = os.open(name, flags, dir_fd=parent)
    try:
        info = os.fstat(fd)
        ordinary(info)
        if directory != stat.S_ISDIR(info.st_mode):
            raise ValueError('Sandbox entry type changed while opening')
        yield fd, info
    finally:
        os.close(fd)


class Volume:
    @classmethod
    def inherited(cls, gate, descriptor, binding):
        """Receive a directory already pinned by the registered outer gate.

        This process cannot observe the ancestor PID namespace. Export requires
        the API's separately validated full-scope exit proof instead.
        """
        if type(descriptor) is not int or descriptor < 3:
            raise ValueError('Invalid inherited sandbox descriptor')
        instance = cls.__new__(cls)
        instance.identity = identity_module()
        instance.gate = dict(instance.identity.validate(gate))
        instance.fd = None
        info = os.fstat(descriptor)
        fs = os.fstatvfs(descriptor)
        filesystem=binding.get('filesystem','tmpfs')
        if filesystem not in {'tmpfs','ext4'}:raise ValueError('Unknown sandbox filesystem')
        expected={'gate': gate, 'device': info.st_dev, 'inode': info.st_ino,'capacity': fs.f_frsize * fs.f_blocks}
        if filesystem=='ext4':expected['filesystem']='ext4'
        buffer = ctypes.create_string_buffer(256)
        libc = ctypes.CDLL(None, use_errno=True)
        if (not stat.S_ISDIR(info.st_mode) or libc.fstatfs(descriptor, buffer) != 0
                or ctypes.c_long.from_buffer(buffer).value != (EXT4_MAGIC if filesystem=='ext4' else TMPFS_MAGIC)
                or not 0 < fs.f_frsize * fs.f_blocks <= MAX_BYTES
                or binding != expected):
            raise ValueError('Inherited sandbox descriptor differs from its pinned volume')
        instance.fd = os.dup(descriptor)
        instance.binding = binding
        instance.inherited_pin = True
        return instance

    def __init__(self, gate, *, filesystem='tmpfs'):
        if sys.platform != 'linux':
            raise ValueError('Sandbox volume pinning requires Linux')
        self.identity = identity_module()
        if filesystem not in {'tmpfs','ext4'}:raise ValueError('Unknown sandbox filesystem')
        self.gate = dict(self.identity.validate(gate))
        self.fd = None
        self.inherited_pin = False
        if self.identity.observe(gate['pid']) != gate:
            raise ValueError('Sandbox gate birth differs')
        # Open the process root first so a path lookup cannot silently switch
        # namespaces partway through. The recorded birth is rechecked below.
        root = os.open('/proc/' + str(gate['pid']) + '/root', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            with opened(root, 'tmp', directory=True) as (temporary, _):
                with opened(temporary, 'case', directory=True) as (source, _):
                    pinned = os.dup(source)
        finally:
            os.close(root)
        try:
            if self.identity.observe(gate['pid']) != gate:
                raise ValueError('Sandbox gate changed during pinning')
            buffer = ctypes.create_string_buffer(256)
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.fstatfs(pinned, buffer) != 0:
                raise OSError(ctypes.get_errno(), 'Cannot identify sandbox filesystem')
            if ctypes.c_long.from_buffer(buffer).value != (EXT4_MAGIC if filesystem=='ext4' else TMPFS_MAGIC):
                raise ValueError('Sandbox source filesystem differs from its registered profile')
            filesystem_stats = os.fstatvfs(pinned)
            capacity = filesystem_stats.f_frsize * filesystem_stats.f_blocks
            if not 0 < capacity <= MAX_BYTES:
                raise ValueError('Sandbox tmpfs capacity exceeds its bound')
            info = os.fstat(pinned)
            self.binding = {'gate': self.gate, 'device': info.st_dev, 'inode': info.st_ino, 'capacity': capacity}
            if filesystem=='ext4':self.binding['filesystem']='ext4'
            self.fd = pinned
        except BaseException:
            os.close(pinned)
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def snapshot(self, destination=None, *, timeout=30, scope_exit_verified=False, include_modes=False):
        """Read actual files once, optionally copying into a fresh private tree.

        Metadata changes, partial copies and invalid entries never produce a
        completion receipt. The caller owns retention/cleanup of failed output.
        Runtime-private .vibeflow content is deliberately excluded.
        """
        if self.fd is None:
            raise ValueError('Sandbox volume is closed')
        if type(timeout) not in {int, float} or not 0 < timeout <= 120:
            raise ValueError('Invalid sandbox scan deadline')
        deadline = time.monotonic() + timeout
        files, directories, names, modes = {}, [], set(), {}
        total = count = 0
        output = None
        if destination is not None:
            if (self.inherited_pin and scope_exit_verified is not True) or (
                    not self.inherited_pin and self.identity.probe(self.gate)['state'] != 'exited'):
                raise ValueError('Sandbox gate still exists; owner must first drain its full scope')
            destination = Path(destination)
            if not destination.is_absolute() or destination.resolve() != destination:
                raise ValueError('Sandbox export destination is not an absolute ordinary path')
            destination.mkdir(exist_ok=False)
            output = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)

        def check():
            if time.monotonic() >= deadline:
                raise TimeoutError('Sandbox export deadline exceeded')

        def walk(source, target, parent='', depth=0):
            nonlocal total, count
            if depth > MAX_DEPTH:
                raise ValueError('Sandbox export directory nesting exceeds its bound')
            before = os.fstat(source)
            os.lseek(source, 0, os.SEEK_SET)
            with os.scandir(source) as entries:
                for entry in entries:
                    check()
                    if not parent and entry.name.rstrip(' .').casefold() == '.vibeflow':
                        continue
                    count += 1
                    if count > MAX_ENTRIES:
                        raise ValueError('Sandbox export entry count exceeds its bound')
                    name = component(entry.name)
                    relative = parent + name
                    folded = relative.casefold()
                    if folded in names:
                        raise ValueError('Sandbox export paths collide on the destination')
                    names.add(folded)
                    observed = entry.stat(follow_symlinks=False)
                    ordinary(observed)
                    directory = stat.S_ISDIR(observed.st_mode)
                    with opened(source, name, directory=directory) as (fd, current):
                        if stamp(observed) != stamp(current) or current.st_dev != self.binding['device']:
                            raise ValueError('Sandbox entry changed or crosses a mount')
                        if include_modes:
                            modes[relative] = stat.S_IMODE(current.st_mode)
                        if directory:
                            directories.append(relative)
                            child = None
                            try:
                                if target is not None:
                                    os.mkdir(name, dir_fd=target)
                                    child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=target)
                                walk(fd, child, relative + '/', depth + 1)
                            finally:
                                if child is not None:os.close(child)
                        else:
                            if total + current.st_size > MAX_BYTES:
                                raise ValueError('Sandbox export bytes exceed the admitted bound')
                            digest, size, sink = hashlib.sha256(), 0, None
                            try:
                                if target is not None:
                                    sink = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=target)
                                while True:
                                    check()
                                    data = os.read(fd, min(CHUNK, current.st_size - size + 1))
                                    if not data:break
                                    size += len(data)
                                    if size > current.st_size:
                                        raise ValueError('Sandbox file grew during export')
                                    if sink is not None:
                                        view = memoryview(data)
                                        while view:
                                            check()
                                            written = os.write(sink, view)
                                            if written <= 0:raise OSError('Sandbox output write made no progress')
                                            view = view[written:]
                                    digest.update(data)
                                if size != current.st_size or stamp(os.fstat(fd)) != stamp(current):
                                    raise ValueError('Sandbox file changed during export')
                                if sink is not None:os.fsync(sink)
                            finally:
                                if sink is not None:os.close(sink)
                            total += size
                            files[relative] = {'size': size, 'sha256': digest.hexdigest()}
            if stamp(os.fstat(source)) != stamp(before):
                raise ValueError('Sandbox directory changed during export')
            if target is not None:os.fsync(target)

        try:
            walk(self.fd, output)
            document = {'schema': 'vibeflow.sandbox-volume.v1', 'binding': self.binding,
                        'files': files, 'directories': sorted(directories), 'bytes': total, 'entries': count}
            if include_modes:
                document['modes'] = modes
            encoded = json.dumps(document, sort_keys=True, separators=(',', ':')).encode('utf8')
            if len(encoded) > MAX_MANIFEST:
                raise ValueError('Sandbox manifest exceeds its bound')
            return document, encoded
        finally:
            if output is not None:os.close(output)
