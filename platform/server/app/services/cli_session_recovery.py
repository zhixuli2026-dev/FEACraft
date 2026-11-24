"""Recover a dead owner's durably admitted managed CLI scope, never its PID alone."""
import asyncio,copy,json
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import update

from ..db import SessionLocal
from ..models import CliInitializationReservation,CliScopeAllocation,NativeCliSession,User
from . import codex_records as records,cli_scope_lifecycle as lifecycle,cli_scope_allocation as allocations
from . import cli_initialization_capacity as capacity,process_identity_native as identity
from .async_ops import finish_before_cancel
from .durable_events import json_text,now
from .storage_admission import lock_owner
from .workflow_state import ExecutionConflict

SCHEMA='vibeflow.cli-session-scope-recovery.v1'
SLOTS=asyncio.Semaphore(2)


def original_journal(journal):
    value=copy.deepcopy(journal)
    value['management'].pop('recovery',None)
    value['management']['retirement']=None
    return value


def binding(row,journal):
    return {'session_id':row.id,'selection_sha256':records.digest(json.loads(row.profile_json)),
        'owner_sha256':records.digest(json.loads(row.owner_identity_json)),
        'admission_sha256':records.digest(original_journal(journal)),
        'stop_sha256':records.digest(json.loads(row.stop_json))}


def validate(row,journal):
    value=journal['management']['recovery']
    if (not isinstance(value,dict) or set(value)!={'schema','binding','original_version','original_phase','owner_exit','observed_at'}
            or value['schema']!=SCHEMA or value['binding']!=binding(row,journal)
            or type(value['original_version']) is not int or value['original_version']<0 or row.version<=value['original_version']
            or value['original_phase'] not in {'starting','active','failed'}
            or row.phase!='failed' or row.ended_at is None or row.error is None
            or journal['management']['retirement'] is None
            or value['owner_exit']!={'state':'exited','reason':'process_exited'}
            or not isinstance(value['observed_at'],str) or datetime.fromisoformat(value['observed_at']).tzinfo is None):
        raise ValueError('CLI recovery differs from its original session or observed owner exit')
    return value


async def authorized(db,identifier,user_id):
    row=await db.get(NativeCliSession,identifier,populate_existing=True)
    if row is None or row.created_by!=user_id:raise HTTPException(404,'CLI 会话不存在')
    user=await db.get(User,user_id,populate_existing=True)
    if user is None or user.status!='active':raise HTTPException(403,'CLI 用户已不可用')
    journal,_=records.document(row)
    if 'management' not in journal:
        raise ExecutionConflict('recovery_required','旧 CLI 会话缺少完整范围凭据，不能自动恢复')
    allocation=await db.get(CliScopeAllocation,identifier,populate_existing=True)
    if allocation is None:raise ExecutionConflict('evidence_invalid','CLI 会话缺少分配归属记录')
    bound,_,allocated,retired=allocations.document(allocation)
    selected=json.loads(row.profile_json)
    if (retired!={'kind':'adopted','receipt':{'session_id':identifier}} or allocation.user_id!=user_id
            or bound['owner']!=json.loads(row.owner_identity_json) or bound['distribution']!=selected['distro']
            or allocated!=journal['management']['allocation']):
        raise ExecutionConflict('evidence_invalid','CLI 会话已不属于登记的范围')
    credit=await db.get(CliInitializationReservation,identifier,populate_existing=True)
    if credit is None:raise ExecutionConflict('evidence_invalid','CLI 会话缺少初始化预留')
    capacity.validate(credit,row)
    return row,journal,credit


async def observe(row,journal):
    """All process, guardian and cgroup I/O runs outside SQL transactions."""
    from .sandbox_image_guardian import recover as guardian_recover
    from .owned_process import probe_native
    owner_exit=await finish_before_cancel(asyncio.to_thread(identity.probe,json.loads(row.owner_identity_json)))
    if owner_exit['state']!='exited':
        raise ExecutionConflict('recovery_required','CLI 所属进程仍存活或身份无法核验')
    selected=json.loads(row.profile_json);management=journal['management']
    if management['guardian'] is None or management['preflight'] is None:
        raise ExecutionConflict('recovery_required','CLI 缺少已登记的守护或原生出生凭据，保留未验证状态')
    await finish_before_cancel(asyncio.to_thread(lifecycle.check_code,selected))
    guard=await guardian_recover(management['guardian'])
    scope=await lifecycle.scopes.control(selected['distro'],'retire',{'scope':selected['native_management']['scope']})
    admission=management['preflight']
    births={'host':admission['host'],'native':admission['native']['process']}
    if journal['bridge'] is not None:births['bridge']=journal['bridge']
    exits={}
    for role,birth in births.items():
        exits[role]=(await finish_before_cancel(asyncio.to_thread(identity.probe,birth)) if role=='host' else await probe_native(selected['distro'],birth))
        if exits[role]['state']!='exited':raise ExecutionConflict('recovery_required','CLI 范围已处理，但原生出生身份尚未确认退出')
    return {'scope':scope,'guardian':guard,'births':births,'exits':exits},owner_exit


