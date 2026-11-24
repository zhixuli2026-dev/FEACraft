"""Seekable download staging held through the final ASGI send."""
import asyncio
from contextlib import ExitStack
from tempfile import SpooledTemporaryFile

from .. import storage
from .async_ops import finish_before_cancel
from . import backup
from .node_storage_budget import reserve


class SpooledDownload:
    memory_limit = 1024 * 1024
    reservation_step = 4 * 1024 * 1024
    maximum = None

    def __init__(self):
        self._claims = ExitStack()
        self._stream = SpooledTemporaryFile(max_size=self.memory_limit, dir=storage._fs_root())
        self._reserved = 0

    def tell(self):
        return self._stream.tell()

    def seek(self, *args):
        return self._stream.seek(*args)

    def read(self, length=-1):
        return self._stream.read(length)

    def flush(self):
        self._stream.flush()

    def reserve_body(self, size):
        """Admit the entire known disk peak before the first body read."""
        if size > self.memory_limit:
            self._reserve(size)

    def _reserve(self, required):
        self._claims.enter_context(reserve(required - self._reserved))
        if not self._reserved:
            self._stream.rollover()
        self._reserved = required

    def write(self, data):
        end = self.tell() + len(data)
        if end > max(self.memory_limit, self._reserved):
            required = ((end - 1) // self.reservation_step + 1) * self.reservation_step
            if self.maximum is not None:
                required = min(self.maximum, required)
            self._reserve(required)
        return self._stream.write(data)

    async def chunks(self):
        while True:
            if self._reserved:
                chunk = await finish_before_cancel(asyncio.to_thread(self._stream.read, 65536))
            else:
                chunk = self._stream.read(65536)
            if not chunk:
                break
            yield chunk

    def close(self):
        try:
            self._stream.close()
        finally:
            self._claims.close()


class BackupDownload(SpooledDownload):
    @property
    def maximum(self):
        return backup.MAX_BACKUP_BYTES

    def write(self, data):
        if self.tell() + len(data) > self.maximum:
            raise backup.BackupError("备份超过 100 MiB")
        return super().write(data)
