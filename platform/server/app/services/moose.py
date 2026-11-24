"""Find the administrator-configured native MOOSE executable."""
import os
from pathlib import Path, PurePosixPath
import re
import shutil

from .runtime_config import get_runtime_settings


DEFAULT = '/opt/vibeflow-ff-c08/moose/bin/combined-opt'


def host_path(executable, distro):
    path = PurePosixPath(executable)
    if (not path.is_absolute() or '..' in path.parts or path.as_posix() != executable
            or not re.fullmatch(r'/[A-Za-z0-9_./-]+', executable)):
        raise ValueError('MOOSE executable must be an absolute Linux path')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', distro):
        raise ValueError('MOOSE requires a configured WSL distribution')
    if distro == 'native-linux':
        return Path(executable)
    return Path('\\\\wsl.localhost\\'+distro).joinpath(*path.parts[1:])


def configured(env=None):
    env = os.environ if env is None else env
    distro = get_runtime_settings(env)['wsl_distro'] if os.name == 'nt' else 'native-linux'
    executable = env.get('VIBEFLOW_MOOSE')
    if not executable:
        executable = (shutil.which('moose-opt', path=env.get('PATH', os.defpath))
                      if distro == 'native-linux' else None) or DEFAULT
    try:
        with host_path(executable, distro).open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                raise ValueError('MOOSE executable is not a native Linux binary')
    except (OSError, ValueError) as error:
        raise FileNotFoundError('MOOSE 不可用，请配置 VIBEFLOW_MOOSE 指向实际 Linux ELF 文件') from error
    return executable, distro


def moose_executable():
    return configured()[0]
