"""Read a v1 ZIP one file record at a time before acquiring owner write capacity."""
import base64
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, replace
from functools import partial
import hashlib
import io
import json
import os
from pathlib import Path
import re
import uuid

from .. import storage
from ..config import get_settings
from . import backup_manifest as schema
from . import case_copy_local, case_copy_remote, case_provision
from .backup_download import SpooledDownload
from .case_source import CopySource, SourceChanged, remote_versions
from .file_transactions import _capture, _path, _sync
from .fs_directory import move_file_new
from .local_file_edits import _version
from .request_deadline import current


_STRING_BODY = re.compile(r'[^"\\\x00-\x1f]*(?:\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})[^"\\\x00-\x1f]*)*')
STAGE_SCHEMA = 'vibeflow.backup-restore-stage.v1'


class _JSONReader:
    """Frame top-level fields and file rows; the stdlib decodes each JSON value."""
    block = 64 * 1024

    def __init__(self, stream):
        self.stream = stream
        self.buffer = ''
        self.pos = 0
        self.eof = False
        self.decoder = json.JSONDecoder()

    def _extend(self):
        remaining = self.buffer[self.pos:]
        chunk = self.stream.read(max(self.block, len(remaining)))
        self.buffer = remaining + chunk
        self.pos = 0
        self.eof = not chunk

    def peek(self):
        while True:
            self.pos = json.decoder.WHITESPACE.match(self.buffer, self.pos).end()
            if self.pos < len(self.buffer):
                return self.buffer[self.pos]
            if self.eof:
                return ''
            self._extend()

    def take(self, char):
        if self.peek() != char:
            return False
        self.pos += 1
        return True

    def require(self, char):
        if not self.take(char):
            raise schema.BackupError("无效备份ZIP/manifest")

    def entries(self, opening, closing):
        self.require(opening)
        if self.take(closing):
            return
        while True:
            yield
            if self.take(closing):
                return
            self.require(',')

    def value(self, *, maximum=None):
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer, self.pos)
            except json.JSONDecodeError:
                if self.eof:
                    raise
            else:
                if maximum is not None and end - self.pos > maximum:
                    raise ValueError('JSON metadata exceeds its limit')
                # A number may continue into the next block, even after a
                # complete integer followed by the beginning of an exponent.
                numeric = type(value) in (int, float)
                if numeric and not self.eof and len(self.buffer) - end <= 2:
                    partial = self.buffer[end:] in ('', '.', 'e', 'E', 'e-', 'e+', 'E-', 'E+')
                else:
                    partial = False
                if not partial:
                    self.pos = end
                    return value
            if maximum is not None and len(self.buffer) - self.pos > maximum:
                raise ValueError('JSON metadata exceeds its limit')
            self._extend()

    def string_blocks(self):
        """Yield complete JSON string fragments without building the full value."""
        self.require('"')
        while True:
            end = _STRING_BODY.match(self.buffer, self.pos).end()
            if end != self.pos:
                part = self.buffer[self.pos:end]
                self.pos = end
                yield json.loads('"' + part + '"') if '\\' in part else part
            if self.pos < len(self.buffer) and self.buffer[self.pos] == '"':
                self.pos += 1
                return
            # A partial escape has at most five characters.
            if self.eof or len(self.buffer) - self.pos >= 6:
                raise schema.BackupError("无效备份ZIP/manifest")
            self._extend()


@dataclass(frozen=True)
class StagedFile(CopySource):
    archive: object
    offset: int
    size: int
    sha256: str

    def copy_into(self, destination):
        disk = isinstance(destination, (str, os.PathLike))
        target = Path(destination).open('xb') if disk else nullcontext(destination)
        deadline = current()
        digest = hashlib.sha256()
        remaining = self.size
        with target as stream:
            self.archive.seek(self.offset)
            while remaining:
                if deadline is not None:
                    deadline.check()
                block = self.archive.read(min(remaining, 1024 * 1024))
                if not block:
                    raise OSError("恢复暂存文件读取不完整")
                remaining -= len(block)
                digest.update(block)
                stream.write(block)
            if digest.hexdigest() != self.sha256:
                raise OSError("恢复暂存文件摘要不匹配")
            if disk:
                stream.flush()
                os.fsync(stream.fileno())


