"""SU2 可执行路径与输入约束。"""
from pathlib import Path
import os
import shutil
import sys

FALLBACK = Path("D:/vibeflow_reverse/third_party/su2-8.5.0-omp-runtime2/bin/SU2_CFD.exe")


def default_executable(env=None):
    if sys.platform.startswith('linux'):
        env = os.environ if env is None else env
        found = shutil.which('SU2_CFD', path=env.get('PATH', os.defpath))
        return Path(found).resolve() if found else None
    return FALLBACK if FALLBACK.is_file() else None


def su2_executable() -> str:
    explicit = os.environ.get("VIBEFLOW_SU2")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"VIBEFLOW_SU2 不存在: {path}")
        return str(path)
    fallback = default_executable()
    if fallback is None:
        raise FileNotFoundError("SU2 未安装，请设置 VIBEFLOW_SU2")
    return str(fallback)
