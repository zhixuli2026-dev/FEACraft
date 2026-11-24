"""Ordered branch history and the v1 conversation download."""
import asyncio
import json
from contextlib import aclosing, asynccontextmanager

from sqlalchemy import JSON, case, cast, func, select, tuple_
from sqlalchemy.orm import aliased

from ..models import AgentEvent, Conversation, ToolCall
from .async_ops import finish_before_cancel
from .durable_events import ensure_parent_transaction
from .workflow_state import ExecutionConflict


async def lineage(db, conversation, cutoff=None):
    """A fork can point at a grandparent event or at an empty parent."""
    case_id = conversation.case_id
    segments = []
    seen = set()
    while True:
        if conversation.id in seen or conversation.case_id != case_id:
            raise ExecutionConflict('evidence_invalid', '会话祖先记录不一致')
        seen.add(conversation.id)
        segments.append((conversation.id, cutoff))
        if conversation.forked_from_id is None or conversation.branch_head_event_id is None:
            break
        head = conversation.branch_head_event_id
        cutoff = head if cutoff is None else min(cutoff, head)
        conversation = await db.get(Conversation, conversation.forked_from_id)
        if conversation is None:
            raise ExecutionConflict('evidence_invalid', '会话祖先记录缺失')
    return list(reversed(segments))


@asynccontextmanager
async def read_snapshot(db):
    """Read endpoints call this after authentication and before business writes."""
    await db.rollback()
    if db.get_bind().dialect.name == "postgresql":
        await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    else:
        await ensure_parent_transaction(db)
    try:
        yield
    finally:
        await finish_before_cancel(db.rollback())


def visible(query, cid, cutoff):
    # Native approval previews and private draft bodies use the actor-owned ACP
    # turn endpoint. Case conversation history only shares conversation events.
    query = query.where(AgentEvent.conversation_id == cid, AgentEvent.type != 'acp')
    return query if cutoff is None else query.where(AgentEvent.id <= cutoff)


def answer_candidate(kind, payload):
    return (kind == 'message' and payload.get('role') == 'assistant'
            and not any(payload.get(key) for key in ('tool_calls', 'cancelled', 'interrupted', 'failed'))
            and payload.get('status') in (None, 'completed'))


def event_text_field(db, key, event=AgentEvent):
    if db.get_bind().dialect.name == 'sqlite':
        return func.json_extract(event.payload, '$.' + key)
    return cast(event.payload, JSON)[key].as_string()


def run_id_field(db):
    return event_text_field(db, 'run_id')


async def answer_states(db, events):
    """Only look up the outcome of the matching run in the answer's owning session."""
    by_owner = {}
    for event in events:
        rid = event['answer_run_id']
        if event['answer_candidate'] and isinstance(rid, str) and rid:
            by_owner.setdefault(event['conversation_id'], set()).add(rid)
    run = run_id_field(db)
    states = {}
    for owner, ids in by_owner.items():
        ids = sorted(ids)
        for start in range(0, len(ids), 400):
            latest = (select(AgentEvent.payload, AgentEvent.seq, AgentEvent.id, func.row_number().over(
                partition_by=run, order_by=(AgentEvent.seq.desc(), AgentEvent.id.desc())).label('position'))
                .where(AgentEvent.conversation_id == owner, AgentEvent.type == 'run_state',
                       run.in_(ids[start:start+400])).subquery())
            for payload, seq, event_id in await db.execute(select(latest.c.payload, latest.c.seq, latest.c.id)
                                                           .where(latest.c.position == 1)):
                value = json.loads(payload)
                fields = ('run_id', 'status', 'started_at', 'ended_at', 'duration_ms')
                states[owner, value['run_id']] = ((seq, event_id), {k:value[k] for k in fields if k in value})
    return states


def answer_outcome(event, states):
    if not event['answer_candidate']:
        return False, None
    rid = event['answer_run_id']
    if rid is None:
        return True, None  # Legacy standalone answers predate per-run records.
    if isinstance(rid, str) and (found := states.get((event['conversation_id'], rid))):
        position, state = found
        if position > (event['seq'], event['id']):
            return state.get('status') == 'completed', state
    return False, None


