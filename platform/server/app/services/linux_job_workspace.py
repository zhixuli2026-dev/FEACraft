"""Launch registered Linux workers with an isolated attempt workspace."""
from pathlib import Path
import shutil
import sys


PYTHON_WORKSPACES = {
    'geometry.preview': 'geometry',
    'geometry.export': 'geometry',
    'postprocess.case': 'postprocess',
    'postprocess.run': 'postprocess',
    'pdf.number': 'office',
    'ppt.build': 'office',
    'ppt.video': 'ppt-media',
    'report.compose': 'compose',
    'report.compose-generated': 'compose',
    'flotherm.convert': 'flotherm-convert',
}
LAUNCHER = Path(__file__).with_name('agent_shell_linux.py')


def isolated_command(workspace, mode, argv, *, options=()):
    return ['/usr/bin/unshare', '--user', '--map-root-user', '--mount', '--net', '--ipc',
            '--pid', '--fork', '--kill-child=SIGKILL',
            sys.executable, '-I', '-S', '-u', str(LAUNCHER), '--workspace', str(workspace),
            mode, *options, '--', *argv]


def python_argv(workspace, mode, argv):
    interpreter = shutil.which(argv[0])
    if interpreter is None:
        raise ValueError('Configured worker Python is unavailable at launch')
    return isolated_command(workspace, mode, [str(Path(interpreter).absolute()), *argv[1:]])


def document_argv(workspace, kind, argv, *, folders=(), executable=None):
    options = [item for folder in folders for item in ('--document-folder', str(folder))]
    if executable is not None:
        options.extend(('--document-executable', executable))
    return isolated_command(workspace, '--document='+kind, argv, options=options)


def child_environment(env, *, extra=frozenset()):
    return {name: value for name, value in env.items()
            if name in extra or name in {'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ', 'VIBEFLOW_CASE_ROOT'}
            or name.startswith(('OMP_', 'MKL_', 'OPENBLAS_'))}
