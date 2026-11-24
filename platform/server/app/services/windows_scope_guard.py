"""Retain a fixed guardian's actual creation handle and transfer one empty Job."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import time

from . import windows_scope_guardian as protocol, process_identity_native as identity
from .windows_process_scope import ProcessHandle, _kernel, _require


class Guardian:
    def __init__(self, scope, root, *, max_seconds=360):
        if scope.handle is None or scope.observer is not None or scope.active_processes() != 0:
            raise ValueError('Guardian requires a fresh, unobserved, empty Job')
        root = Path(root).resolve()
        root.mkdir(exist_ok=False)
        info = root.stat()
        self.request = {'schema': protocol.SCHEMA, 'nonce': scope.name.removeprefix('Local\\VibeFlow-'),
            'owner': identity.observe(os.getpid()), 'root': {'path': str(root), 'device': info.st_dev, 'inode': info.st_ino},
            'max_seconds': max_seconds, 'helpers': {name: hashlib.sha256(Path(protocol.__file__).with_name(name).read_bytes()).hexdigest()
                                                  for name in protocol.FILES}}
        protocol.validate(self.request)
        if scope.name != 'Local\\VibeFlow-' + self.request['nonce']:
            raise ValueError('Guardian selection differs from its created Job')
        self.scope = scope
        self.proc = None
        self.handle = None
        self.prepared = None
        self.receipt = None
        self.on_created = None
        path = root / 'request.json'
        with path.open('x', encoding='utf8') as stream:
            json.dump(self.request, stream)
            stream.flush()
            os.fsync(stream.fileno())
        self.path = path

    def send(self, value):
        raw = (json.dumps(value) + '\n').encode()
        if len(raw) > protocol.LIMIT:
            raise ValueError('Guardian control exceeds bound')
        self.proc.stdin.write(raw)
        self.proc.stdin.flush()

    def _frame(self, timeout):
        kernel = _kernel()
        kernel.PeekNamedPipe.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.PeekNamedPipe.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(self.proc.stdout.fileno())
        pending = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            available = wintypes.DWORD()
            if not kernel.PeekNamedPipe(handle, None, 0, None, ctypes.byref(available), None):
                raise ValueError('Guardian ended without preparation')
            if available.value:
                pending.extend(os.read(self.proc.stdout.fileno(), min(available.value, protocol.LIMIT + 1 - len(pending))))
                if len(pending) > protocol.LIMIT:
                    raise ValueError('Guardian preparation exceeds bound')
                if b'\n' in pending:
                    return json.loads(pending)
            elif self.proc.poll() is not None:
                raise ValueError('Guardian exited before preparation')
            time.sleep(.01)
        raise TimeoutError('Guardian did not prepare before deadline')

    def start(self):
        if self.proc is not None:
            raise ValueError('Guardian cannot start twice')
        protocol.validate(self.request)
        base = Path(getattr(sys, '_base_executable', sys.executable)).resolve()
        self.proc = subprocess.Popen([str(base), '-I', '-S', '-u', str(Path(protocol.__file__).resolve()),
            '--request', str(self.path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            close_fds=True, bufsize=0, creationflags=subprocess.CREATE_NO_WINDOW)
        self.handle = ProcessHandle(int(self.proc._handle))
        try:
            if self.on_created is not None:self.on_created(self.handle.identity())
            kernel = _kernel()
            transferred = wintypes.HANDLE()
            _require(kernel.DuplicateHandle(kernel.GetCurrentProcess(), self.scope.handle, int(self.proc._handle),
                ctypes.byref(transferred), 0, False, 2))
            self.send({'action': 'attach', 'handle': transferred.value, 'request_sha256': protocol.digest(self.request)})
            prepared = protocol.validate_prepared(self.request, self._frame(10))
            if self.handle.identity() != prepared['guardian'] or protocol.read(self.request, 'prepared.json') != prepared:
                raise ValueError('Guardian frame differs from actual creation or durable preparation')
            self.prepared = prepared
            return prepared
        except BaseException:
            self.close()
            raise

    def wait(self, timeout=15):
        if self.proc is None or self.prepared is None:
            raise ValueError('Guardian was not prepared')
        code = self.proc.wait(timeout)
        self.handle.wait_exit(0)
        if self.handle.identity(allow_exited=True) != self.prepared['guardian']:
            raise ValueError('Guardian creation handle changed')
        if code != 0:
            diagnostic = self.proc.stderr.read(protocol.LIMIT).decode('utf8', 'replace')
            (self.path.parent / 'failure-stderr.txt').write_text(diagnostic, encoding='utf8')
            raise ValueError('Guardian failed to prove complete scope closure; exit=' + str(code))
        self.receipt = protocol.validate_receipt(self.request, self.prepared, protocol.read(self.request, 'receipt.json'))
        return {'request': self.request, 'preparation': self.prepared, 'receipt': self.receipt,
                'guardian_exit': {'state': 'exited', 'reason': 'retained_creation_handle'}}

    def retire(self):
        if self.proc is None:
            raise ValueError('Guardian was not started')
        if self.proc.poll() is None:
            self.send({'action': 'retire', 'nonce': self.request['nonce']})
        return self.wait()

    def close(self):
        if self.proc is not None:
            if self.proc.stdin is not None and not self.proc.stdin.closed:
                self.proc.stdin.close()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                # Exact creation handle only; a forced guardian exit is never proof.
                self.proc.kill()
                self.proc.wait(5)
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream is not None:
                    stream.close()
        if self.handle is not None:
            self.handle.close()
            self.handle = None
