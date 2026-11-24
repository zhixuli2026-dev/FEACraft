"""Bounded, lock-free preparation for uploaded geometry conversions."""
import asyncio
from contextlib import contextmanager
import hashlib
import io
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore

from fastapi import HTTPException

from .. import storage
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict
from .file_transactions import MAX_BYTES
from .node_storage_budget import reserve
from .request_deadline import current


INPUT_LIMIT = 16 * 1024 * 1024
_slots = BoundedSemaphore(2)


@contextmanager
def admission():
    if not _slots.acquire(blocking=False):
        raise HTTPException(429, {'code': 'geometry_conversion_busy', 'message': '几何转换繁忙，请稍后重试'},
                            headers={'Retry-After': '1'})
    try:
        yield
    finally:
        _slots.release()


def check_time():
    deadline = current()
    if deadline is not None:
        deadline.check()


def _prepare(reader, path, convert, produces_file):
    root = storage._fs_root()
    output_limit = MAX_BYTES if produces_file else 0
    with reserve(reader.size + output_limit):
        check_time()
        with TemporaryDirectory(dir=root, prefix='.geometry-conversion-') as folder:
            source = Path(folder) / ('input' + PurePosixPath(path).suffix.lower())
            digest = hashlib.sha256()
            address = hashlib.sha256(path.encode('utf8') + b'\0')
            with source.open('wb') as output:
                for block in reader.chunks():
                    output.write(block)
                    digest.update(block)
                    address.update(block)
            check_time()
            result = convert(source)
            check_time()
            return result, digest.hexdigest(), address.hexdigest()


async def prepare(reader, path, convert, *, produces_file=False):
    try:
        return await finish_before_cancel(asyncio.to_thread(_prepare, reader, path, convert, produces_file))
    except Exception:
        check_time()
        raise


def output_bytes(path):
    size = path.stat().st_size
    if size > MAX_BYTES:
        raise CaseWriteConflict('geometry_output_limit', '几何转换产物超过文件事务大小限制')
    with path.open('rb') as stream, io.BytesIO() as payload:
        while block := stream.read(64 * 1024):
            check_time()
            if payload.tell() + len(block) > size:
                raise OSError('Geometry conversion output grew during read')
            payload.write(block)
        if payload.tell() != size:
            raise OSError('Geometry conversion output changed during read')
        return payload.getvalue()
