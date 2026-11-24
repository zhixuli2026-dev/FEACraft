"""Freeze selected run inputs before publishing a new derived directory."""
import hashlib
from functools import partial
from pathlib import Path, PureWindowsPath
import re
import os
import stat

from .. import storage
from .case_write import normalize_path, file_record
from .case_source import SourceChanged, SourceFile, _identity
from .fs_directory import DirectoryMoveError
from .request_deadline import current
from .storage_listing_budget import ListingBudget

if os.name == 'nt':
    from .local_file_edits import _change_time


def destination(base, name):
    base, name = normalize_path(base.strip()), name.strip()
    if (not re.fullmatch(r"[\w.\-一-鿿]+", name) or name in {".", ".."} or
            name.endswith(".") or PureWindowsPath(name).is_reserved()):
        raise ValueError("目录名只允许中英文/数字/._-，不能使用保留名或以点结尾")
    relative = f"C-run/{name}"
    if base == relative or base.startswith(relative + '/') or relative.startswith(base + '/'):
        raise ValueError("源工况和目标目录不能互相包含")
    return base, relative


def remote_inventory(prefix, base):
    from . import file_transactions as journal
    from .s3_streams import snapshot
    entries = [entry for entry in storage.list_tree(prefix, base)
               if selected(entry['path'][len(base) + 1:])]
    if not any(entry['path'] == base + '/system/controlDict' for entry in entries):
        raise ValueError(f"{base} 不是有效算例目录（缺 system/controlDict）")
    if len(entries) > journal.MAX_FILES or sum(entry['size'] for entry in entries) > journal.MAX_LOCAL_TRANSACTION_BYTES:
        raise ValueError("工况派生超过单次文件事务限制（256 文件、2 GiB）")
    records = {}
    for entry in entries:
        observed, _, _ = snapshot(prefix, entry['path'], limit=journal.MAX_BYTES)
        if observed is None or observed['size'] != entry['size']:
            raise SourceChanged("复制期间源文件已变化，请重试")
        records[entry['path'][len(base) + 1:]] = observed
    return records


async def source_binding(db, case):
    from sqlalchemy import func, select
    from ..models import DurableEvent

    # Storage lease bookkeeping can advance while a competing derivation
    # prepares. The lease itself fences writers and is not a source edit.
    sequence = await db.scalar(select(func.max(DurableEvent.seq)).where(
        DurableEvent.case_id == case.id, DurableEvent.type.not_like('storage.operation.%')))
    return case.workspace_id, case.s3_prefix, case.dict_revision, sequence or 0


def require_remote_destination(prefix, target):
    if storage.exists(prefix, target) or storage.list_tree(prefix, target):
        raise DirectoryMoveError("目标工况已存在，拒绝覆盖")


def prepare_remote(prefix, base, name):
    base, target = destination(base, name)
    require_remote_destination(prefix, target)
    return base, target, remote_inventory(prefix, base)


def _verify_remote_result(prefix, base, target, records):
    observed = remote_inventory(prefix, base)
    if observed != records:
        raise SourceChanged("复制期间源文件清单已变化，请重试")
    paths = {entry['path'] for entry in storage.list_tree(prefix, target)}
    if storage.exists(prefix, target) or paths != {target + '/' + path for path in records}:
        raise SourceChanged("提交前目标工况已变化，请重试")


async def derive_remote(checked, prefix, base, target, records):
    """Copy and verify S3 data while the durable writer owns the case."""
    from . import file_transactions as journal, s3_staging_transactions, storage_operations
    from .s3_file_transactions import RemoteSource
    from .s3_storage_recovery import recover_reserved

    changes = {target + '/' + relative: RemoteSource(base + '/' + relative, expected)
               for relative, expected in records.items()}
    async with storage_operations.hold(checked, purpose='staging') as lease:
        if lease.case.s3_prefix != prefix:
            raise SourceChanged("派生期间源工况已变化，请重试")
        await recover_reserved(lease)
        await journal._io(require_remote_destination, prefix, target)
        await s3_staging_transactions.write_reserved(lease, changes, dict.fromkeys(changes), scope='files',
            verify=partial(_verify_remote_result, prefix, base, target, records))
    return {'ok': True, 'created': target, 'files': len(records)}


def _excluded_root(first):
    # OpenFOAM also writes exponential and negative time names. Keep initial 0/.
    result_time = first != "0" and re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", first)
    processor_output = re.fullmatch(r"(?:processor\d+|processors\d+(?:_\d+-\d+)?)", first)
    return bool(result_time or processor_output or first in {".vibeflow", "postProcessing"} or
                first.startswith("log."))


def selected(relative):
    return not (_excluded_root(relative.split('/', 1)[0]) or relative.endswith(('.log', '.foam')))


