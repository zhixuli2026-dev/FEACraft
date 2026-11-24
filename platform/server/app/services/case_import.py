"""外部 StandardCase ZIP 导入：staging 校验、恶意路径防护、失败清理。"""
from dataclasses import dataclass
from contextlib import asynccontextmanager
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile

from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Case, User, Workspace
from .. import storage
from .async_ops import finish_before_cancel
from .node_storage_budget import reserve
from .casedict import CASE_DICT_PATH, get_workflow_state, parse_casedict, prepare_case_dict, _apply_dict_mirror
from . import case_copy_local, case_copy_remote, case_provision, owned_staging
from .case_source import CopySource, SourceChanged
from .file_transactions import _capture, _sync
from .local_file_edits import _version
from .quota import check_storage_quota
from .request_deadline import current
from .workspace_write import coordinate_workspaces

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 200 * 1024 * 1024
MAX_FILES = 2000
MAX_COMPRESSION_RATIO = 200
STAGE_SCHEMA = 'vibeflow.case-import-stage.v1'


class CaseImportError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class PreparedImport:
    files: dict[str, "ImportedFile"]
    case_dict: str
    workflow_state: str
    total_bytes: int


@dataclass(frozen=True)
class ImportedFile(CopySource):
    path: Path
    size: int
    sha256: str

    def copy_into(self, destination):
        path = destination if isinstance(destination, Path) else None
        stream = None if path is not None else destination
        try:
            observed = case_provision.journal._capture(self.path, path, stream=stream, limit=self.size, deadline=current())
        except ValueError as error:
            raise CaseImportError("ZIP 暂存内容在创建期间变化", 409) from error
        if observed != self.record:
            raise CaseImportError("ZIP 暂存内容在创建期间变化", 409)


def _save_upload(upload: UploadFile, target: Path, target_root: Path | None = None):
    """Copy the actual parser spool under a node claim, never trust a header."""
    source = upload.file
    source.seek(0, 2)
    size = source.tell()
    source.seek(0)
    if size > MAX_UPLOAD_BYTES:
        raise CaseImportError("ZIP 超过 50 MiB 上传上限", 413)
    with reserve(size, target_root=target_root), target.open("xb") as output:
        written = 0
        while chunk := source.read(1024 * 1024):
            case_provision.check_deadline()
            written += len(chunk)
            if written > size:
                raise CaseImportError("上传内容在复制期间变化", 409)
            output.write(chunk)
        if written != size:
            raise CaseImportError("上传内容在复制期间变化", 409)


def _entry_path(info: zipfile.ZipInfo) -> str:
    raw = info.filename.replace("\\", "/")
    if not raw or "\x00" in raw or raw.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", raw):
        raise CaseImportError(f"非法 ZIP 路径: {info.filename!r}")
    if "//" in raw:
        raise CaseImportError(f"非规范 ZIP 路径: {info.filename!r}")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise CaseImportError(f"ZIP 路径越界或含别名: {info.filename!r}")
    if info.flag_bits & 0x1:
        raise CaseImportError("不支持加密 ZIP")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise CaseImportError("ZIP 不得包含符号链接")
    return path.as_posix()


def _strip_single_root(entries: list[tuple[str, zipfile.ZipInfo]]):
    paths = [path for path, _ in entries]
    if CASE_DICT_PATH in paths:
        return entries
    roots = {path.split("/", 1)[0] for path in paths if "/" in path}
    if len(roots) == 1 and all("/" in path for path in paths):
        prefix = roots.pop() + "/"
        if all(path.startswith(prefix) for path in paths):
            return [(path[len(prefix):], info) for path, info in entries]
    return entries


