"""Find the administrator-configured serial Linux Code_Aster installation."""
import json
import os
from pathlib import PurePosixPath
import re

from .moose import host_path
from .runtime_config import get_runtime_settings


DEFAULT = '/opt/vibeflow-ff-c08/code-aster/bin/run_aster'


def runtime_root(executable):
    path = PurePosixPath(executable)
    if path.parts[-2:] != ('bin','run_aster') or len(path.parts)<5 or path.parts[1]!='opt':
        raise ValueError('Code_Aster requires a dedicated /opt installation containing bin/run_aster')
    return path.parents[1].as_posix()


def configured(env=None):
    env = os.environ if env is None else env
    distro = get_runtime_settings(env)['wsl_distro'] if os.name=='nt' else 'native-linux'
    executable = env.get('VIBEFLOW_CODE_ASTER') or DEFAULT
    try:
        runtime_root(executable)
        with host_path(executable,distro).open('rb') as stream:
            if not stream.readline(128).startswith(b'#!'):
                raise ValueError('Code_Aster run_aster shell launcher is unavailable')
    except (OSError,ValueError) as error:
        raise FileNotFoundError('Code_Aster 不可用，请将 VIBEFLOW_CODE_ASTER 指向 /opt 内的 bin/run_aster') from error
    return executable,distro


def code_aster_executable():
    return configured()[0]


def state_package_digest(executable, distro):
    prefix = host_path(runtime_root(executable),distro)
    packages = sorted((prefix/'conda-meta').glob('code-aster-*.json'))
    if len(packages)!=1 or packages[0].stat().st_size>4*1024*1024:
        raise ValueError('Code_Aster state requires an identifiable Conda solver build')
    value = json.loads(packages[0].read_text(encoding='utf8'))
    digest = value.get('sha256')
    if value.get('name')!='code-aster' or not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):
        raise ValueError('Code_Aster state runtime package digest is unavailable')
    return digest
