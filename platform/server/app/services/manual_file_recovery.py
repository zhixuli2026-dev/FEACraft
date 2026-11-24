"""Explicit retained-version isolation and new edits, never an inferred rollback.

Local and remote adapters share immutable SQL events binding confirmation, captured
bytes, original location, actor and request key. Existing storage reservations
fence other API writers across process death; leases alone never prove exit.
"""
import asyncio,hashlib,json,os,re,stat,uuid
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import select
from .. import storage
from ..db import SessionLocal
from ..models import Case,CaseEventCounter,CaseStorageOperation,DurableEvent,User
from . import file_transactions as journal,storage_operations as operations
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict,assert_case_available,coordinate,is_case_dictionary
from .durable_events import append_event,json_text,lock_case_execution,now

SCHEMA='vibeflow.manual-file-recovery.v1'
REQUESTED='files.manual_recovery.requested'
COMMITTED='files.manual_recovery.committed'
SETTLED='files.manual_recovery.settled'
REJECTED='files.manual_recovery.rejected'
BASE=journal.PREFIX+'/quarantine'
MAX_MEMBERS=512
MAX_TREE_BYTES=2*1024**3
NAMESPACE=uuid.UUID('2f1689f8-0254-49d4-9e41-c58b96ba560c')


def digest(value):return hashlib.sha256(json_text(value).encode()).hexdigest()
def conflict(message,code='file_recovery_required'):return CaseWriteConflict(code,message)
async def io(function,*args,**kwargs):return await finish_before_cancel(asyncio.to_thread(function,*args,**kwargs))
def event_id(ticket,phase):return str(uuid.uuid5(NAMESPACE,ticket+':'+phase))
def reference(case):return SimpleNamespace(id=case.id,s3_prefix=case.s3_prefix)


def binding(case):
    if storage._use_s3():
        from .s3_file_transactions import _binding
        return _binding(case)
    return journal._binding(case)


def source_path(source):
    if isinstance(source,str) and source.startswith('uploads/'):
        from .manual_upload_recovery import path
        return path(source)
    if storage._use_s3():
        from .manual_file_recovery_s3 import source_path as remote_path
        return remote_path(source)
    if source=='pending':return journal.PREFIX+'/pending'
    if isinstance(source,str) and re.fullmatch('preparing-[0-9a-f-]{36}',source):
        value=source.removeprefix('preparing-')
        if str(uuid.UUID(value))==value:return journal.PREFIX+'/'+source
    raise HTTPException(422,'请选择当前待恢复事务或明确的准备版本')


def tree(case,path):
    """Exact regular bytes, bounded traversal and no followed links/aliases."""
    from .attempt_workspace import ordinary
    from .request_deadline import current
    root=storage._fs_path(case.s3_prefix,'.');directory=journal._path(root,path)
    ordinary(directory)
    if not directory.is_dir():raise ValueError('Retained version is not a directory')
    files={};directories=[];specials={};total=0;members=0
    def scan(base,relative=''):
        nonlocal total,members
        budget=current()
        if budget is not None:budget.check()
        with os.scandir(base) as entries:
            for entry in entries:
                members+=1
                if members>MAX_MEMBERS:raise ValueError('Manual version exceeds bounded member inventory')
                name=relative+'/'+entry.name if relative else entry.name
                from .attempt_workspace import canonical_path
                if canonical_path(name)!=name:raise ValueError('Noncanonical retained member')
                # Windows DirEntry.stat may omit st_nlink/identity fields.
                # Observe the path without following it before classifying it.
                path=base/entry.name;info=path.lstat()
                if (stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400
                        or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))
                        or stat.S_ISREG(info.st_mode) and info.st_nlink>1):
                    special={'kind':'link' if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400 else 'hardlink' if info.st_nlink>1 else 'special',
                        'device':info.st_dev,'inode':info.st_ino,'size':info.st_size,'mtime_ns':info.st_mtime_ns,'links':info.st_nlink}
                    if special['kind']=='link':special['target_sha256']=hashlib.sha256(os.readlink(path).encode()).hexdigest()
                    specials[name]=special;continue
                ordinary(path)
                if stat.S_ISDIR(info.st_mode):directories.append(name);scan(path,name)
                else:
                    record=journal._capture(path,limit=journal.MAX_BYTES)
                    if record is None:raise conflict('版本在读取时消失，请重新核对','input_stale')
                    total+=record['size']
                    if total>MAX_TREE_BYTES:raise ValueError('Manual version exceeds 2 GiB')
                    files[name]=record
    scan(directory)
    return {'files':dict(sorted(files.items())),'directories':sorted(directories),'specials':dict(sorted(specials.items())),'total_bytes':total}


