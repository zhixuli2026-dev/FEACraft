"""ACP v1 JSON-RPC client for installed OpenCode and Kimi processes."""
from __future__ import annotations

import asyncio
import json

from .async_ops import finish_before_cancel
from .owned_stdio import MAX_LINE, packed


class AcpError(RuntimeError):
    def __init__(self, code):
        super().__init__(f'Agent Client Protocol request failed ({code})')
        self.code = code


class AcpClient:
    def __init__(self, transport, *, update=None, reverse_request=None):
        self.transport = transport
        self.update = update
        self.reverse_request = reverse_request
        self.capabilities = {}
        self.sequence = 0
        self.pending = {}
        self.reverse = set()
        self.reader = None
        self.failure = None

    async def start(self, **ownership_callbacks):
        try:
            await self.transport.start(**ownership_callbacks)
            self.reader = asyncio.create_task(self._read())
            result = await self.request('initialize', {'protocolVersion': 1, 'clientCapabilities': {},
                'clientInfo': {'name': 'vibeflow', 'version': '1.0.0'}})
            if result.get('protocolVersion') != 1 or not isinstance(result.get('agentCapabilities', {}), dict):
                raise ValueError('Installed agent did not negotiate ACP v1')
            self.capabilities = result.get('agentCapabilities', {})
            return result
        except BaseException:
            await finish_before_cancel(self.close('executor_error'))
            raise

    async def _write(self, value):
        data = packed({'jsonrpc': '2.0', **value})
        if len(data) > MAX_LINE:
            raise ValueError('ACP frame exceeds 1 MiB')
        await self.transport.write(data)

    async def request(self, method, params):
        if self.reader is None or self.failure is not None:
            raise EOFError('ACP connection unavailable') from self.failure
        if len(self.pending) >= 16:
            raise ValueError('Too many outstanding ACP requests')
        self.sequence += 1
        identifier = self.sequence
        waiter = asyncio.get_running_loop().create_future()
        waiter.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
        self.pending[identifier] = waiter
        try:
            await self._write({'id': identifier, 'method': method, 'params': params})
            return await waiter
        finally:
            self.pending.pop(identifier, None)
            if not waiter.done():
                waiter.cancel()

    async def notify(self, method, params):
        await self._write({'method': method, 'params': params})

    async def new_session(self, cwd):
        result = await self.request('session/new', {'cwd': cwd, 'mcpServers': []})
        if not isinstance(result.get('sessionId'), str) or not 1 <= len(result['sessionId']) <= 200:
            raise ValueError('Invalid ACP session identity')
        return result

    async def load_session(self, session_id, cwd):
        if self.capabilities.get('loadSession') is not True:
            raise ValueError('This ACP agent does not support loading an existing session')
        return await self.request('session/load', {'sessionId': session_id, 'cwd': cwd, 'mcpServers': []})

    async def prompt(self, session_id, text):
        return await self.request('session/prompt', {'sessionId': session_id,
            'prompt': [{'type': 'text', 'text': text}]})

    async def cancel(self, session_id):
        await self.notify('session/cancel', {'sessionId': session_id})

    async def _reverse_reply(self, frame):
        identifier, method, params = frame['id'], frame['method'], frame['params']
        try:
            if self.reverse_request is not None:
                result = await self.reverse_request(method, params)
            elif method == 'session/request_permission':
                result = {'outcome': {'outcome': 'cancelled'}}
            else:
                raise AcpError(-32601)
        except AcpError as error:
            reply = {'error': {'code': error.code, 'message': 'ACP client declined the request'}}
        except Exception:
            reply = {'error': {'code': -32603, 'message': 'ACP client could not complete the request'}}
        else:
            reply = {'result': result}
        await self._write({'id': identifier, **reply})

    def _reverse_done(self, task):
        self.reverse.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.reader.cancel()

    async def _read(self):
        try:
            while True:
                frame = json.loads(await self.transport.readline())
                if not isinstance(frame, dict) or frame.get('jsonrpc') != '2.0':
                    raise ValueError('Malformed ACP frame')
                identifier = frame.get('id')
                if 'method' in frame:
                    method, params = frame['method'], frame.get('params', {})
                    if not isinstance(method, str) or not isinstance(params, dict):
                        raise ValueError('Malformed ACP notification')
                    if identifier is not None:
                        if type(identifier) not in (int, str) or len(self.reverse) >= 16:
                            raise ValueError('Invalid ACP reverse request')
                        task = asyncio.create_task(self._reverse_reply({**frame, 'params': params}))
                        self.reverse.add(task)
                        task.add_done_callback(self._reverse_done)
                    elif method == 'session/update' and self.update is not None:
                        await self.update(params)
                    continue
                if type(identifier) is not int:
                    raise ValueError('ACP response is missing a request ID')
                waiter = self.pending.get(identifier)
                if waiter is None or waiter.done():
                    continue
                if 'error' in frame:
                    error = frame['error']
                    code = error.get('code') if isinstance(error, dict) else None
                    waiter.set_exception(AcpError(code if type(code) is int else -32603))
                else:
                    result = frame.get('result')
                    if not isinstance(result, dict):
                        raise ValueError('ACP response result is not an object')
                    waiter.set_result(result)
        except asyncio.CancelledError:
            self.failure = EOFError('ACP reader stopped')
            raise
        except Exception as error:
            self.failure = error
        finally:
            for waiter in self.pending.values():
                if not waiter.done():
                    waiter.set_exception(self.failure or EOFError('ACP transport ended'))

    async def close(self, reason='cancelled'):
        tasks = [task for task in [self.reader, *self.reverse] if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await self.transport.finish_input()
        finally:
            stop = await self.transport.close(reason)
        return stop
