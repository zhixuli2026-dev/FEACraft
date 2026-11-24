"""Claude's native stream-json protocol over the existing owned process scope.

The caller supplies durable registration callbacks and persists native events.
This client is not itself a public session registry or a Codex RPC emulator.
"""
import asyncio
import contextlib
import copy
import json
import math
from pathlib import Path
import uuid

from .async_ops import finish_before_cancel
from .owned_stdio import MAX_LINE, OwnedStdio


def command(program, model, permission_mode, *, resume=None, fork=False, tools=None):
    program = Path(program)
    if not program.is_absolute() or not program.is_file() or program.suffix.lower() in {'.cmd', '.bat'}:
        raise ValueError('Claude stream-json requires an installed native executable')
    if not isinstance(model, str) or not model or len(model) > 120 or any(ord(c) < 32 for c in model):
        raise ValueError('Invalid Claude model')
    modes = {'approve_writes': 'manual', 'ask': 'manual', 'full_access': 'bypassPermissions', 'plan': 'plan'}
    if permission_mode not in modes or type(fork) is not bool or (fork and resume is None):
        raise ValueError('Invalid Claude permission or fork selection')
    argv = [str(program), '--print', '--input-format', 'stream-json', '--output-format', 'stream-json',
            '--verbose', '--include-partial-messages', '--permission-prompt-tool', 'stdio',
            '--model', model, '--permission-mode', modes[permission_mode]]
    if permission_mode == 'full_access':
        argv.append('--allow-dangerously-skip-permissions')
    if resume is not None:
        if not isinstance(resume, str) or str(uuid.UUID(resume)) != resume:
            raise ValueError('Resume requires an exact Claude session UUID')
        argv.append('--resume=' + resume)
    if fork:
        argv.append('--fork-session')
    if tools is not None:
        if not isinstance(tools, list) or not tools or any(not isinstance(t, str) or not t.isidentifier() for t in tools):
            raise ValueError('Invalid Claude tool selection')
        argv.extend(['--tools', ','.join(tools)])
    return argv


