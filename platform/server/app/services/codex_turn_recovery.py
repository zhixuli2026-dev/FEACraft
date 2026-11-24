"""Reconcile an exited CLI turn owner without replaying inference or approvals."""
import asyncio,json

from fastapi import HTTPException
from sqlalchemy import and_,select,update

from ..db import SessionLocal
from ..models import AgentEvent,Case,ConversationExecution,NativeCliApproval,NativeCliEvent,NativeCliSession,NativeCliTurn
from . import codex_conversations, codex_records, codex_turn_records as turns,conversation_execution as control
from . import process_identity_native as identity
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict, coordinate
from .codex_projection import MAX_TEXT, Secrets
from .durable_events import now
from .workflow_state import ExecutionConflict

SCHEMA='vibeflow.cli-turn-recovery.v1'
EVENT='execution.recovered'
RELEASE='execution.released'
SLOTS=asyncio.Semaphore(2)
EXECUTION_FIELDS=('phase','version','backend','resource_id','owner_id','owner_token_sha256','owner_identity_json','lease_expires_at')
TURN_FIELDS=('version','phase','session_id','case_id','conversation_id','created_by','client_message_id','request_sha256',
    'mode','thread_id','native_turn_id','native_status','attempts_json','error','next_seq','event_bytes')


def turn_binding(row):
    return turns.digest({key:getattr(row,key) for key in TURN_FIELDS if key not in {'next_seq','event_bytes'}})


def session_binding(row):
    return turns.digest({key:getattr(row,key) for key in ('id','version','phase','created_by','case_id','conversation_id',
        'profile_json','process_json','stop_json','owner_identity_json')})


async def evidence(db,row,session):
    # Another worker can commit recovery after this reader loaded the turn.
    # Read the receipt and its bound turn/session in one SQL statement;
    # refreshing only the event list mixes a new receipt with an old ORM row.
    snapshots=(await db.execute(select(NativeCliTurn,NativeCliSession,NativeCliEvent)
        .join(NativeCliSession,NativeCliSession.id==NativeCliTurn.session_id)
        .outerjoin(NativeCliEvent,and_(NativeCliEvent.turn_id==NativeCliTurn.id,
            NativeCliEvent.type.in_((EVENT,RELEASE))))
        .where(NativeCliTurn.id==row.id,NativeCliSession.id==session.id)
        .order_by(NativeCliEvent.seq).limit(3)
        .execution_options(populate_existing=True))).all()
    if not snapshots:raise ExecutionConflict('evidence_invalid','原生回合和会话归属不一致')
    events=[item[2] for item in snapshots if item[2] is not None]
    if not events:return None,False
    if len(events)>2 or events[0].type!=EVENT or len(events)==2 and events[1].type!=RELEASE:
        raise ExecutionConflict('evidence_invalid','原生回合恢复记录重复或顺序无效')
    value=json.loads(events[0].payload_json)
    if (set(value)!={'schema','turn_id','session_id','turn_binding','session_binding','owner','owner_exit',
            'original_execution_version','original_turn_phase','lock_released','observed_at'}
            or value['schema']!=SCHEMA or value['turn_id']!=row.id or value['session_id']!=session.id
            or value['turn_binding']!=turn_binding(row) or value['session_binding']!=session_binding(session)
            or value['owner']!=json.loads(session.owner_identity_json)
            or value['owner_exit']!={'state':'exited','reason':'process_exited'}
            or type(value['original_execution_version']) is not int or value['original_execution_version']<0
            or type(value['lock_released']) is not bool or row.phase not in turns.TERMINAL or row.ended_at is None):
        raise ExecutionConflict('evidence_invalid','原生回合恢复记录与当前回合不符')
    released=value['lock_released']
    if len(events)==2:
        extra=json.loads(events[1].payload_json)
        if released or extra!={'schema':SCHEMA,'recovery_sha256':turns.digest(value),'lock_released':True}:
            raise ExecutionConflict('evidence_invalid','原生回合执行锁释放凭据无效')
        released=True
    return value,released


async def authorized(db,identifier,user_id):
    row=await db.get(NativeCliTurn,identifier,populate_existing=True)
    if row is None or row.created_by!=user_id:raise HTTPException(404,'原生回合不存在')
    conversation=await codex_conversations.authorize(db,row.conversation_id,user_id)
    session=await db.get(NativeCliSession,row.session_id,populate_existing=True)
    if (conversation.case_id!=row.case_id or session is None or session.case_id!=row.case_id
            or session.conversation_id!=row.conversation_id or session.created_by!=user_id
            or (await turns.public(db,row))['phase']=='evidence_invalid'):
        raise ExecutionConflict('evidence_invalid','原生回合和会话归属不一致')
    receipt,released=await evidence(db,row,session)
    execution=await db.get(ConversationExecution,row.conversation_id,populate_existing=True)
    if not released:
        if execution is not None:control.document(execution)
        if (execution is None or execution.phase not in {'active','recovery_required'} or execution.backend!='codex'
                or execution.resource_id!=row.id or execution.owner_identity_json!=session.owner_identity_json):
            # A recovery commit can also land between the coherent snapshot
            # above and this later execution-lock read. Only its verified,
            # already-released receipt permits an idempotent early return.
            receipt,released=await evidence(db,row,session)
            if not released:
                if execution is None:raise ExecutionConflict('evidence_invalid','原生回合缺少执行归属')
                raise ExecutionConflict('state_conflict','原生回合已不属于当前执行锁')
    if (row.created_by!=user_id or session.created_by!=user_id or session.id!=row.session_id
            or conversation.case_id!=row.case_id or session.case_id!=row.case_id
            or session.conversation_id!=row.conversation_id):
        raise ExecutionConflict('evidence_invalid','原生回合和会话归属不一致')
    return row,session,execution,receipt,released


