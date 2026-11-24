"""Bounded file listing and pinned reads for the Agent tools."""
import codecs
from contextlib import ExitStack, closing
from contextvars import ContextVar
from heapq import merge
import hashlib
import os
from pathlib import PurePosixPath
import re
import stat

from .. import storage
from ..config import get_settings
from ..storage_paths import is_file_stage
from .case_write import normalize_path
from .file_read import FileRead
from .request_deadline import current
from .storage_listing_budget import ListingBudget

stop_signal = ContextVar('agent_file_read_stop', default=None)
SCAN_BYTES = 32 * 1024 * 1024
FILE_BYTES = 5_000_000
PROPOSAL_SNAPSHOT_BYTES = 256 * 1024
PROPOSAL_HEADER_BYTES = 8192
HEAVY = {'.cache', 'node_modules', '__pycache__', 'build', 'build-win',
         'build-deb', 'build-linux-package', 'cmake'}
_LINE_BREAK = re.compile(r'\r\n|[\n\r\v\f\x1c-\x1e\x85\u2028\u2029]')


def _check():
    stop = stop_signal.get()
    if stop is not None and stop.is_set():
        raise ValueError('文件读取已取消')
    deadline = current()
    if deadline is not None:
        deadline.check()


class Scan(ListingBudget):
    def check(self):
        _check()
        super().check()


def _excluded(path, internal):
    parts = [part.rstrip(' .').casefold() for part in PurePosixPath(path).parts]
    return (any(part in HEAVY or is_file_stage(part) for part in parts)
            or ('.vibeflow' in parts and not (internal and parts[0] == '.vibeflow'
                                             and '.vibeflow' not in parts[1:]))
            or parts[:2] == ['.vibeflow', 'live-store'])


def _local(prefix, directory, internal, budget):
    from .storage_usage import _ordinary_info

    root = storage._fs_path(prefix, '.')
    start = storage._fs_path(prefix, directory or '.')
    real = start.relative_to(root).as_posix()
    if _excluded(real, internal) or not start.is_dir():
        return

    def children_at(base, relative):
        budget.check()
        children = []
        with os.scandir(base) as entries:
            for entry in entries:
                budget.visit()
                name = relative + '/' + entry.name if relative else entry.name
                if _excluded(name, internal):
                    continue
                info = _ordinary_info(entry.stat(follow_symlinks=False))
                is_dir = stat.S_ISDIR(info.st_mode)
                children.append((name + '/' if is_dir else name, entry.path, info, is_dir))
        return iter(sorted(children))

    pending = [children_at(start, '' if real == '.' else real)]
    while pending:
        budget.check()
        entry = next(pending[-1], None)
        if entry is None:
            pending.pop()
            continue
        name, path, info, is_dir = entry
        if is_dir:
            pending.append(children_at(path, name[:-1]))
        else:
            yield name, info.st_size, None


def _raw_files(client, bucket, prefix, directory, internal, budget):
    root = prefix.rstrip('/') + '/'

    def children_at(relative):
        stem = root + (relative + '/' if relative else '')
        pages = iter(client.get_paginator('list_objects_v2').paginate(
            Bucket=bucket, Prefix=stem, Delimiter='/', PaginationConfig={'PageSize':128}))
        while True:
            budget.check()
            page = next(pages, None)
            if page is None:
                break
            budget.check()
            candidates = [(item['Prefix'], None) for item in page.get('CommonPrefixes', [])]
            candidates += [(item['Key'], item) for item in page.get('Contents', [])]
            for key, item in sorted(candidates, key=lambda pair: pair[0]):
                budget.visit()
                if not key.startswith(stem):
                    raise ValueError('对象列表超出了选定目录')
                if key == stem or (item is not None and key.endswith('/')):
                    continue  # Explicit empty directory marker.
                if '/' in key[len(stem):].rstrip('/'):
                    raise ValueError('对象列表缺少所请求的目录分隔符')
                name = key[len(root):].rstrip('/')
                if normalize_path(name) != name or _excluded(name, internal):
                    continue
                yield name, key, item

    pending = [children_at(directory)]
    try:
        while pending:
            budget.check()
            entry = next(pending[-1], None)
            if entry is None:
                pending.pop()
                continue
            name, key, item = entry
            if item is None:
                pending.append(children_at(name))
            else:
                yield name, item['Size'], (client, bucket, key, item.get('ETag'))
    finally:
        for children in pending:
            children.close()


