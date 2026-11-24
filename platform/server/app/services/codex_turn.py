"""Match actual turn IDs and terminal statuses; retire unknown active turns."""
from __future__ import annotations
import asyncio
import contextlib
import inspect
import json
import uuid
from collections.abc import Awaitable, Callable

from .async_ops import finish_before_cancel
from .codex_case_tools import TOOL_CALL
from .codex_capacity import CAPACITY_CONTINUE_PROMPT, is_capacity_error, run_with_capacity_retry
from .codex_stdio import CodexStdioClient

APPROVAL_METHODS = {
    'item/commandExecution/requestApproval', 'item/fileChange/requestApproval',
    'item/permissions/requestApproval', 'item/tool/requestUserInput',
}
SAFE_RETRY_ITEMS = {'userMessage','agentMessage','reasoning','plan','contextCompaction'}


class CodexTurnError(RuntimeError):
    pass


class CodexCapacityFailure(CodexTurnError):
    pass


class CodexCapacityRequiresReview(CodexTurnError):
    pass


class CodexApprovalRequired(CodexTurnError):
    def __init__(self, event):
        super().__init__(f"User action required: {event.get('method')}")
        self.event = event


async def run_codex_turn(
        client: CodexStdioClient, thread_id: str, text: str,
        client_user_message_id: str, *, goal=False, on_event=None,
        cancel=None, turn_timeout_seconds=600,
        before_attempt=None, on_started=None, on_backoff=None, collaboration_mode=None, image_paths=None, model_options=None,
        permission_options=None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[int], float] | None = None):
    """One active consumer per client; approvals are never implicitly granted."""
    client._timeout(turn_timeout_seconds)
    cancel = cancel or asyncio.Event()
    observed_action = False

    async def retire(turn_id):
        try:
            async with asyncio.timeout(5):
                await client.interrupt(thread_id, turn_id)
                while True:
                    event = await client.next_event(timeout_seconds=5)
                    params = event.get('params', {})
                    if event.get('method') == 'turn/completed' and params.get('threadId') == thread_id:
                        turn = params.get('turn', {})
                        if turn.get('id') == turn_id and turn.get('status') in {'completed', 'failed', 'interrupted'}:
                            if on_event is not None:
                                result = on_event(event)
                                if inspect.isawaitable(result):
                                    await result
                            return
        except Exception:
            # An interrupt response alone is not a terminal acknowledgement.
            await client.close()

    async def attempt_body(message, message_id):
        nonlocal observed_action
        if cancel.is_set():
            raise asyncio.CancelledError
        turn_id = None
        ended = False
        try:
            async with asyncio.timeout(turn_timeout_seconds):
                if before_attempt is not None:
                    await before_attempt(message_id)
                options = {'collaboration_mode': collaboration_mode, **(permission_options or {})}
                if model_options:
                    options['model_options'] = model_options
                if image_paths and message_id == client_user_message_id:
                    options['image_paths'] = image_paths
                started = await client.start_turn(thread_id, message, message_id, **options)
                turn = started.get('turn', {}) if isinstance(started, dict) else {}
                turn_id = turn.get('id')
                if not isinstance(turn_id, str) or not turn_id:
                    await client.close()
                    raise CodexTurnError('Turn start response has no turn.id')
                if on_started is not None:
                    await on_started(turn_id)
                while True:
                    if cancel.is_set():
                        raise asyncio.CancelledError
                    try:
                        event = await client.next_event(timeout_seconds=0.1)
                    except TimeoutError:
                        continue
                    method = event.get('method', '')
                    params = event.get('params', {})
                    if (method in APPROVAL_METHODS or method == TOOL_CALL) and (params.get('threadId') != thread_id or params.get('turnId') != turn_id):
                        await client.close()
                        raise CodexTurnError('Server request does not identify the active turn')
                    if params.get('threadId', thread_id) != thread_id:
                        continue
                    event_turn = params.get('turnId')
                    if event_turn is not None and event_turn != turn_id:
                        continue
                    if method == 'turn/completed':
                        terminal = params.get('turn', {})
                        if params.get('threadId') != thread_id or terminal.get('id') != turn_id:
                            continue
                        status = terminal.get('status')
                        if status not in {'completed', 'failed', 'interrupted'}:
                            raise CodexTurnError('Unknown terminal turn status')
                        ended = True
                    if method in APPROVAL_METHODS or method == TOOL_CALL:
                        observed_action = True
                        if on_event is None:
                            raise CodexApprovalRequired(event)
                    if method in {'item/commandExecution/outputDelta','item/fileChange/outputDelta'}:
                        observed_action = True
                    if method in {'item/started','item/completed'}:
                        item = params.get('item')
                        if not isinstance(item,dict) or item.get('type') not in SAFE_RETRY_ITEMS:
                            observed_action = True
                    if on_event is not None:
                        result = on_event(event)
                        if inspect.isawaitable(result):
                            await result
                    if method == 'turn/completed' and ended:
                        if status == 'failed':
                            message=json.dumps(terminal.get('error'),ensure_ascii=False)
                            if is_capacity_error(message):
                                if observed_action:
                                    raise CodexCapacityRequiresReview('A native action was already recorded')
                                raise CodexCapacityFailure('Native provider capacity exhausted')
                            raise CodexTurnError(message)
                        if status == 'interrupted':
                            return {'status': 'interrupted', 'threadId': thread_id, 'lastTurnId': turn_id}
                        return {'status': 'waiting_continue' if goal else 'completed',
                                'threadId': thread_id, 'lastTurnId': turn_id}
        except BaseException:
            if turn_id is not None and not ended:
                await finish_before_cancel(retire(turn_id))
            raise

    async def attempt(message, message_id):
        # The cancellation flag must also interrupt start RPC and an awaiting
        # approval callback, rather than only the next event poll.
        operation = asyncio.create_task(attempt_body(message, message_id))
        stopped = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait((operation, stopped), return_when=asyncio.FIRST_COMPLETED)
            if stopped in done and not operation.done():
                operation.cancel()
            return await operation
        finally:
            stopped.cancel()
            if not operation.done():
                operation.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await finish_before_cancel(operation)
            with contextlib.suppress(asyncio.CancelledError):
                await stopped

    options = {'cancel':cancel,'sleep':sleep,'on_backoff':on_backoff,
               'retryable':lambda error: isinstance(error,CodexCapacityFailure)}
    if jitter is not None:
        options['jitter'] = jitter
    async with client.turn_lock:
        return await run_with_capacity_retry(
            lambda: attempt(text, client_user_message_id),
            lambda: attempt(CAPACITY_CONTINUE_PROMPT,
                            f'{client_user_message_id}-continue-{uuid.uuid4().hex[:8]}'),
            **options)
