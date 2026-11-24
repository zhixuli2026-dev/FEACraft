"""Flotherm 输出安全覆盖；只在完整预检通过后删除 marker 登记的生成物。"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import stat

from .tree_metadata_native import name as canonical_path, ordinary

MARKER = ".flotherm2foam-generated"
SCHEMA = "vibeflow.flotherm2foam.generated.v1"
VERSION = "0.1.0"
MAX_MARKER_BYTES = 8 * 1024 * 1024


class ConversionCancelled(Exception):
    pass


class CancellationToken:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def checkpoint(self, output: Path, job_id: str):
        if self.cancelled:
            (output / "conversion-cancelled.json").write_text(json.dumps({
                "schema": "vibeflow.flotherm2foam.cancelled.v1", "job_id": job_id,
                "partial_output_preserved": True}, indent=2) + "\n", encoding="utf-8")
            raise ConversionCancelled("转换已取消，部分输出已保留")


def _ordinary_output(path):
    path = path.absolute()
    for component in reversed((path, *path.parents)):
        try:
            info = ordinary(component)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("输出路径及其父目录必须是真实目录")
    return path.resolve()


def validate_conversion_dirs(case_output: Path, ir_output: Path) -> tuple[Path, Path]:
    case = _ordinary_output(case_output)
    ir = _ordinary_output(ir_output)
    if case == ir or case in ir.parents or ir in case.parents:
        raise ValueError("case 与 IR 输出目录必须不同且不能互相嵌套")
    for path in (case, ir):
        if path.exists():
            if not path.is_dir() or path.is_symlink() or any(path.iterdir()):
                raise ValueError(f"输出目录必须为空且不能为符号链接: {path}")
    return case, ir


def _manifest(stream) -> dict:
    try:
        raw = stream.read(MAX_MARKER_BYTES + 1)
        if len(raw) > MAX_MARKER_BYTES:
            raise ValueError("生成标记超过大小上限")
        data = json.loads(raw)
    except (OSError, ValueError) as error:
        raise ValueError("输出目录没有有效的生成标记，拒绝覆盖") from error
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("files"), list):
        raise ValueError("生成标记 schema/files 无效")
    paths = [canonical_path(item).casefold() for item in data['files']]
    if MARKER.casefold() in paths or len(set(paths)) != len(paths):
        raise ValueError("生成标记文件列表无效或含重复路径")
    return data


@contextmanager
def _locked_marker(path):
    if not stat.S_ISREG(ordinary(path).st_mode):
        raise ValueError('生成标记必须是普通文件')
    with path.open('r+b') as stream:
        if os.name == 'nt':
            import msvcrt
            lock = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            lock = lambda: fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            lock()
        except OSError as error:
            raise ValueError('已有转换进程正在重置同一目录，请稍后重试') from error
        if not os.path.samestat(os.fstat(stream.fileno()), ordinary(path)):
            raise ValueError('生成标记在预检期间已变化')
        yield stream


def _inventory(root):
    files, directories = {}, {}
    pending = [root]
    while pending:
        for candidate in pending.pop().iterdir():
            info = ordinary(candidate)
            relative = candidate.relative_to(root).as_posix()
            if relative == MARKER:
                continue
            canonical_path(relative)
            if stat.S_ISDIR(info.st_mode):
                directories[relative] = info
                pending.append(candidate)
            else:
                files[relative] = info
    return files, directories


def _file_identity(info):
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _reset_identity(files, directories):
    return {'files': {name: _file_identity(info) for name, info in files.items()},
            'directories': {name: [info.st_dev, info.st_ino] for name, info in directories.items()}}


def _check_reset(pending, observed, registered, owned_dirs):
    if (not isinstance(pending, dict) or not isinstance(pending.get('files'), dict)
            or not isinstance(pending.get('directories'), dict)
            or pending['files'].keys() != registered or pending['directories'].keys() != owned_dirs):
        raise ValueError('中断清理记录无效，已保留当前文件')
    if any(pending[kind].get(name) != before for kind, items in observed.items() for name, before in items.items()):
        raise ValueError('中断后生成路径已变化，已保留当前文件')


def _rewrite_marker(stream, data):
    encoded = (json.dumps(data, indent=2) + '\n').replace('\n', os.linesep).encode('utf-8')
    if len(encoded) > MAX_MARKER_BYTES:
        raise ValueError('生成标记超过大小上限')
    stream.seek(0)
    stream.write(encoded)
    stream.truncate()
    stream.flush()
    os.fsync(stream.fileno())


def _write_initial(root):
    with (root / MARKER).open('xb') as stream:
        _rewrite_marker(stream, _new_manifest([]))


def prepare_output(root: Path, force: bool = False,
                   studio: bool = False, keep_stage: bool = False):
    """为空目录或安全重置本工具生成目录；预检失败时零删除。"""
    if studio and (force or keep_stage):
        raise ValueError("Studio 不支持 force/keep-stage，请使用新的空输出目录")
    root = _ordinary_output(root)
    if root.parent == root:
        raise ValueError("输出目录不能是文件系统根目录")
    if not root.exists():
        if keep_stage:
            raise ValueError("keep-stage 需要已有生成目录")
        root.mkdir(parents=True)
        _write_initial(root)
        return
    if not any(root.iterdir()):
        _write_initial(root)
        return
    if not force:
        raise FileExistsError("输出目录非空；CLI 需显式 --force，Studio 必须换空目录")
    with _locked_marker(root / MARKER) as stream:
        data = _manifest(stream)
        registered = set(data['files'])
        owned_dirs = {parent.as_posix() for rel in registered for parent in Path(rel).parents if parent != Path('.')}
        existing, directories = _inventory(root)
        pending = data.get('reset_pending')
        if (existing.keys() - registered or directories.keys() - owned_dirs
                or pending is None and existing.keys() != registered):
            raise ValueError("目录含未登记文件或目录，拒绝 force 且未删除任何内容")
        observed = _reset_identity(existing, directories)
        if pending is not None:
            _check_reset(pending, observed, registered, owned_dirs)
        elif existing:
            _rewrite_marker(stream, {**data, 'reset_pending': observed})
        for rel, before in sorted(existing.items()):
            target = root / rel
            if _ordinary_output(target.parent) != target.parent:
                raise ValueError("生成目录在清理期间变化，已保留其余文件")
            if _file_identity(ordinary(target)) != _file_identity(before):
                raise ValueError("生成物在清理期间变化，已保留其余文件")
            target.unlink()
        for rel in sorted(directories, key=lambda path: (path.count('/'), path), reverse=True):
            directory = root / rel
            if _ordinary_output(directory) != directory or not os.path.samestat(ordinary(directory), directories[rel]):
                raise ValueError("生成目录在清理期间变化，已保留其余文件")
            directory.rmdir()
        _rewrite_marker(stream, _new_manifest([]))


def _new_manifest(files):
    return {"schema": SCHEMA, "generator": "flotherm2foam", "version": VERSION,
            "files": sorted(files)}


def write_manifest(root: Path, files: list[str]):
    data = _new_manifest(files)
    path = _ordinary_output(root) / MARKER
    try:
        with path.open('xb') as stream:
            _rewrite_marker(stream, data)
    except FileExistsError:
        with _locked_marker(path) as stream:
            _rewrite_marker(stream, data)
