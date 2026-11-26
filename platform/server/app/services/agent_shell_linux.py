"""Fixed Linux launcher for one approved command in a case workspace."""

import argparse
import ctypes
import os
from pathlib import Path
import shutil
import subprocess


RUNTIME_DIRECTORIES = ('/usr', '/opt', '/bin', '/sbin', '/lib', '/lib32', '/lib64', '/libx32',
                       '/etc/alternatives', '/etc/fonts', '/etc/texmf', '/var/lib/texmf', '/var/cache/fontconfig')
RUNTIME_FILES = ('/etc/ld.so.cache', '/etc/localtime', '/etc/nsswitch.conf', '/etc/passwd', '/etc/group',
                 '/etc/os-release', '/etc/papersize', '/etc/locale.alias')
DEVICES = ('null', 'zero', 'random', 'urandom')
RO, NOSUID, NODEV, NOEXEC, REMOUNT, BIND = 1, 2, 4, 8, 32, 4096
LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_char_p]
LIBC.umount2.argtypes = [ctypes.c_char_p, ctypes.c_int]


def mount(source, target, *, filesystem=None, flags=0, options=None):
    encode = lambda item: None if item is None else os.fsencode(item)
    if LIBC.mount(encode(source), os.fsencode(target), encode(filesystem), flags, encode(options)) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), target)


def bind(root, target, *, source=None, flags=RO | NOSUID | NODEV):
    destination = root / target.relative_to('/')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir():
        destination.mkdir(exist_ok=True)
    else:
        destination.touch(exist_ok=False)
    mount(source or target, destination, flags=BIND)
    mount(None, destination, flags=REMOUNT | BIND | flags)


def prepare_root(case):
    if (not case.is_absolute() or case != Path.cwd() or case != case.resolve(strict=True)
            or len(case.parts) < 3 or case.parts[1] in {'bin','dev','etc','lib','lib32','lib64','libx32',
                                                       'opt','proc','sbin','sys','usr','var'}):
        raise ValueError('Linux Agent workspace overlaps the runtime filesystem or changed before launch')
    root = Path('/dev/shm')
    mount('tmpfs', root, filesystem='tmpfs', flags=NOSUID | NODEV, options='mode=0755')
    for name in ('dev','etc','proc','tmp','.old-root'):
        (root / name).mkdir()
    (root / 'tmp').chmod(0o1777)
    for name in RUNTIME_DIRECTORIES:
        source = Path(name)
        if source.is_symlink():
            (root / source.relative_to('/')).symlink_to(os.readlink(source))
        elif source.is_dir():
            bind(root, source)
    for name in RUNTIME_FILES:
        source = Path(name)
        if source.is_file():
            destination = root / source.relative_to('/')
            shutil.copyfile(source, destination)
            destination.chmod(0o444)
    for name in DEVICES:
        bind(root, Path('/dev') / name, flags=NOSUID | NOEXEC)
    mount('proc', root / 'proc', filesystem='proc', flags=RO | NOSUID | NODEV | NOEXEC)

    # Keep the authorized directory rather than re-resolving its source when
    # placing the only writable host mount. The bind excludes child mounts.
    descriptor = os.open('.', os.O_RDONLY | os.O_DIRECTORY)
    try:
        bind(root, case, source=Path(f'/proc/self/fd/{descriptor}'), flags=NOSUID | NODEV)
    finally:
        os.close(descriptor)

    os.chdir(root)
    subprocess.run(['/usr/sbin/pivot_root', '.', '.old-root'], check=True)
    os.chdir('/')
    if LIBC.umount2(b'/.old-root', 2) != 0:  # MNT_DETACH
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), '/.old-root')
    Path('/.old-root').rmdir()
    os.chdir(case)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('command')
    args = parser.parse_args()
    if os.getpid() != 1:
        raise ValueError('Linux Agent filesystem launch requires its own PID namespace')
    prepare_root(args.workspace)
    launcher = '/usr/bin/setpriv'
    os.execv(launcher, [launcher, '--no-new-privs', '--bounding-set=-all', '--inh-caps=-all',
                        '--ambient-caps=-all', '/bin/sh', '-c', args.command])


if __name__ == '__main__':
    main()