async def output(db,row,receipt,released):
    return {'turn':await turns.public(db,row),'recovery':receipt,'execution_lock_recovered':released,
        'staging_reconciled':False}


async def _completed_answer(db, row, session):
    # Session closure proves the original delivery finished its native and
    # storage cleanup. Only use projected items from the successful attempt.
    if (session.phase != 'closed' or row.phase != 'finalizing' or row.native_status != 'completed'
            or not row.native_turn_id or row.thread_id != session.thread_id):
        return None
    terminal = await db.get(NativeCliEvent, (row.id, row.next_seq - 1))
    if (terminal is None or terminal.type != 'request.completed'
            or json.loads(terminal.payload_json) != {'native_turn_id': row.native_turn_id, 'status': 'completed'}):
        return None
    started = await db.scalar(select(NativeCliEvent).where(NativeCliEvent.turn_id == row.id,
        NativeCliEvent.type == 'turn.started', NativeCliEvent.seq < terminal.seq)
        .order_by(NativeCliEvent.seq.desc()).limit(1))
    if started is None:
        return None
    start = json.loads(started.payload_json)
    if start.get('thread_id') != row.thread_id or start.get('native_turn_id') != row.native_turn_id:
        return None
    items = {}
    size = 0
    events = await db.stream_scalars(select(NativeCliEvent.payload_json).where(
        NativeCliEvent.turn_id == row.id, NativeCliEvent.type == 'item.completed',
        NativeCliEvent.seq > started.seq, NativeCliEvent.seq < terminal.seq)
        .order_by(NativeCliEvent.seq).execution_options(yield_per=16))
    try:
        async for payload in events:
            item = json.loads(payload)
            if item.get('type') not in {'agentMessage', 'plan'}:
                continue
            identifier, text = item.get('id'), item.get('text')
            if not isinstance(identifier, str) or not isinstance(text, str) or identifier in items:
                return None
            size += len(text.encode('utf8')) + (2 if items else 0)
            if size > MAX_TEXT:
                return None
            items[identifier] = text
    finally:
        await events.close()
    text = '\n\n'.join(items.values())
    if not text.strip():
        return None
    # Individual items were redacted at ingestion. Apply the account's
    # stored answer redaction again after joining separate items.
    secrets = Secrets(json.loads(session.profile_json)['auth_home'])
    try:
        text = await finish_before_cancel(asyncio.to_thread(secrets.clean, text))
    except (OSError, ValueError):
        return None
    return text, start.get('model')


async def _publish_answer(db, row, session, answer):
    from .codex_sessions import authorize as authorize_delivery

    try:
        await authorize_delivery(db, row.case_id, row.conversation_id, row.created_by,
                                 expected=json.loads(session.profile_json))
    except (CaseWriteConflict, ExecutionConflict, HTTPException):
        # Apply the same authorization required for an ordinary live delivery.
        return False
    last = await db.scalar(select(AgentEvent.payload).where(
        AgentEvent.conversation_id == row.conversation_id, AgentEvent.type == 'message')
        .order_by(AgentEvent.seq.desc(), AgentEvent.id.desc()).limit(1))
    anchor = json.loads(last) if last is not None else {}
    if (anchor.get('role') != 'user' or anchor.get('backend') != 'codex'
            or anchor.get('native_execution_id') != row.id
            or anchor.get('client_user_message_id') != row.client_message_id):
        return False
    await codex_conversations.append_answer(db, row, *answer)
    row.phase, row.error = 'completed', None
    return True