def _remote(prefix, directory, internal, budget):
    from .s3_live_store import PRIVATE, managed, split, visible

    client = storage.s3()
    bucket = get_settings().s3_bucket
    prefix = prefix.rstrip('/')
    snapshot = (client.public_snapshot(bucket, prefix, check=budget.check)
                if split(prefix+'/') == (prefix, '') else None)
    if snapshot is None:
        yield from _raw_files(client.raw, bucket, prefix, directory, internal, budget)
        return

    def catalog():
        stem = directory + '/' if directory else ''
        for name in sorted(snapshot.value['files']):
            budget.visit()
            if not name.startswith(stem):
                continue
            entry = snapshot.value['files'][name]
            if not visible(entry) or _excluded(name, internal):
                continue
            yield name, entry['size'], (client.raw, bucket, entry['key'], entry['physical_etag'])

    if not internal:
        yield from catalog()
        return
    roots = []
    if directory and not managed(directory):
        roots = [directory]
    elif directory in {'', '.vibeflow'}:
        roots = ['.vibeflow/'+kind for kind in PRIVATE if kind != 'live-store']
    with ExitStack() as stack:
        streams = [stack.enter_context(closing(catalog()))]
        for root in roots:
            streams.append(stack.enter_context(closing(_raw_files(client.raw, bucket, prefix, root, internal, budget))))
        yield from merge(*streams, key=lambda row: row[0])


def _entries(prefix, subdir, internal):
    directory = '' if subdir in {'', '.'} else normalize_path(subdir)
    if not _excluded(directory, internal):
        budget = Scan()
        scan = _remote if storage._use_s3() else _local
        yield from scan(prefix, directory, internal, budget)


def _bytes(reader, start=None, end=None):
    with closing(reader.chunks(start, end)) as chunks:
        blocks = []
        while True:
            _check()
            block = next(chunks, None)
            if block is None:
                break
            blocks.append(block)
    _check()
    return b''.join(blocks)


def proposal_hash(prefix, path):
    digest, _ = proposal_source(prefix, path, snapshot=False)
    return digest


class ReplacementScan:
    """Check the same pinned bytes used for the proposal hash."""

    def __init__(self, old: bytes, new: bytes, replace_all: bool):
        self.old = old
        self.new_size = len(new)
        self.replace_all = replace_all
        self.decoder = codecs.getincrementaldecoder('utf8')()
        self.header = bytearray()
        self.pending = bytearray()
        self.batch_size = max(64 * 1024, 2 * len(old))
        self.count = 0
        self.binary = False
        self.error = None

    def feed(self, block):
        if self.binary:
            return
        self.header.extend(block[:max(0, PROPOSAL_HEADER_BYTES - len(self.header))])
        try:
            self.decoder.decode(block)
        except UnicodeDecodeError:
            self.binary = True
        if self.binary or b'\x00' in block:
            self.binary = True
            return
        self.pending.extend(block)
        if len(self.pending) >= self.batch_size:
            self._count_matches()

    def _count_matches(self):
        parts = self.pending.split(self.old)
        self.count += len(parts) - 1
        # Retain only a prefix match that could finish in the next block.
        self.pending = parts[-1][max(0, len(parts[-1]) - len(self.old) + 1):]

    def finish(self, path, size):
        from ..routers.files import _is_text_path
        from .file_transactions import MAX_BYTES
        from .openfoam_editor import declared_format, wants_header

        if size is None:
            self.error = ('replacement_file_missing', '目标文件不存在，请先核对文件路径')
            return
        try:
            self.decoder.decode(b'', final=True)
        except UnicodeDecodeError:
            self.binary = True
        if self.binary or (wants_header(path, _is_text_path(path)) and declared_format(self.header) == 'binary'):
            self.error = ('replacement_binary_file', '目标文件无法作为 UTF-8 文本替换')
            return
        self._count_matches()
        if not self.count:
            self.error = ('replacement_text_missing', '文件中找不到待替换的原文，请先读取并核对文件')
        elif self.count > 1 and not self.replace_all:
            self.error = ('replacement_text_ambiguous', f'原文出现 {self.count} 次，请扩大匹配范围或明确要求全部替换')
        elif size + self.count * (self.new_size - len(self.old)) > MAX_BYTES:
            self.error = ('replacement_size_limit', '替换后的正文超过文件事务大小上限')


