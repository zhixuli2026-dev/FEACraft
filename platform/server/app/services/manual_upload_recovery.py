"""Raw upload preservation through the existing manual-version protocol."""
import hashlib,json,os,re,stat,uuid
from fastapi import HTTPException
from .. import storage
from ..models import DurableEvent,UploadStorageReservation
from . import upload_storage,uploads
from .case_write import CaseWriteConflict
from .durable_events import append_event,json_text,now

SCHEMA='vibeflow.upload-isolation.v1'
EVENT='files.upload_isolated'
NS=uuid.UUID('01fabeb5-97a4-46ef-91bd-8c5b144d57ed')


def identifier(source):
    if not isinstance(source,str) or re.fullmatch('uploads/[0-9a-f]{32}',source) is None:
        raise ValueError('Select an exact retained upload namespace')
    return source[8:]


def path(source):return '.vibeflow/uploads/'+identifier(source)
def event_id(case_id,upload_id):return str(uuid.uuid5(NS,str(case_id)+':'+upload_id))


async def closed(db,case,upload_id):
    row=await db.get(DurableEvent,event_id(case.id,upload_id),populate_existing=True)
    if row is None:return None
    value=json.loads(row.payload_json)
    if (row.case_id!=case.id or row.type!=EVENT or value['schema']!=SCHEMA or value['upload_id']!=upload_id
            or value['binding']!=upload_storage.binding(case)):
        raise CaseWriteConflict('upload_recovery_required','上传隔离证明与当前存储不一致，请保留现场')
    return value


async def require_open(db,case,upload_id):
    value=await closed(db,case,upload_id)
    if value is not None:raise uploads.UploadError('上传已隔离并关闭，请查看原人工版本记录',409)


async def available(db,case,value):
    upload_id=value['upload_id']
    if await closed(db,case,upload_id) is not None:
        raise CaseWriteConflict('upload_already_isolated','上传已经隔离，请查看原人工版本记录')
    if value.get('root_special'):
        raise CaseWriteConflict('upload_path_unverified','上传入口是链接或特殊条目，仅可登记保留，不跟随读取或移动')
    row=await db.get(UploadStorageReservation,upload_id,populate_existing=True)
    if row is not None:
        if row.case_id!=case.id:raise HTTPException(404,'上传会话不存在')
        if row.binding_json!=upload_storage.binding(case):
            raise CaseWriteConflict('upload_binding_unverified','上传额度绑定与当前现场不同，可登记保留后核查归属')
        if row.state not in {'active','completed','cancelled'}:
            raise CaseWriteConflict('upload_recovery_required','上传额度状态无法核验，请保留现场')


def snapshot(case,source):
    from . import manual_file_recovery as manual,manual_file_recovery_s3 as remote
    location=path(source);upload_id=identifier(source)
    if not storage._use_s3():
        from .file_transactions import _path
        from .attempt_workspace import ordinary
        parent=_path(storage._fs_path(case.s3_prefix,'.'),'.vibeflow/uploads');ordinary(parent)
        entry=parent/upload_id;info=entry.lstat()
        special=stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400 or not stat.S_ISDIR(info.st_mode)
        if special:
            value={'kind':'link' if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400 else 'special',
                'device':info.st_dev,'inode':info.st_ino,'size':info.st_size,'mtime_ns':info.st_mtime_ns,'links':info.st_nlink}
            if value['kind']=='link':value['target_sha256']=hashlib.sha256(os.readlink(entry).encode()).hexdigest()
            return {'schema':manual.SCHEMA,'binding':manual.binding(case),'source':source,'path':location,
                'tree':{'files':{},'directories':[],'specials':{'.':value},'total_bytes':0},'upload_id':upload_id,'root_special':True}
        return {'schema':manual.SCHEMA,'binding':manual.binding(case),'source':source,'path':location,
            'tree':manual.tree(case,location),'upload_id':upload_id}
    from .s3_streams import snapshot as observe
    files={};versions={};mapping={};original=remote.inventory(case,location)
    if not original:raise FileNotFoundError(location)
    for row in original:
        name=row['path'];relative=location+'/'+name
        record,etag,_=observe(case.s3_prefix,relative,limit=row['size'])
        if record is None or etag!=row['etag']:raise remote.fail('上传现场在核验期间变化','input_stale')
        files[name]=record;versions[name]=etag;mapping[name]=relative
    if remote.inventory(case,location)!=original:raise remote.fail('上传现场清单在核验期间变化','input_stale')
    value={'schema':manual.SCHEMA,'binding':manual.binding(case),'source':source,'path':location,
        'tree':{'files':files,'directories':[],'specials':{},'total_bytes':sum(v['size'] for v in files.values())},
        'upload_id':upload_id,'mapping':mapping,'versions':versions}
    if len(json_text(value).encode())>160*1024:raise ValueError('Raw upload inventory exceeds its metadata bound')
    return value