def snapshot(case,source):
    if isinstance(source,str) and source.startswith('uploads/'):
        from .manual_upload_recovery import snapshot as upload_snapshot
        return upload_snapshot(case,source)
    if storage._use_s3():
        from .manual_file_recovery_s3 import snapshot as remote_snapshot
        return remote_snapshot(case,source)
    path=source_path(source)
    value={'schema':SCHEMA,'binding':journal._binding(case),'source':source,'path':path,'tree':tree(case,path)}
    if len(json_text(value).encode())>160*1024:raise ValueError('Manual version inventory is too large')
    return value


async def authorized(db,case_id,user_id,*,write=False):
    from ..routers.workspaces import get_owned_case
    user=await db.get(User,user_id,populate_existing=True)
    if user is None or user.status!='active':raise HTTPException(403,'账户已不可用')
    case=await get_owned_case(case_id,user,db,write=write);assert_case_available(case)
    return case


async def controlled(db,case_id,user_id,*,write=False):
    await authorized(db,case_id,user_id,write=write)
    await lock_case_execution(db,case_id)
    return await authorized(db,case_id,user_id,write=write)


async def write_access(db,case_id,user_id):
    try:await authorized(db,case_id,user_id,write=True)
    except HTTPException as error:
        if error.status_code not in {403,404}:raise
        return {'code':'read_only','message':'当前账户只有查看权限，请由有写权限的成员确认处理'}
    return None


def require_isolatable(value):
    if storage._use_s3() and 'upload_id' not in value:
        from .manual_file_recovery_s3 import require_isolatable as remote_check
        remote_check(value)


async def require_no_pending(case):
    if storage._use_s3():
        from .s3_file_transactions import _read,PENDING,FILE_MANIFEST
        value,_,_=await io(_read,case.s3_prefix,PENDING,FILE_MANIFEST)
        exists=value is not None
    else:exists=journal._path(storage._fs_path(case.s3_prefix,'.'),journal.PREFIX+'/pending').exists()
    if exists:raise conflict('当前还有待恢复文件事务，请先核对该事务，再恢复所选文件')


async def drained(db,case):
    from .case_validation import assert_writable_locked
    await assert_writable_locked(db,case.id)
    await writers_stopped(db,case)


async def writers_stopped(db,case):
    from .case_validation import require_sessions_stopped_locked
    from .job_write_checkpoint import require_drained_locked
    await require_sessions_stopped_locked(db,case)
    await require_drained_locked(db,case)


def decode(event,case_id,kind):
    try:
        value=json.loads(event.payload_json)
        if event.case_id!=case_id or event.type!=kind or value['schema']!=SCHEMA or value['case_id']!=case_id:raise ValueError()
        return value
    except (ValueError,KeyError,TypeError,AttributeError):raise conflict('人工恢复审计无法完整核验，请保留现场') from None


async def recorded(db,case_id,ticket,kind):
    event=await db.get(DurableEvent,event_id(ticket,kind),populate_existing=True)
    return decode(event,case_id,kind) if event is not None else None


async def request_record(db,case_id,ticket):
    value=await recorded(db,case_id,ticket,REQUESTED)
    if value is None:raise HTTPException(404,'人工恢复记录不存在')
    if value['ticket_id']!=ticket or value['request_sha256']!=digest(value['request']):raise conflict('人工恢复请求摘要不匹配')
    return value


