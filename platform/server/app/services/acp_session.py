"""Session-scoped ACP updates and permission decisions."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import secrets

from .acp_client import AcpClient, AcpError
from .async_ops import finish_before_cancel


@dataclass(frozen=True)
class Permission:
    id: str
    tool_call: dict
    options: tuple[dict, ...]
    waiting: asyncio.Future

    def public(self):
        return {'id': self.id, 'tool_call': self.tool_call, 'options': list(self.options)}


class AcpSession:
    """A loaded native session with at most one running prompt."""

    def __init__(self, transport, *, emit=None, request_permission=None):
        self.client = AcpClient(transport, update=self._update, reverse_request=self._reverse)
        self.emit = emit
        self.request_permission = request_permission
        self.session_id = None
        self.running = False
        self.cancelling = False
        self.pending = {}

    async def start(self, cwd, *, session_id=None, **ownership_callbacks):
        initialized = await self.client.start(**ownership_callbacks)
        try:
            if session_id is None:
                response = await self.client.new_session(cwd)
                self.session_id = response['sessionId']
            else:
                # The platform already has its transcript. Forward live updates
                # during prompts without inserting the native load replay again.
                response = await self.client.load_session(session_id, cwd)
                self.session_id = session_id
            return initialized, response
        except BaseException:
            await finish_before_cancel(self.close('executor_error'))
            raise

    async def prompt(self, text):
        if self.session_id is None or self.running:
            raise ValueError('ACP session is not ready for a new prompt')
        self.running = True
        self.cancelling = False
        try:
            return await self.client.prompt(self.session_id, text)
        finally:
            self.running = False
            self._cancel_permissions()

    async def _update(self, params):
        if not self.running or params.get('sessionId') != self.session_id or self.emit is None:
            return
        payload = params.get('update')
        if not isinstance(payload, dict) or not isinstance(payload.get('sessionUpdate'), str):
            raise ValueError('ACP session update is invalid')
        await self.emit(payload)

    async def _reverse(self, method, params):
        if method != 'session/request_permission':
            raise AcpError(-32601)
        if (not self.running or self.cancelling or params.get('sessionId') != self.session_id
                or self.request_permission is None):
            return {'outcome': {'outcome': 'cancelled'}}
        call, choices = params.get('toolCall'), params.get('options')
        if (not isinstance(call, dict) or not isinstance(call.get('toolCallId'), str) or
                not isinstance(choices, list) or not 1 <= len(choices) <= 16 or len(self.pending) >= 8):
            raise AcpError(-32602)
        options = tuple(option for option in choices if isinstance(option, dict)
                        and option.get('kind') in {'allow_once', 'allow_always', 'reject_once', 'reject_always'}
                        and isinstance(option.get('optionId'), str) and 1 <= len(option['optionId']) <= 160)
        if len({option['optionId'] for option in options}) != len(options) or not options:
            return {'outcome': {'outcome': 'cancelled'}}
        state = Permission(secrets.token_hex(16), call, options, asyncio.get_running_loop().create_future())
        self.pending[state.id] = state
        try:
            await self.request_permission(state.public())
            selection = await state.waiting
        finally:
            self.pending.pop(state.id, None)
        return {'outcome': {'outcome': 'cancelled'} if selection is None else
                {'outcome': 'selected', 'optionId': selection}}

    def respond(self, identifier, option_id):
        state = self.pending.get(identifier)
        if state is None or state.waiting.done() or self.cancelling:
            return False
        if option_id is not None and not any(option['optionId'] == option_id for option in state.options):
            raise ValueError('Permission decision was not offered')
        state.waiting.set_result(option_id)
        return True

    def _cancel_permissions(self):
        for state in self.pending.values():
            if not state.waiting.done():
                state.waiting.set_result(None)

    async def cancel(self):
        if self.running and not self.cancelling:
            self.cancelling = True
            self._cancel_permissions()
            await self.client.cancel(self.session_id)

    async def close(self, reason='cancelled'):
        self.cancelling = True
        self._cancel_permissions()
        return await self.client.close(reason)
