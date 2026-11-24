"""One-shot approvals for text drafts in the native thread's private workspace."""
import asyncio
import json
from pathlib import Path, PurePosixPath
import re
import stat

from fastapi import HTTPException
from sqlalchemy import select

from ..db import SessionLocal
from ..models import AcpTurn, AgentEvent, ConversationExecution
from . import audit, conversation_execution as control
from .case_write import coordinate
from .conversation_history import event_text_field
from .file_transactions import _path


FILENAME = re.compile(r'[a-z0-9][a-z0-9_-]{0,70}\.(?:txt|md)', re.IGNORECASE)
RESERVED = {'agents', 'con', 'prn', 'aux', 'nul', *('com'+str(n) for n in range(1,10)),
            *('lpt'+str(n) for n in range(1,10))}


def checked_file(workspace, value, native_workspace=None):
    if not isinstance(value, str) or not value or '\0' in value:
        raise ValueError('Native draft path is invalid')
    if native_workspace is not None and PurePosixPath(value).is_absolute():
        candidate = PurePosixPath(value).relative_to(native_workspace)
    else:
        candidate = Path(value)
        if candidate.is_absolute():
            candidate = candidate.relative_to(workspace)
    if len(candidate.parts) != 1 or not FILENAME.fullmatch(candidate.name) or candidate.stem.lower() in RESERVED:
        raise ValueError('Draft must be a single unreserved text file')
    return _path(workspace, candidate.name)


def file_stamp(path):
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('Native draft must be an ordinary file')
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def file_request(harness, call, workspace, native_input=None, native_workspace=None):
    def checked(value):
        return checked_file(workspace, value, native_workspace)

    if harness == 'opencode':
        raw = call.get('rawInput')
        locations = call.get('locations')
        if (call.get('kind') != 'edit' or not isinstance(raw, dict) or set(raw) != {'path','content'}
                or not isinstance(locations, list) or len(locations) != 1 or not isinstance(locations[0], dict)):
            raise ValueError('OpenCode approval is not a standalone file write')
        path = checked(raw['path'])
        if path != checked(locations[0].get('path')):
            raise ValueError('OpenCode file locations differ')
        content = raw['content']
    else:
        entries = call.get('content')
        if call.get('title') != 'Write' or not isinstance(entries, list):
            raise ValueError('Kimi approval is not a standalone file write')
        diffs = [entry for entry in entries if isinstance(entry, dict) and entry.get('type') == 'diff']
        if len(diffs) == 1:
            path = checked(diffs[0].get('path'))
            content = diffs[0].get('newText')
        elif not diffs and isinstance(native_input, dict) and native_input.get('kind') == 'edit':
            # Kimi omits a diff for a file that does not exist yet. The
            # correlated tool start carries the native Write arguments.
            raw = native_input['rawInput']
            names = set(raw) - {'content'}
            if names not in ({'path'},{'file_path'}):
                raise ValueError('Kimi file arguments differ from a single write')
            path = checked(raw[names.pop()])
            content = raw.get('content')
        elif not diffs:
            # Kimi can request approval before tool/call starts. Its Write
            # policy renders the actual target in this action summary.
            summaries = [entry.get('content') for entry in entries if isinstance(entry, dict)
                         and entry.get('type') == 'content']
            prefix = 'Requesting approval to Writing '
            target = summaries[0].get('text') if len(summaries) == 1 and isinstance(summaries[0], dict) else None
            if not isinstance(target, str) or not target.startswith(prefix):
                raise ValueError('Kimi write approval does not identify its file')
            return checked(target[len(prefix):]), None
        else:
            raise ValueError('Kimi did not identify a single file')
    if not isinstance(content, str) or len(content.encode('utf8')) > 65536:
        raise ValueError('Native draft exceeds its text limit')
    return path, content