async def receipt(db,case_id,ticket):
    requested=await request_record(db,case_id,ticket)
    committed=await recorded(db,case_id,ticket,COMMITTED)
    settled=await recorded(db,case_id,ticket,SETTLED)
    rejected=await recorded(db,case_id,ticket,REJECTED)
    return {'schema':SCHEMA,'id':ticket,'case_id':case_id,'action':requested['request']['action'],
        'actor_user_id':requested['actor_user_id'],'recorded_at':requested['recorded_at'],'note':requested['request']['note'],
        'source_snapshot_sha256':requested['request']['snapshot_sha256'],
        'state':'rejected' if rejected else 'completed' if settled else 'committed_cleanup_pending' if committed else 'pending',
        'rejection':rejected['result'] if rejected else None,
        'result':committed['result'] if committed else None,'stop_evidence_created':False,'old_sql_commit_rewritten':False,
        'execution_authorized':False}


async def plan(case_id,user_id,source):
    async with SessionLocal() as db:
        case=await controlled(db,case_id,user_id)
        value=await io(snapshot,reference(case),source)
        readonly=await write_access(db,case_id,user_id);blocked=readonly
        if blocked is None:
            try:
                await drained(db,case);require_isolatable(value)
                if 'upload_id' in value:
                    from .manual_upload_recovery import available
                    await available(db,case,value)
            except CaseWriteConflict as error:blocked={'code':error.code,'message':str(error)}
        counter=await db.get(CaseEventCounter,case_id)
        return {'schema':SCHEMA,'case_id':case_id,'source':source,'snapshot_sha256':digest(value),
            'expected_revision':case.dict_revision,'expected_event_sequence':counter.next_seq if counter else 1,
            'files':[{'path':path,**record} for path,record in value['tree']['files'].items()],
            'directories':value['tree']['directories'],'total_bytes':value['tree']['total_bytes'],
            'special_entries':[{'path':path,'kind':value['kind']} for path,value in value['tree']['specials'].items()],
            'available_actions':([] if readonly else ['retain_for_review'])+(['isolate_version'] if blocked is None else []),
            'blocked':blocked,'commit_state':'not_determined','automatic_recovery_authorized':False}


async def history(case_id,user_id,limit=50):
    return (await history_page(case_id,user_id,limit))['items']


async def history_page(case_id,user_id,limit=50,before_seq=None):
    async with SessionLocal() as db:
        await authorized(db,case_id,user_id)
        query=select(DurableEvent).where(DurableEvent.case_id==case_id,DurableEvent.type==REQUESTED)
        if before_seq is not None:query=query.where(DurableEvent.seq<before_seq)
        rows=list(await db.scalars(query.order_by(DurableEvent.seq.desc()).limit(limit+1)))
        values=[decode(row,case_id,REQUESTED) for row in rows[:limit]]
        return {'items':[await receipt(db,case_id,row['ticket_id']) for row in values],
            'next_before_seq':rows[limit-1].seq if len(rows)>limit else None}


async def detail(case_id,user_id,ticket):
    async with SessionLocal() as db:
        await authorized(db,case_id,user_id)
        old=await request_record(db,case_id,ticket)
        value=await receipt(db,case_id,ticket)
        inventory=old['source'].get('tree',{})
        return {**value,'files':[{'path':path,**record} for path,record in inventory.get('files',{}).items()],
            'confirmation':{'request':old['request'],'request_sha256':old['request_sha256'],
                'can_continue':old['actor_user_id']==user_id and await write_access(db,case_id,user_id) is None},
            'special_entries':[{'path':path,'kind':record['kind']} for path,record in inventory.get('specials',{}).items()]}


