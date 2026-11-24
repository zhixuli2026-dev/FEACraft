"""Cache the Server Manager descriptions belonging to the configured pvpython."""

from functools import lru_cache
import json
from pathlib import Path
import subprocess
import tempfile
from threading import Lock

from .paraview import paraview_executable


PROBE = Path(__file__).with_name('paraview_native')/'catalogue.py'
PLUGIN_SUPPORT = PROBE.with_name('plugin_support.py')
_catalog_lock = Lock()


def _read(executable, args):
    with tempfile.TemporaryDirectory(prefix='vibeflow-paraview-catalog-') as stage:
        target = Path(stage)/'catalog.json'
        command = [str(executable), str(PROBE), str(target), *args]
        process = subprocess.run(command, timeout=60,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, cwd=stage)
        if process.returncode or not target.is_file():
            raise ValueError(f'ParaView 目录读取失败，退出码 {process.returncode}')
        with target.open('rb') as stream:
            payload = stream.read(16*1024*1024+1)
        if len(payload)>16*1024*1024:
            raise ValueError('ParaView 目录超过大小上限')
        return json.loads(payload)


@lru_cache(maxsize=64)
def _load(executable, executable_stamp, probe_stamp, plugin):
    catalogue = _read(executable,[plugin] if plugin else [])
    if plugin:
        if catalogue.get('schema') != 'vibeflow.paraview.plugin.v1' or catalogue['plugin']['name'] != plugin:
            raise ValueError('ParaView 插件信息格式无效')
    elif (catalogue.get('schema')!='vibeflow.paraview.catalog.v1'
            or set(catalogue['groups'])!={'sources','filters'}):
        raise ValueError('ParaView 目录格式无效')
    return catalogue


def matching(root, relative):
    from .attempt_workspace import checked_path

    source = checked_path(root, relative)
    if not source.is_file():
        raise FileNotFoundError(relative)
    return _read(paraview_executable('pvpython'), ['--input',str(source)])


def catalogue(plugin=None, executable=None):
    executable = Path(executable or paraview_executable('pvpython')).resolve(strict=True)
    state = executable.stat()
    with _catalog_lock:
        stamps = (PROBE.stat().st_mtime_ns,PLUGIN_SUPPORT.stat().st_mtime_ns)
        return _load(str(executable),(state.st_mtime_ns,state.st_size),stamps,plugin)


def search(catalogue, group, query):
    needle = query.strip().casefold()
    rows = []
    for name, entries in catalogue['groups'].items():
        if group and group!=name:
            continue
        for entry in entries.values():
            if needle and not any(needle in entry[key].casefold() for key in ('name','label')):
                continue
            rows.append({key:entry[key] for key in ('group','name','label')})
    return sorted(rows,key=lambda row:(row['group'],row['label'].casefold(),row['name']))
