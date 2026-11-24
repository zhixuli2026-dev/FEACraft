"""Bound each terminal viewer without stopping its reconnectable command."""
import asyncio

MAX_OUTPUT_BYTES=1024*1024
MAX_OUTPUT_ITEMS=256
SEND_TIMEOUT=10
CLOSE_TIMEOUT=2


class OutputQueue(asyncio.Queue):
    def __init__(self):
        super().__init__(maxsize=MAX_OUTPUT_ITEMS)
        self.pending_bytes=0
        self.overflow=asyncio.Event()

    def put_nowait(self,item):
        if self.overflow.is_set():return
        size=len(item.encode('utf8')) if item is not None else 0
        if self.full() or self.pending_bytes+size>MAX_OUTPUT_BYTES:
            self.fail();return
        super().put_nowait(item)
        self.pending_bytes+=size

    def get_nowait(self):
        item=super().get_nowait()
        if item is not None:self.pending_bytes-=len(item.encode('utf8'))
        return item

    def fail(self):
        if self.overflow.is_set():return
        self.overflow.set()
        while not self.empty():self.get_nowait()
        # Wake a reader even if there will be no further terminal output.
        super().put_nowait(None)