async def item(db, cid, turn_id, value, event_kind, *, key='approval_id'):
    statement = select(AgentEvent.payload).where(AgentEvent.conversation_id == cid,
        AgentEvent.type == 'acp', event_text_field(db, 'turn_id') == turn_id,
        event_text_field(db, 'type') == event_kind)
    if value is not None:
        statement = statement.where(event_text_field(db, key) == value)
    payload = await db.scalar(statement.order_by(AgentEvent.seq.desc()).limit(1))
    return json.loads(payload) if payload is not None else None


async def request(execution, workspace, params):
    allowed = {option['kind']:option['optionId'] for option in params['options'] if option['kind'] in {'allow_once','reject_once'}}
    try:
        call = params['tool_call']
        arguments = {'native_workspace':'/workspace'} if execution.docker_runtime else {}
        path, content = file_request(execution.thread.harness, call, workspace,
            execution.native_inputs.get(call['toolCallId']), **arguments)
        if set(allowed) != {'allow_once','reject_once'} or len(execution.approved_files) >= 8:
            raise ValueError('This approval would exceed the draft limit')
    except (ValueError, OSError):
        execution.session.respond(params['id'], allowed.get('reject_once'))
        return
    preview = execution.projection.secrets.clean(content[:8000]) if content is not None else None
    cid = execution.thread.conversation_id
    await execution.record({'type':'permission', 'approval_id':params['id'], 'path':path.name,
                            'preview':preview, 'truncated':content is not None and len(content) > 8000})
    while not execution.cancelled.is_set() and execution.session.running:
        async with SessionLocal() as db:
            decision = await item(db, cid, execution.id, params['id'], 'permission_decision')
        if decision is not None:
            accepted = decision['decision'] == 'approve'
            before = None
            if accepted:
                try:
                    accepted = checked_file(workspace, str(path)) == path
                    await execution.reauthorize(None)
                    async with SessionLocal() as db:
                        accepted = accepted and not await control._stop_pending(db, cid, control.stop_binding(execution.owner))
                    if accepted:
                        before = file_stamp(path)
                except (ValueError, OSError, HTTPException):
                    accepted = False
            delivered = execution.session.respond(params['id'], allowed['allow_once' if accepted else 'reject_once'])
            if delivered and accepted:
                execution.approved_files.setdefault(path.name, before)
            return
        await asyncio.sleep(.25)
    execution.session.respond(params['id'], None)


async def decide(db, identifier, approval_id, decision, user):
    from ..routers.agent import _append_event
    from . import acp_execution as execution

    turn, thread = await execution.lookup(db, identifier, user)
    cid = turn.conversation_id
    case = await execution.owned_case(db, cid, user)
    async with coordinate(db, case):
        await db.refresh(user)
        if user.status != 'active':
            raise HTTPException(401, '用户不可用')
        await execution.owned_case(db, cid, user)
        pending = await item(db, cid, identifier, approval_id, 'permission') if thread.allow_drafts else None
        if pending is None:
            raise HTTPException(404, '审批请求不存在')
        accepted = await item(db, cid, identifier, approval_id, 'permission_decision')
        if accepted is not None:
            if accepted['decision'] != decision:
                raise HTTPException(409, '此审批已作出决定')
            return accepted
        latest = await db.get(AcpTurn, identifier, populate_existing=True)
        owner = await db.get(ConversationExecution, cid, populate_existing=True)
        if (latest.phase != 'running' or owner is None or owner.phase != 'active' or owner.resource_id != identifier
                or await control._stop_pending(db, cid, owner.owner_token_sha256)):
            raise HTTPException(409, '该回合已结束或正在停止')
        accepted = {'type':'permission_decision','turn_id':identifier,'approval_id':approval_id,'decision':decision}
        await _append_event(db, cid, 'acp', accepted)
        await audit.case_event(db, case, user.id, 'acp.permission.decided', 'acp_turn', identifier,
                               {'approval_id':approval_id, 'decision':decision})
    from .async_ops import finish_before_cancel
    await finish_before_cancel(db.commit())
    return accepted
