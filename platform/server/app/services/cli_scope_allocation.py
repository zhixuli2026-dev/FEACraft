"""Durable allocation before a frozen CLI session can be constructed.

This service owns only an unused native scope. Adoption by a business process
must first transfer responsibility in the same transaction as its CLI journal;
an adopted allocation can never be reclaimed by this pre-session recovery.
"""
import asyncio,hashlib,json,os,re,secrets,uuid
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import update

from ..db import SessionLocal
from ..models import CliScopeAllocation,NativeCliSession,User
from . import native_scope_control as control,native_scope_controller as protocol
from . import process_identity_native as identity
from .async_ops import finish_before_cancel
from .durable_events import json_text
from .workflow_state import ExecutionConflict

SCHEMA='vibeflow.cli-scope-intent.v1'
SLOTS=asyncio.Semaphore(2)


def digest(value):return hashlib.sha256(json_text(value).encode()).hexdigest()


@dataclass(frozen=True)
class Owner:
    id:str
    token:str
    binding_sha256:str


def document(row):
    bound=json.loads(row.binding_json)
    if (set(bound)!={'schema','id','user_id','distribution','seed','owner','helpers'}
            or bound['schema']!=SCHEMA or bound['id']!=row.id or bound['user_id']!=row.user_id
            or str(uuid.UUID(row.id))!=row.id or type(row.user_id) is not int or row.user_id<=0
            or not isinstance(bound['distribution'],str) or not re.fullmatch('[a-z0-9-]{1,128}',bound['distribution'])
            or not isinstance(bound['seed'],str) or not re.fullmatch('[0-9a-f]{64}',bound['seed'])
            or set(bound['helpers'])!=set(protocol.FILES)
            or any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for v in bound['helpers'].values())):
        raise ValueError('Invalid durable CLI scope intent')
    identity.validate(bound['owner'])
    if bound['owner']['platform']!='windows':raise ValueError('CLI scope intent requires its Windows owner')
    prepared=json.loads(row.prepared_json);allocated=json.loads(row.allocated_json);retired=json.loads(row.retirement_json)
    if any(not isinstance(value,dict) for value in (prepared,allocated,retired)):
        raise ValueError('CLI scope journal entries must be objects')
    if prepared:
        control.validate_prepared(prepared)
        request=prepared['request']
        if (request['operation']!='allocate' or request['distribution']!=bound['distribution']
                or request['arguments']!={'seed':bound['seed']} or request['helpers']!=bound['helpers']):
            raise ValueError('Prepared scope changed its durable intent')
    if allocated:
        control.validate_receipt(allocated)
        if not prepared or allocated.get('preparation')!=prepared:
            raise ValueError('Allocated scope lacks its committed preparation')
    if retired:
        if set(retired)!={'kind','receipt'}:raise ValueError('Invalid scope retirement')
        if retired['kind']=='retiring':
            if retired['receipt']!={}:raise ValueError('In-progress retirement cannot contain a result')
        elif retired['kind']=='unused':
            if prepared or allocated or retired['receipt']!={}:raise ValueError('Prepared scope cannot be retired as unused')
        elif retired['kind']=='adopted':
            if not allocated or retired['receipt']!={'session_id':row.id}:
                raise ValueError('Scope adoption differs from its intended session')
        else:
            receipt=control.validate_receipt(retired['receipt'])
            request=receipt['request']
            expected=({'scope':allocated['result']} if allocated else
                {'allocation_request':prepared['request'],'preparation':prepared['native']} if prepared else None)
            operation='retire' if allocated else 'abandon'
            if (retired['kind']!=operation or request['operation']!=operation
                    or request['arguments']!=expected or request['distribution']!=bound['distribution']
                    or request['helpers']!=bound['helpers']):
                raise ValueError('Retirement differs from the persisted native allocation')
    return bound,prepared,allocated,retired


async def create(db,identifier,user_id,distribution):
    user=await db.get(User,user_id,populate_existing=True)
    if user is None or user.status!='active':raise HTTPException(403,'CLI 用户已不可用')
    bound={'schema':SCHEMA,'id':identifier,'user_id':user_id,'distribution':distribution,
        'seed':secrets.token_hex(32),'owner':await asyncio.to_thread(identity.observe,os.getpid()),
        'helpers':{name:hashlib.sha256(control.CONTROLLER.with_name(name).read_bytes()).hexdigest() for name in protocol.FILES}}
    owner=Owner(identifier,secrets.token_hex(32),digest(bound))
    row=CliScopeAllocation(id=identifier,user_id=user_id,binding_json=json_text(bound),
        token_sha256=hashlib.sha256(owner.token.encode()).hexdigest(),prepared_json='{}',allocated_json='{}',retirement_json='{}')
    document(row);db.add(row);await db.flush()
    return owner


async def owned(db,owner):
    row=await db.get(CliScopeAllocation,owner.id,populate_existing=True)
    if (row is None or row.token_sha256!=hashlib.sha256(owner.token.encode()).hexdigest()
            or digest(json.loads(row.binding_json))!=owner.binding_sha256):
        raise ExecutionConflict('state_conflict','CLI 范围分配所有权已变化')
    bound,*_=document(row)
    if bound['owner']!=await asyncio.to_thread(identity.observe,os.getpid()):
        raise ExecutionConflict('state_conflict','CLI 范围分配所属进程已变化')
    return row


async def change(db,row,field,value):
    result=await db.execute(update(CliScopeAllocation).where(CliScopeAllocation.id==row.id,
        CliScopeAllocation.version==row.version,CliScopeAllocation.binding_json==row.binding_json,
        CliScopeAllocation.prepared_json==row.prepared_json,CliScopeAllocation.allocated_json==row.allocated_json,
        CliScopeAllocation.retirement_json==row.retirement_json).values(
            **{field:json_text(value),'version':row.version+1}),execution_options={'synchronize_session':False})
    if result.rowcount!=1:raise ExecutionConflict('state_conflict','CLI 范围分配记录在核验期间发生变化')
    await db.refresh(row);document(row)


