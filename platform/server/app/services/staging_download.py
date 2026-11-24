"""Admit a complete local staging snapshot before downloading its first byte."""
import hashlib,json

from .. import storage
from . import attempt_workspace as workspace
from . import staging_preparation as preparation
from .attempt_objects import arguments
from .case_write import CaseWriteConflict
from .node_storage_budget import reserve
from .s3_file_transactions import MAX_MANIFEST,STAGING_MAX_FILES
from .s3_streams import CHUNK_BYTES
from .request_deadline import current


def check_deadline():
    deadline=current()
    if deadline is not None:deadline.check()


def inventory(prefix):
    check_deadline()
    files={};names={}
    for item in storage.list_tree(prefix):
        check_deadline()
        path=item['path']
        if workspace._reserved(path) and path != '.vibeflow/tree-state.json':continue
        if workspace.canonical_path(path)!=path:raise ValueError('Invalid staging source path')
        if (type(item.get('size')) is not int or item['size']<0
                or not isinstance(item.get('etag'),str) or not item['etag']):
            raise ValueError('Staging source has no verified size or version')
        if path in files:raise ValueError('Duplicate staging source path')
        parts=path.split('/')
        for length in range(1,len(parts)+1):
            name='/'.join(parts[:length]);kind='file' if length==len(parts) else 'directory'
            previous=names.get(name.casefold())
            if previous is not None and previous!=(name,kind):raise ValueError('Staging source path collision')
            names[name.casefold()]=(name,kind)
        files[path]={'sha256':'0'*64,'size':item['size'],'modified':item.get('modified'),'etag':item['etag']}
        if len(files)>STAGING_MAX_FILES:raise ValueError('Staging source inventory is too large')
    check_deadline()
    return files


def encode(manifest):
    check_deadline()
    encoded=json.dumps(manifest,separators=(',',':')).encode('utf8')
    if len(encoded)>MAX_MANIFEST:raise ValueError('Staging source manifest is too large')
    check_deadline()
    return encoded


def download(prefix,path,record,target):
    """Version-pinned streaming avoids allocating a whole solver output in RAM."""
    from botocore.exceptions import ClientError
    check_deadline()
    try:
        response=storage.s3().get_object(**arguments(prefix,path),IfMatch='"'+record['etag'].strip('"')+'"')
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode'] in {404,412}:
            raise CaseWriteConflict('input_stale','Staging source changed before download',path=path) from error
        raise
    body=response['Body'];total=0;digest=hashlib.sha256()
    try:
        check_deadline()
        if response.get('ContentLength')!=record['size'] or response.get('ETag','').strip('"')!=record['etag'].strip('"'):
            raise CaseWriteConflict('input_stale','Staging response differs from admitted inventory',path=path)
        target.parent.mkdir(parents=True,exist_ok=True)
        check_deadline()
        with target.open('xb') as output:
            while True:
                check_deadline()
                block=body.read(min(CHUNK_BYTES,record['size']-total+1))
                check_deadline()
                if not block:break
                total+=len(block)
                if total>record['size']:raise ValueError('Staging response exceeds admitted size')
                output.write(block);digest.update(block)
            if total!=record['size']:raise ValueError('Staging response is incomplete')
    finally:body.close()
    return digest.hexdigest()


def prepare(prefix,stage):
    files=inventory(prefix)
    from .attempt_objects import binding
    manifest={'version':1,'source_prefix':prefix,'storage':binding(prefix),'files':files}
    predicted=encode(manifest)
    root=preparation.stage_root(stage)
    path=root.with_suffix('.manifest.json')
    receipt=preparation.receipt(root)
    # Admit payload, manifest and the preparation record before any download.
    # Keep the full claim until all I/O and cleanup drain. Actual partial files
    # remain charged if the writer exits before cleanup can run.
    with reserve(sum(row['size'] for row in files.values())+len(predicted)+preparation.RECEIPT_PEAK_BYTES):
        check_deadline()
        root.mkdir(parents=True,exist_ok=False)
        try:
            owner=preparation.record(root)
        except BaseException as error:
            try:root.rmdir()  # No download or manifest has been attempted.
            except OSError as cleanup_error:error.add_note('Empty stage cleanup: '+type(cleanup_error).__name__)
            raise
        try:
            preparation.write(receipt,owner)
            for name,record in files.items():
                check_deadline()
                record['sha256']=download(prefix,name,record,storage._fs_path(stage,name))
            check_deadline()
            current=inventory(prefix)
            if {name:{**row,'sha256':'0'*64} for name,row in files.items()}!=current:
                raise CaseWriteConflict('input_stale','Staging source changed while downloading')
            encoded=encode(manifest)
            if len(encoded)!=len(predicted):raise ValueError('Staging manifest exceeded its reservation')
            check_deadline()
            with path.open('xb') as output:
                output.write(encoded)
            check_deadline()
            receipt.unlink()  # Ownership can pass to the caller only now.
            return stage
        except BaseException as error:
            preparation.discard(root,receipt,owner,error)
            raise
