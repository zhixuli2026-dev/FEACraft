"""Bounded observations of real upload bytes; no browser record is required."""
import hashlib,heapq,json,os,re
from .. import storage
from ..config import get_settings
from . import uploads
from .attempt_workspace import assert_mutable_path,canonical_path,ordinary
from .case_write import CaseWriteConflict,is_case_dictionary
from .durable_events import json_text
from .file_read import FileRead

BLOCK=4*1024*1024
HEX=re.compile('[0-9a-f]{32}')


def check_id(identifier):
    if not isinstance(identifier,str) or HEX.fullmatch(identifier) is None:
        raise uploads.UploadError('上传会话不存在',404)
    return identifier


def budget():
    from .request_deadline import current
    value=current()
    if value is not None:value.check()


def identifiers(prefix,after,limit):
    """Return ascending session IDs with a bounded result, including SQL orphans."""
    if after:check_id(after)
    if not storage._use_s3():
        from .file_transactions import _path
        root=_path(storage._fs_path(prefix,'.'),'.vibeflow/uploads')
        if not root.exists():return []
        ordinary(root)
        def names():
            with os.scandir(root) as entries:
                for entry in entries:
                    budget()
                    if HEX.fullmatch(entry.name) and entry.name>after:yield entry.name
        return heapq.nsmallest(limit,names())
    base=storage._s3_key(prefix,'.vibeflow/uploads/')
    base=base.rstrip('/')+'/'
    found=set();cursor=base+after+'0' if after else base
    while len(found)<limit:
        budget()
        page=storage.s3().list_objects_v2(Bucket=get_settings().s3_bucket,Prefix=base,StartAfter=cursor,MaxKeys=128)
        keys=[item['Key'] for item in page.get('Contents',[])]
        if not keys:break
        values={key[len(base):].split('/',1)[0] for key in keys}
        valid={value for value in values if HEX.fullmatch(value) and value>after}
        found.update(valid)
        # Every child path sorts before '<32-hex-id>0'. Skip all remaining
        # chunks of that session without loading their payloads or every page.
        cursor=base+max(valid)+'0' if valid else keys[-1]
        if not page.get('IsTruncated'):break
    return sorted(found)[:limit]


def metadata(prefix,identifier):
    check_id(identifier)
    if storage._use_s3():
        from .s3_uploads import _meta
        value,etag=_meta(prefix,identifier)
        if value['status']!='uploading':raise uploads.UploadError('上传已取消',404)
        size=value['offset'];identity=etag
    else:
        directory,value=uploads._read_meta(prefix,identifier)
        ordinary(directory);ordinary(directory/'meta.json');ordinary(directory/'data.part')
        reader=FileRead(prefix,f'.vibeflow/uploads/{identifier}/data.part')
        size=reader.size;identity=reader.version
    path=value['path']
    if canonical_path(path)!=path or is_case_dictionary(prefix,path):
        raise uploads.UploadError('上传路径无法核验，请保留现场检查',409)
    assert_mutable_path(prefix,path)
    if not 0<=size<=value['size'] or 'target_sha256' not in value:
        raise uploads.UploadError('上传进度或原目标版本缺失，请保留现场检查',409)
    return value,size,identity


def chunks(prefix,identifier):
    """Yield verified, pinned bytes in at most 8 MiB pieces."""
    meta,size,identity=metadata(prefix,identifier)
    if not storage._use_s3():
        reader=FileRead(prefix,f'.vibeflow/uploads/{identifier}/data.part')
        if reader.version!=identity:raise CaseWriteConflict('input_stale','上传内容已经变化')
        for start in range(0,size,BLOCK):
            budget();yield reader.read(start,min(size,start+BLOCK)-1)
    else:
        from .s3_uploads import _records
        for base,record,_ in _records(prefix,meta):
            budget();reader=FileRead(prefix,base+'/data')
            if reader.size!=record['size']:raise uploads.UploadError('远端分块大小不匹配',409)
            value=reader.read()
            if hashlib.sha256(value).hexdigest()!=record['sha256']:
                raise uploads.UploadError('远端分块摘要不匹配',409)
            yield value
    if metadata(prefix,identifier)!=(meta,size,identity):
        raise CaseWriteConflict('input_stale','上传内容在读取期间变化，请重新核对')