async def record(owner,field,value):
    if field not in {'prepared_json','allocated_json'}:raise ValueError('Invalid allocation stage')
    async with SessionLocal() as db:
        row=await owned(db,owner)
        if getattr(row,field)!='{}' or row.retirement_json!='{}':
            raise ExecutionConflict('state_conflict','CLI 范围分配阶段已提交或结束')
        await change(db,row,field,value);await finish_before_cancel(db.commit())


async def allocate(owner,*,check_active=None):
    async with SessionLocal() as db:
        row=await owned(db,owner);bound,prepared,allocated,retired=document(row)
        if prepared or allocated or retired:raise ExecutionConflict('state_conflict','CLI 范围分配不能重复启动')
    async def preparing(value):await record(owner,'prepared_json',value)
    async with asyncio.timeout(60):
        receipt=await control.ScopeControl(bound['distribution'],'allocate',{'seed':bound['seed']},
            on_prepared=preparing,check_active=check_active).run()
    # Persist a completed allocation even when its caller is being cancelled.
    # A crash before this commit is covered by the prepared creator receipt.
    await finish_before_cancel(record(owner,'allocated_json',receipt))
    return receipt['result']


async def adopt(db,owner,session):
    """Caller must commit this together with the frozen CLI session journal."""
    row=await owned(db,owner);bound,_,allocated,retired=document(row)
    selected=json.loads(session.profile_json)
    if (not allocated or retired or session.id!=row.id or session.created_by!=row.user_id
            or session.runtime!='wsl' or selected.get('distro')!=bound['distribution']
            or selected.get('native_management',{}).get('scope')!=allocated['result']
            or json.loads(session.owner_identity_json)!=bound['owner']):
        raise ValueError('CLI session does not own this exact native allocation')
    await change(db,row,'retirement_json',{'kind':'adopted','receipt':{'session_id':row.id}})


async def authorized(db,identifier,user_id):
    row=await db.get(CliScopeAllocation,identifier,populate_existing=True)
    if row is None or row.user_id!=user_id:raise HTTPException(404,'CLI 范围分配不存在')
    user=await db.get(User,user_id,populate_existing=True)
    if user is None or user.status!='active':raise HTTPException(403,'CLI 用户已不可用')
    document(row)
    return row


async def _retire(identifier,user_id,owner=None):
    async with SessionLocal() as db:
        row=await owned(db,owner) if owner is not None else await authorized(db,identifier,user_id)
        bound,prepared,allocated,retired=document(row)
        # A pre-session repair must never kill an adopted or otherwise recorded
        # business scope. Full session recovery owns those separate receipts.
        if retired.get('kind')=='adopted' or await db.get(NativeCliSession,row.id) is not None:
            raise ExecutionConflict('recovery_required','该范围已交给 CLI 会话，必须使用会话恢复')
        if retired and retired['kind']!='retiring':return retired
        # Fence new preparations/adoption before any physical retirement.
        # The durable marker can be resumed after this worker itself dies.
        if not retired:
            if owner is None and (await asyncio.to_thread(identity.probe,bound['owner']))['state']!='exited':
                raise ExecutionConflict('recovery_required','CLI 范围所属进程仍存活或身份无法核验')
            await change(db,row,'retirement_json',{'kind':'retiring','receipt':{}})
            await finish_before_cancel(db.commit())
        snapshot=(row.version,row.binding_json,row.prepared_json,row.allocated_json)
    if owner is None and (await asyncio.to_thread(identity.probe,bound['owner']))['state']!='exited':
        raise ExecutionConflict('recovery_required','CLI 范围所属进程仍存活或身份无法核验')
    if any(hashlib.sha256(control.CONTROLLER.with_name(name).read_bytes()).hexdigest()!=v for name,v in bound['helpers'].items()):
        raise ExecutionConflict('evidence_invalid','CLI 范围控制实现已变化，保留原记录')
    if allocated:
        kind='retire';receipt=await control.control(bound['distribution'],kind,{'scope':allocated['result']})
    elif prepared:
        kind='abandon';receipt=await control.control(bound['distribution'],kind,
            {'allocation_request':prepared['request'],'preparation':prepared['native']})
    else:kind='unused';receipt={}
    result={'kind':kind,'receipt':receipt}
    async with SessionLocal() as db:
        row=await owned(db,owner) if owner is not None else await authorized(db,identifier,user_id)
        _,_,_,retired=document(row)
        if retired and retired['kind']!='retiring':
            if retired['kind']=='adopted':raise ExecutionConflict('state_conflict','CLI 范围已交接')
            return retired
        if (row.version,row.binding_json,row.prepared_json,row.allocated_json)!=snapshot:
            raise ExecutionConflict('state_conflict','CLI 范围记录在核验期间发生变化')
        await change(db,row,'retirement_json',result);await finish_before_cancel(db.commit())
    return result


async def retire(owner):return await _retire(owner.id,None,owner)


async def recover(identifier,user_id):
    try:await asyncio.wait_for(SLOTS.acquire(),.1)
    except TimeoutError as error:raise ExecutionConflict('recovery_busy','CLI 范围恢复正在核验') from error
    try:
        async with asyncio.timeout(90):
            for attempt in range(3):
                try:return await _retire(identifier,user_id)
                except ExecutionConflict as error:
                    if error.code!='state_conflict' or attempt==2:raise
                    await asyncio.sleep(.05)
    finally:SLOTS.release()