def _prepare_files(archive_path: Path, stage: Path, target_root: Path | None = None) -> PreparedImport:
    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = []
            total = 0
            for info in archive.infolist():
                path = _entry_path(info)
                if info.is_dir():
                    continue
                entries.append((path, info))
            if not entries:
                raise CaseImportError("ZIP 不含文件")
            if len(entries) > MAX_FILES:
                raise CaseImportError(f"ZIP 文件数超过 {MAX_FILES}", 413)
            entries = _strip_single_root(entries)
            folded = {path.casefold() for path, _ in entries}
            if len(folded) != len(entries):
                raise CaseImportError("ZIP 包含重复或大小写冲突路径")
            from .attempt_workspace import canonical_path
            for path, _ in entries:
                try:
                    if canonical_path(path) != path:
                        raise ValueError("Noncanonical archive path")
                except ValueError as error:
                    raise CaseImportError("ZIP 包含不规范或不可移植路径") from error
                parts = path.casefold().split('/')
                if any('/'.join(parts[:i]) in folded for i in range(1, len(parts))):
                    raise CaseImportError("ZIP 文件与父目录冲突")
            for path, info in entries:
                size = info.file_size
                if size > MAX_FILE_BYTES:
                    raise CaseImportError(f"{path} 超过 50 MiB 单文件上限", 413)
                total += size
                if total > MAX_TOTAL_BYTES:
                    raise CaseImportError("ZIP 解压总大小超过 200 MiB", 413)
                if size > 1024 * 1024 and (info.compress_size == 0 or size / info.compress_size > MAX_COMPRESSION_RATIO):
                    raise CaseImportError(f"{path} 压缩比异常，拒绝疑似 zip bomb", 413)
            files = {}
            actual_total = 0
            # This claim is held until all extraction IO finishes. Afterwards
            # the materialized files remain in the measured storage root.
            with reserve(total, target_root=target_root):
                for path, info in entries:
                    target = stage / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    actual = 0
                    digest = hashlib.sha256()
                    with archive.open(info) as source, target.open("xb") as output:
                        while chunk := source.read(min(1024 * 1024, info.file_size - actual + 1)):
                            case_provision.check_deadline()
                            actual += len(chunk)
                            if actual > info.file_size or actual_total + actual > MAX_TOTAL_BYTES:
                                raise CaseImportError("ZIP 解压大小与目录不一致或超限", 413)
                            digest.update(chunk)
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    _sync(target.parent)
                    if actual != info.file_size:
                        raise CaseImportError("ZIP 解压大小与目录不一致", 413)
                    actual_total += actual
                    files[path] = ImportedFile(target, actual, digest.hexdigest())
    except zipfile.BadZipFile as error:
        raise CaseImportError("不是有效 ZIP 文件") from error
    if CASE_DICT_PATH not in files:
        raise CaseImportError("不是 StandardCase：缺少 0-caseDict/caseDict")
    try:
        case_dict = (stage / CASE_DICT_PATH).read_text(encoding="utf-8")
        parse_casedict(case_dict)
    except (UnicodeError, ValueError) as error:
        raise CaseImportError(f"caseDict 无法解析: {error}", 422) from error
    return PreparedImport(files, case_dict, get_workflow_state(case_dict), actual_total)


async def _prepare_upload(upload: UploadFile, stage: Path, target_root: Path | None) -> PreparedImport:
    filename = upload.filename or ""
    if not filename.lower().endswith(".zip"):
        raise CaseImportError("只支持 .zip StandardCase 包")
    archive_path = stage / "upload.zip"
    await case_provision.io(_save_upload, upload, archive_path, target_root)
    extracted = stage / "files"
    extracted.mkdir()
    prepared = await case_provision.io(_prepare_files, archive_path, extracted, target_root)
    await case_provision.io(archive_path.unlink)
    return prepared


@asynccontextmanager
async def _staging(user_id):
    if storage._use_s3():
        root = storage._fs_path('.', f'.archive-imports/{user_id}')
        batch = owned_staging.batch(root, 0, STAGE_SCHEMA)
        target_root = None
    else:
        location = await case_provision.io(case_copy_local.staging_location, user_id, '.archive-imports')
        batch = case_copy_local.staging_batch(location, 0, STAGE_SCHEMA)
        target_root = location.scratch if location.local else None
    async with batch as directory:
        yield directory, target_root


def _metadata(files):
    existing = {}
    for path in case_copy_local.RENDERED:
        if path in files:
            case_provision.check_deadline()
            with io.BytesIO() as output:
                files[path].copy_into(output)
                existing[path] = output.getvalue()
    return existing


