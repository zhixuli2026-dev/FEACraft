"""Remote retained versions and logical isolation over the permanent CAS catalog.

Immutable original objects are retained; a verified independent copy and SQL
confirmation identify the selected version. Removing the pending logical name
uses its exact identity after revoking already-sent catalog mutations. Nothing
here infers the original business commit or authorizes an unknown process.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib,json,tempfile,uuid

from botocore.exceptions import ClientError
from .. import storage
from . import attempt_objects as objects,s3_file_transactions as remote
from .case_write import CaseWriteConflict
from .durable_events import json_text
from .s3_streams import snapshot as object_snapshot

_restore=ContextVar('manual_file_restore_provenance',default=None)
BASE=remote.journal.PREFIX+'/quarantine'
MARKERS=remote.journal.PREFIX+'/quarantined'


def digest(value):return hashlib.sha256(json_text(value).encode()).hexdigest()
def fail(message,code='file_recovery_required'):return CaseWriteConflict(code,message)


@contextmanager
def restoring(ticket,request_sha256,source_sha256):
    marker={'schema':'vibeflow.manual-file-restore.v1','ticket_id':ticket,'request_sha256':request_sha256,'source_snapshot_sha256':source_sha256}
    validate_marker(marker);token=_restore.set(marker)
    try:yield marker
    finally:_restore.reset(token)


def validate_marker(marker):
    import re
    if (not isinstance(marker,dict) or set(marker)!={'schema','ticket_id','request_sha256','source_snapshot_sha256'}
            or marker['schema']!='vibeflow.manual-file-restore.v1' or str(uuid.UUID(marker['ticket_id']))!=marker['ticket_id']
            or any(not re.fullmatch('[0-9a-f]{64}',marker[key]) for key in ['request_sha256','source_snapshot_sha256'])):
        raise ValueError('Invalid manual restore journal provenance')


def mark_plan(plan):
    marker=_restore.get()
    if marker is not None:validate_marker(marker);plan['manual_recovery']=dict(marker)


def source_path(source):
    if source=='pending':return remote.PENDING
    if isinstance(source,str) and source.startswith('objects/'):
        value=source[len('objects/'):]
        if str(uuid.UUID(value))==value:return remote._directory(value)
    raise ValueError('Select a pending journal or an exact immutable object version')


def inventory(case,path):
    from .manual_file_recovery import MAX_MEMBERS,MAX_TREE_BYTES
    from ..config import get_settings
    from .attempt_workspace import canonical_path
    client=storage.s3();prefix=storage._s3_key(case.s3_prefix,path).rstrip('/')+'/'
    records=[];continuation=None;total=0
    while True:
        page=client.list_objects_v2(Bucket=get_settings().s3_bucket,Prefix=prefix,MaxKeys=min(MAX_MEMBERS+1,1000),
            **({'ContinuationToken':continuation} if continuation else {}))
        for row in page.get('Contents',[]):
            key=row['Key']
            if not key.startswith(prefix):raise ValueError('S3 retained version listing escaped its namespace')
            relative=key[len(prefix):]
            if not relative or canonical_path(relative)!=relative:raise ValueError('Noncanonical retained object')
            if not row.get('ETag') or type(row.get('Size')) is not int or not 0<=row['Size']<=remote.journal.MAX_BYTES:
                raise ValueError('Retained object lacks a bounded conditional identity')
            total+=row['Size'];records.append({'path':relative,'size':row['Size'],'etag':row['ETag']})
            if len(records)>MAX_MEMBERS or total>MAX_TREE_BYTES:raise ValueError('Remote retained version exceeds inventory bounds')
        if not page.get('IsTruncated'):break
        next_token=page.get('NextContinuationToken')
        if not next_token or next_token==continuation:raise ValueError('Remote retained inventory did not advance')
        continuation=next_token
    return sorted(records,key=lambda row:row['path'])


def marker_path(identifier):return MARKERS+'/'+identifier+'.json'


def snapshot(case,source):
    from .manual_file_recovery import SCHEMA,MAX_TREE_BYTES
    path=source_path(source);files={};versions={};mapping={};total=0;writer='not_recorded';pending_present=False
    encoded,pending_etag,_=remote._read(case.s3_prefix,remote.PENDING,remote.FILE_MANIFEST)
    identifier=None
    if source=='pending':
        if encoded is None:raise FileNotFoundError(path)
        files['pending.json']=objects.digest(encoded);versions['pending.json']=pending_etag;mapping['pending.json']=remote.PENDING;total=len(encoded)
        try:
            document=json.loads(encoded);plan=document['plan']
            if 'writer' in plan:
                from .s3_staging_transactions import writer_state
                try:writer=writer_state(case,document)
                except (ValueError,KeyError,TypeError):writer='unverified'
            candidate=plan['id']
            if str(uuid.UUID(candidate))==candidate:identifier=candidate
        except (ValueError,KeyError,TypeError):pass
        object_path=remote._directory(identifier) if identifier else None
    else:
        identifier=source[len('objects/'):];object_path=path;pending_present=encoded is not None
    if object_path is not None:
        original=inventory(case,object_path)
        for row in original:
            relative=('objects/'+identifier+'/' if source=='pending' else '')+row['path']
            remote_path=object_path+'/'+row['path']
            record,etag,_=object_snapshot(case.s3_prefix,remote_path,limit=row['size'])
            if record is None or etag!=row['etag']:raise fail('保留对象在核对期间变化','input_stale')
            files[relative]=record;versions[relative]=etag;mapping[relative]=remote_path;total+=record['size']
            if total>MAX_TREE_BYTES or len(files)>512:raise ValueError('Remote retained version exceeds its inventory bounds')
        if inventory(case,object_path)!=original:raise fail('保留对象清单在核对期间变化','input_stale')
        if source!='pending' and not files:raise FileNotFoundError(path)
    marker,_,_=remote._read(case.s3_prefix,marker_path(identifier),16384) if identifier else (None,None,None)
    observed,observed_etag,_=remote._read(case.s3_prefix,remote.PENDING,remote.FILE_MANIFEST)
    if encoded!=observed or pending_etag!=observed_etag:raise fail('待恢复指针在核对期间变化','input_stale')
    value={'schema':SCHEMA,'binding':remote._binding(case),'source':source,'path':path,
        'tree':{'files':dict(sorted(files.items())),'directories':[],'specials':{},'total_bytes':sum(r['size'] for r in files.values())},
        'versions':versions,'mapping':mapping,'pointer_etag':pending_etag if source=='pending' else None,
        'object_id':identifier,'writer_state':writer,'pending_blocks_preparation':pending_present,
        'isolation_marker':objects.digest(marker) if marker is not None else None}
    if len(json_text(value).encode())>160*1024:raise ValueError('Manual remote inventory is too large')
    return value


def require_isolatable(source):
    if source.get('writer_state') in {'active','unverified'}:raise fail('远端原写者仍活动或停止尚未核验，请先核查原写者','file_sync_busy')
    if source.get('pending_blocks_preparation'):raise fail('仍有待恢复指针，请先核对并隔离当前事务，再选择准备版本')
    if source.get('isolation_marker') is not None:raise fail('此对象版本已有隔离登记，请查看原操作记录','version_already_isolated')


def copy_new(case,source_path,target,expected):
    existing,_,_=object_snapshot(case.s3_prefix,target,limit=expected['size'])
    if existing is not None:
        if existing!=expected:raise fail('隔离副本已变化，请保留现场')
        return
    from .node_storage_budget import reserve
    root=storage._fs_root();root.mkdir(parents=True,exist_ok=True)
    with reserve(expected['size']),tempfile.TemporaryFile(dir=root) as stream:
        value,_,_=object_snapshot(case.s3_prefix,source_path,output=stream,limit=expected['size'])
        if value!=expected:raise fail('原对象已变化，未采用不同版本','input_stale')
        try:storage.s3().put_object(**objects.arguments(case.s3_prefix,target),Body=stream,IfNoneMatch='*')
        except ClientError as error:
            if error.response.get('ResponseMetadata',{}).get('HTTPStatusCode')!=412:raise
    value,_,_=object_snapshot(case.s3_prefix,target,limit=expected['size'])
    if value!=expected:raise fail('隔离副本不能完整核验')


def isolate(case,ticket,source):
    retained=BASE+'/'+ticket+'/original';descriptor=BASE+'/'+ticket+'/snapshot.json'
    identity={'schema':'vibeflow.remote-file-isolation.v1','ticket_id':ticket,'source_snapshot_sha256':digest(source),
        'source_binding':source['binding'],'files':source['tree']['files']}
    encoded=json_text(identity).encode();saved,_,_=remote._read(case.s3_prefix,descriptor,160*1024)
    if saved is not None and saved!=encoded:raise fail('隔离记录与原确认不同，请保留现场')
    if saved is None:
        require_isolatable(source)
        if snapshot(case,source['source'])!=source:raise fail('原对象版本已变化，未接管不同版本','input_stale')
        for path,expected in source['tree']['files'].items():copy_new(case,source['mapping'][path],retained+'/'+path,expected)
        objects.put_new(case.s3_prefix,descriptor,encoded)
    for path,expected in source['tree']['files'].items():
        value,_,_=object_snapshot(case.s3_prefix,retained+'/'+path,limit=expected['size'])
        if value!=expected:raise fail('隔离副本已变化，不能完成原确认')
    if source['object_id']:
        marker=json_text({'schema':'vibeflow.remote-file-isolation.v1','ticket_id':ticket,'source_snapshot_sha256':digest(source)}).encode()
        objects.put_new(case.s3_prefix,marker_path(source['object_id']),marker)
    if source['source']=='pending':
        data,etag,_=remote._read(case.s3_prefix,remote.PENDING,remote.FILE_MANIFEST)
        if data is not None:
            if etag!=source['pointer_etag'] or objects.digest(data)!=source['tree']['files']['pending.json']:
                raise fail('待恢复指针已有另一个版本，未删除新指针')
            from .s3_live_store import Client,fence
            if not isinstance(storage.s3(),Client):raise fail('远端隔离需要永久版本目录，不能退回裸对象删除')
            fence(case.s3_prefix)
            remote._conditional(case.s3_prefix,remote.PENDING,None,etag,None)
        remaining,_,_=remote._read(case.s3_prefix,remote.PENDING,remote.FILE_MANIFEST)
        if remaining is not None:raise fail('待恢复指针尚未完成隔离')
    return {'retained_path':retained,'original_path':source['path'],'tree_sha256':digest(source['tree']),
        'original_bytes_preserved':True,'live_files_overwritten':False,'original_commit_state':'not_determined',
        'original_remote_objects_retained':True,'old_writer_stop_proven':False,'pending_pointer_detached':source['source']=='pending'}


def selected(case,requested,result,path):
    if not isinstance(path,str) or path not in requested['source']['tree']['files']:raise ValueError('Selected file is outside the isolation record')
    expected=requested['source']['tree']['files'][path];relative=result['retained_path']+'/'+path
    value,_,_=object_snapshot(case.s3_prefix,relative,limit=expected['size'])
    if value!=expected:raise fail('隔离对象已变化，请保留现场','input_stale')
    return relative,expected