async def visible_event(db, source, head):
    """Resolve the exact event without reading unrelated message bodies."""
    if not 0 < head <= 2**63-1:
        return None
    owner = await db.scalar(select(AgentEvent.conversation_id).where(AgentEvent.id == head, AgentEvent.type != 'acp'))
    bounds = dict(await lineage(db, source))
    if owner not in bounds or (bounds[owner] is not None and head > bounds[owner]):
        return None
    return await db.get(AgentEvent, head)


async def visible_answer(db, source, head):
    row = await visible_event(db, source, head)
    if row is None:
        return None
    payload = json.loads(row.payload)
    event = {'conversation_id':row.conversation_id, 'seq':row.seq, 'id':head, 'answer_run_id':payload.get('run_id'),
             'answer_candidate':answer_candidate(row.type, payload)}
    allowed, _ = answer_outcome(event, await answer_states(db, [event]))
    return allowed


async def counts(db, segments):
    sizes = {}
    latest = 0
    for cid, cutoff in segments:
        query = select(func.count(AgentEvent.id), func.max(AgentEvent.id))
        count, last = (await db.execute(visible(query, cid, cutoff))).one()
        sizes[cid] = count
        latest = max(latest, last or 0)
    return sizes, latest


def window_query(cid, cutoff, limit, offset):
    return (visible(select(AgentEvent), cid, cutoff)
            .order_by(AgentEvent.seq.desc(), AgentEvent.id.desc())
            .offset(offset).limit(limit))


def interrupted_checkpoint(db):
    """Only the last unsuperseded checkpoint in this run can supply a partial reply."""
    prior = aliased(AgentEvent)
    latest = (select(case((prior.type == 'run_checkpoint', prior.payload)))
              .where(prior.conversation_id == AgentEvent.conversation_id,
                     tuple_(prior.seq, prior.id) < tuple_(AgentEvent.seq, AgentEvent.id),
                     prior.type.in_(('run_checkpoint', 'run_state', 'message')),
                     event_text_field(db, 'run_id', prior) == run_id_field(db),
                     (prior.type != 'message') | (event_text_field(db, 'role', prior) == 'assistant'))
              .order_by(prior.seq.desc(), prior.id.desc()).limit(1).correlate(AgentEvent).scalar_subquery())
    return case(((AgentEvent.type == 'run_state') &
                 event_text_field(db, 'status').in_(('interrupted','failed','timed_out')), latest))


async def newest(db, segments, limit, offset=0, sizes=None, *, display=False, text_handoff=False):
    """Skip whole ancestor segments or index entries before loading payloads."""
    for cid, cutoff in reversed(segments):
        if not limit:
            return
        if sizes is not None and offset >= sizes[cid]:
            offset -= sizes[cid]
            continue
        query = window_query(cid, cutoff, limit, offset)
        if display:
            query = query.add_columns(interrupted_checkpoint(db))
        if text_handoff:
            query = query.where(
                ((AgentEvent.type == 'message') & event_text_field(db, 'role').in_(('user', 'assistant')))
                | ((AgentEvent.type == 'system') & (event_text_field(db, 'kind') == 'handoff')
                   & (event_text_field(db, 'handoff_mode') == 'bounded_text')))
        result = await db.stream(query.execution_options(yield_per=4))
        try:
            async for record in result:
                yield record[0], record[1] if display else None
                limit -= 1
        finally:
            await result.close()
        offset = 0


async def rows(db, segments, *, starts_only=False):
    for index, (cid, cutoff) in enumerate(segments):
        query = visible(select(AgentEvent), cid, cutoff)
        if starts_only:
            query = query.where(AgentEvent.type == 'run_state')
        query = query.order_by(AgentEvent.seq, AgentEvent.id).execution_options(yield_per=16)
        result = await db.stream_scalars(query)
        try:
            async for event in result:
                payload = json.loads(event.payload)
                if not starts_only or payload.get('status') == 'started':
                    yield (index, event.seq, event.id), event, payload
        finally:
            await result.close()


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode('utf8')