class RestoreFiles(SpooledDownload):
    @property
    def maximum(self):
        return schema.MAX_SOURCE_BYTES

    def _stage_payload(self, reader, offset):
        if reader.peek() != '"':
            reader.value()
            return schema.BackupError("备份文件数据超限或无效")
        parts = reader.string_blocks()
        digest = hashlib.sha256()
        deadline = current()
        max_encoded = 4 * ((self.maximum + 2) // 3)
        partial = ''
        encoded = 0
        padding = None

        def decode(value):
            try:
                content = base64.b64decode(value, validate=True)
            except ValueError as error:
                raise schema.BackupError("备份文件Base64无效") from error
            if self.tell() + len(content) > self.maximum:
                raise schema.BackupError("备份文件总量超过100MiB")
            digest.update(content)
            self.write(content)

        try:
            for part in parts:
                if deadline is not None:
                    deadline.check()
                encoded += len(part)
                if encoded > max_encoded:
                    raise schema.BackupError("备份文件数据超限或无效")
                if padding is not None:
                    if part.strip('='):
                        raise schema.BackupError("备份文件Base64无效")
                    padding += len(part)
                    continue
                split = part.find('=')
                if split >= 0:
                    suffix = part[split:]
                    if suffix.strip('='):
                        raise schema.BackupError("备份文件Base64无效")
                    padding = len(suffix)
                    part = part[:split]
                value = partial + part
                end = len(value) - len(value) % 4
                if end:
                    decode(value[:end])
                partial = value[end:]
            if partial or (padding is not None and self.tell() == offset):
                if padding is not None and padding > 3:
                    raise schema.BackupError("备份文件Base64无效")
                decode(partial + '=' * (padding or 0))
        except schema.BackupError as error:
            # Keep the same last-key behavior as decoding the whole JSON row.
            for _ in parts:
                if deadline is not None:
                    deadline.check()
            return error
        return self.tell() - offset, digest.hexdigest()

    def _stage(self, reader):
        offset = self.tell()
        payload = schema.BackupError("备份文件数据超限或无效")
        row = {}
        for _ in reader.entries('{', '}'):
            if reader.peek() != '"':
                raise schema.BackupError("无效备份ZIP/manifest")
            key = reader.value()
            reader.require(':')
            if key == 'data':
                self.seek(offset)
                payload = self._stage_payload(reader, offset)
            else:
                row[key] = reader.value()
        if isinstance(payload, schema.BackupError):
            raise payload
        size, digest = payload
        if digest != row.get('sha256'):
            raise schema.BackupError("备份文件hash不匹配")
        return {'case_id': row.get('case_id'), 'path': row.get('path'),
                'sha256': digest, '_offset': offset, '_size': size}

    def _parse(self, stream):
        with io.BufferedReader(stream) as buffered:
            encoding = json.detect_encoding(buffered.peek(4)[:4])
            with io.TextIOWrapper(buffered, encoding=encoding, newline='') as source:
                reader = _JSONReader(source)
                manifest = {}
                for _ in reader.entries('{', '}'):
                    if reader.peek() != '"':
                        raise schema.BackupError("无效备份ZIP/manifest")
                    key = reader.value()
                    reader.require(':')
                    if key != 'files':
                        manifest[key] = reader.value()
                        continue
                    self.seek(0)
                    files = manifest[key] = []
                    for _ in reader.entries('[', ']'):
                        if reader.peek() != '{' or len(files) >= schema.MAX_FILES:
                            raise schema.BackupError("文件清单无效或超限")
                        files.append(self._stage(reader))
                # Drain the ZIP member and reject trailing JSON or a failed CRC.
                if reader.peek():
                    raise schema.BackupError("无效备份ZIP/manifest")
        return manifest

    def load(self, data):
        manifest = schema.read_archive(data, self._parse)
        cases, paths = schema.validate_structure(manifest)
        schema.validate_case_dicts(cases, paths, self.read_file)
        return manifest

    def read_file(self, row):
        self.seek(row['_offset'])
        content = self._stream.read(row['_size'])
        if len(content) != row['_size']:
            raise OSError("恢复暂存文件读取不完整")
        return content

    def file_source(self, row):
        return StagedFile(self._stream, row['_offset'], row['_size'], row['sha256'])


@dataclass(frozen=True)
class CasePayload:
    files: dict[str, StagedFile]
    log_sources: dict[str, str]

    def destinations(self, job_logs):
        moves = {staged: job_logs[path] for path, staged in self.log_sources.items()}
        destinations = {moves.get(path, path): source for path, source in self.files.items()}
        paths = {path.casefold() for path in destinations}
        if len(paths) != len(self.files):
            raise schema.BackupError('恢复后日志重映射与其他文件路径冲突')
        for path in paths:
            parent = path.rpartition('/')[0]
            while parent:
                if parent in paths:
                    raise schema.BackupError('恢复后日志重映射与其他文件路径冲突')
                parent = parent.rpartition('/')[0]
        return destinations, moves


def case_payloads(manifest, staged):
    cases = {row['id']: CasePayload({}, {}) for row in manifest.get('cases', [])}
    log_paths = {(row['case_id'], row['log_s3_key']) for row in manifest.get('jobs', []) if row['log_s3_key']}
    namespace = '.restore-' + uuid.uuid4().hex
    for number, row in enumerate(manifest['files'], 1):
        case_provision.check_deadline()
        case = cases[row['case_id']]
        path = row['path']
        if path.split('/', 1)[0].casefold() == namespace:
            raise schema.BackupError('备份文件与私有暂存路径冲突，请重试')
        if (row['case_id'], path) in log_paths:
            selected = f'{namespace}/{number}.log'
            case.log_sources[path] = selected
        else:
            selected = path
        case.files[selected] = staged.file_source(row)
    return cases


def _stage_local(directory, payload):
    directory.mkdir()
    versions = {}
    for path, source in payload.files.items():
        case_provision.check_deadline()
        target = _path(directory, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        source.copy_into(target)
        _sync(target.parent)
        before = _version(target)
        if (_capture(target, limit=source.size, deadline=current()) != source.record or _version(target) != before):
            raise SourceChanged('恢复暂存未通过正文校验，请重试')
        versions[path] = before
    return case_copy_local.LocalCopy(directory, versions, {})


@asynccontextmanager
async def stage_local(user_id, location, cases):
    total = sum(source.size for payload in cases.values() for source in payload.files.values())
    async with case_copy_local.staging_batch(location, total, STAGE_SCHEMA) as directory:
        prepared = {}
        prefix = case_copy_local.case_destination(user_id)
        for number, (old_case_id, payload) in enumerate(cases.items(), 1):
            inventory = {path: source.record for path, source in payload.files.items()}
            await case_provision.io(case_provision._validate_inventory, prefix, inventory)
            prepared[old_case_id] = await case_provision.io(_stage_local, directory / str(number), payload)
        yield prepared


def stage_remote(user_id, cases):
    prepared = {}
    for number, (old_case_id, payload) in enumerate(cases.items(), 1):
        inventory = {path: source.record for path, source in payload.files.items()}
        draft = case_provision.prepare_remote_restore(user_id, number, inventory)
        prepared[old_case_id] = case_copy_remote.stage_draft(draft, payload.files, {})
    return prepared


def _move_local_logs(prepared, moves):
    versions = dict(prepared.versions)
    for source, target in moves.items():
        case_provision.check_deadline()
        before = _path(prepared.directory, source)
        after = _path(prepared.directory, target)
        if _version(before) != versions.pop(source):
            raise SourceChanged('恢复暂存中的日志已变化，请重试')
        move_file_new(before, after, root=prepared.directory)
        versions[target] = _version(after)
        _sync(after.parent)
    if moves:
        parent = prepared.directory / next(iter(moves)).split('/', 1)[0]
        parent.rmdir()
        _sync(prepared.directory)
    return replace(prepared, versions=versions)


def _bind_remote(prepared, case_id, workspace_id, inventory, moves):
    prefix = prepared.draft['binding']['prefix']
    if remote_versions(prefix) != prepared.versions:
        raise SourceChanged('提交前远端恢复暂存已变化，请重试')
    client = storage.s3()
    if moves:
        catalog = client.public_snapshot(get_settings().s3_bucket, prefix)
        if catalog is None or {path: (entry['size'], entry['etag'].strip('"'))
                for path, entry in catalog.value['files'].items() if not entry.get('deleted')} != prepared.versions:
            raise SourceChanged('提交前远端恢复清单已变化，请重试')
    plan = case_provision.bind_remote_copy(prepared.draft, case_id, workspace_id, inventory, file_moves=moves)
    if moves:
        from botocore.exceptions import ClientError
        try:
            client.remap_unpublished(catalog, moves)
        except ClientError as error:
            if error.response.get('ResponseMetadata', {}).get('HTTPStatusCode') == 412:
                raise SourceChanged('提交期间远端恢复清单已变化，请重试') from error
            raise
    if remote_versions(prefix) != {moves.get(path, path): version for path, version in prepared.versions.items()}:
        raise SourceChanged('恢复成果目录与已校验清单不一致，请重试')
    return plan


async def publish_case(db, case, user_id, destinations, moves, prepared):
    inventory = {path: source.record for path, source in destinations.items()}
    if isinstance(prepared, case_copy_local.LocalCopy):
        local = await case_provision.io(_move_local_logs, prepared, moves) if moves else prepared
        await local.publish(db, case, user_id, inventory, {}, 'backup.restore')
    elif isinstance(prepared, case_copy_remote.RemoteCopy):
        plan = await case_provision.io(_bind_remote, prepared, case.id, case.workspace_id, inventory, moves)
        case.s3_prefix = plan['binding']['prefix']
        await case_provision.record_witness(db, case, plan)
    else:
        await case_provision.create_from_manifest(db, case, user_id, inventory, destinations.__getitem__, 'backup.restore')