def _prepare_local(extracted, files, metadata):
    versions = {}
    for path, source in files.items():
        case_provision.check_deadline()
        if path in metadata:
            source.path.unlink()
            continue
        try:
            before = _version(source.path)
            observed = _capture(source.path, limit=source.size, deadline=current())
            stable = _version(source.path) == before
        except TimeoutError:
            raise
        except (OSError, ValueError) as error:
            raise SourceChanged('ZIP 暂存内容已变化，请重试') from error
        if observed != source.record or not stable:
            raise SourceChanged('ZIP 暂存内容已变化，请重试')
        versions[path] = before
    return case_copy_local.LocalCopy(extracted, versions, metadata)


def _prepare_remote(user_id, workspace_id, sources, metadata):
    files = {path: source.record for path, source in sources.items() if path not in metadata}
    draft = case_provision.prepare_remote_import(user_id, workspace_id, files)
    return case_copy_remote.stage_draft(draft, sources, metadata)


async def import_case_zip(db: AsyncSession, workspace: Workspace, user: User,
                          name: str, upload: UploadFile):
    from .owner_storage_budget import hold as owner_capacity
    name = name.strip()
    if not name or len(name) > 200 or any(char in name for char in "/\\\x00"):
        raise CaseImportError("算例名不能为空、超过 200 字或包含路径分隔符")
    user_id, workspace_id = user.id, workspace.id
    remote = storage._use_s3()
    duplicate = await db.scalar(select(Case.id).where(Case.workspace_id == workspace_id, Case.name == name).limit(1))
    if duplicate is not None:
        raise CaseImportError('同名算例已存在', 409)
    await finish_before_cancel(db.commit())
    async with _staging(user_id) as (stage, target_root):
        try:
            prepared = await _prepare_upload(upload, stage, target_root)
            sources = prepared.files
            existing = await case_provision.io(_metadata, sources)
            if not remote:
                local = await case_provision.io(_prepare_local, stage / 'files', sources, existing)
            async with owner_capacity(db, user_id):
                await case_provision.recover_owner(db, user_id)
                if remote:
                    await check_storage_quota(db, user, prepared.total_bytes)
                    await finish_before_cancel(db.commit())
                    remote_draft = await case_provision.io(_prepare_remote, user_id, workspace_id, sources, existing)
                async with coordinate_workspaces(db, [workspace_id]):
                    await db.refresh(workspace)
                    if workspace.user_id != user_id:
                        raise CaseImportError('工作区不存在', 404)
                    duplicate = await db.scalar(select(Case).where(
                        Case.workspace_id == workspace_id, Case.name == name))
                    if duplicate:
                        raise CaseImportError("同名算例已存在", 409)
                    case = Case(workspace_id=workspace_id, name=name, s3_prefix="")
                    db.add(case)
                    await db.flush()
                    # 导入保留原状态，不把历史 errors 当成“本次推进”门控。
                    case.workflow_state = prepared.workflow_state
                    try:
                        rendered = prepare_case_dict(case, prepared.case_dict, source='case-import',
                                                     existing_files=existing, actor_id=user_id)
                    except ValueError as error:
                        raise CaseImportError(str(error), 422) from error
                    manifest = {path: source.record for path, source in sources.items()}
                    manifest.update({path: case_provision.digest(data) for path, data in rendered.files.items()})
                    _apply_dict_mirror(case, rendered)
                    if remote:
                        await remote_draft.publish(db, case, manifest, rendered.files)
                    else:
                        additional = sum(record['size'] for record in manifest.values()) - local.accounted_bytes(user_id)
                        await check_storage_quota(db, user, additional)
                        await local.publish(db, case, user_id, manifest, rendered.files, 'case.import')
                await finish_before_cancel(db.commit())
        except SourceChanged as error:
            await finish_before_cancel(db.rollback())
            raise CaseImportError(str(error), 409) from error
        except BaseException:
            await finish_before_cancel(db.rollback())
            raise
        return case, len(sources), prepared.total_bytes
