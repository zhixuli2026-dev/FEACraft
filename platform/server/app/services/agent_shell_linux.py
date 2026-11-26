"""Fixed Linux workspace launcher for approved commands, PTYs and native solvers."""

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess


RUNTIME_DIRECTORIES = ('/usr', '/opt', '/bin', '/sbin', '/lib', '/lib32', '/lib64', '/libx32',
                       '/etc/alternatives', '/etc/fonts', '/etc/texmf', '/var/lib/texmf', '/var/cache/fontconfig')
RUNTIME_FILES = ('/etc/ld.so.cache', '/etc/localtime', '/etc/nsswitch.conf', '/etc/passwd', '/etc/group',
                 '/etc/os-release', '/etc/papersize', '/etc/locale.alias')
ELMER_RUNTIME = Path('/run/elmer')
ELMER_MODULES = Path('share/elmersolver/lib')
SHELL_HOME = Path('/home/user')
RESERVED_PATHS = tuple(Path(path) for path in (*RUNTIME_DIRECTORIES, '/dev', '/etc', '/proc', '/sys', '/.old-root',
                                              ELMER_RUNTIME, SHELL_HOME))
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
    source = source or target
    destination = root / target.relative_to('/')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        destination.mkdir(exist_ok=True)
    else:
        destination.touch(exist_ok=False)
    mount(source, destination, flags=BIND)
    mount(None, destination, flags=REMOUNT | BIND | flags)


def prepare_root(case, *, terminal=False, executable=None, elmer=False, home=None):
    if case is not None and (
        not case.is_absolute() or case != Path.cwd() or case != case.resolve(strict=True)
        or len(case.parts) < 3
        or any(case.is_relative_to(path) or path.is_relative_to(case) for path in RESERVED_PATHS)
    ):
        raise ValueError('Linux workspace overlaps the runtime filesystem or changed before launch')
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
    entry = None
    if executable is not None:
        path, digest = executable
        # Verify the selected executable again inside the launch gate. If it's
        # installed outside the runtime, expose only that opened file.
        with path.open('rb') as binary:
            info = os.fstat(binary.fileno())
            if not stat.S_ISREG(info.st_mode) or hashlib.file_digest(binary, 'sha256').hexdigest() != digest:
                raise ValueError('Linux solver executable differs from its sealed digest')
            home = path.parent.parent
            library = home / ELMER_MODULES if elmer else None
            entry = path
            pinned_binary = Path(f'/proc/self/fd/{binary.fileno()}')
            installed = root / path.relative_to('/')
            if library is not None and library.is_dir():
                resolved = library.resolve(strict=True)
                if home == Path('/') or resolved == home or not resolved.is_relative_to(home):
                    raise ValueError('Linux solver modules are outside the selected package')
                if any(item.is_relative_to(case) or case.is_relative_to(item) for item in (path, resolved)):
                    raise ValueError('Linux solver bundle overlaps its writable workspace')
                # Preserve the package's relative loader layout, including
                # roots provided by dedicated deployment mounts.
                entry = ELMER_RUNTIME / 'bin' / path.name
                modules = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    bind(root, ELMER_RUNTIME / ELMER_MODULES, source=Path(f'/proc/self/fd/{modules}'))
                    bind(root, entry, source=pinned_binary)
                finally:
                    os.close(modules)
            elif not installed.exists():
                if (path.is_relative_to(case) or any(path.is_relative_to(Path(name)) for name in ('/dev', '/proc', '/sys', '/.old-root'))):
                    raise ValueError('Linux solver executable overlaps a private workspace path')
                bind(root, path, source=pinned_binary)
            elif not os.path.samestat(info, installed.stat()):
                raise ValueError('Linux solver executable differs from its runtime mount')
    devices = (*DEVICES, 'tty') if terminal else DEVICES
    for name in devices:
        bind(root, Path('/dev') / name, flags=NOSUID | NOEXEC)
    mount('proc', root / 'proc', filesystem='proc', flags=RO | NOSUID | NODEV | NOEXEC)

    # Keep the authorized directory rather than re-resolving its source when
    # placing the only writable host mount. The bind excludes child mounts.
    if case is not None:
        descriptor = os.open('.', os.O_RDONLY | os.O_DIRECTORY)
        try:
            bind(root, case, source=Path(f'/proc/self/fd/{descriptor}'), flags=NOSUID | NODEV)
        finally:
            os.close(descriptor)

    if home is not None:
        path = Path(home['path'])
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(descriptor)
            if (info.st_dev, info.st_ino, info.st_uid) != (home['device'], home['inode'], os.geteuid()):
                raise ValueError('Linux shell home changed after account selection')
            bind(root, SHELL_HOME, source=Path(f'/proc/self/fd/{descriptor}'), flags=NOSUID | NODEV)
        finally:
            os.close(descriptor)

    os.chdir(root)
    subprocess.run(['/usr/sbin/pivot_root', '.', '.old-root'], check=True)
    os.chdir('/')
    if LIBC.umount2(b'/.old-root', 2) != 0:  # MNT_DETACH
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), '/.old-root')
    Path('/.old-root').rmdir()
    os.chdir(case or (SHELL_HOME if home is not None else '/tmp'))
    return entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--terminal', action='store_true')
    mode.add_argument('--solver-sha256')
    parser.add_argument('--elmer', action='store_true')
    parser.add_argument('--home-selection', type=json.loads)
    parser.add_argument('command', nargs='*')
    args = parser.parse_args()
    if os.getpid() != 1:
        raise ValueError('Linux workspace launch requires its own PID namespace')
    if args.elmer and args.solver_sha256 is None:
        parser.error('Elmer runtime requires a fixed solver executable')
    if args.home_selection is not None and args.solver_sha256 is not None:
        parser.error('Native solver children have no account home')
    executable = None
    if args.terminal:
        if args.command:
            parser.error('An interactive terminal takes no command')
        import fcntl
        import termios

        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        os.environ.update(HOME='/tmp', TMPDIR='/tmp', HISTFILE='/tmp/.bash_history')
        command = ['/bin/bash', '--noprofile', '--norc', '-i']
    elif args.solver_sha256 is not None:
        if args.workspace is None or not args.command:
            parser.error('A solver takes a workspace and a fixed executable')
        path = Path(args.command[0]).resolve(strict=True)
        executable = (path, args.solver_sha256)
        os.environ.update(HOME='/tmp', TMPDIR='/tmp')
        command = [str(path), *args.command[1:]]
    else:
        if args.workspace is None or len(args.command) != 1:
            parser.error('A command takes a workspace and one shell expression')
        command = ['/bin/sh', '-c', args.command[0]]
    entry = prepare_root(args.workspace, terminal=args.terminal, executable=executable, elmer=args.elmer,
                         home=args.home_selection)
    if args.home_selection is not None:
        os.environ.update(HOME=str(SHELL_HOME), TMPDIR='/tmp')
    if entry is not None:
        command[0] = str(entry)
    if args.elmer:
        home = entry.parent.parent
        os.environ.update(ELMER_HOME=str(home), ELMER_LIB=str(home / ELMER_MODULES),
                          PATH=str(entry.parent)+os.pathsep+os.environ.get('PATH', os.defpath))
    launcher = '/usr/bin/setpriv'
    os.execv(launcher, [launcher, '--no-new-privs', '--bounding-set=-all', '--inh-caps=-all',
                        '--ambient-caps=-all', *command])


if __name__ == '__main__':
    main()