def validate_body(body,key):
    if not isinstance(key,str) or not 1<=len(key)<=128 or '\x00' in key:raise HTTPException(422,'需要固定的 Idempotency-Key')
    if body['action'] not in {'retain_for_review','isolate_version','restore_file'}:raise HTTPException(422,'人工恢复操作无效')
    if not body['note'].strip():raise HTTPException(422,'请填写本次人工处理的原因')
    if body['action']=='restore_file':
        if not body.get('isolation_id') or not body.get('file_path') or not body.get('target_path'):raise HTTPException(422,'请明确选择隔离记录、保留文件和目标路径')
        from .attempt_workspace import canonical_path
        if canonical_path(body['target_path'])!=body['target_path']:raise HTTPException(422,'目标路径必须是算例内规范路径')
        if body['target_path'].split('/')[0].rstrip(' .').casefold()=='.vibeflow':raise HTTPException(422,'不能向内部执行或恢复目录写入所选版本')
    else:
        source_path(body['source'])
        if any(body.get(k) is not None for k in ['isolation_id','file_path','target_path','expected_target_sha256']):raise HTTPException(422,'此操作不接受恢复文件参数')


async def isolation(db,case_id,ticket):
    requested=await request_record(db,case_id,ticket)
    committed=await recorded(db,case_id,ticket,COMMITTED)
    if requested['request']['action']!='isolate_version' or committed is None or not await recorded(db,case_id,ticket,SETTLED):
        raise conflict('请先完成原版本隔离，再选择保留文件')
    return requested,committed['result']


def selected(case,requested,result,path):
    if storage._use_s3():
        from .manual_file_recovery_s3 import selected as remote_selected
        return remote_selected(case,requested,result,path)
    if not isinstance(path,str) or path not in requested['source']['tree']['files']:raise HTTPException(422,'所选文件不属于原隔离版本')
    expected=requested['source']['tree']['files'][path]
    root=storage._fs_path(case.s3_prefix,'.');file=journal._path(root,result['retained_path']+'/'+path)
    if journal._capture(file)!=expected:raise conflict('保留文件已经变化，请保留现场并重新核对','input_stale')
    return result['retained_path']+'/'+path,expected


async def prepare_request(db,case,user_id,body,ticket):
    if body['action']!='retain_for_review':await drained(db,case)
    counter=await db.get(CaseEventCounter,case.id,populate_existing=True)
    if case.dict_revision!=body['expected_revision'] or (counter.next_seq if counter else 1)!=body['expected_event_sequence']:
        raise conflict('算例版本或事件已变化，请刷新后重新确认','input_stale')
    if body['action']=='restore_file':
        await require_no_pending(case)
        old,result=await isolation(db,case.id,body['isolation_id'])
        relative,expected=await io(selected,reference(case),old,result,body['file_path'])
        from .case_write import file_hash
        if await io(file_hash,case.s3_prefix,body['target_path'])!=body.get('expected_target_sha256'):
            raise conflict('目标文件已经变化，请重新检查目标版本','file_conflict')
        if is_case_dictionary(case.s3_prefix,body['target_path']):
            # Reject an invalid schema/stage choice before persisting a request
            # or reserving storage; it must not strand a healthy case in recovery.
            from .casedict import _validate_update
            content=await io(selected_case_dict,reference(case),relative,expected)
            await io(_validate_update,SimpleNamespace(dict_revision=case.dict_revision,workflow_state=case.workflow_state),
                content,'manual_recovery',expected_revision=body['expected_revision'])
        source={'schema':SCHEMA,'binding':binding(case),'isolation_id':body['isolation_id'],
            'file_path':body['file_path'],'retained_path':relative,'file':expected}
    else:
        source=await io(snapshot,reference(case),body['source'])
        if body['action']=='isolate_version':
            require_isolatable(source)
            if 'upload_id' in source:
                from .manual_upload_recovery import available
                await available(db,case,source)
    if digest(source)!=body['snapshot_sha256']:raise conflict('保留版本已经变化，请刷新后重新确认','input_stale')
    value={'schema':SCHEMA,'ticket_id':ticket,'case_id':case.id,'actor_user_id':user_id,'recorded_at':now().isoformat(),
        'request_sha256':digest(body),'request':body,'source':source}
    await append_event(db,case.id,REQUESTED,value,event_id=event_id(ticket,REQUESTED))
    await finish_before_cancel(db.commit())
    return value


