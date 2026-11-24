"""Bound complete listings before callers allocate another inventory from them."""
from contextlib import contextmanager
import time

from ..config import get_settings
from .case_write import CaseWriteConflict
from .request_deadline import current


class ListingBudget:
    def __init__(self):
        settings = get_settings()
        self.maximum = settings.storage_measurement_max_entries
        self.deadline = time.monotonic() + settings.storage_measurement_timeout_seconds
        self.parent = current()
        self.entries = 0
        self.check()

    def check(self):
        if self.parent is not None:
            self.parent.check()
        if time.monotonic() >= self.deadline:
            raise CaseWriteConflict('storage_listing_timeout', '文件列表扫描超时，请缩小目录范围后重试')

    def visit(self):
        self.check()
        self.entries += 1
        if self.entries > self.maximum:
            raise CaseWriteConflict('storage_listing_limit', '文件列表扫描数量超过上限，请缩小目录范围后重试')

    @contextmanager
    def processing_entry(self):
        """Leave downstream work on the original request deadline."""
        started = time.monotonic()
        try:
            yield
        finally:
            self.deadline += time.monotonic() - started
