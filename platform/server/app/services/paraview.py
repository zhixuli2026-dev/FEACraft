"""ParaView pvpython/pvbatch 可执行路径。"""
import os
import shutil
import sys
from pathlib import Path


def default_executable(backend: str, env=None):
    if backend not in {"pvpython", "pvbatch"}:
        raise ValueError("backend 只能是 pvpython/pvbatch")
    if sys.platform.startswith('linux'):
        env = os.environ if env is None else env
        found = shutil.which(backend, path=env.get('PATH', os.defpath))
        return Path(found).resolve() if found else None
    path = Path(f"D:/vibeflow_reverse/extracted/bin/{backend}.exe")
    return path if path.is_file() else None


def paraview_executable(backend: str) -> str:
    if backend not in {"pvpython", "pvbatch"}:
        raise ValueError("backend 只能是 pvpython/pvbatch")
    explicit = os.environ.get(f"VIBEFLOW_{backend.upper()}")
    path = Path(explicit).expanduser() if explicit else default_executable(backend)
    if path is None or not path.is_file():
        raise FileNotFoundError(f"{backend} 不存在，请设置 VIBEFLOW_{backend.upper()}: {path}")
    return str(path)


def catalyst_implementation(executable):
    bundled = Path(executable).resolve().parent.parent/'lib'/'catalyst'
    configured = os.environ.get('CATALYST_IMPLEMENTATION_PATHS','')
    folders = [Path(entry) for entry in configured.split(os.pathsep) if entry] + [bundled]
    name = 'catalyst-paraview.dll' if os.name == 'nt' else 'libcatalyst-paraview.so'
    for folder in folders:
        if (folder/name).is_file():
            return str(folder.resolve())
    raise ValueError('This ParaView installation lacks a Catalyst 2 ParaView implementation')


def catalyst_versions(executable):
    binary = Path(executable).resolve().parent
    versions = []
    if any(binary.glob('vtkPVPythonCatalyst-*.dll')) or any((binary.parent/'lib').glob('libvtkPVPythonCatalyst-*.so')):
        versions.append(1)
    try:
        catalyst_implementation(executable)
    except ValueError:
        return versions
    return [*versions,2]


def visualizer_path(executable):
    configured = os.environ.get('VIBEFLOW_PARAVIEW_VISUALIZER_DIR')
    if configured:
        root = Path(configured).expanduser()
        if not root.is_absolute() or not (root/'index.html').is_file() or not (root/'Visualizer.js').is_file():
            raise FileNotFoundError('Configured ParaView Web Visualizer directory is unavailable')
        return root.resolve(strict=True)
    share = Path(executable).resolve().parent.parent/'share'
    candidates = [item for item in share.glob('paraview-*/web/visualizer/www')
                  if (item/'index.html').is_file() and (item/'Visualizer.js').is_file()]
    if len(candidates) != 1:
        raise FileNotFoundError('Installed ParaView does not provide its Web Visualizer')
    return candidates[0]