async def restore_plan(case_id,user_id,isolation_id,file_path):
    async with SessionLocal() as db:
        case=await authorized(db,case_id,user_id)
        source_case=reference(case)
        source_binding=binding(case)
        old,result=await isolation(db,case_id,isolation_id)
    # The settled isolation receipt and member digest fix the retained source.
    # Hash it outside SQL, then refresh authorization and confirmation state.
    relative,expected=await io(selected,source_case,old,result,file_path)
    async with SessionLocal() as db:
        case=await controlled(db,case_id,user_id)
        if binding(case)!=source_binding or await isolation(db,case_id,isolation_id)!=(old,result):
            raise conflict('保留版本已经变化，请刷新后重新确认','input_stale')
        source={'schema':SCHEMA,'binding':source_binding,'isolation_id':isolation_id,
            'file_path':file_path,'retained_path':relative,'file':expected}
        blocked=await write_access(db,case_id,user_id)
        if blocked is None:
            try:await drained(db,case);await require_no_pending(case)
            except CaseWriteConflict as error:blocked={'code':error.code,'message':str(error)}
        counter=await db.get(CaseEventCounter,case_id)
        return {'case_id':case_id,'isolation_id':isolation_id,'file_path':file_path,'file':expected,
            'snapshot_sha256':digest(source),'expected_revision':case.dict_revision,
            'expected_event_sequence':counter.next_seq if counter else 1,'available':blocked is None,'blocked':blocked}


def isolate_local(case,ticket,source):
    expected=source['tree'];root=storage._fs_path(case.s3_prefix,'.')
    original=journal._path(root,source['path']);retained=BASE+'/'+ticket+'/original'
    target=journal._path(root,retained)
    if target.exists():
        if original.exists() or tree(case,retained)!=expected:raise conflict('隔离位置或原版本已变化，请保留现场')
    else:
        if snapshot(case,source['source'])!=source:raise conflict('原版本已经变化，未移动任何文件','input_stale')
        from .fs_directory import move_new
        move_new(original,target,root=root)
        journal._sync(original.parent);journal._sync(target.parent)
        if tree(case,retained)!=expected:raise conflict('隔离后版本无法完整核验，请保留现场')
    return {'retained_path':retained,'original_path':source['path'],'tree_sha256':digest(expected),
        'original_bytes_preserved':True,'live_files_overwritten':False,'original_commit_state':'not_determined'}


async def emit(db,case_id,ticket,kind,result):
    value={'schema':SCHEMA,'ticket_id':ticket,'case_id':case_id,'result':result}
    old=await recorded(db,case_id,ticket,kind)
    if old is not None:
        if old!=value:raise conflict('恢复操作已有不同的持久结果')
        return
    await append_event(db,case_id,kind,value,event_id=event_id(ticket,kind))


