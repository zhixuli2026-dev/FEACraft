"""Test-owned file barriers for deterministic cross-process failure windows.

Only import in tests or a temporary test application. Production routes have no
fault switches. A barrier records arrival and blocks until explicitly released,
with a hard timeout so a broken test cannot hang a compiler/worker indefinitely.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import re
import time


class FileBarrier:
    def __init__(self, root: Path, name: str, timeout=30):
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
            raise ValueError("Barrier names must be simple identifiers")
        self.root, self.name, self.timeout = Path(root), name, timeout
        self.ready = self.root / (name + ".ready")
        self.released = self.root / (name + ".release")

    def arrive(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.ready.write_text("ready", encoding="ascii")

    def release(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.released.write_text("release", encoding="ascii")

    def wait(self):
        self.arrive()
        deadline = time.monotonic() + self.timeout
        while not self.released.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Fault barrier timed out: " + self.name)
            time.sleep(0.01)

    async def wait_async(self):
        self.arrive()
        deadline = time.monotonic() + self.timeout
        while not self.released.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("Fault barrier timed out: " + self.name)
            await asyncio.sleep(0.01)


@contextmanager
def intercept(target, name, barrier: FileBarrier, *, after=False, asynchronous=True):
    """Wrap a real operation in the test process; always restore its implementation."""
    original = getattr(target, name)
    if asynchronous:
        @wraps(original)
        async def wrapped(*args, **kwargs):
            if not after:
                await barrier.wait_async()
            result = await original(*args, **kwargs)
            if after:
                await barrier.wait_async()
            return result
    else:
        @wraps(original)
        def wrapped(*args, **kwargs):
            if not after:
                barrier.wait()
            result = original(*args, **kwargs)
            if after:
                barrier.wait()
            return result
    setattr(target, name, wrapped)
    try:
        yield
    finally:
        setattr(target, name, original)