def proposal_source(prefix, path, *, snapshot, replacement=None):
    from .attempt_workspace import assert_mutable_path
    from .file_transactions import MAX_BYTES

    path = normalize_path(path)
    assert_mutable_path(prefix, path)
    if not storage._use_s3():
        try:
            mode = storage._fs_path(prefix, path).stat().st_mode
        except FileNotFoundError:
            if replacement is not None:
                replacement.finish(path, None)
            return None, {'kind': 'text', 'before': ''} if snapshot else None
        if not stat.S_ISREG(mode):
            raise ValueError('提案目标须为普通文件')
    _check()
    try:
        selected = FileRead(prefix, path)
    except FileNotFoundError:
        if replacement is not None:
            replacement.finish(path, None)
        return None, {'kind': 'text', 'before': ''} if snapshot else None
    if selected.size > MAX_BYTES:
        raise ValueError('目标文件超过文件事务大小上限')
    digest = hashlib.sha256()
    previous = bytearray() if snapshot and selected.size <= PROPOSAL_SNAPSHOT_BYTES else None
    # A pinned zero-byte S3 selection already identifies its entire body.
    if not selected.remote or selected.size:
        with closing(selected.chunks()) as blocks:
            while True:
                _check()
                block = next(blocks, None)
                if block is None:
                    break
                digest.update(block)
                if previous is not None:
                    previous.extend(block)
                if replacement is not None:
                    replacement.feed(block)
    _check()
    if replacement is not None:
        replacement.finish(path, selected.size)
    if not snapshot:
        return digest.hexdigest(), None
    if previous is None:
        return digest.hexdigest(), {'kind': 'limit', 'size': selected.size}

    from ..routers.files import _is_text_path
    from .openfoam_editor import declared_format, wants_header

    ordinary_text = _is_text_path(path)
    declared = declared_format(previous) if wants_header(path, ordinary_text) else None
    if declared == 'ascii' or ordinary_text and declared != 'binary':
        return digest.hexdigest(), {'kind': 'text', 'before': previous.decode('utf8', 'replace')}
    return digest.hexdigest(), {'kind': 'binary', 'size': selected.size}


def read_text(prefix, path):
    path = normalize_path(path)
    if any(is_file_stage(part) for part in PurePosixPath(path).parts):
        raise ValueError('文件暂存目录为内部路径')
    _check()
    try:
        reader = FileRead(prefix, path)
    except FileNotFoundError as error:
        raise FileNotFoundError(f'文件不存在: {path}') from error
    amount = min(reader.size, 200_004)
    if reader.remote and not amount:
        from .attempt_objects import selected_stream
        with selected_stream(prefix, path, etag=reader.etag, size=0) as chunks:
            _check()
            if next(chunks, None) is not None:
                raise ValueError('空文件的对象响应包含正文')
            _check()
        return ''
    data = _bytes(reader, 0, amount - 1) if amount < reader.size else _bytes(reader)
    text = data.decode('utf-8', 'replace')
    if len(text) > 50_000 or amount < reader.size:
        return text[:50_000] + f'\n...[截断，共 {reader.size} 字节]'
    return text


def list_files(prefix, subdir, internal):
    lines = []
    with closing(_entries(prefix, subdir, internal)) as files:
        for name, size, _ in files:
            if len(lines) == 500:
                return '\n'.join(lines) + '\n...[超过 500 条截断，用 subdir 缩小范围]'
            lines.append(f'{size:>10}  {name}')
    return '\n'.join(lines) if lines else '(空目录)'


def _lines(text):
    start = 0
    for boundary in _LINE_BREAK.finditer(text):
        yield text[start:boundary.start()]
        start = boundary.end()
    if start < len(text):
        yield text[start:]


def search_text(prefix, query, subdir):
    needle = query.lower()
    hits, used = [], 0
    with closing(_entries(prefix, subdir, False)) as files:
        for name, size, remote in files:
            _check()
            if not 0 < size <= FILE_BYTES:
                continue
            reader = FileRead(prefix, name) if remote is None else FileRead.from_version(*remote, size)
            if reader.size > FILE_BYTES:
                continue
            if used + reader.size > SCAN_BYTES:
                return '\n'.join([*hits, '...[达到 32 MiB 正文扫描上限，用 subdir 缩小范围]'])
            used += reader.size
            for number, line in enumerate(_lines(_bytes(reader).decode('utf8', 'replace')), 1):
                if number % 256 == 1:
                    _check()
                if needle in line.lower():
                    if len(hits) == 50:
                        return '\n'.join([*hits, '...[超过 50 条截断]'])
                    hits.append(f'{name}:{number}: {line.strip()[:160]}')
    return '\n'.join(hits) if hits else '(无匹配)'
