"""Bounded duplex streams inside a retained process scope.

The two startup callbacks are integration points for durable ownership. This
transport is not an API session registry and must not be exposed as one.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time

from . import process_identity_native as identity
from .async_ops import finish_before_cancel
from .execution_budget import Budget
from .owned_process import OwnedProcess, wsl_path

BRIDGE = Path(__file__).with_name('stdio_bridge.py').resolve()
IDENTITY = Path(identity.__file__).resolve()
NATIVE_PROBE = Path(__file__).with_name('terminal_identity_probe.py').resolve()
NATIVE_STATE = Path(__file__).with_name('codex_wsl_state.py').resolve()
NATIVE_HANDOFF = Path(__file__).with_name('cli_native_handoff.py').resolve()
NATIVE_CGROUP = Path(__file__).with_name('process_cgroup_native.py').resolve()
CHUNK = 8192
MAX_LINE = 1024 * 1024
MAX_BUFFER = 2 * MAX_LINE


def packed(value):
    return (json.dumps(value, ensure_ascii=True, separators=(',', ':'), sort_keys=True) + '\n').encode('ascii')


class OwnedStdio:
    def __init__(self, argv, *, cwd=None, env=None, distro=None, native_state=None, native_scope=None,
                 lifetime_seconds=0, max_output_bytes=0, provider_token=None):
        if (not isinstance(argv, list) or not argv or len(argv) > 128
                or any(not isinstance(x, str) or not x or '\0' in x for x in argv)
                or Path(argv[0]).name.lower() == 'wsl.exe'):
            raise ValueError('Invalid host stdio command')
        self.argv = list(argv)
        if distro is not None and (os.name != 'nt' or not isinstance(distro, str)
                or not re.fullmatch('[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}', distro)
                or not PurePosixPath(argv[0]).is_absolute() or '\\' in argv[0]
                or argv[0].lower().endswith(('.exe', '.cmd', '.bat')) or cwd is None):
            raise ValueError('Invalid native WSL stdio selection')
        self.distro = distro
        self.cwd = str(Path(cwd).resolve()) if cwd is not None else None
        self.env = dict(env) if env is not None else None
        if provider_token is not None and (distro is None or not isinstance(provider_token, str)
                or not 1 <= len(provider_token) <= 4096 or any(not 33 <= ord(char) <= 126 for char in provider_token)):
            raise ValueError('Invalid native stdio provider credential')
        self._provider_token = provider_token
        self.owned = OwnedProcess()
        self.lifetime_seconds = lifetime_seconds
        self.max_output_bytes = max_output_bytes
        self.runtime_budget = None
        self.native_management=None
        if native_scope is not None:
            from .cli_native_handoff import selection
            if distro is None or native_state is None:raise ValueError('Managed CLI requires native private state')
            self.native_management=selection(native_scope)
            self.owned.nonce=self.native_management['scope']['nonce']
            self.owned.native_scope={'scope':copy.deepcopy(self.native_management['scope']),
                'helper_sha256':self.native_management['helpers']['process_cgroup_native.py']}
        self.profile = {'argv': self.argv, 'cwd': wsl_path(self.cwd) if distro is not None else self.cwd, 'nonce': self.owned.nonce}
        if self.native_management is not None:self.profile['native_management']=copy.deepcopy(self.native_management)
        self.native_state = copy.deepcopy(native_state)
        self.native_state_prepared = False
        if native_state is not None:
            from .codex_wsl_state import validate_selection
            if distro is None:
                raise ValueError('Native state initialization requires WSL')
            validate_selection(self.native_state)
            self.profile['native_state'] = self.native_state
        self.code = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.code_paths()}
        self.profile_sha256 = hashlib.sha256(packed(self.profile)).hexdigest()
        self.bridge_birth = None
        self.bridge_reported = None
        self.bridge_handle = None
        self.child_pid = None  # Diagnostic only. Never used as signal authority.
        self.exit_code = None
        self.stderr_tail = bytearray()
        self.buffer = bytearray()
        self.changed = asyncio.Event()
        self.failure = None
        self.eof = False
        self.pump = None
        self.sequence = 0
        self.acks = {}
        self.input_lock = asyncio.Lock()
        self.input_ended = False
        self.close_operation = None
        self.started = False

    @property
    def proc(self):
        return self.owned.proc

    @property
    def stop_result(self):
        return self.owned.stop_result

    def code_paths(self):
        paths = (BRIDGE, IDENTITY, NATIVE_PROBE) if self.distro is not None or os.name != 'nt' else (BRIDGE, IDENTITY)
        if self.native_state is not None:paths+=(NATIVE_STATE,)
        if self.native_management is not None:paths+=(NATIVE_HANDOFF,NATIVE_CGROUP)
        return paths

    def check_code(self):
        if self.code != {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in self.code_paths()}:
            raise ValueError('Stdio implementation changed after selection')
        if self.native_management is not None:
            if (self.profile.get('native_management')!=self.native_management
                    or self.profile_sha256!=hashlib.sha256(packed(self.profile)).hexdigest()
                    or self.owned.native_scope!={'scope':self.native_management['scope'],
                        'helper_sha256':self.native_management['helpers']['process_cgroup_native.py']}):
                raise ValueError('Managed CLI scope changed after selection')

    async def native_bridge(self):
        if self.owned.native is None or self.owned.native['distribution'] != self.distro:
            raise ValueError('Stdio native root differs from its selected distribution')
        root = self.owned.native['process']
        if self.native_management is not None:
            if root['uid']!=0 or self.bridge_reported['uid']!=65534:
                raise ValueError('Managed CLI root and bridge do not match their fixed privilege separation')
        elif root['uid'] == 0 or self.bridge_reported['uid'] != root['uid']:
            raise ValueError('Stdio native bridge must retain its non-root identity')
        from .control_identity import read
        return await read(self.distro,'ancestry',{'root':root,'reported':self.bridge_reported})

    async def verify_bridge(self):
        self.check_code()
        await self.owned.verify_registration()
        if self.distro is not None:
            if await self.native_bridge() != self.bridge_birth:
                raise ValueError('Stdio native bridge changed before release')
        elif self.bridge_handle is not None:
            if self.bridge_handle.identity() != self.bridge_birth or not self.owned.scope.contains(self.bridge_handle):
                raise ValueError('Stdio bridge ownership changed before release')
        else:
            from .terminal_identity_probe import locate
            birth = await asyncio.to_thread(locate, self.owned.record['host'], self.bridge_reported)
            if birth != self.bridge_birth:
                raise ValueError('Stdio bridge changed within its owned Linux scope')

    async def _send(self, value):
        if self.proc is None or self.proc.stdin is None:
            raise EOFError('Stdio transport is unavailable')
        frame = packed(value)
        envelope = {'action': 'terminal_io', 'nonce': self.owned.nonce,
                    'data': base64.b64encode(frame).decode('ascii')}
        self.proc.stdin.write(packed(envelope))
        await asyncio.wait_for(self.proc.stdin.drain(), 5)

    async def _receive(self):
        line = await self.proc.stdout.readline()
        if not line:
            raise EOFError('Stdio bridge ended')
        if len(line) > 32768 or not line.endswith(b'\n'):
            raise ValueError('Invalid stdio frame length')
        value = json.loads(line)
        if not isinstance(value, dict) or value.get('nonce') != self.owned.nonce:
            raise ValueError('Stdio frame belongs to another launch')
        return value

    async def _authorize_bridge(self, action, *, final):
        message = {'action': action, 'nonce': self.owned.nonce}
        if final and self._provider_token is not None:
            message['provider_token'] = self._provider_token
            self._provider_token = None
        await self._send(message)

    async def start(self, *, before_release=None, before_start=None, after_prepare=None):
        if self.started or self.owned.proc is not None or self.close_operation is not None:
            raise RuntimeError('Stdio transport can only start once')
        try:
            self.check_code()
            encoded = base64.b64encode(packed(self.profile)).decode('ascii')
            if len(encoded) > 24576:
                raise ValueError('Stdio launch profile exceeds its limit')
            argv = ([sys.executable, '-I', '-S', '-u', str(BRIDGE), encoded] if self.distro is None else
                    ['wsl.exe', '-d', self.distro, '--user', 'nobody', '--exec', '/usr/bin/python3', '-I', '-S', '-u',
                     wsl_path(BRIDGE), encoded, '--wsl-environment'])
            if self.native_management is not None:
                if self.owned.guardian is None or self.owned.on_native_preflight is None:
                    raise ValueError('Managed CLI requires a prepared guardian and durable native admission')
                argv=['wsl.exe','-d',self.distro,'--exec','/usr/bin/python3','-I','-S','-u',wsl_path(NATIVE_HANDOFF),encoded]
            await self.owned.prepare(argv, cwd=self.cwd, env=self.env,
                                     runtime='windows' if self.distro is None else 'wsl', terminal_io=True)
            await self.owned.verify_registration()
            if before_release is not None:
                await before_release(copy.deepcopy(self.owned.record))
            self.check_code()
            await self.owned.verify_registration()
            await self.owned.release()
            value = await asyncio.wait_for(self._receive(), 15)
            if (set(value) != {'type', 'nonce', 'identity', 'profile_sha256'}
                    or value['type'] != 'gated' or value['profile_sha256'] != self.profile_sha256):
                raise ValueError('Stdio bridge crosses its launch profile')
            self.bridge_reported = identity.validate(value['identity'])
            self.bridge_birth = self.bridge_reported
            if self.distro is not None:
                self.bridge_birth = await self.native_bridge()
            elif os.name == 'nt':
                from .windows_process_scope import ProcessHandle
                self.bridge_handle = await asyncio.to_thread(ProcessHandle.query, self.bridge_birth)
                if not self.owned.scope.contains(self.bridge_handle):
                    raise ValueError('Stdio bridge escaped its owned scope')
            else:
                # The Linux gate runs the bridge inside its private PID namespace.
                # Resolve that reported identity through the retained root's live
                # descendants; its inner PID is not a PID in the API namespace.
                from .terminal_identity_probe import locate
                self.bridge_birth = await asyncio.to_thread(locate, self.owned.record['host'], self.bridge_reported)
            if before_start is not None:
                await before_start(copy.deepcopy(self.bridge_birth))
            await self.verify_bridge()
            await self._authorize_bridge('release', final=self.native_state is None)
            value = await asyncio.wait_for(self._receive(), 15)
            if self.native_state is not None:
                from .codex_wsl_state import home_path
                expected = {'type': 'prepared', 'nonce': self.owned.nonce,
                            'home': home_path(self.native_state), 'uid': 65534}
                if value != expected:
                    raise ValueError('Native private state differs from its selected account')
                self.native_state_prepared = True
                if after_prepare is not None:
                    await after_prepare()
                await self.verify_bridge()
                await self._authorize_bridge('launch', final=True)
                value = await asyncio.wait_for(self._receive(), 15)
            if (set(value) != {'type', 'nonce', 'pid'} or value['type'] != 'started'
                    or type(value['pid']) is not int or value['pid'] <= 0):
                raise ValueError('Stdio child did not start')
            self.child_pid = value['pid']
            self.started = True
            if self.lifetime_seconds or self.max_output_bytes:
                deadline = time.monotonic() + self.lifetime_seconds if self.lifetime_seconds else float('inf')
                self.runtime_budget = Budget(self.owned.nonce, deadline, self.max_output_bytes)
            self.pump = asyncio.create_task(self._pump())
            return self
        except BaseException:
            await finish_before_cancel(self.close('executor_error'))
            raise

    def _fail(self, error):
        if self.failure is None:
            self.failure = error
        self.changed.set()
        for waiter in tuple(self.acks.values()):
            if not waiter.done():
                waiter.set_exception(error)

    @property
    def resource_limit_reason(self):
        return self.runtime_budget.violation if self.runtime_budget is not None else None

    async def _receive_runtime_frame(self):
        budget = self.runtime_budget
        if budget is None or not self.lifetime_seconds or self.close_operation is not None:
            return await self._receive()
        budget.check()
        timer = asyncio.timeout_at(budget.deadline)
        try:
            async with timer:
                value = await self._receive()
        except TimeoutError:
            if timer.expired() and self.close_operation is None:
                budget.trip('timeout', 'deadline')
            raise
        if self.close_operation is None:
            budget.check()
        return value

    async def _pump(self):
        try:
            while True:
                value = await self._receive_runtime_frame()
                kind = value.get('type')
                if kind in {'stdout', 'stderr'}:
                    if set(value) != {'type', 'nonce', 'data'}:
                        raise ValueError('Invalid stdio data frame')
                    data = base64.b64decode(value['data'], validate=True)
                    if not data or len(data) > CHUNK:
                        raise ValueError('Oversized stdio output chunk')
                    if self.max_output_bytes and self.close_operation is None:
                        self.runtime_budget.consume_output(len(data), 'root')
                        self.runtime_budget.check()
                    if kind == 'stdout':
                        if len(self.buffer) + len(data) > MAX_BUFFER:
                            raise ValueError('Stdio output buffer exceeded its limit')
                        self.buffer.extend(data)
                        self.changed.set()
                    else:
                        self.stderr_tail.extend(data)
                        del self.stderr_tail[:-16384]
                elif kind == 'ack':
                    identifier = value.get('id')
                    if set(value) != {'type', 'nonce', 'id'} or type(identifier) is not int or identifier not in self.acks:
                        raise ValueError('Unexpected stdio input acknowledgment')
                    waiter = self.acks[identifier]
                    if waiter.done():
                        raise ValueError('Duplicate stdio input acknowledgment')
                    waiter.set_result(None)
                elif kind == 'exit':
                    if set(value) != {'type', 'nonce', 'exit_code'} or type(value['exit_code']) is not int:
                        raise ValueError('Invalid stdio exit frame')
                    self.exit_code = value['exit_code']
                    return
                elif kind == 'input_closed':
                    raise EOFError('Native stdio input closed')
                else:
                    raise ValueError('Unknown stdio frame')
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail(error)
            # Gate controls remain available even when native stdin blocks.
            await finish_before_cancel(self.owned.stop(reason=self.resource_limit_reason or 'executor_error', grace_ms=300))
        finally:
            self.eof = True
            self.changed.set()
            for waiter in tuple(self.acks.values()):
                if not waiter.done():
                    waiter.set_exception(EOFError('Stdio input channel ended'))

    async def write(self, data):
        if not self.started or self.close_operation is not None:
            raise EOFError('Stdio transport is closed')
        async with self.input_lock:
            if self.input_ended:
                raise EOFError('Stdio input has ended')
            for offset in range(0, len(data), CHUNK):
                if self.failure is not None:
                    raise self.failure
                if self.eof:
                    raise EOFError('Stdio input channel ended')
                self.sequence += 1
                identifier = self.sequence
                waiter = asyncio.get_running_loop().create_future()
                waiter.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
                self.acks[identifier] = waiter
                try:
                    await self._send({'action': 'input', 'id': identifier,
                                      'data': base64.b64encode(data[offset:offset + CHUNK]).decode('ascii')})
                    await waiter
                finally:
                    # On cancellation the reader can still receive this ack.
                    # Closing the entire transport prevents a partial RPC from
                    # being reused by another request.
                    self.acks.pop(identifier, None)
                    if not waiter.done():
                        waiter.cancel()

    async def finish_input(self):
        """Offer EOF so a cooperative CLI can flush and release its own files."""
        if not self.started or self.eof or self.close_operation is not None:
            return
        with contextlib.suppress(TimeoutError, EOFError, OSError):
            async with asyncio.timeout(3):
                async with self.input_lock:
                    if not self.input_ended and not self.eof:
                        self.input_ended = True
                        await self._send({'action':'end_input'})
                if self.pump is not None:
                    await asyncio.shield(self.pump)

    async def readline(self):
        while True:
            if self.failure is not None:
                raise self.failure
            end = self.buffer.find(b'\n')
            if end >= 0:
                if end + 1 > MAX_LINE:
                    raise ValueError('JSON-RPC response exceeds 1 MiB')
                result = bytes(self.buffer[:end + 1])
                del self.buffer[:end + 1]
                return result
            if len(self.buffer) > MAX_LINE:
                raise ValueError('JSON-RPC response exceeds 1 MiB')
            if self.eof:
                if self.buffer:
                    raise ValueError('Truncated JSON-RPC response')
                raise EOFError('Stdio transport ended')
            self.changed.clear()
            await self.changed.wait()

    async def close(self, reason='cancelled'):
        if self.close_operation is None:
            if self.runtime_budget is not None and self.runtime_budget.remaining() <= 0:
                self.runtime_budget.trip('timeout', 'deadline')
            self.close_operation = asyncio.create_task(self._close(reason))
        return await finish_before_cancel(self.close_operation)

    async def _close(self, reason):
        try:
            self._provider_token = None
            self._fail(EOFError('Stdio transport closed'))
            if self.owned.proc is not None:
                await finish_before_cancel(self.owned.stop(reason=self.resource_limit_reason or reason, grace_ms=500))
            elif self.owned.guardian is not None:
                await finish_before_cancel(self.owned.abort())
            if self.pump is not None:
                if not self.pump.done():
                    self.pump.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.pump
        finally:
            if self.bridge_handle is not None:
                self.bridge_handle.close()
                self.bridge_handle = None
            self.owned.close()
            self.eof = True
            self.changed.set()
        if self.owned.stop_result is not None and self.owned.stop_result['verified'] is not True:
            raise RuntimeError('Stdio scope exit could not be verified')
        return self.stop_result