def isolate_remote(case,ticket,value):
    from . import manual_file_recovery_s3 as remote,s3_file_transactions as journal,attempt_objects as objects
    from .s3_streams import snapshot as observe
    from .s3_live_store import Client,fence
    if not isinstance(storage.s3(),Client):raise remote.fail('上传隔离需要永久版本目录，不能使用裸对象删除')
    retained=remote.BASE+'/'+ticket+'/original';descriptor=remote.BASE+'/'+ticket+'/snapshot.json'
    encoded=json_text({'schema':SCHEMA,'ticket_id':ticket,'source_snapshot_sha256':remote.digest(value),
        'source_binding':value['binding'],'files':value['tree']['files']}).encode()
    saved,_,_=journal._read(case.s3_prefix,descriptor,160*1024)
    if saved is not None and saved!=encoded:raise remote.fail('上传隔离副本与原确认不一致')
    if saved is None:
        if snapshot(case,value['source'])!=value:raise remote.fail('原上传现场已变化，未隔离不同版本','input_stale')
        for name,record in value['tree']['files'].items():remote.copy_new(case,value['mapping'][name],retained+'/'+name,record)
        objects.put_new(case.s3_prefix,descriptor,encoded)
    for name,record in value['tree']['files'].items():
        observed,_,_=observe(case.s3_prefix,retained+'/'+name,limit=record['size'])
        if observed!=record:raise remote.fail('上传隔离副本已变化，请保留现场')
    # Revoke every already-sent catalog mutation before permanently closing the
    # exact original names. Verified independent copies remain readable.
    fence(case.s3_prefix)
    for row in remote.inventory(case,value['path']):
        if row['path'] not in value['tree']['files'] and row['size']:
            raise remote.fail('上传现场出现新内容，原版本未被采用为新现场','input_stale')
    for name,record in value['tree']['files'].items():
        original=value['mapping'][name];observed,etag,_=observe(case.s3_prefix,original,limit=record['size'])
        if observed is not None and observed['size']==0:continue
        if observed!=record or etag!=value['versions'][name]:raise remote.fail('原上传已有另一个版本，未关闭新内容','input_stale')
        journal._conditional(case.s3_prefix,original,b'',etag,{'ContentType':'application/vnd.vibeflow.closed-upload-source'})
    return {'retained_path':retained,'original_path':value['path'],'tree_sha256':remote.digest(value['tree']),
        'original_bytes_preserved':True,'live_files_overwritten':False,'original_commit_state':'not_determined',
        'upload_id':value['upload_id'],'upload_closed':True,'old_writer_stop_proven':False}


async def register(db,case,requested,result):
    """Close future admission and its promise; preserved bytes remain charged."""
    source=requested['source'];upload_id=source['upload_id']
    prior=await closed(db,case,upload_id)
    if prior is not None:
        if prior['manual_ticket_id']!=requested['ticket_id']:raise CaseWriteConflict('upload_recovery_required','上传已由另一条确认隔离')
        return
    await available(db,case,source)
    row=await db.get(UploadStorageReservation,upload_id,populate_existing=True)
    released=0
    if row is not None and row.state=='active':
        # This only decreases a closed upload's future promise. Physical copies
        # stay in inventory; no other owner's write grant or stop proof changes.
        released=row.remaining_bytes;row.remaining_bytes=0;row.state='cancelled';row.updated_at=now()
    value={'schema':SCHEMA,'case_id':case.id,'upload_id':upload_id,'binding':upload_storage.binding(case),
        'manual_ticket_id':requested['ticket_id'],'source_snapshot_sha256':requested['request']['snapshot_sha256'],
        'retained_path':result['retained_path'],'future_payload_released':released,'stop_evidence_created':False}
    await append_event(db,case.id,EVENT,value,event_id=event_id(case.id,upload_id))


async def fence_pending(lease,value):
    if not storage._use_s3():return
    from .upload_chunk_credit import chunk_paths, close_pending_paths, registered_offsets
    async with lease.control() as db:
        offsets = await registered_offsets(db, value['upload_id'])
    await close_pending_paths(lease.case,chunk_paths(value['upload_id'],offsets))