async def publish(identifier,user_id,snapshot,owner_exit):
    async with SessionLocal() as db:
        row,session,execution,receipt,released=await authorized(db,identifier,user_id)
        if released:return await output(db,row,receipt,True)
        case=await db.get(Case,row.case_id)
        async with coordinate(db,case):
            row,session,execution,receipt,released=await authorized(db,identifier,user_id)
            if released:return await output(db,row,receipt,True)
            value=codex_records.public(session)
            if value['phase'] not in {'closed','failed'} or value['cleanup_verified'] is not True:
                raise ExecutionConflict('recovery_required','原生回合的完整进程范围尚未核验关闭')
            # Retained S3 staging keeps the same fence. Re-reading its receipt
            # must not advance the execution version on every concurrent retry.
            if receipt is not None and await control.unsafe_native(db,row.conversation_id):
                return await output(db,row,receipt,False)
            expected=snapshot['execution']
            # The case fence serializes admission/recovery; this CAS additionally
            # rejects heartbeat or ownership changes since the OS observation.
            found=await db.execute(update(ConversationExecution).where(
                ConversationExecution.conversation_id==row.conversation_id,
                *(getattr(ConversationExecution,key)==val for key,val in expected.items())).values(
                    phase='recovery_required',version=expected['version']+1),execution_options={'synchronize_session':False})
            if found.rowcount!=1:raise ExecutionConflict('state_conflict','执行锁在核验期间发生变化，请重试')
            await db.refresh(execution)
            if receipt is None:
                if any(getattr(row,key)!=val for key,val in snapshot['turn'].items()) or session_binding(session)!=snapshot['session']:
                    raise ExecutionConflict('state_conflict','原生回合在核验期间发生变化，请重试')
                original_phase=row.phase
                interrupted = row.phase not in turns.TERMINAL
                if interrupted:
                    row.phase='interrupted';row.error='OwnerExited';row.ended_at=now()
                await db.execute(update(NativeCliApproval).where(NativeCliApproval.turn_id==row.id,
                    NativeCliApproval.phase.in_(('pending','claimed'))).values(phase='expired'))
                row.version+=1
                await db.flush()
                released=not await control.unsafe_native(db,row.conversation_id)
                if interrupted:
                    status = 'interrupted'
                    if released and snapshot['answer'] is not None and await _publish_answer(db, row, session, snapshot['answer']):
                        status = 'waiting_continue' if row.mode == 'goal' else 'completed'
                    await turns.append(db,row,'stream.done',{'status':status,'native_status':row.native_status,
                        'error':row.error,'cleanup_verified':True},terminal=True)
                receipt={'schema':SCHEMA,'turn_id':row.id,'session_id':session.id,'turn_binding':turn_binding(row),
                    'session_binding':session_binding(session),'owner':json.loads(session.owner_identity_json),'owner_exit':owner_exit,
                    'original_execution_version':expected['version'],'original_turn_phase':original_phase,
                    'lock_released':released,'observed_at':now().isoformat()}
                await turns.append(db,row,EVENT,receipt,terminal=True)
            else:
                released=not await control.unsafe_native(db,row.conversation_id)
                if released:await turns.append(db,row,RELEASE,{'schema':SCHEMA,'recovery_sha256':turns.digest(receipt),'lock_released':True},terminal=True)
            if released:
                execution.phase='idle'
                execution.backend=execution.resource_id=execution.owner_id=execution.owner_token_sha256=execution.owner_identity_json=execution.lease_expires_at=None
            control.document(execution)
            await db.flush()
            await evidence(db,row,session)
        await finish_before_cancel(db.commit())
        # SQLite reloads timezone-aware writes as naive UTC; return the same
        # database representation that an idempotent subsequent request reads.
        await db.refresh(row)
        return await output(db,row,receipt,released)


async def _recover(identifier,user_id):
    async with SessionLocal() as db:
        row,session,execution,receipt,released=await authorized(db,identifier,user_id)
        if released:return await output(db,row,receipt,True)
        owner=json.loads(execution.owner_identity_json)
        original={key:getattr(execution,key) for key in EXECUTION_FIELDS}
    observed=await finish_before_cancel(asyncio.to_thread(identity.probe,owner))
    if observed!={'state':'exited','reason':'process_exited'}:
        raise ExecutionConflict('recovery_required','原生回合所属进程仍存活或身份无法核验')
    if receipt is None:
        from .cli_session_recovery import recover as recover_scope
        if 'management' in json.loads(session.process_json) or codex_records.public(session)['cleanup_verified'] is not True:
            await recover_scope(session.id,user_id)
    async with SessionLocal() as db:
        row,session,execution,receipt,released=await authorized(db,identifier,user_id)
        if released:return await output(db,row,receipt,True)
        if any(getattr(execution,key)!=val for key,val in original.items()):
            if receipt is not None:
                return await output(db,row,receipt,False)
            raise ExecutionConflict('state_conflict','执行锁在范围恢复期间发生变化，请重试')
        snapshot={'execution':original,'turn':{key:getattr(row,key) for key in TURN_FIELDS},'session':session_binding(session),
            'answer':await _completed_answer(db, row, session) if receipt is None else None}
    return await publish(identifier,user_id,snapshot,observed)


async def recover(identifier,user_id):
    try:await asyncio.wait_for(SLOTS.acquire(),.1)
    except TimeoutError as error:raise ExecutionConflict('recovery_busy','原生回合恢复正在核验，请稍后重试') from error
    try:
        async with asyncio.timeout(120):return await _recover(identifier,user_id)
    finally:SLOTS.release()
