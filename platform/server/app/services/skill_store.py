"""用户/组织技能写入、打包与安全安装；内置 < org < user 覆盖。"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import stat
import tempfile
import zipfile
import zlib

SKILLS_DIR = Path(__file__).parents[2] / "skills"

DATA_ROOT = Path(__file__).parents[2] / "data" / "skills"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_SKILL_BYTES = 1024 * 1024
MAX_ZIP_ENTRIES = 100
MAX_ZIP_BYTES = 2 * MAX_SKILL_BYTES
MAX_SCAN_ENTRIES = 10000
READ_CHUNK_BYTES = 64 * 1024


def _check_deadline():
    from .request_deadline import current
    budget = current()
    if budget is not None:
        budget.check()


def _ordinary(path: Path):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
            or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
        raise ValueError("技能目录只能包含普通文件和目录")
    return info


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _source_files(target: Path):
    """Bound the inventory before allocating a ZIP or reading file contents."""
    root_info = _ordinary(target)
    if not stat.S_ISDIR(root_info.st_mode):
        raise FileNotFoundError(target.name)
    pending = [target]
    records = {"": _identity(root_info)}
    files = []
    total, visited = 0, 1
    while pending:
        parent = pending.pop()
        _check_deadline()
        parent_key = "" if parent == target else parent.relative_to(target).as_posix()
        if _identity(_ordinary(parent)) != records[parent_key]:
            raise ValueError("打包期间技能目录已变化")
        with os.scandir(parent) as entries:
            for entry in entries:
                _check_deadline()
                visited += 1
                if visited > MAX_SCAN_ENTRIES:
                    raise ValueError("技能目录扫描条目超过上限")
                path = Path(entry.path)
                info = _ordinary(path)
                relative = path.relative_to(target).as_posix()
                records[relative] = _identity(info)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                else:
                    files.append((relative, info))
                    total += info.st_size
                    if len(files) > MAX_ZIP_ENTRIES:
                        raise ValueError("ZIP 文件数超过 100")
                    if total > MAX_SKILL_BYTES:
                        raise ValueError("技能总内容超过 1 MiB")
    if not any(relative == "SKILL.md" for relative, _ in files):
        raise FileNotFoundError(target.name)
    return sorted(files, key=lambda item: item[0]), records


class _BoundedZip(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_ZIP_BYTES:
            raise ValueError("技能 ZIP 超过 2 MiB")
        return super().write(data)


def storage_root(settings=None) -> Path:
    from ..config import get_settings
    from .cli_private_paths import root as cli_root
    settings = settings or get_settings()
    configured = getattr(settings, "skills_storage_root", "")
    selected = (Path(configured) if configured else DATA_ROOT).resolve()
    for other in [Path(settings.storage_fs_root).resolve(), cli_root(settings)]:
        if selected.is_relative_to(other) or other.is_relative_to(selected):
            raise ValueError("技能目录必须独立于算例及CLI私有目录")
    return selected


def _scope_root(scope: str, user_id: int, org_id: int | None) -> Path:
    root = storage_root()
    if scope == "user":
        return root / "users" / str(user_id)
    if scope == "org" and org_id is not None:
        return root / "orgs" / str(org_id)
    raise ValueError("无效技能 scope")


def skill_directories(user_id: int | None = None, org_id=None) -> list[Path]:
    root = storage_root()
    dirs = []
    if user_id is not None:
        dirs.append(root / "users" / str(user_id))
    if org_id is not None:
        org_ids = org_id if isinstance(org_id, (list, tuple, set)) else [org_id]
        for item in org_ids:
            dirs.append(root / "orgs" / str(item))
    dirs.append(SKILLS_DIR)
    return dirs


def _write_plan(scope, user_id, org_id, name, description, body, interface, overwrite):
    if not NAME_RE.fullmatch(name):
        raise ValueError("技能名只能包含小写字母、数字和连字符")
    if not description.strip() or not body.strip():
        raise ValueError("description/body 不能为空")
    if len(body) > MAX_SKILL_BYTES or len(body.encode("utf-8")) > MAX_SKILL_BYTES:
        raise ValueError("技能正文超过 1 MiB")
    if len(description) > MAX_SKILL_BYTES:
        raise ValueError("技能总内容超过 1 MiB")
    text = f"---\nname: {name}\ndescription: {description.strip()}\n---\n\n{body.strip()}\n"
    content = {"SKILL.md": text.encode("utf8")}
    if interface:
        values = {}
        for key in ("display_name", "short_description", "default_prompt"):
            value = interface.get(key, "")
            if not isinstance(value, str) or len(value) > MAX_SKILL_BYTES:
                raise ValueError("技能元数据无效或超过上限")
            values[key] = value
        # JSON's quoted strings are also YAML syntax and round-trip line breaks.
        content["agents/openai.yaml"] = (json.dumps({"interface": values}, ensure_ascii=False, indent=2) + "\n").encode("utf8")
    if sum(map(len, content.values())) > MAX_SKILL_BYTES:
        raise ValueError("技能总内容超过 1 MiB")
    root = _scope_root(scope, user_id, org_id)
    target = root / name
    if target.exists() and not overwrite:
        raise FileExistsError(name)
    files, inventory = [], {}
    if target.exists():
        files, inventory = _source_files(target)
        total = sum(info.st_size for relative, info in files if relative not in content)
        if total + sum(map(len, content.values())) > MAX_SKILL_BYTES:
            raise ValueError("技能总内容超过 1 MiB")
        if len({relative for relative, _ in files} | content.keys()) > MAX_ZIP_ENTRIES:
            raise ValueError("ZIP 文件数超过 100")
    return target, content, files, inventory


def write_peak_bytes(scope, user_id, org_id, name, description, body, interface=None, overwrite=False):
    from .skill_installation import scope as publication_scope
    with publication_scope(_scope_root(scope, user_id, org_id), name=name):
        _, content, _, _ = _write_plan(scope, user_id, org_id, name, description, body, interface, overwrite)
    return sum(map(len, content.values()))


def write_skill(scope: str, user_id: int, org_id: int | None, name: str,
                description: str, body: str, interface: dict | None = None,
                overwrite: bool = False) -> Path:
    from .case_write import CaseWriteConflict
    from .node_storage_budget import reserve
    from .skill_installation import JOURNAL, MAX_JOURNAL_BYTES, publish, scope as publication_scope
    root = _scope_root(scope, user_id, org_id)
    arguments = (scope, user_id, org_id, name, description, body, interface, overwrite)
    with publication_scope(root, name=name):
        _, content, files, _ = _write_plan(*arguments)
        size = _prepared_size(content, files)
    with reserve(size + MAX_JOURNAL_BYTES, target_root=storage_root()):
        with publication_scope(root, name=name, writing=True, create=True):
            target, content, files, inventory = _write_plan(*arguments)
            if _prepared_size(content, files) > size:
                raise CaseWriteConflict('skill_storage_busy', '技能附件已变化，请重试更新')
            stage = Path(tempfile.mkdtemp(prefix='.install-', dir=root))
            prepared = stage / 'payload' / name
            try:
                _prepare_update(target, prepared, content, files, inventory)
                return publish(root, stage, prepared, name, overwrite)
            finally:
                if not os.path.lexists(root / JOURNAL) and stage.exists():
                    _remove_owned_directory(stage, root)


def _prepared_size(content, files):
    return sum(map(len, content.values())) + sum(info.st_size for relative, info in files if relative not in content)


def _prepare_update(target, prepared, content, files, inventory):
    prepared.mkdir(parents=True, exist_ok=False)
    for relative, fingerprint in sorted(inventory.items()):
        _check_deadline()
        if relative and stat.S_ISDIR(fingerprint[2]):
            (prepared / relative).mkdir()
    for relative, info in files:
        if relative in content:
            continue
        source, destination = target / relative, prepared / relative
        _check_deadline()
        expected = _identity(info)
        if _identity(_ordinary(source)) != expected:
            raise ValueError('更新期间技能附件已变化')
        with source.open('rb') as reader, destination.open('xb') as writer:
            if _identity(os.fstat(reader.fileno())) != expected:
                raise ValueError('更新期间技能附件已变化')
            remaining = info.st_size
            while remaining:
                _check_deadline()
                block = reader.read(min(remaining, READ_CHUNK_BYTES))
                if not block:
                    raise ValueError('更新期间技能附件已变化')
                writer.write(block)
                remaining -= len(block)
            if _identity(os.fstat(reader.fileno())) != expected:
                raise ValueError('更新期间技能附件已变化')
        shutil.copystat(source, destination, follow_symlinks=False)
    for relative, data in content.items():
        _check_deadline()
        path = prepared / relative
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
    if inventory and _source_files(target)[1] != inventory:
        raise ValueError('更新期间技能目录已变化')


def package_skill(scope: str, user_id: int, org_id: int | None, name: str) -> bytes:
    from .skill_installation import scope as publication_scope
    if not NAME_RE.fullmatch(name):
        raise ValueError("技能名只能包含小写字母、数字和连字符")
    root = _scope_root(scope, user_id, org_id)
    with publication_scope(root, name=name):
        return _package(root / name, name)


def _package(target, name):
    if not target.is_dir() or not (target / "SKILL.md").is_file():
        raise FileNotFoundError(name)
    files, before = _source_files(target)
    output = _BoundedZip()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative, expected in files:
            _check_deadline()
            path = target / relative
            if _identity(_ordinary(path)) != _identity(expected):
                raise ValueError("打包期间技能文件已变化")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            with os.fdopen(os.open(path, flags), "rb") as source:
                if _identity(os.fstat(source.fileno())) != _identity(expected):
                    raise ValueError("打包期间技能文件已变化")
                info = zipfile.ZipInfo.from_file(path, f"{name}/{relative}")
                _entry_name(info)
                info.compress_type = zipfile.ZIP_DEFLATED
                actual = 0
                with archive.open(info, "w") as destination:
                    while True:
                        _check_deadline()
                        chunk = source.read(min(READ_CHUNK_BYTES, expected.st_size - actual + 1))
                        if not chunk:
                            break
                        actual += len(chunk)
                        if actual > expected.st_size:
                            raise ValueError("打包期间技能文件已增长")
                        destination.write(chunk)
                if actual != expected.st_size or _identity(os.fstat(source.fileno())) != _identity(expected):
                    raise ValueError("打包期间技能文件已变化")
    if _source_files(target)[1] != before:
        raise ValueError("打包期间技能目录已变化")
    return output.getvalue()


def _entry_name(info: zipfile.ZipInfo) -> str:
    raw = info.filename.replace("\\", "/")
    mode = info.external_attr >> 16
    if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ValueError("ZIP 只能包含普通文件和目录")
    if (not raw or raw.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", raw)
            or "\x00" in info.orig_filename or len(raw.encode("utf8")) > 4096):
        raise ValueError("非法 ZIP 路径")
    path = PurePosixPath(raw)
    if not path.parts or ".." in path.parts or len(path.parts) > 100:
        raise ValueError("ZIP 路径穿越")
    if any(re.search(r'[<>:"|?*\x00-\x1f]', part) or part.endswith((".", " "))
            or PureWindowsPath(part).is_reserved() for part in path.parts):
        raise ValueError("ZIP 路径含不支持的文件名")
    return path.as_posix().rstrip("/")


def _preflight(archive):
    infos = archive.infolist()
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ValueError("ZIP 文件数超过 100")
    result = []
    seen = {}
    total = 0
    skill = []
    tree = {}
    for info in infos:
        _check_deadline()
        name = _entry_name(info)
        directory = info.filename.replace("\\", "/").endswith("/") or stat.S_ISDIR(info.external_attr >> 16)
        key = name.casefold()
        if key in seen:
            raise ValueError("ZIP 包含重复或大小写冲突路径")
        seen[key] = directory
        if info.flag_bits & 0x1 or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ValueError("技能 ZIP 仅支持未加密的存储或Deflate压缩")
        if info.file_size < 0 or (directory and info.file_size != 0):
            raise ValueError("ZIP 文件大小或目录内容无效")
        total += info.file_size
        if total > MAX_SKILL_BYTES:
            raise ValueError("技能解压后超过 1 MiB")
        path = PurePosixPath(name)
        for parent in [path, *path.parents]:
            if parent == PurePosixPath("."):
                continue
            spelling = parent.as_posix()
            key = spelling.casefold()
            if key in tree and tree[key] != spelling:
                raise ValueError("ZIP 目录路径大小写冲突")
            tree[key] = spelling
            if len(tree) > MAX_SCAN_ENTRIES:
                raise ValueError("技能目录扫描条目超过上限")
        if not directory and path.name == "SKILL.md":
            skill.append(path)
        result.append((info, name, directory))
    for _, name, _ in result:
        if any(seen.get(parent.as_posix().casefold()) is False for parent in PurePosixPath(name).parents if parent != PurePosixPath(".")):
            raise ValueError("ZIP 文件与目录路径冲突")
    if len(skill) != 1:
        raise ValueError("ZIP 必须且只能包含一个 SKILL.md")
    if not NAME_RE.fullmatch(skill[0].parent.name):
        raise ValueError("技能目录名无效")
    return result, skill[0]


def _remove_owned_directory(path, root):
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved == resolved_root or not resolved.is_relative_to(resolved_root):
        raise ValueError("技能暂存目录不在所属根内")
    if not stat.S_ISDIR(_ordinary(path).st_mode):
        raise ValueError("技能暂存目录不是普通目录")
    shutil.rmtree(path)


def install_peak_bytes(data: bytes) -> int:
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError("技能 ZIP 超过 2 MiB")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries, _ = _preflight(archive)
            return sum(info.file_size for info, _, _ in entries)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, RuntimeError, zlib.error) as error:
        raise ValueError("技能 ZIP 内容损坏或无效") from error


def install_skill_zip(data: bytes, scope: str, user_id: int, org_id: int | None,
                      overwrite: bool = False) -> Path:
    from .node_storage_budget import reserve
    from .skill_installation import MAX_JOURNAL_BYTES
    size = install_peak_bytes(data)
    with reserve(size + MAX_JOURNAL_BYTES, target_root=storage_root()):
        return _install_skill_zip(data, scope, user_id, org_id, overwrite)


def _install_skill_zip(data: bytes, scope: str, user_id: int, org_id: int | None,
                       overwrite: bool = False) -> Path:
    from .skill_installation import JOURNAL, publish, scope as publication_scope
    if len(data) > MAX_ZIP_BYTES:
        raise ValueError("技能 ZIP 超过 2 MiB")
    root = _scope_root(scope, user_id, org_id)
    stage = None
    handed_off = False
    try:
        total = 0
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries, skill = _preflight(archive)
            with publication_scope(root, name=skill.parent.name, writing=True, create=True):
                stage = Path(tempfile.mkdtemp(prefix=".install-", dir=root))
            payload = stage / "payload"
            for info, name, directory in entries:
                _check_deadline()
                if directory:
                    continue
                target = payload / name
                target.parent.mkdir(parents=True, exist_ok=True)
                actual = 0
                with archive.open(info) as source, target.open("xb") as destination:
                    while True:
                        _check_deadline()
                        chunk = source.read(min(READ_CHUNK_BYTES, info.file_size - actual + 1))
                        if not chunk:
                            break
                        actual += len(chunk)
                        if actual > info.file_size or total + actual > MAX_SKILL_BYTES:
                            raise ValueError("技能解压后超过 1 MiB")
                        destination.write(chunk)
                if actual != info.file_size:
                    raise ValueError("ZIP 实际内容与声明大小不符")
                total += actual
        skill_file = payload / skill.as_posix()
        name = skill.parent.name
        with publication_scope(root, name=name, writing=True):
            try:
                return publish(root, stage, skill_file.parent, name, overwrite)
            finally:
                handed_off = os.path.lexists(root / JOURNAL)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, RuntimeError, zlib.error) as error:
        raise ValueError("技能 ZIP 内容损坏或无效") from error
    finally:
        if not handed_off and stage is not None and stage.exists():
            _remove_owned_directory(stage, root)