def content(prefix,identifier):
    pending=bytearray();blocks=[];whole=hashlib.sha256();offset=0
    for part in chunks(prefix,identifier):
        whole.update(part);offset+=len(part);pending.extend(part)
        while len(pending)>=BLOCK:
            blocks.append({'size':BLOCK,'sha256':hashlib.sha256(pending[:BLOCK]).hexdigest()});del pending[:BLOCK]
    if pending:blocks.append({'size':len(pending),'sha256':hashlib.sha256(pending).hexdigest()})
    return {'offset':offset,'sha256':whole.hexdigest(),'blocks':blocks}


def snapshot(case,identifier):
    from .upload_storage import binding
    meta,offset,identity=metadata(case.s3_prefix,identifier)
    value=content(case.s3_prefix,identifier)
    if value['offset']!=offset or metadata(case.s3_prefix,identifier)!=(meta,offset,identity):
        raise CaseWriteConflict('input_stale','上传内容在读取期间变化，请重新核对')
    if storage._use_s3():
        from .s3_streams import snapshot as remote_snapshot
        target,_,_=remote_snapshot(case.s3_prefix,meta['path'])
        target_sha=target['sha256'] if target else None
    else:
        from .file_transactions import _capture,_path
        target=_capture(_path(storage._fs_path(case.s3_prefix,'.'),meta['path']))
        target_sha=target['sha256'] if target else None
    return {'schema':'vibeflow.upload-source.v1','binding':binding(case),'id':identifier,
        'metadata':meta,'identity':identity,'content':value,'target':target,'target_matches':target_sha==meta['target_sha256']}


def digest(value):return hashlib.sha256(json_text(value).encode()).hexdigest()


def validate_blocks(blocks,size):
    if (not isinstance(blocks,list) or len(blocks)!=(size+BLOCK-1)//BLOCK
            or any(not isinstance(v,str) or re.fullmatch('[0-9a-f]{64}',v) is None for v in blocks)):
        raise uploads.UploadError('所选文件摘要清单不完整',422)


def guard_chunk(prefix,identifier,offset,data,manifest):
    """Enforce the explicitly selected source even through the ordinary PUT API."""
    meta,actual,_=metadata(prefix,identifier);validate_blocks(manifest['blocks'],meta['size'])
    if (meta['path'],meta['size'],actual)!=(manifest['path'],manifest['size'],offset):
        raise CaseWriteConflict('input_stale','上传来源或进度已经变化，请重新核对')
    end=offset+len(data)
    if end>meta['size'] or end!=meta['size'] and end%BLOCK:
        raise uploads.UploadError('已恢复上传须按4 MiB边界提交分块',409)
    begin=offset-offset%BLOCK;prefix_bytes=bytearray()
    if begin!=offset:
        if not storage._use_s3():
            prefix_bytes.extend(FileRead(prefix,f'.vibeflow/uploads/{identifier}/data.part').read(begin,offset-1))
        else:
            from .s3_uploads import _records
            for base,record,_ in _records(prefix,meta):
                start=record['offset'];stop=start+record['size']
                if stop<=begin:continue
                reader=FileRead(prefix,base+'/data')
                prefix_bytes.extend(reader.read(max(begin,start)-start,min(offset,stop)-start-1))
                if stop>=offset:break
    combined=bytes(prefix_bytes)+data
    for start in range(0,len(combined),BLOCK):
        if hashlib.sha256(combined[start:start+BLOCK]).hexdigest()!=manifest['blocks'][(begin+start)//BLOCK]:
            raise uploads.UploadError('分块与人工确认的原文件不一致，未追加内容',409)


def guard_complete(prefix,identifier,manifest):
    value=content(prefix,identifier)
    if value['offset']!=manifest['size'] or [v['sha256'] for v in value['blocks']]!=manifest['blocks']:
        raise uploads.UploadError('上传内容与人工确认的文件不一致，未发布目标文件',409)