def inherited_tool_history(events, cid):
    """Send only matched ancestor calls/results to the provider. The timeline stays intact."""
    finished = {(e['conversation_id'], e['payload'].get('tool_call_id')) for e in events
                if e['conversation_id'] != cid and e['type'] == 'tool_result'
                and isinstance(e['payload'].get('tool_call_id'), str)}
    calls = set()
    user_position = {}
    selected = []
    for e in events:
        owner, payload = e['conversation_id'], e['payload']
        if owner != cid:
            if e['type'] == 'message' and payload.get('role') == 'user':
                user_position[owner] = len(selected)
            if e['type'] == 'message' and payload.get('tool_calls'):
                matched = [tool for tool in payload['tool_calls'] if isinstance(tool, dict)
                           and isinstance(tool.get('id'), str) and (owner, tool['id']) in finished]
                calls.update((owner, tool['id']) for tool in matched)
                if len(matched) != len(payload['tool_calls']) and owner in user_position:
                    index = user_position.pop(owner)
                    origin = selected[index]
                    text = json.dumps(origin['payload'].get('content', ''), ensure_ascii=False)
                    note = f'父会话历史引用（当时的工具操作未在分支点完成）：{text}。该请求留在父会话处理。'
                    selected[index] = {**origin, 'payload':{**origin['payload'], 'content':note}}
                if not matched and not payload.get('content'):
                    continue
                e = {**e, 'payload':{**payload, 'tool_calls':matched}}
            elif e['type'] == 'tool_result':
                key = payload.get('tool_call_id')
                if not isinstance(key, str) or (owner, key) not in calls:
                    continue
        selected.append(e)
    return selected


async def archive_parts(db, conversation, session, clean):
    segments = await lineage(db, conversation)
    yield b'{"schema": "vibeflow.agent.session.v1", "session": ' + encoded(session) + b', "tool_calls": ['
    tools = await db.stream_scalars(select(ToolCall).where(ToolCall.conversation_id == conversation.id)
                                    .order_by(ToolCall.created_at, ToolCall.id).execution_options(yield_per=16))
    try:
        separator = b''
        async for tool in tools:
            yield separator + encoded({'id': tool.id, 'tool': tool.tool, 'args': json.loads(tool.args),
                                       'status': tool.status, 'result': tool.result})
            separator = b', '
    finally:
        await tools.close()
    yield b']}\n'

    # A user message precedes its run-start event. A second forward cursor
    # supplies that metadata without retaining all messages or rescanning tails.
    fields = ('workingDirectory', 'mode', 'permission', 'model')
    current = {key: session[key] for key in fields}
    async with aclosing(rows(db, segments, starts_only=True)) as starts, aclosing(rows(db, segments)) as events:
        upcoming = await anext(starts, None)
        async for position, event, payload in events:
            while upcoming is not None and upcoming[0] <= position:
                current = {key: upcoming[2].get(key, session[key]) for key in fields}
                upcoming = await anext(starts, None)
            metadata = current
            if payload.get('role') == 'user' and upcoming is not None:
                metadata = {key: upcoming[2].get(key, session[key]) for key in fields}
            created = event.created_at.isoformat()
            yield encoded({'schema': 'vibeflow.agent.event.v1', 'session_id': event.conversation_id,
                           'export_session_id': conversation.id, 'event_id': event.event_id, 'eventId': event.event_id,
                           'id': event.id, 'seq': event.seq, 'type': event.type, 'created_at': created, 'time': created,
                           **metadata, 'payload': clean(event.type, payload)}) + b'\n'


async def write_archive(db, conversation, session, clean, download):
    buffer = bytearray()
    async with aclosing(archive_parts(db, conversation, session, clean)) as parts:
        async for part in parts:
            buffer.extend(part)
            if len(buffer) >= 65536:
                await finish_before_cancel(asyncio.to_thread(download.write, buffer))
                buffer.clear()
    if buffer:
        await finish_before_cancel(asyncio.to_thread(download.write, buffer))
    length = download.tell()
    await finish_before_cancel(asyncio.to_thread(download.seek, 0))
    return length
