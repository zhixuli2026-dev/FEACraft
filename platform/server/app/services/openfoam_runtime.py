"""Select the existing owned host executor on Linux, or the WSL executor on Windows."""
import os
from pathlib import Path
import shutil
import sys

from .linux_job_workspace import isolated_command

NATIVE_LINUX='native-linux'
SERIAL_HANDLERS = frozenset({'mesh.generate', 'solve.openfoam', 'solve.openfoam.resume'})


def distribution(settings=None):
    if sys.platform.startswith('linux'):return NATIVE_LINUX
    if settings is not None:return settings['wsl_distro']
    from .runtime_config import get_runtime_setting
    return get_runtime_setting('wsl_distro','vibeflow-linux')


def configured():
    from .runtime_config import get_runtime_setting
    from .workflow_state import ExecutionConflict
    distro=distribution()
    bashrc=get_runtime_setting('openfoam_bashrc','/usr/lib/openfoam/openfoam2406/etc/bashrc')
    if distro==NATIVE_LINUX:
        if not Path('/bin/bash').is_file() or not Path(bashrc).is_file():
            raise ExecutionConflict('runtime_unavailable','Configured Linux OpenFOAM bashrc or bash is unavailable')
    elif not shutil.which('wsl.exe'):
        raise ExecutionConflict('runtime_unavailable','WSL runtime is unavailable')
    return distro,bashrc


def transport(command,distro):
    if distro==NATIVE_LINUX:
        return {'runtime':'windows','argv':['/bin/bash','--noprofile','--norc','-c',command]}
    return {'runtime':'wsl','argv':None}


def valid_transport(spec,distro):
    expected=transport(spec['command'],distro)
    return (distro!=NATIVE_LINUX or sys.platform.startswith('linux')) and all(spec.get(k)==v for k,v in expected.items())


def verify_worker(distro):
    if distro==NATIVE_LINUX:
        if not sys.platform.startswith('linux'):raise ValueError('Prepared native Linux runtime is unavailable')
    elif os.environ.get('WSL_DISTRO_NAME')!=distro:
        raise ValueError('WSL runtime changed after preparation')


def isolated_argv(workspace, argv, *, source_libraries=None):
    """Wrap the validated native worker without rewriting its sealed command."""
    if len(argv) != 5 or argv[:4] != ['/bin/bash', '--noprofile', '--norc', '-c']:
        raise ValueError('Invalid native OpenFOAM worker invocation')
    options = []
    if source_libraries:
        options.extend(('--openfoam-libraries', source_libraries))
    return isolated_command(workspace, '--openfoam', argv, options=options)