def _source_version(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
        raise SourceChanged("派生期间源文件已变化，请重试")
    identity = _identity(info)
    if os.name != 'nt':
        return identity
    changed = _change_time(path)
    if _identity(path.lstat()) != identity:
        raise SourceChanged("派生期间源文件已变化，请重试")
    return (*identity[:4], changed)


def source_versions(prefix, base):
    source = storage._fs_path(prefix, base)
    if not (source / "system/controlDict").is_file():
        raise ValueError(f"{base} 不是有效算例目录（缺 system/controlDict）")
    budget = ListingBudget()
    versions = {}
    pending = [source]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for item in children:
                budget.visit()
                path = Path(item.path)
                relative = path.relative_to(source).as_posix()
                if _excluded_root(relative.split('/', 1)[0]):
                    continue
                info = item.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0) & 0x400:
                    if selected(relative):
                        raise ValueError("工况派生不支持源符号链接，请使用算例内普通文件")
                elif stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode) and selected(relative):
                    versions[relative] = _source_version(path)
    return versions


def source_files(prefix, base):
    records = {}
    for relative, version in source_versions(prefix, base).items():
        path = f"{base}/{relative}"
        record = file_record(prefix, path)
        if (record is None or record['size'] != version[2]
                or _source_version(storage._fs_path(prefix, path)) != version):
            raise SourceChanged("复制期间源工况已变化，请重试")
        records[relative] = SourceFile(prefix, path, record['size'], record['sha256'], version)
    return records


class DerivedRun:
    STAGE_SCHEMA = 'vibeflow.derived-run-stage.v1'
    JOURNAL_BYTES = 8192

    def __init__(self, prefix, base, name):
        if storage._use_s3():
            raise ValueError("工况派生当前仅支持 fs 存储")
        self.prefix = prefix
        self.base, self.relative = destination(base, name)
        self.root = storage._fs_path(prefix, ".")
        self.source = storage._fs_path(prefix, self.base)
        self.target = storage._fs_path(prefix, self.relative)
        if self.target.exists():
            raise DirectoryMoveError("目标工况已存在，拒绝覆盖")
        if self.target.is_relative_to(self.source) or self.source.is_relative_to(self.target):
            raise ValueError("源工况和目标目录不能互相包含")
        self.storage_root = storage._fs_root().resolve()
        self.stage_is_billed = self.root.stat().st_dev != self.storage_root.stat().st_dev
        # A distinct case mount needs a same-volume rename. Otherwise keep
        # temporary copies outside the workspace owner's visible usage.
        self.staging_root = (storage._fs_path(prefix, '.vibeflow/tree-transactions/derive-staging')
                             if self.stage_is_billed else storage._fs_path('.', '.derive-staging'))

    def inventory(self):
        self.records = source_files(self.prefix, self.base)
        return sum(record.size for record in self.records.values())

    def prepare(self):
        from .file_transactions import _sync
        from .tree_transactions import _verified_tree, DERIVED_RUN, PreparedTree

        payload = self.stage/'after/0'
        payload.mkdir(parents=True, exist_ok=False)
        deadline = current()
        for relative, expected in self.records.items():
            source = storage._fs_path(self.prefix, expected.path)
            target = payload / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            digest, size = hashlib.sha256(), 0
            if _source_version(source) != expected.version:
                raise SourceChanged("复制期间源工况已变化，请重试")
            with source.open("rb") as incoming, target.open("xb") as outgoing:
                if _identity(os.fstat(incoming.fileno()))[:4] != expected.version[:4]:
                    raise SourceChanged("复制期间源工况已变化，请重试")
                for chunk in iter(lambda: incoming.read(1024*1024), b""):
                    if deadline is not None:
                        deadline.check()
                    size += len(chunk)
                    if size > expected.size:
                        raise SourceChanged("复制期间源文件超过已预留大小，请重试")
                    digest.update(chunk)
                    outgoing.write(chunk)
                if _identity(os.fstat(incoming.fileno()))[:4] != expected.version[:4]:
                    raise SourceChanged("复制期间源工况已变化，请重试")
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if ((size, digest.hexdigest()) != (expected.size, expected.sha256)
                    or _source_version(source) != expected.version):
                raise SourceChanged("复制期间源工况已变化，请重试")
        if source_files(self.prefix, self.base) != self.records:
            raise SourceChanged("复制期间源工况已变化，请重试")
        for directory, _, _ in os.walk(payload, topdown=False):
            if deadline is not None:
                deadline.check()
            _sync(Path(directory))
        signature, identity = _verified_tree(payload, compact=True)
        new_bytes = 0 if self.stage_is_billed else signature['bytes']
        return PreparedTree(self, self.stage, [{'path':self.relative,'before':None,'after':signature}],
                            [(None,identity)], [self.relative], new_bytes+self.JOURNAL_BYTES, DERIVED_RUN)

    def verify(self):
        try:
            observed = source_versions(self.prefix, self.base)
        except (FileNotFoundError, NotADirectoryError, ValueError) as error:
            raise SourceChanged("提交前源工况已变化，请重试") from error
        if observed != {path: record.version for path, record in self.records.items()}:
            raise SourceChanged("提交前源工况已变化，请重试")
        if storage._fs_path(self.prefix, self.relative) != self.target or self.target.exists():
            raise DirectoryMoveError("目标工况已存在或路径已变化，拒绝覆盖")
