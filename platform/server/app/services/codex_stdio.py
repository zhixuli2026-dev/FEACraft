"""Initialized, bounded Codex app-server RPC over an owned stdio scope."""
from __future__ import annotations
import asyncio
import contextlib
import copy
import json
import math
from pathlib import Path

from .async_ops import finish_before_cancel
from .owned_stdio import MAX_LINE, OwnedStdio


class CodexRpcError(RuntimeError):
    def __init__(self, error):
        super().__init__(str(error.get('message', 'Codex RPC failed')))
        self.error = error


class CodexStdioClient:
    APPROVAL_DECISIONS = {'accept', 'acceptForSession', 'decline', 'cancel'}
    DECISION_METHODS = {'item/commandExecution/requestApproval', 'item/fileChange/requestApproval'}
    MAX_EVENTS = 256
    MAX_EVENT_BYTES = 4 * 1024 * 1024
    MAX_SERVER_REQUESTS = 128

    def __init__(self, argv: list[str], env: dict | None = None, *, cwd=None,
                 before_release=None, before_start=None, distro=None, native_state=None, after_prepare=None,
                 native_scope=None, lifetime_seconds=0, max_output_bytes=0, transport=None):
        home = env.get('CODEX_HOME') if isinstance(env, dict) else None
        if (not isinstance(home, str) or not Path(home).is_absolute()
                or (native_state is None and not Path(home).is_dir())
                or Path(home).resolve() == (Path.home() / '.codex').resolve()):
            raise ValueError('Codex requires an explicit private CODEX_HOME')
        self.argv = list(argv)
        provider_token = None
        if distro is not None:
            from .codex_credentials import TOKEN_ENV
            from .codex_environment import wsl_codex_environment
            provider_token = env.get(TOKEN_ENV)
            env = wsl_codex_environment(env, distro, native_state=native_state)
        self.env = env
        self.transport = transport if transport is not None else OwnedStdio(
            self.argv, env=env, cwd=cwd, distro=distro, native_state=native_state,
            native_scope=native_scope, lifetime_seconds=lifetime_seconds, max_output_bytes=max_output_bytes,
            provider_token=provider_token)
        self.before_release = before_release
        self.before_start = before_start
        self.after_prepare = after_prepare
        self.initialize_result = None
        self.initialized = False
        self._next_id = 0
        self._events = asyncio.Queue(maxsize=self.MAX_EVENTS)
        self._event_bytes = 0
        self._event_changed = asyncio.Event()
        self._pending = {}
        self._server_requests = {}
        self._reader_task = None
        self._failure = None
        self._close_operation = None
        self._write_lock = asyncio.Lock()
        self.turn_lock = asyncio.Lock()

    @property
    def proc(self):
        return None if self._close_operation is not None and self._close_operation.done() else self.transport.proc

    @property
    def stop_result(self):
        return self.transport.stop_result

    async def start(self):
        if self.initialized or self._close_operation is not None:
            raise RuntimeError('Codex client can only start once')
        try:
            await self.transport.start(before_release=self.before_release, before_start=self.before_start,
                                       after_prepare=self.after_prepare)
            self._reader_task = asyncio.create_task(self._read_messages())
            result = await self.request('initialize', {
                'clientInfo': {'name': 'vibeflow', 'title': 'VibeFlow', 'version': '0.1.0'},
                'capabilities': {'experimentalApi': True}}, _initializing=True)
            if not isinstance(result, dict) or not isinstance(result.get('userAgent'), str):
                raise ValueError('Codex initialize response is invalid')
            await asyncio.wait_for(self._write({'method': 'initialized', 'params': {}}), 10)
            self.initialize_result = result
            self.initialized = True
            return self
        except BaseException:
            await finish_before_cancel(self.close())
            raise

    @staticmethod
    def _timeout(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 3600:
            raise ValueError('Invalid Codex RPC timeout')

    def _fail(self, error):
        if self._failure is None:
            self._failure = error
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(self._failure)
        self._event_changed.set()

    async def _write(self, payload):
        data = (json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False) + '\n').encode('utf8')
        if len(data) > MAX_LINE:
            raise ValueError('JSON-RPC request exceeds 1 MiB')
        if self._failure is not None:
            raise self._failure
        async with self._write_lock:
            await self.transport.write(data)

    async def request(self, method: str, params: dict | None = None,
                      timeout_seconds: float = 10, *, _initializing=False):
        self._timeout(timeout_seconds)
        if not self.initialized and not (_initializing and method == 'initialize'):
            raise RuntimeError('Codex must initialize before requests')
        if self.initialized and method in {'initialize', 'initialized'}:
            raise ValueError('Codex is already initialized')
        if not isinstance(method, str) or not method or len(method) > 128 or (params is not None and not isinstance(params, dict)):
            raise ValueError('Invalid Codex RPC request')
        if self._failure is not None:
            raise self._failure
        if len(self._pending) >= 64:
            raise RuntimeError('Too many pending Codex requests')
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        self._pending[request_id] = future
        sent = False
        try:
            # Deadline includes native stdin, not only the response wait.
            async with asyncio.timeout(timeout_seconds):
                await self._write({'id': request_id, 'method': method, 'params': params or {}})
                sent = True
                return await future
        except BaseException as error:
            if isinstance(error, (asyncio.CancelledError, TimeoutError)) or not sent or self._failure is not None:
                # A partially written line or an unknown side effect cannot
                # safely be retried on this stream.
                await finish_before_cancel(self.close())
            raise
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    @staticmethod
    def _request_key(identifier):
        if isinstance(identifier, bool) or not isinstance(identifier, (int, str)) or identifier == '':
            raise ValueError('Invalid server request ID')
        return type(identifier).__name__, identifier

    async def _read_messages(self):
        try:
            while True:
                line = await self.transport.readline()
                message = json.loads(line)
                if (not isinstance(message, dict) or message.get('jsonrpc', '2.0') != '2.0'
                        or (('method' in message) == ('result' in message or 'error' in message))):
                    raise ValueError('Invalid JSON-RPC envelope')
                if 'method' not in message:
                    identifier = message.get('id')
                    if type(identifier) is not int:
                        raise ValueError('Invalid JSON-RPC response ID')
                    if ('result' in message) == ('error' in message):
                        raise ValueError('Ambiguous JSON-RPC response')
                    future = self._pending.get(identifier)
                    if future is None or future.done():
                        continue
                    if 'error' in message:
                        if not isinstance(message['error'], dict):
                            raise ValueError('Invalid JSON-RPC error')
                        future.set_exception(CodexRpcError(message['error']))
                    else:
                        future.set_result(message['result'])
                    continue
                if not isinstance(message['method'], str) or not isinstance(message.get('params', {}), dict):
                    raise ValueError('Invalid Codex notification')
                if message['method'] == 'turn/completed':
                    params = message.get('params', {})
                    turn_id = params.get('turn', {}).get('id')
                    for key, pending in tuple(self._server_requests.items()):
                        binding = pending.get('params', {})
                        if (binding.get('threadId') == params.get('threadId') and turn_id is not None
                                and binding.get('turnId') == turn_id):
                            del self._server_requests[key]
                if message['method'] == 'serverRequest/resolved':
                    params = message.get('params', {})
                    key = self._request_key(params.get('requestId'))
                    pending = self._server_requests.get(key)
                    if pending is not None and pending.get('params', {}).get('threadId') == params.get('threadId'):
                        del self._server_requests[key]
                if 'id' in message:
                    key = self._request_key(message['id'])
                    if key in self._server_requests or len(self._server_requests) >= self.MAX_SERVER_REQUESTS:
                        raise ValueError('Duplicate or excessive Codex server requests')
                    self._server_requests[key] = copy.deepcopy(message)
                if self._events.full() or self._event_bytes + len(line) > self.MAX_EVENT_BYTES:
                    raise ValueError('Codex event buffer exceeded its limit')
                self._events.put_nowait((message, len(line)))
                self._event_bytes += len(line)
                self._event_changed.set()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._fail(error)
            await finish_before_cancel(self.transport.close('executor_error'))

    async def new_thread(self, *, cwd=None, approval_policy='untrusted', sandbox='read-only', ephemeral=False, model=None,
                         dynamic_tools=None, developer_instructions=None):
        if approval_policy not in {'untrusted', 'on-failure', 'on-request', 'never'}:
            raise ValueError('Invalid approval policy')
        if sandbox not in {'read-only', 'workspace-write', 'danger-full-access'} or type(ephemeral) is not bool:
            raise ValueError('Invalid sandbox or persistence selection')
        params = {'approvalPolicy': approval_policy, 'sandbox': sandbox, 'ephemeral': ephemeral}
        if dynamic_tools is not None:
            params['dynamicTools'] = dynamic_tools
        if developer_instructions is not None:
            params['developerInstructions'] = developer_instructions
        if cwd is not None:
            params['cwd'] = str(cwd)
        if model is not None:
            if not isinstance(model, str) or not model or len(model) > 120 or '\0' in model:
                raise ValueError('Invalid CLI model selection')
            params['model'] = model
        return await self.request('thread/start', params)

    async def resume_thread(self, thread_id: str, *, exclude_turns=False, cwd=None, approval_policy=None, sandbox=None, model=None):
        if not isinstance(thread_id, str) or not thread_id or type(exclude_turns) is not bool:
            raise ValueError('Resume requires a thread ID and boolean excludeTurns')
        params = {'threadId': thread_id, 'excludeTurns': exclude_turns}
        if cwd is not None:
            params['cwd'] = str(cwd)
        if approval_policy is not None:
            if approval_policy not in {'untrusted', 'on-failure', 'on-request', 'never'}:
                raise ValueError('Invalid approval policy')
            params['approvalPolicy'] = approval_policy
        if sandbox is not None:
            if sandbox not in {'read-only', 'workspace-write', 'danger-full-access'}:
                raise ValueError('Invalid sandbox mode')
            params['sandbox'] = sandbox
        if model is not None:
            if not isinstance(model, str) or not model or len(model) > 120 or '\0' in model:
                raise ValueError('Invalid CLI model selection')
            params['model'] = model
        return await self.request('thread/resume', params)

    async def latest_turn(self, thread_id: str):
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError('Turn lookup requires a thread ID')
        try:
            result = await self.request('thread/turns/list', {
                'threadId': thread_id, 'limit': 1, 'sortDirection': 'desc', 'itemsView': 'notLoaded'})
            turns = result.get('data')
            latest = turns[0] if isinstance(turns, list) and len(turns) == 1 else None
        except CodexRpcError as error:
            if error.error.get('code') != -32601:
                raise
            result = await self.request('thread/read', {'threadId': thread_id, 'includeTurns': True})
            thread = result.get('thread', {})
            turns = thread.get('turns') if thread.get('id') == thread_id else None
            latest = turns[-1] if isinstance(turns, list) and turns else None
        return latest if isinstance(latest, dict) else {}

    async def fork_thread(self, thread_id: str, last_turn_id=None, exclude_turns=False, *, cwd=None,
                          approval_policy=None, sandbox=None, defer_goal_continuation=None, model=None):
        if not isinstance(thread_id, str) or not thread_id or type(exclude_turns) is not bool:
            raise ValueError('Fork requires a thread ID and boolean excludeTurns')
        if last_turn_id is not None and (not isinstance(last_turn_id, str) or not last_turn_id):
            raise ValueError('Invalid lastTurnId')
        params = {'threadId': thread_id, 'excludeTurns': exclude_turns}
        if last_turn_id is not None:
            params['lastTurnId'] = last_turn_id
        if cwd is not None:
            params['cwd'] = str(cwd)
        if approval_policy is not None:
            if approval_policy not in {'untrusted', 'on-request', 'never'}:
                raise ValueError('Invalid fork approval policy')
            params['approvalPolicy'] = approval_policy
        if sandbox is not None:
            if sandbox not in {'read-only', 'workspace-write', 'danger-full-access'}:
                raise ValueError('Invalid fork sandbox')
            params['sandbox'] = sandbox
        if defer_goal_continuation is not None:
            if type(defer_goal_continuation) is not bool:
                raise ValueError('Invalid fork goal continuation selection')
            params['deferGoalContinuation'] = defer_goal_continuation
        if model is not None:
            if not isinstance(model, str) or not model or len(model) > 120 or '\0' in model:
                raise ValueError('Invalid CLI model selection')
            params['model'] = model
        return await self.request('thread/fork', params)

    async def start_turn(self, thread_id: str, text: str, client_user_message_id: str, *, collaboration_mode=None,
                         image_paths=None, model_options=None, approval_policy=None, sandbox_policy=None):
        if any(not isinstance(x, str) or not x for x in (thread_id, text, client_user_message_id)):
            raise ValueError('Turn requires thread, text and client message ID')
        params = {
            'threadId': thread_id, 'input': [{'type': 'text', 'text': text}],
            'clientUserMessageId': client_user_message_id}
        if image_paths:
            if (not isinstance(image_paths, list) or len(image_paths) > 3 or
                any(not isinstance(path, str) or not path or len(path) > 8192 or '\0' in path for path in image_paths)):
                raise ValueError('Invalid native image paths')
            params['input'].extend({'type': 'localImage', 'path': path} for path in image_paths)
        if collaboration_mode is not None:
            if (not isinstance(collaboration_mode, dict) or collaboration_mode.get('mode') not in {'plan','default'}
                    or not isinstance(collaboration_mode.get('settings',{}).get('model'),str)):
                raise ValueError('Invalid native collaboration mode')
            params['collaborationMode'] = collaboration_mode
        if approval_policy is not None:
            params['approvalPolicy'] = approval_policy
        if sandbox_policy is not None:
            params['sandboxPolicy'] = sandbox_policy
        from .codex_model_options import protocol
        params.update(protocol(model_options))
        return await self.request('turn/start', params)

    async def interrupt(self, thread_id: str, turn_id: str):
        if any(not isinstance(x, str) or not x for x in (thread_id, turn_id)):
            raise ValueError('Interrupt requires thread and turn IDs')
        return await self.request('turn/interrupt', {'threadId': thread_id, 'turnId': turn_id})

    async def _respond(self, request_id, allowed, result):
        key = self._request_key(request_id)
        request = self._server_requests.get(key)
        if request is None or request['method'] not in allowed:
            raise ValueError('Response does not match an outstanding server request')
        del self._server_requests[key]  # Claim before yielding: only one response.
        try:
            await asyncio.wait_for(self._write({'id': request_id, 'result': result}), 10)
        except BaseException:
            await finish_before_cancel(self.close())
            raise

    async def respond_to_approval(self, request_id, decision: str):
        if not isinstance(decision, str) or decision not in self.APPROVAL_DECISIONS:
            raise ValueError('Unknown Codex approval decision')
        # Permission grants have another schema and must not accept this form.
        await self._respond(request_id, self.DECISION_METHODS, {'decision': decision})

    async def respond_to_case_tool(self, request_id, result):
        from .codex_case_tools import TOOL_CALL

        await self._respond(request_id, {TOOL_CALL}, {'success': result['ok'],
            'contentItems': [{'type': 'inputText', 'text': result['result']}]})

    async def respond_to_request(self, request_id, body):
        from .codex_approval import bounded, response
        request = self._server_requests.get(self._request_key(request_id))
        if request is None:
            raise ValueError('Server request is no longer pending')
        result, _public, _secrets = response(request['method'], request.get('params', {}), bounded(body))
        await self._respond(request_id, {request['method']}, result)

    async def respond_to_user_input(self, request_id, answers):
        if not isinstance(answers, dict) or not answers:
            raise ValueError('User input requires answers')
        normalized = {}
        for question_id, values in answers.items():
            if (not isinstance(question_id, str) or not question_id or not isinstance(values, list)
                    or not values or any(not isinstance(v, str) or not v for v in values)):
                raise ValueError('Invalid user input answer')
            normalized[question_id] = {'answers': list(values)}
        request = self._server_requests.get(self._request_key(request_id))
        if request is None:
            raise ValueError('User input request is no longer pending')
        if set(answers) != {q.get('id') for q in request.get('params', {}).get('questions', [])}:
            raise ValueError('Answers must match the pending question IDs')
        await self._respond(request_id, {'item/tool/requestUserInput'}, {'answers': normalized})

    async def start_browser_login(self):
        return await self.request('account/login/start', {'type': 'chatgpt'})

    async def start_device_code_login(self):
        return await self.request('account/login/start', {'type': 'chatgptDeviceCode'})

    async def cancel_login(self, login_id: str):
        if not isinstance(login_id, str) or not login_id:
            raise ValueError('Cancel login requires login ID')
        return await self.request('account/login/cancel', {'loginId': login_id})

    async def get_account(self, refresh_token=False):
        if type(refresh_token) is not bool:
            raise ValueError('refreshToken must be boolean')
        return await self.request('account/read', {'refreshToken': refresh_token})

    async def get_rate_limits(self):
        return await self.request('account/rateLimits/read', {})

    async def list_models(self, *, cursor=None, limit=50):
        if type(limit) is not int or not 1 <= limit <= 100 or (cursor is not None and (
                not isinstance(cursor, str) or len(cursor) > 4096 or '\0' in cursor)):
            raise ValueError('Invalid model pagination')
        return await self.request('model/list', {'cursor': cursor, 'limit': limit})

    async def windows_sandbox_readiness(self):
        return await self.request('windowsSandbox/readiness', {})

    async def windows_sandbox_setup(self, mode, cwd=None, *, confirmed=False):
        if confirmed is not True:
            raise ValueError('Windows sandbox setup requires explicit user confirmation')
        if mode not in {'elevated', 'unelevated'}:
            raise ValueError('Unknown Windows sandbox mode')
        params = {'mode': mode}
        if cwd is not None:
            params['cwd'] = str(cwd)
        return await self.request('windowsSandbox/setupStart', params)

    async def next_event(self, timeout_seconds=10):
        self._timeout(timeout_seconds)
        async with asyncio.timeout(timeout_seconds):
            while True:
                if self._failure is not None:
                    raise self._failure
                if not self._events.empty():
                    message, size = self._events.get_nowait()
                    self._event_bytes -= size
                    return message
                self._event_changed.clear()
                await self._event_changed.wait()

    async def close(self):
        if self._close_operation is None:
            self._close_operation = asyncio.create_task(self._close())
        return await finish_before_cancel(self._close_operation)

    async def _close(self):
        self._fail(EOFError('Codex client closed'))
        try:
            return await self.transport.close()
        finally:
            if self._reader_task is not None:
                if not self._reader_task.done():
                    self._reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._reader_task
                self._reader_task = None
            self._server_requests.clear()
            self._events = asyncio.Queue(maxsize=self.MAX_EVENTS)
            self._event_bytes = 0
            self.initialized = False


async def start_codex_stdio(argv, env=None, **kwargs):
    return await CodexStdioClient(argv, env, **kwargs).start()