async def _perform(case_id,user_id,body,ticket,*,existing_only=False):
    async with SessionLocal() as db:
        case=await controlled(db,case_id,user_id,write=True)
        existing=await recorded(db,case_id,ticket,REQUESTED)
        if existing is None:
            if existing_only:raise HTTPException(404,'原人工恢复记录不存在，未创建新的确认')
            requested=await prepare_request(db,case,user_id,body,ticket)
        else:
            requested=await request_record(db,case_id,ticket)
            if requested['actor_user_id']!=user_id or requested['request']!=body:raise conflict('同一提交标识已经绑定另一份确认','idempotency_conflict')
        if await recorded(db,case_id,ticket,SETTLED):return await receipt(db,case_id,ticket)
        rejected=await recorded(db,case_id,ticket,REJECTED)
        if rejected:raise conflict(rejected['result']['message'],rejected['result']['code'])
        if body['action']=='retain_for_review':
            await emit(db,case_id,ticket,COMMITTED,{'original_location_retained':True,'source_path':requested['source']['path'],
                'original_commit_state':'not_determined','write_limits_relaxed':False})
            await emit(db,case_id,ticket,SETTLED,{'action_complete':True});await finish_before_cancel(db.commit())
            return await receipt(db,case_id,ticket)
    reservation=(ticket,body['snapshot_sha256'],event_id(ticket,REQUESTED))
    async with SessionLocal() as db:
        await authorized(db,case_id,user_id,write=True)
        row=await operations.active(db,case_id)
        owner=row.owner_json if row is not None and (row.journal_id,row.plan_sha256,row.event_id)==reservation else None
    if owner is not None:
        from . import process_identity_native as identity
        state=await io(identity.probe,identity.validate(json.loads(owner)))
        if state['state']=='match':
            async with SessionLocal() as db:
                await authorized(db,case_id,user_id,write=True)
                return await receipt(db,case_id,ticket)
    if storage._use_s3():
        from . import owner_storage_budget
        async with SessionLocal() as db:
            case=await authorized(db,case_id,user_id,write=True)
            owner_id=(await owner_storage_budget.owner_for_case(db,case)).id
        # Only a proven exited writer permits closure; unknown PUTs retain credit.
        await owner_storage_budget.recover_owner(owner_id)
    async def checked(db):
        current=await controlled(db,case_id,user_id,write=True)
        if binding(current)!=requested['source']['binding']:raise conflict('存储位置已经变化，请保留原记录','input_stale')
        rows=await db.scalars(select(CaseStorageOperation).where(CaseStorageOperation.case_id==case_id,CaseStorageOperation.state!='settled'))
        for row in rows:
            if (row.purpose!='recovery' or row.source_prefix!=current.s3_prefix
                    or (row.journal_id,row.plan_sha256,row.event_id)!=reservation):raise operations.busy()
        # Serialize with new native admissions before committing this lease.
        # The original owner may have died: existing storage ownership is
        # checked independently by hold(), never inferred from these SQL rows.
        await writers_stopped(db,current)
        return current
    async with operations.hold(checked,purpose='recovery',binding=reservation) as lease:
        async with lease.control() as db:
            case=await db.get(Case,case_id,populate_existing=True);await drained(db,case)
            committed=await recorded(db,case_id,ticket,COMMITTED)
        if committed is None:
            if body['action']=='isolate_version':
                try:
                    if storage._use_s3():
                        if 'upload_id' in requested['source']:
                            from .manual_upload_recovery import isolate_remote as isolate
                        else:from .manual_file_recovery_s3 import isolate
                    else:isolate=isolate_local
                    result=await io(isolate,lease.case,ticket,requested['source'])
                except CaseWriteConflict as error:
                    if error.code=='input_stale':
                        async with lease.control() as db:
                            await emit(db,case_id,ticket,REJECTED,{'code':error.code,'message':str(error)})
                        lease.resolved=True
                    raise
                if 'upload_id' in requested['source']:
                    from .manual_upload_recovery import fence_pending
                    await fence_pending(lease,requested['source'])
                async with lease.control() as db:
                    if 'upload_id' in requested['source']:
                        from .manual_upload_recovery import register
                        case=await db.get(Case,case_id,populate_existing=True)
                        await register(db,case,requested,result)
                    await emit(db,case_id,ticket,COMMITTED,result)
            else:
                from . import owner_storage_budget
                from .attempt_workspace import assert_mutable_path,canonical_path
                target=body['target_path']
                if canonical_path(target)!=target:raise HTTPException(422,'目标路径必须是算例内规范路径')
                assert_mutable_path(lease.case.s3_prefix,target)
                if storage._use_s3():await recover_remote_restore(lease,requested,committed=False)
                async with SessionLocal() as db:
                    case=await authorized(db,case_id,user_id,write=True)
                    async with owner_storage_budget.hold_case(db,case) as payer:
                        case=await checked(db)
                        if (await owner_storage_budget.owner_for_case(db,case)).id!=payer.id:
                            raise conflict('存储额度归属已变化，请重新核对','input_stale')
                        old,result=await isolation(db,case_id,body['isolation_id'])
                        relative,expected=await io(selected,reference(case),old,result,body['file_path'])
                        if expected!=requested['source']['file']:raise conflict('保留文件已经变化','input_stale')
                        async with coordinate(db,case,expected_revision=body['expected_revision'],expected_files={target:body.get('expected_target_sha256')}):
                            from contextlib import nullcontext
                            from .manual_file_recovery_s3 import restoring
                            provenance=restoring(ticket,requested['request_sha256'],body['snapshot_sha256']) if storage._use_s3() else nullcontext()
                            with provenance:
                                transaction=await restore_selected(db,case,body,relative,expected)
                            await emit(db,case_id,ticket,COMMITTED,{'target_path':target,'file':expected,'transaction_id':transaction,
                                'isolation_id':body['isolation_id'],'new_edit':True,'dict_revision':case.dict_revision})
                        await finish_before_cancel(db.commit())
        if storage._use_s3() and body['action']=='restore_file':await recover_remote_restore(lease,requested,committed=True)
        async with lease.control() as db:
            case=await db.get(Case,case_id,populate_existing=True)
            completed=await recorded(db,case_id,ticket,COMMITTED)
            if body['action']=='restore_file' and not storage._use_s3():
                pending=journal._path(storage._fs_path(case.s3_prefix,'.'),journal.PREFIX+'/pending')
                if pending.exists():
                    document=await io(journal._load,reference(case),pending)
                    if document['plan']['id']!=completed['result']['transaction_id']:raise conflict('另一个文件事务尚未核对，请保留现场')
                    await journal.recover_locked(db,case)
            await emit(db,case_id,ticket,SETTLED,{'action_complete':True})
        lease.resolved=True
        async with SessionLocal() as db:
            await authorized(db,case_id,user_id,write=True)
            return await receipt(db,case_id,ticket)