class ClaudeStdioClient:
    MAX_EVENTS = 256
    MAX_EVENT_BYTES = 4 * 1024 * 1024

    def __init__(self, argv, env, *, cwd, before_release=None, before_start=None):
        home = env.get('CLAUDE_CONFIG_DIR')
        if not isinstance(home, str) or not Path(home).is_absolute() or not Path(home).is_dir():
            raise ValueError('Claude requires an existing private configuration directory')
        if Path(home).resolve() == (Path.home() / '.claude').resolve():
            raise ValueError('Platform Claude must not use the personal configuration directory')
        self.transport = OwnedStdio(argv, env=env, cwd=cwd)
        self.before_release, self.before_start = before_release, before_start
        self.initialize_result = None
        self.session_id = None
        self.active_message = None
        self._reader = None
        self._closing = None
        self._failure = None
        self._pending = {}
        self._permissions = {}
        self._events = asyncio.Queue(maxsize=self.MAX_EVENTS)
        self._event_bytes = 0
        self._changed = asyncio.Event()
        self._write_lock = asyncio.Lock()

    @property
    def stop_result(self):
        return self.transport.stop_result

    def _fail(self, error):
        if self._failure is None:
            self._failure = error
        for future in self._pending.values():
            if not future.done():
                future.set_exception(self._failure)
        self._changed.set()

    async def _write(self, value):
        data = (json.dumps(value, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
        if len(data) > MAX_LINE:
            raise ValueError('Claude input exceeds the existing stream line limit')
        if self._failure is not None:
            raise self._failure
        async with self._write_lock:
            await self.transport.write(data)

    async def control(self, request, timeout=30):
        if self._reader is None or self._closing is not None:
            raise RuntimeError('Claude client is not running')
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or not 0 < timeout <= 120):
            raise ValueError('Invalid Claude control timeout')
        if not isinstance(request, dict) or not isinstance(request.get('subtype'), str) or len(self._pending) >= 64:
            raise ValueError('Invalid or excessive Claude control request')
        identifier = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        self._pending[identifier] = future
        try:
            async with asyncio.timeout(timeout):
                await self._write({'type': 'control_request', 'request_id': identifier, 'request': request})
                return await future
        except BaseException:
            # A timed out or partially sent control command has unknown effects.
            await finish_before_cancel(self.close())
            raise
        finally:
            self._pending.pop(identifier, None)
            if not future.done():
                future.cancel()

    async def start(self):
        if self._reader is not None or self._closing is not None:
            raise RuntimeError('Claude client can only start once')
        try:
            await self.transport.start(before_release=self.before_release, before_start=self.before_start)
            self._reader = asyncio.create_task(self._read())
            self.initialize_result = await self.control({'subtype': 'initialize', 'hooks': {}}, timeout=60)
            return self
        except BaseException:
            await finish_before_cancel(self.close())
            raise

    async def _read(self):
        try:
            while True:
                line = await self.transport.readline()
                message = json.loads(line)
                if not isinstance(message, dict) or not isinstance(message.get('type'), str):
                    raise ValueError('Invalid Claude message envelope')
                kind = message['type']
                if kind == 'control_response':
                    response = message.get('response')
                    if not isinstance(response, dict) or not isinstance(response.get('request_id'), str):
                        raise ValueError('Invalid Claude control response')
                    future = self._pending.get(response['request_id'])
                    if future is not None and not future.done():
                        if response.get('subtype') == 'success' and isinstance(response.get('response', {}), dict):
                            future.set_result(response.get('response', {}))
                        else:
                            future.set_exception(RuntimeError('Claude control request rejected: ' + str(response.get('error', 'invalid response'))))
                    continue
                if kind == 'control_request':
                    identifier, request = message.get('request_id'), message.get('request')
                    if (not isinstance(identifier, str) or not identifier or not isinstance(request, dict)
                            or identifier in self._permissions or len(self._permissions) >= 128):
                        raise ValueError('Invalid or duplicate Claude permission request')
                    if request.get('subtype') != 'can_use_tool':
                        raise ValueError('Unsupported Claude control request')
                    if not isinstance(request.get('tool_name'), str) or not isinstance(request.get('input'), dict):
                        raise ValueError('Invalid Claude tool permission')
                    self._permissions[identifier] = copy.deepcopy(request)
                elif kind == 'control_cancel_request':
                    self._permissions.pop(message.get('request_id'), None)
                sid = message.get('session_id')
                if sid is not None:
                    if not isinstance(sid, str) or str(uuid.UUID(sid)) != sid:
                        raise ValueError('Invalid native Claude session ID')
                    if self.session_id is not None and self.session_id != sid:
                        raise ValueError('Claude session identity changed')
                    self.session_id = sid
                if kind == 'result':
                    self.active_message = None
                    self._permissions.clear()
                if self._events.full() or self._event_bytes + len(line) > self.MAX_EVENT_BYTES:
                    raise ValueError('Claude event buffer exceeded its limit')
                self._events.put_nowait((message, len(line)))
                self._event_bytes += len(line)
                self._changed.set()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail(error)
            await finish_before_cancel(self.transport.close('executor_error'))

    async def next_event(self):
        while True:
            if not self._events.empty():
                message, length = self._events.get_nowait()
                self._event_bytes -= length
                return message
            if self._failure is not None:
                raise self._failure
            self._changed.clear()
            await self._changed.wait()

    async def send_message(self, content, identifier):
        if self.initialize_result is None or self._closing is not None or self.active_message is not None:
            raise RuntimeError('Claude must initialize and finish its current turn first')
        if not isinstance(content, str) or not content or not isinstance(identifier, str) or str(uuid.UUID(identifier)) != identifier:
            raise ValueError('Claude input requires content and a message UUID')
        self.active_message = identifier
        try:
            await asyncio.wait_for(self._write({'type': 'user', 'uuid': identifier,
                'session_id': self.session_id or '', 'parent_tool_use_id': None,
                'message': {'role': 'user', 'content': content}}), 10)
        except BaseException:
            await finish_before_cancel(self.close())
            raise

    async def respond_to_permission(self, identifier, *, allow, message='User denied this tool', interrupt=False):
        if type(allow) is not bool or type(interrupt) is not bool or not isinstance(message, str) or len(message) > 4096:
            raise ValueError('Invalid Claude permission decision')
        request = self._permissions.pop(identifier, None)
        if request is None:
            raise ValueError('Claude permission is no longer pending')
        result = {'behavior': 'allow', 'updatedInput': request['input']} if allow else {
            'behavior': 'deny', 'message': message, 'interrupt': interrupt}
        try:
            await asyncio.wait_for(self._write({'type': 'control_response', 'response': {
                'subtype': 'success', 'request_id': identifier, 'response': result}}), 10)
        except BaseException:
            await finish_before_cancel(self.close())
            raise

    async def interrupt(self):
        if self.active_message is None:
            raise ValueError('No active Claude turn to interrupt')
        return await self.control({'subtype': 'interrupt'}, timeout=10)

    async def close(self):
        if self._closing is None:
            self._closing = asyncio.create_task(self._close())
        return await finish_before_cancel(self._closing)

    async def _close(self):
        self._fail(EOFError('Claude client closed'))
        try:
            return await self.transport.close()
        finally:
            if self._reader is not None:
                self._reader.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._reader
            self._permissions.clear()
