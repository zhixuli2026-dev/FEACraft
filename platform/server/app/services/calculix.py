"""CalculiX 可执行路径与输入约束。"""
from pathlib import Path
import os
import shutil
import sys

FALLBACK = Path("D:/vibeflow_reverse/third_party/calculix-2.23-bin/calculix_2.23_4win/ccx_static.exe")


def default_executable(env=None):
    if sys.platform.startswith('linux'):
        env = os.environ if env is None else env
        found = shutil.which('ccx', path=env.get('PATH', os.defpath))
        return Path(found).resolve() if found else None
    return FALLBACK if FALLBACK.is_file() else None


def calculix_executable() -> str:
    explicit = os.environ.get("VIBEFLOW_CALCULIX")
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"VIBEFLOW_CALCULIX 不存在: {path}")
        return str(path)
    fallback = default_executable()
    if fallback is None:
        raise FileNotFoundError("CalculiX 未安装，请设置 VIBEFLOW_CALCULIX")
    return str(fallback)