async def file_reader(case_id,user_id,ticket,path):
    async with SessionLocal() as db:
        case=await authorized(db,case_id,user_id)
        old,result=await isolation(db,case_id,ticket)
        relative,expected=await io(selected,reference(case),old,result,path)
        from .file_read import FileRead
        reader=await io(FileRead,case.s3_prefix,relative)
        if storage._use_s3():
            from .s3_streams import snapshot as remote_snapshot
            value,etag,_=await io(remote_snapshot,case.s3_prefix,relative,limit=expected['size'])
            if value!=expected or etag!=reader.etag:raise conflict('保留对象已经变化','input_stale')
        else:
            from .file_read import _identity
            if (reader.size!=expected['size'] or await io(journal._capture,reader.path)!=expected
                    or _identity(reader.path.stat())!=reader.identity):raise conflict('保留文件已经变化','input_stale')
        return reader


def selected_case_dict(case,relative,expected):
    if expected['size']>4*1024*1024:raise HTTPException(422,'caseDict版本过大，请保留文件并人工核查')
    from .attempt_objects import read,checked
    if storage._use_s3():data=read(case.s3_prefix,relative,limit=expected['size'])
    else:
        from .attempt_workspace import ordinary
        from .file_read import FileRead,FileReadConflict,_identity
        path=journal._path(storage._fs_path(case.s3_prefix,'.'),relative)
        before=ordinary(path)
        if before.st_size!=expected['size']:raise conflict('所选caseDict版本已经变化','input_stale')
        reader=FileRead(case.s3_prefix,relative)
        if reader.identity!=_identity(before):raise conflict('所选caseDict版本已经变化','input_stale')
        try:data=reader.read()
        except FileReadConflict as error:raise conflict('所选caseDict版本已经变化','input_stale') from error
        if _identity(ordinary(path))!=_identity(before):raise conflict('所选caseDict版本已经变化','input_stale')
    return checked(data,expected).decode('utf8')