async def publish(snapshot,user_id,proof,owner_exit):
    journal=json.loads(snapshot.process_json)
    journal['management']['retirement']=proof
    journal['management']['recovery']={'schema':SCHEMA,'binding':binding(snapshot,journal),
        'original_version':snapshot.version,'original_phase':snapshot.phase,'owner_exit':owner_exit,'observed_at':now().isoformat()}
    async with SessionLocal() as db:
        # Fresh transaction: no case lock is held while queued readers settle.
        await lock_owner(db,user_id,wait=True)
        current,existing,credit=await authorized(db,snapshot.id,user_id)
        if existing['management']['retirement'] is not None:
            if current.phase not in {'closed','failed'}:raise ExecutionConflict('state_conflict','CLI 范围关闭尚未结束会话')
            return
        expected={name:getattr(snapshot,name) for name in ('version','phase','profile_json','process_json','stop_json',
            'owner_identity_json','owner_token_sha256','owner_id','launch_nonce')}
        found=await db.execute(update(NativeCliSession).where(NativeCliSession.id==snapshot.id,NativeCliSession.created_by==user_id,
            *(getattr(NativeCliSession,key)==value for key,value in expected.items())).values(
                version=snapshot.version+1,phase='failed',error=snapshot.error or 'OwnerExited',ended_at=now(),
                process_json=json_text(journal)),execution_options={'synchronize_session':False})
        if found.rowcount!=1:raise ExecutionConflict('state_conflict','CLI 会话在核验期间发生变化，请重新核验')
        await db.refresh(current);records.document(current)
        if credit.state=='active':capacity.settle(credit,'verified_scope',{'scope_sha256':records.digest(proof)})
        capacity.settlement_ticket(credit,current)
        await finish_before_cancel(db.commit())


async def finish(identifier,user_id):
    """Idempotently settle this exact credit and release its node metadata."""
    async with SessionLocal() as db:
        await lock_owner(db,user_id,wait=True)
        row,journal,credit=await authorized(db,identifier,user_id)
        proof=journal['management']['retirement']
        if proof is None or row.phase not in {'closed','failed'}:
            raise ExecutionConflict('recovery_required','CLI 完整范围关闭尚未得到证明')
        if credit.state=='active':capacity.settle(credit,'verified_scope',{'scope_sha256':records.digest(proof)})
        ticket=capacity.settlement_ticket(credit,row)
        await finish_before_cancel(db.commit())
    if ticket is not None:
        from .cli_node_capacity import release
        await finish_before_cancel(asyncio.to_thread(release,ticket,allow_fragments=True))
    async with SessionLocal() as db:
        row,journal,credit=await authorized(db,identifier,user_id)
        from .cli_node_capacity import outstanding
        pending=ticket is not None and await finish_before_cancel(asyncio.to_thread(outstanding,ticket))
        return {'session':records.public(row),'recovery':journal['management'].get('recovery'),
            'initialization':{'state':credit.state,'remaining_bytes':credit.remaining_bytes,
                'receipt':json.loads(credit.receipt_json),'node_claim_pending':pending},
            'execution_lock_recovered':False,'staging_reconciled':False}


async def _recover(identifier,user_id):
    async with SessionLocal() as db:
        row,journal,_=await authorized(db,identifier,user_id)
        done=journal['management']['retirement'] is not None
    if not done:
        proof,owner_exit=await observe(row,journal)
        await quota_retry(publish,row,user_id,proof,owner_exit)
    return await quota_retry(finish,identifier,user_id)


async def quota_retry(operation,*args):
    # Each failed transaction has exited before this wait. Keep the retained
    # OS proof; a quota-lock conflict never restarts native scope operations.
    for attempt in range(6):
        try:return await operation(*args)
        except HTTPException as error:
            if (error.status_code!=409 or not isinstance(error.detail,dict)
                    or error.detail.get('code')!='storage_quota_busy' or attempt==5):raise
            await asyncio.sleep(.05*2**attempt)


async def recover(identifier,user_id):
    try:await asyncio.wait_for(SLOTS.acquire(),.1)
    except TimeoutError as error:raise ExecutionConflict('recovery_busy','CLI 会话恢复正在核验，请稍后重试') from error
    try:
        async with asyncio.timeout(90):
            async with SessionLocal() as db:
                row=await db.get(NativeCliSession,identifier)
                container=row is not None and row.created_by==user_id and row.runtime=='docker'
            if container:
                from .codex_docker_recovery import recover as recover_docker
                return await recover_docker(identifier,user_id)
            return await _recover(identifier,user_id)
    finally:SLOTS.release()
