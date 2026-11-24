"""Locate the installed serial Windows Elmer solver."""
import os
from pathlib import Path


def default_executable(_env=None):
    if os.name != 'nt':
        return None
    parents = Path(__file__).resolve().parents
    if len(parents) <= 4:
        return None
    fallback = (parents[4] / 'third_party' / 'elmer-csc-nompi-20260805'
                / 'ElmerFEM-nogui-nompi-Windows-AMD64' / 'bin' / 'ElmerSolver.exe')
    return fallback if fallback.is_file() else None


def elmer_executable():
    explicit = os.environ.get('VIBEFLOW_ELMER')
    path = Path(explicit).expanduser() if explicit else default_executable()
    if os.name != 'nt' or path is None or not path.is_file():
        raise FileNotFoundError('ElmerSolver 不可用，请配置 Windows 版 VIBEFLOW_ELMER')
    return str(path.resolve())


def child_environment(executable, env):
    bin_path = Path(executable).parent
    home = bin_path.parent
    return {**env, 'ELMER_HOME':str(home), 'ELMER_LIB':str(home/'share/elmersolver/lib'),
            'PATH':str(bin_path)+os.pathsep+env.get('PATH',''), 'OMP_NUM_THREADS':'1', 'OPENBLAS_NUM_THREADS':'1'}