async def restore_selected(db,case,body,relative,expected):
    target=body['target_path']
    if is_case_dictionary(case.s3_prefix,target):
        from .casedict import write_case_dict
        content=await io(selected_case_dict,reference(case),relative,expected)
        await write_case_dict(db,case,content,source='manual_recovery',expected_revision=body['expected_revision'])
        return db.info['local_file_transactions'][case.id][1]
    if storage._use_s3():
        from .s3_file_transactions import RemoteSource
        from .s3_streams import snapshot as remote_snapshot
        previous,_,_=await io(remote_snapshot,case.s3_prefix,target,limit=journal.MAX_BYTES)
        data=RemoteSource(relative,expected)
    else:
        previous=await io(journal._capture,storage._fs_path(case.s3_prefix,target))
        data=journal.FileSource(relative,expected)
    if (previous['sha256'] if previous else None)!=body.get('expected_target_sha256'):
        raise conflict('目标文件已变化，未采用新目标作为旧确认的前镜像','file_conflict')
    return await journal.write(db,case,{target:data},expected_files={target:previous})


async def recover_remote_restore(lease,requested,*,committed):
    from . import s3_file_transactions as remote
    from .s3_storage_recovery import recover_reserved
    from .manual_file_recovery_s3 import restoring
    body=requested['request'];ticket=requested['ticket_id']
    with restoring(ticket,requested['request_sha256'],body['snapshot_sha256']) as marker:
        document,_=await io(remote._load,lease.case,verify_blobs=False)
        if document is None:return
        if document['plan'].get('manual_recovery')!=marker:raise conflict('另一个远端文件事务尚未核对，请保留现场')
        async with lease.control(authorize=False) as db:
            witness=await remote.journal._witness(db,lease.case,document)
            actual=await recorded(db,lease.case.id,ticket,COMMITTED)
            if bool(witness)!=committed or (actual is not None)!=committed:raise conflict('远端恢复文件与人工提交审计不一致，请保留现场')
            if committed and actual['result']['transaction_id']!=document['plan']['id']:raise conflict('远端事务不属于本次已提交恢复')
        try:await recover_reserved(lease)
        finally:lease.resolved=False


async def perform(case_id,user_id,body,key):
    validate_body(body,key)
    ticket=str(uuid.uuid5(NAMESPACE,str(case_id)+':'+str(user_id)+':'+key))
    return await perform_ticket(case_id,user_id,body,ticket)


async def continue_action(case_id,user_id,ticket,request_sha256):
    # A ticket is a persisted operation identity, never a newly generated key.
    # Recheck actor and write access before returning even a completed receipt.
    async with SessionLocal() as db:
        await authorized(db,case_id,user_id,write=True)
        requested=await request_record(db,case_id,ticket)
        if requested['actor_user_id']!=user_id:raise HTTPException(403,'只能由原确认账户接续此操作')
        if requested['request_sha256']!=request_sha256:raise conflict('原确认内容与核对的摘要不一致','idempotency_conflict')
        body=requested['request']
        validate_body(body,ticket)
    return await perform_ticket(case_id,user_id,body,ticket,existing_only=True)


async def perform_ticket(case_id,user_id,body,ticket,*,existing_only=False):
    try:return await _perform(case_id,user_id,body,ticket,existing_only=existing_only)
    except CaseWriteConflict as error:
        if error.code!='file_sync_busy':raise
        # Two identical confirmations can both observe an empty slot before
        # either acquires it. Report the admitted original, never start a
        # second physical mutation or turn a different key into this request.
        async with SessionLocal() as db:
            await authorized(db,case_id,user_id,write=True)
            record=await recorded(db,case_id,ticket,REQUESTED)
            if record is None or record['actor_user_id']!=user_id or record['request']!=body:raise
            if await recorded(db,case_id,ticket,SETTLED):return await receipt(db,case_id,ticket)
            row=await operations.active(db,case_id)
            binding=(ticket,body['snapshot_sha256'],event_id(ticket,REQUESTED))
            owner=row.owner_json if row is not None and (row.journal_id,row.plan_sha256,row.event_id)==binding else None
        if owner is not None:
            from . import process_identity_native as identity
            observed=await io(identity.probe,identity.validate(json.loads(owner)))
            if observed['state']=='match':
                async with SessionLocal() as db:
                    await authorized(db,case_id,user_id,write=True)
                    return await receipt(db,case_id,ticket)
        raise
