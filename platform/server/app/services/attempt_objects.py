"""S3 sealed inputs and private outputs; local directories are execution caches.

Objects are created conditionally and verified by content. A database manifest
is the authority; an orphan object prefix never authorizes an execution.
"""
import hashlib
import io
import json
import os
import tempfile
import threading
from pathlib import Path
import uuid
from contextlib import ExitStack,contextmanager
from concurrent.futures import ThreadPoolExecutor,wait,FIRST_COMPLETED
from contextvars import copy_context

from .. import storage
from ..config import get_settings
from . import attempt_workspace as workspace
from .case_write import CaseWriteConflict
from .durable_events import json_text
from .fs_directory import move_new, move_file_new, remove_owned, DirectoryMoveError


def binding(prefix):
    settings=get_settings()
    return {'backend':'s3','endpoint':settings.s3_endpoint.rstrip('/'),
            'bucket':settings.s3_bucket,'prefix':prefix}


def arguments(prefix,path):
    if workspace.canonical_path(path)!=path:
        raise ValueError('Object path is not canonical')
    return {'Bucket':get_settings().s3_bucket,'Key':storage._s3_key(prefix,path)}


@contextmanager
def _selected_body(prefix,path,etag,limit):
    from botocore.exceptions import ClientError
    if etag is not None:
        etag='"'+etag.strip('"')+'"'
    try:
        try:
            response=storage.s3().get_object(**arguments(prefix,path),**({'IfMatch':etag} if etag else {}))
        except ClientError as error:
            # SeaweedFS 3.99 rejects even its own empty-object ETag on GET.
            # An empty read is still bounded and its returned version must match;
            # source inventory is checked again before the seal can commit.
            if limit!=0 or etag is None or error.response['ResponseMetadata']['HTTPStatusCode']!=412:raise
            response=storage.s3().get_object(**arguments(prefix,path))
        try:
            if etag and response.get('ETag','').strip('"')!=etag.strip('"'):
                raise CaseWriteConflict('input_stale','S3 response version changed',path=path)
            if response['ContentLength']>limit:raise ValueError('S3 object exceeds its admitted size')
            yield response['Body'],response['ContentLength']
        finally:response['Body'].close()
    except ClientError as error:
        code=error.response['ResponseMetadata']['HTTPStatusCode']
        if code==404:raise FileNotFoundError(path) from error
        if code==412:raise CaseWriteConflict('input_stale','S3 input changed during sealing',path=path) from error
        raise


def read(prefix,path,*,etag=None,limit=16*1024*1024):
    with _selected_body(prefix,path,etag,limit) as (body,_):
        data=body.read(limit+1)
        if len(data)>limit:raise ValueError('S3 response exceeds its admitted size')
        return data


@contextmanager
def selected_stream(prefix,path,*,etag,size):
    """Stream the version and length admitted by a case-view selection."""
    from .request_deadline import current
    deadline=current()
    if deadline is not None:
        deadline.check()
    with _selected_body(prefix,path,etag,size) as (body,length):
        if length!=size:
            raise ValueError('Selected S3 body differs from its admitted size')

        def read_chunk():
            if deadline is not None:
                deadline.check()
            return body.read(64*1024)

        yield iter(read_chunk,b'')


def selected_digest(prefix, path, *, etag, size):
    """Hash exactly the admitted object version without writing a local copy."""
    from .request_deadline import current
    from .s3_streams import _digest_body

    deadline = current()
    if deadline is not None:
        deadline.check()
    with _selected_body(prefix, path, etag, size) as (body, length):
        if length != size:
            raise ValueError('Selected S3 body differs from its admitted size')
        return _digest_body(body, size, deadline=deadline)


def digest(data):
    return {'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}


def checked(data,expected):
    if digest(data)!={k:expected[k] for k in ('size','sha256')}:
        raise CaseWriteConflict('input_stale','Immutable S3 object content differs from its manifest')
    return data


def put_new(prefix,path,data):
    """Never replace an existing version, including after an unknown response."""
    _create_immutable(prefix,path,data)
    checked(read(prefix,path,limit=len(data)),digest(data))


def _create_immutable(prefix,path,data):
    """One conditional write; callers must still verify the returned content."""
    from botocore.exceptions import ClientError
    if not workspace._reserved(path):
        raise ValueError('Immutable object writes require an owned attempt/publication path')
    try:
        storage.s3().put_object(**arguments(prefix,path),Body=data,IfNoneMatch='*')
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode']!=412:raise


def put_input_members(prefix,base,records,root,sources=None,contents=None,*,archive=None):
    """Verify four input archives concurrently, with one serial writer.

    The caller already admitted the complete snapshot. Each member retains its
    local content check and conditional create. Large bodies use one admitted
    scratch file, and remote verification never accumulates entire members.
    """
    from .s3_streams import snapshot
    if not workspace._reserved(base):raise ValueError('Input uploads require an owned attempt path')
    storage.s3()  # Construct the shared client before entering reader threads.
    writer=threading.Lock()
    sources,contents=sources or {},contents or {}
    def put_one(path,record):
        remote=base+'/'+path
        # The owner's durable capacity journal supports one physical write at
        # a time. Hold at most one upload scratch file under the same lock.
        if archive is not None:
            original_base,original_versions=archive
            with writer, _frozen_archive_input(prefix,original_base,path,record,original_versions[path]) as body:
                _create_immutable(prefix,remote,body)
        elif path in contents:
            data=contents[path]
            if not isinstance(data,bytes) or digest(data)!={key:record[key] for key in ('size','sha256')}:
                raise ValueError('Inline sealed input differs from its manifest')
            with writer:
                _create_immutable(prefix,remote,data)
        else:
            source=workspace.checked_path(sources.get(path,root),path)
            with writer, _frozen_member_body(source,path,record,kind='input') as body:
                _create_immutable(prefix,remote,body)
        try:
            observed, etag, _ = snapshot(prefix, remote, limit=record['size'])
            if observed != {name: record[name] for name in ('size', 'sha256')}:
                raise ValueError('Uploaded input differs')
        except ValueError as error:
            raise CaseWriteConflict('input_stale','Immutable S3 input differs from its seal',path=path) from error
        return path, (record['size'], etag.strip('"'))
    items=iter(records.items());pending=set();failure=None;versions={}
    with ThreadPoolExecutor(max_workers=4,thread_name_prefix='immutable-input-write') as pool:
        def fill():
            while len(pending)<4:
                item=next(items,None)
                if item is None:break
                pending.add(pool.submit(copy_context().run,put_one,*item))
        fill()
        while pending:
            done,pending=wait(pending,return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    path, version = future.result()
                    versions[path] = version
                except BaseException as error:
                    if failure is None:failure=error
            if failure is None:fill()
    if failure is not None:raise failure
    return versions


def _selected_rows(prefix,paths):
    """List exact files and descendants without enumerating unrelated archives."""
    roots=[]
    for path in sorted(set(paths)):
        if workspace.canonical_path(path)!=path:
            raise ValueError('S3 input path is not portable')
        if not any(path==parent or path.startswith(parent+'/') for parent in roots):
            roots.append(path)
    base=storage._s3_key(prefix,'').rstrip('/')+'/'
    paginator=storage.s3().get_paginator('list_objects_v2')
    for root in roots:
        # No trailing slash: a selection can be a file or a directory. Filter
        # sibling prefix matches so "system-old" cannot satisfy "system".
        for page in paginator.paginate(Bucket=get_settings().s3_bucket,Prefix=base+root):
            for item in page.get('Contents',[]):
                key=item['Key']
                if not key.startswith(base):raise ValueError('S3 listing escaped its case')
                path=key[len(base):]
                if path==root or path.startswith(root+'/'):
                    yield {'path':path,'size':item['Size'],'etag':item.get('ETag','').strip('"')}


def selection(prefix,paths,all_live,*,optional_paths=(),known_directories=()):
    result={}
    # A versioned case catalog already contains every public logical file.
    # Querying hundreds of absent include candidates must not issue a physical
    # archive LIST for every path. Legacy/raw and private selections retain
    # their original reader; no read operation creates or migrates a catalog.
    rows=None
    from .s3_live_store import Client
    client=storage.s3()
    if isinstance(client,Client) and (all_live or all(p.split('/')[0].rstrip(' .').casefold()!='.vibeflow' for p in (*paths,*optional_paths))):
        rows=client.public_file_rows(get_settings().s3_bucket,prefix)
    if rows is None:rows=storage._s3_list(prefix) if all_live else _selected_rows(prefix,(*paths,*optional_paths))
    for item in rows:
        path=item['path']
        if path.split('/')[0].rstrip(' .').casefold()=='.vibeflow':continue
        if path.endswith('/'):continue  # S3 directory markers are not file inputs.
        if not all_live and not any(path==p or path.startswith(p+'/') for p in (*paths,*optional_paths)):continue
        if workspace.canonical_path(path)!=path:raise ValueError('S3 input path is not portable')
        if not isinstance(item.get('etag'),str) or not item['etag']:raise ValueError('S3 input has no conditional read version')
        if type(item.get('size')) is not int or not 0<=item['size']<=workspace.MAX_BYTES:
            raise ValueError('S3 input has no valid admitted size')
        result[path]={'size':item['size'],'etag':item['etag']}
    if len(result)>workspace.MAX_FILES or sum(item['size'] for item in result.values())>workspace.MAX_BYTES:
        raise ValueError('Attempt input limit exceeded (10000 files / 1 GiB)')
    if not all_live and any(path not in known_directories and not any(p==path or p.startswith(path+'/') for p in result) for path in paths):
        raise ValueError('Selected S3 input does not exist')
    # Validate aliases and file/directory collisions before writing any cache.
    names={}
    for path in result:
        parts=path.split('/')
        for index in range(1,len(parts)+1):
            name='/'.join(parts[:index]);kind='file' if index==len(parts) else 'directory'
            previous=names.get(name.casefold())
            if previous is not None and previous!=(name,kind):raise ValueError('S3 input path collision')
            names[name.casefold()]=(name,kind)
    return dict(sorted(result.items()))


class Snapshot:
    def __init__(self,prefix,attempt_id,paths,**options):
        self.prefix=prefix;self.attempt_id=attempt_id;self.paths=sorted({workspace.canonical_path(p) for p in paths});self.options=options
        self.local=None;self.temporary=None;self.versions=None
        self.materialized=False
        self.mode_document=None

    def inspect(self):
        source=self.options.get('source')
        if source is None:
            from .attempt_modes import read_remote
            self.mode_document=read_remote(self.prefix)
            paths=sorted({workspace.canonical_path(p) for p in self.paths})
            if any(path.split('/')[0].casefold()=='.vibeflow' for path in paths):
                raise ValueError('Runtime metadata cannot be selected as attempt inputs')
            from .attempt_modes import directories as mode_directories
            self.versions=selection(self.prefix,paths,self.options.get('all_live',False),
                known_directories=mode_directories(self.mode_document,paths,self.options.get('all_live',False)))
        # No Cache constructor, downloads or directory allocation during
        # preflight. Hash placeholders have the exact encoded digest width.
        root=storage._fs_path(self.prefix,'.')
        options={**self.options,'source':source or root}
        self.local=workspace.Snapshot(self.prefix,self.attempt_id,self.paths,**options)
        if self.versions is not None:
            from .attempt_modes import directories as mode_directories
            selected_directories=mode_directories(self.mode_document,self.paths,self.local.all_live)
            self.local.mode_document=self.mode_document
            self.local.mode_document_external=True
            self.local.mode_hashes_verified=False
            self.local.provenance={**self.local.provenance,'object_versions':self.versions}
            if self.local.all_live:
                self.local.paths=sorted({path.split('/')[0] for path in [*self.versions,*selected_directories]})
                self.local.selection=list(self.local.paths)
            records={path:{'size':version['size'],'sha256':'0'*64,'executable':False}
                for path,version in self.versions.items()}
            directories=sorted(set(selected_directories)|{'/'.join(path.split('/')[:n]) for path in records for n in range(1,len(path.split('/')))})
            return self.local.inspect_records(records,directories)
        self.materialized=True
        return self.local.inspect()

    def materialize(self):
        if self.local is None:raise ValueError('S3 seal requires a preflight inventory')
        self.local.root.mkdir(parents=True,exist_ok=True)
        if self.materialized:return
        if self.local is None or self.versions is None:raise ValueError('S3 seal requires a preflight inventory')
        self.require_source()
        predicted=self.local.describe()
        from .private_cache import Cache
        self.temporary=Cache('.attempt-sources')
        self.local.source=Path(self.temporary.name)
        # The selected view is already bounded and storage-admitted. Admit its
        # cache bytes/journal once; per-file admission rescans the growing node
        # for every object and can exhaust an ordinary submission's HTTP wait.
        items = ((path,version['size'],
            lambda path=path,version=version:selected_stream(
                self.prefix,path,etag=version['etag'],size=version['size']))
            for path,version in self.versions.items())
        self.temporary.fetch_many(items,stream=True)
        self.require_source()
        from .attempt_modes import directories as mode_directories
        self.temporary.directories(mode_directories(self.mode_document,self.paths,self.local.all_live))
        self.local.mode_hashes_verified=True
        self.local.inspect()
        from .tree_metadata_native import PATH as mode_path
        if ({path:row['size'] for path,row in predicted['files'].items() if path!=mode_path} !=
                {path:row['size'] for path,row in self.local.records.items() if path!=mode_path} or
                set(predicted['directories'])-{'.vibeflow'}!=set(self.local.directories)-{'.vibeflow'} or predicted['selection']!=self.local.selection):
            raise CaseWriteConflict('input_stale','S3 materialization differs from its admitted inventory')
        # Retain preflight provenance/toolchain observations; only real file
        # hashes and executable bits replace the admission-only placeholders.
        self.local.manifest={**predicted,'files':self.local.records,'directories':self.local.directories}
        self.materialized=True

    def require_source(self):
        if self.versions is not None:
            from .attempt_modes import read_remote
            if read_remote(self.prefix)!=self.mode_document:
                raise CaseWriteConflict('input_stale','S3 portable modes changed during sealing')
        if self.versions is not None:
            from .attempt_modes import directories as mode_directories
            if selection(self.prefix,self.paths,self.options.get('all_live',False),
                    known_directories=mode_directories(self.mode_document,self.paths,self.options.get('all_live',False)))!=self.versions:
                raise CaseWriteConflict('input_stale','S3 input selection changed during sealing')

    def prepare(self):
        if not self.materialized:raise ValueError('S3 inputs must materialize after storage admission')
        self.require_source()
        from .node_storage_budget import reserve,copy_bytes
        local_encoded=json.dumps(self.local.describe(),ensure_ascii=False,sort_keys=True).encode('utf8')
        with reserve(copy_bytes(self.local.records,local_encoded,copies=2)):
            manifest=self.local.prepare()
        encoded=json_text(manifest).encode('utf8')
        if len(encoded)>16*1024*1024:raise ValueError('S3 attempt manifest exceeds 16 MiB')
        inputs=self.local.target/'inputs'
        put_input_members(self.prefix,manifest['inputs_path'],manifest['files'],inputs)
        put_new(self.prefix,self.local.relative+'/manifest.json',encoded)
        self.require_source()
        if self.temporary:self.temporary.cleanup();self.temporary=None
        return manifest

    def cleanup(self):
        if self.local:self.local.cleanup()
        if self.temporary:self.temporary.cleanup();self.temporary=None
        # Uploaded prefixes without a committed DB manifest remain unreferenced.
        # Never guess whether an uncertain transaction committed and delete them.


def _restore_missing_cached_inputs(prefix, manifest, encoded, target):
    from .file_transactions import _path, _sync
    from .private_cache import Cache
    from .request_deadline import current

    deadline = current()
    check = deadline.check if deadline is not None else None
    local_manifest = _path(target, 'manifest.json', check=check)
    inputs = _path(target, 'inputs', check=check)
    records = manifest['files']
    paths = {name: _path(target, 'inputs/' + name, check=check) for name in records}
    directories = [inputs, *(_path(target, 'inputs/' + name, check=check) for name in manifest['directories'])]
    missing = {name: records[name] for name, path in paths.items() if not path.exists()}
    missing_manifest = not local_manifest.exists()
    if not (missing or missing_manifest or any(not path.exists() for path in directories)):
        verify_members(prefix, manifest['inputs_path'], records)
        return

    if not missing_manifest:
        with local_manifest.open('rb') as stream:
            stored = stream.read(16 * 1024 * 1024 + 1)
            if len(stored) > 16 * 1024 * 1024 or json.loads(stored) != manifest:
                raise CaseWriteConflict('input_stale', '本地封存清单与原归档不一致，保留已有文件')
    present_directories = []
    selections = sorted(path.name for path in inputs.iterdir()) if inputs.exists() else []
    observed = workspace.inventory(inputs, selections, present_directories, deadline=deadline) if selections else {}
    if (observed.keys() != records.keys() - missing.keys() or
            any(record != records[name] for name, record in observed.items()) or
            not set(present_directories) <= set(manifest['directories'])):
        raise CaseWriteConflict('input_stale', '保留的本地封存输入与原归档不一致')
    verify_members(prefix, manifest['inputs_path'], {name: records[name] for name in observed})

    @contextmanager
    def read_manifest():
        yield iter((encoded,))

    items = [('inputs/' + name, record['size'], lambda name=name, record=record:
              _verified_archive_stream(prefix, manifest['inputs_path'] + '/' + name, record))
             for name, record in missing.items()]

    with ExitStack() as resources:
        cache = Cache('.attempt-sources') if items or missing_manifest else None
        if cache is not None:
            resources.callback(cache.cleanup)
            if items:
                cache.fetch_many(items, stream=True)
            # The manifest has its own bound. It does not count as a sealed member.
            if missing_manifest:
                cache.fetch_many([('manifest.json', len(encoded), read_manifest)], stream=True)
        for directory in directories:
            selected = _path(target, directory.relative_to(target).as_posix(), check=check)
            selected.mkdir(exist_ok=True)
            if not selected.is_dir():
                raise ValueError('Sealed input directory was replaced')
        if cache is None:
            return
        for name, record in missing.items():
            relative = 'inputs/' + name
            source = _path(cache.root, relative, check=check)
            if record['executable']:
                source.chmod(0o755)
            move_file_new(source, _path(target, relative, check=check), root=storage._fs_root())
            _sync(paths[name].parent)
        if missing_manifest:
            move_file_new(_path(cache.root, 'manifest.json', check=check),
                          _path(target, 'manifest.json', check=check), root=storage._fs_root())
            _sync(target)


def ensure_inputs(prefix,manifest,*,create_run=True):
    if type(create_run) is not bool:raise ValueError('Invalid runtime cache selection')
    workspace.validate_identity(manifest)
    if manifest.get('storage')!=binding(prefix):
        raise CaseWriteConflict('input_stale','Attempt object store binding changed')
    relative=workspace.PREFIX+'/'+manifest['attempt_id']
    encoded=read(prefix,relative+'/manifest.json')
    if json.loads(encoded)!=manifest:raise CaseWriteConflict('input_stale','S3 manifest differs from database')
    remote={item['path'] for item in storage._s3_list(prefix,manifest['inputs_path'])}
    if remote!=set(manifest['files']):raise CaseWriteConflict('input_stale','S3 sealed input inventory changed')
    from .file_transactions import _path

    root=storage._fs_path(prefix,'.')
    target=_path(root,relative) if root.exists() else root/relative
    if not create_run and target.exists():
        _restore_missing_cached_inputs(prefix,manifest,encoded,target)
        return
    stage=None
    reservations=ExitStack()
    try:
        if target.exists():workspace.ordinary(target)
        if not target.exists():
            from .node_storage_budget import reserve,copy_bytes
            reservations.enter_context(reserve(copy_bytes(manifest['files'],encoded,copies=2 if create_run else 1)))
            root.mkdir(parents=True,exist_ok=True);workspace.ordinary(root);parent=root
            for part in workspace.PREFIX.split('/'):
                parent=parent/part;parent.mkdir(exist_ok=True);workspace.ordinary(parent)
            stage=parent/('.restoring-'+manifest['attempt_id']+'-'+uuid.uuid4().hex);stage.mkdir()
            (stage/'inputs').mkdir()
            for directory in manifest['directories']:(stage/'inputs'/workspace.canonical_path(directory)).mkdir(parents=True,exist_ok=True)
        # Recheck every remote member even when the local cache already exists.
        # The existing bounded stream verifier also supports cold hydration;
        # it drains all readers before a failed stage can be removed below.
        try:
            verify_members(prefix,manifest['inputs_path'],manifest['files'],
                destination=stage/'inputs' if stage is not None else None)
        except ValueError as error:
            # Preserve the original input-stale classification for mismatched
            # immutable bytes; output-archive verification retains its own API.
            raise CaseWriteConflict('input_stale','S3 sealed input content differs from its manifest') from error
        if stage:
            if create_run:workspace.copy_records(stage/'inputs',stage/'run',manifest['files'],manifest['directories'])
            (stage/'manifest.json').write_bytes(encoded)
            try:move_new(stage,target,root=root)
            except (FileExistsError,DirectoryMoveError):
                # A concurrent reader may have hydrated exactly this immutable input.
                if json.loads(workspace.checked_path(target,'manifest.json').read_text())!=manifest:raise
    finally:
        try:
            if stage is not None and stage.exists():remove_owned(stage,root=root)
        finally:reservations.close()


def output_plan(prefix,manifest,records,directories):
    if manifest.get('storage')!=binding(prefix):raise ValueError('Attempt object store binding changed')
    if (str(uuid.UUID(manifest['attempt_id']))!=manifest['attempt_id']
            or manifest['run_path']!=workspace.PREFIX+'/'+manifest['attempt_id']+'/run'):
        raise ValueError('Invalid output archive target')
    from .publication_objects import validate_records
    validate_records(records)
    if (not isinstance(directories,list) or directories!=sorted(set(directories))
            or any(workspace.canonical_path(path)!=path for path in directories)):
        raise ValueError('Invalid output archive directories')
    value={'schema':'vibeflow.attempt-object-outputs.v1','attempt_id':manifest['attempt_id'],
        'source':manifest['source'],'run_path':manifest['run_path'],'files':records,'directories':directories}
    encoded=json_text(value).encode('utf8');path=workspace.PREFIX+'/'+manifest['attempt_id']+'/outputs.json'
    if len(encoded)>16*1024*1024:raise ValueError('S3 output manifest exceeds 16 MiB')
    return encoded,{'manifest_path':path,**digest(encoded),'storage':binding(prefix)}


def persist_outputs(prefix,manifest,run,records,directories):
    encoded,receipt=output_plan(prefix,manifest,records,directories)
    for name,record in records.items():
        put_output_member(prefix,manifest['run_path'],name,workspace.checked_path(run,name),record)
    put_new(prefix,receipt['manifest_path'],encoded)
    return receipt


def put_output_member(prefix, base, name, source, expected):
    """Freeze captured bytes in admitted scratch before a conditional PUT."""
    path = base + '/' + name
    with _frozen_member_body(source,name,expected,kind='output') as body:
        if isinstance(body,bytes):
            put_new(prefix,path,body)
            return
        _create_immutable(prefix,path,body)
    _verify_output_member(prefix,path,expected)


@contextmanager
def _member_buffer(size):
    from .node_storage_budget import reserve

    with ExitStack() as resources:
        if size > 64 * 1024:
            resources.enter_context(reserve(size))
            stream = resources.enter_context(tempfile.TemporaryFile(dir=storage._fs_root()))
        else:
            stream = resources.enter_context(io.BytesIO())
        yield stream


@contextmanager
def _frozen_member_body(source, name, expected, *, kind):
    from .file_transactions import _capture
    from .request_deadline import current

    record = {key: expected[key] for key in ('size', 'sha256')}
    deadline = current()
    stale = f'Local {kind} differs from its captured content'
    with _member_buffer(record['size']) as stream:
        try:
            captured = _capture(source,limit=record['size'],deadline=deadline,stream=stream)
        except ValueError as error:
            if kind!='input':
                raise
            raise CaseWriteConflict('input_stale',stale,path=name) from error
        if captured!=record:
            raise CaseWriteConflict('input_stale',stale,path=name)
        if deadline is not None:
            deadline.check()
        stream.seek(0)
        yield stream.getvalue() if isinstance(stream,io.BytesIO) else stream


@contextmanager
def _frozen_archive_input(prefix,base,name,record,version):
    with _member_buffer(record['size']) as stream:
        digest=hashlib.sha256()
        try:
            with selected_stream(prefix,base+'/'+name,etag=version[1],size=record['size']) as chunks:
                for chunk in chunks:
                    digest.update(chunk)
                    stream.write(chunk)
        except (FileNotFoundError,ValueError) as error:
            raise CaseWriteConflict('input_stale','Archived retry input changed',path=name) from error
        if stream.tell()!=record['size'] or digest.hexdigest()!=record['sha256']:
            raise CaseWriteConflict('input_stale','Archived retry input differs from its seal',path=name)
        stream.seek(0)
        yield stream.getvalue() if isinstance(stream,io.BytesIO) else stream


def output_inventory(prefix,manifest,output):
    receipt=output.get('object_store',{})
    path=workspace.PREFIX+'/'+manifest['attempt_id']+'/outputs.json'
    if (receipt.get('state')!='complete' or receipt.get('storage')!=binding(prefix)
            or receipt.get('manifest_path')!=path or output.get('input_attempt_id')!=manifest['attempt_id']
            or output.get('run_path')!=manifest['run_path']):
        raise ValueError('S3 attempt outputs have no complete bound upload receipt')
    value=json.loads(checked(read(prefix,path),receipt))
    expected={'schema':'vibeflow.attempt-object-outputs.v1','attempt_id':manifest['attempt_id'],
        'source':manifest['source'],'run_path':manifest['run_path'],
        'files':output['files'],'directories':output['directories']}
    if value!=expected:raise ValueError('S3 output manifest differs from database')
    remote={item['path'] for item in storage._s3_list(prefix,manifest['run_path'])}
    if remote!=set(output['files']):raise ValueError('S3 output inventory differs from database')
    from .publication_objects import validate_records
    validate_records(output['files'])
    if len(output['files'])>workspace.MAX_FILES or sum(row['size'] for row in output['files'].values())>workspace.MAX_BYTES:
        raise ValueError('Captured output exceeds its inventory limits')
    return output['files']


def verify_outputs(prefix,manifest,output):
    records=output_inventory(prefix,manifest,output)
    verify_members(prefix,manifest['run_path'],records)


@contextmanager
def _verified_archive_stream(prefix, path, expected):
    with output_stream(prefix,path,expected['size']) as body:
        def validated_chunks():
            digest = hashlib.sha256()
            size = 0
            for chunk in body:
                size += len(chunk)
                if size > expected['size']:
                    raise ValueError('Archived object exceeds captured size')
                digest.update(chunk)
                yield chunk
            if size != expected['size'] or digest.hexdigest() != expected['sha256']:
                raise ValueError('Archived object differs from captured content')
        yield validated_chunks()


def _verify_output_member(prefix, path, expected, target=None):
    with ExitStack() as streams:
        chunks = streams.enter_context(_verified_archive_stream(prefix,path,expected))
        output = streams.enter_context(target.open('xb')) if target is not None else None
        for chunk in chunks:
            if output is not None:
                output.write(chunk)
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
    if target is not None:
        target.chmod(0o755 if expected.get('executable') else 0o644)


def verify_members(prefix,base,records,*,destination=None):
    """Four bounded object reads; optionally retain the verified bytes locally.

    A destination is a caller-owned cache under an already admitted allocation.
    It never substitutes for reading and hashing every remote member.
    """
    if not workspace._reserved(base):raise ValueError('Parallel verification requires immutable runtime objects')
    # Construct the client before starting readers; propagate caller budget
    # context to each task. No eager queue of every member and no full bodies.
    storage.s3()
    destinations={}
    if destination is not None:
        from .file_transactions import _path
        destination=Path(destination).resolve(strict=True);workspace.ordinary(destination)
        # Create and validate shared parents before concurrent member reads.
        # Each worker then writes only its own exclusively created file.
        for name in records:
            target=_path(destination,name);target.parent.mkdir(parents=True,exist_ok=True)
            destinations[name]=_path(destination,name)
    def verify_one(name,expected):
        _verify_output_member(prefix, base + '/' + name, expected, destinations.get(name))
    items=iter(records.items());pending=set();failure=None
    with ThreadPoolExecutor(max_workers=4,thread_name_prefix='immutable-object-read') as pool:
        def fill():
            while len(pending)<4:
                item=next(items,None)
                if item is None:break
                pending.add(pool.submit(copy_context().run,verify_one,*item))
        fill()
        while pending:
            done,pending=wait(pending,return_when=FIRST_COMPLETED)
            for future in done:
                try:future.result()
                except BaseException as error:
                    if failure is None:failure=error
            if failure is None:fill()
    if failure is not None:raise failure


@contextmanager
def output_stream(prefix,path,size):
    from botocore.exceptions import ClientError
    from .request_deadline import current
    deadline = current()
    if deadline is not None:
        deadline.check()
    try:response=storage.s3().get_object(**arguments(prefix,path))
    except ClientError as error:
        if error.response['ResponseMetadata']['HTTPStatusCode']==404:raise FileNotFoundError(path) from error
        raise
    try:
        if response.get('ContentLength')!=size:raise ValueError('Archived output length differs from capture')
        def read_chunk():
            if deadline is not None:
                deadline.check()
            return response['Body'].read(1024*1024)
        yield iter(read_chunk,b'')
    finally:response['Body'].close()


class CapturedOutputView:
    """Owned, disposable read view; never installs a runtime or success record."""
    def __init__(self,prefix,manifest,output,root,cache=None):
        self.prefix=prefix;self.identity=hashlib.sha256(json_text([manifest,output]).encode()).hexdigest()
        self.root=root;self.cache=cache;self.input_view=None
        self.runtime_versions=None

    def matches(self,prefix,manifest,output):
        return self.prefix==prefix and self.identity==hashlib.sha256(json_text([manifest,output]).encode()).hexdigest()

    def cleanup(self):
        try:
            if self.cache is not None:self.cache.cleanup();self.cache=None
        finally:
            if self.input_view is not None:self.input_view.cleanup();self.input_view=None


class SealedInputView:
    """Authentic read-only source for a new attempt; never repairs old caches."""
    def __init__(self, prefix, manifest, root, cache=None):
        self.prefix=prefix;self.identity=hashlib.sha256(json_text(manifest).encode()).hexdigest()
        self.root=root;self.cache=cache

    def matches(self, prefix, manifest):
        return self.prefix==prefix and self.identity==hashlib.sha256(json_text(manifest).encode()).hexdigest()

    def cleanup(self):
        if self.cache is not None:self.cache.cleanup();self.cache=None


def sealed_input_versions(prefix, manifest):
    workspace.validate_identity(manifest)
    if manifest.get('storage')!=binding(prefix):
        raise CaseWriteConflict('input_stale','Attempt object store binding changed')
    from .publication_objects import validate_records
    validate_records(manifest['files'])
    directories=manifest['directories']
    if (not isinstance(directories,list) or directories!=sorted(set(directories))
            or any(workspace.canonical_path(path)!=path for path in directories)):
        raise ValueError('Invalid sealed input directories')
    names={}
    for path in [*directories,*manifest['files']]:
        if path.casefold() in names:raise ValueError('Sealed input path collision')
        names[path.casefold()]=path
    encoded=read(prefix,workspace.PREFIX+'/'+manifest['attempt_id']+'/manifest.json')
    if json.loads(encoded)!=manifest:
        raise CaseWriteConflict('input_stale','S3 manifest differs from database')
    remote={row['path']:(row['size'],row['etag']) for row in storage._s3_list(prefix,manifest['inputs_path'])}
    if (remote.keys()!=manifest['files'].keys()
            or any(not etag or size!=manifest['files'][path]['size'] for path,(size,etag) in remote.items())):
        raise CaseWriteConflict('input_stale','S3 sealed input inventory changed')
    return remote


def sealed_input_inventory(prefix, manifest):
    sealed_input_versions(prefix,manifest)
    return manifest['files']


def sealed_input_view(prefix, manifest):
    if not storage._use_s3():
        return SealedInputView(prefix,manifest,workspace.verify_inputs(prefix,manifest))
    records=sealed_input_inventory(prefix,manifest)
    from .private_cache import Cache

    @contextmanager
    def read_member(name, record):
        try:
            with _verified_archive_stream(prefix,manifest['inputs_path']+'/'+name,record) as chunks:
                yield chunks
        except (FileNotFoundError,ValueError) as error:
            raise CaseWriteConflict('input_stale',
                'S3 archived sealed input is missing or differs from database',path=name) from error

    cache=Cache('.attempt-sources')
    try:
        cache.directories(manifest['directories'])
        items=((name,record['size'],lambda name=name,record=record:read_member(name,record))
            for name,record in records.items())
        cache.fetch_many(items,stream=True)
        for name,record in records.items():
            if record.get('executable'):
                workspace.checked_path(cache.root,name).chmod(0o755)
        sealed_input_inventory(prefix,manifest)
        directories=[];paths=sorted(p.name for p in cache.root.iterdir())
        observed=workspace.inventory(cache.root,paths,directories) if paths else {}
        if observed!=records or directories!=manifest['directories']:
            raise CaseWriteConflict('input_stale','Archived sealed input view differs from database')
        return SealedInputView(prefix,manifest,cache.root,cache)
    except BaseException:
        cache.cleanup()
        raise


def captured_output_view(prefix,manifest,output,run):
    if not storage._use_s3():return CapturedOutputView(prefix,manifest,output,run)
    records=output_inventory(prefix,manifest,output)
    from .private_cache import Cache
    from .node_storage_budget import reserve
    metadata=sum(len(json.dumps({'path':name,'size':row['size'],'sha256':row['sha256']},sort_keys=True).encode())+1
        for name,row in records.items())
    cache=None
    try:
        # Admit the complete view before the first body download. One claim
        # avoids rescanning every previous file on each member of a large view.
        with reserve(sum(row['size'] for row in records.values())+metadata):
            cache=Cache('.attempt-sources')
            cache.directories(output['directories'])
            for name,row in records.items():
                cache._write_stream(name,row,lambda name=name,row=row:output_stream(prefix,manifest['run_path']+'/'+name,row['size']))
            output_inventory(prefix,manifest,output)
        return CapturedOutputView(prefix,manifest,output,cache.root,cache)
    except BaseException:
        if cache is not None:cache.cleanup()
        raise


def _verify_runtime_members(run,output):
    """Verify exact local output bytes and directories, including empty ones."""
    from .request_deadline import current

    directories=[];paths=sorted(path.name for path in run.iterdir())
    records=workspace.inventory(run,paths,directories,deadline=current()) if paths else {}
    if records!=output['files'] or directories!=output['directories']:
        raise ValueError('Existing runtime cache differs from the captured output; retained')


def _runtime_versions(run, output, *, allow_missing=False):
    from .local_retry_seal import _tree_versions

    try:
        return _tree_versions(run, output, allow_missing=allow_missing)
    except CaseWriteConflict as error:
        raise ValueError('Runtime cache paths or member versions changed; retained') from error


def _install_missing_runtime_members(prefix,manifest,output,run,before,missing,directories):
    from .file_transactions import _path, _sync
    from .local_file_edits import _version
    from .private_cache import Cache
    from .request_deadline import current

    deadline = current()
    check = deadline.check if deadline is not None else None
    moved = {}
    with ExitStack() as resources:
        cache = Cache('.attempt-sources') if missing else None
        if cache is not None:
            resources.callback(cache.cleanup)
            items = [(name, record['size'], lambda name=name, record=record:
                      _verified_archive_stream(prefix, manifest['run_path'] + '/' + name, record))
                     for name, record in missing.items()]
            cache.fetch_many(items, stream=True)
        output_inventory(prefix,manifest,output)
        if _runtime_versions(run,output,allow_missing=True) != before:
            raise ValueError('Runtime cache changed while downloading missing members; retained')

        # The caller holds finalizing_io for the exact stopped attempt. These
        # private repairs need no SQL write lock, and each rename is exclusive.
        for name in directories:
            destination = _path(run,name,check=check)
            destination.mkdir(exist_ok=True)
            _sync(destination.parent)
        for name,record in missing.items():
            source = _path(cache.root,name,check=check)
            if record['executable']:
                source.chmod(0o755)
            moved[name] = _version(source)
            destination = _path(run,name,check=check)
            move_file_new(source,destination,root=storage._fs_root())
            _sync(destination.parent)

    after = _runtime_versions(run,output)
    for name,version in before.items():
        kept = after[name] if name in output['files'] else after[name][:2]
        expected = version if name in output['files'] else version[:2]
        if kept != expected:
            raise ValueError('Retained runtime cache changed during installation')
    for name,record in missing.items():
        # An individual rename can change ctime. Verify the moved identity and
        # read only these new bytes again while verifying their final version.
        if moved[name] is None or after[name][:4] != moved[name][:4]:
            raise ValueError('Installed runtime member did not retain its source identity')
        digest = workspace.digest_file(_path(run,name,check=check),deadline=deadline)
        if digest != {key:record[key] for key in ('size','sha256')}:
            raise ValueError('Installed runtime member differs from archive')
    if _runtime_versions(run,output) != after:
        raise ValueError('Installed runtime cache changed during verification')
    return after


def _prepare_existing_runtime_cache(prefix,manifest,output,run):
    before = _runtime_versions(run,output,allow_missing=True)
    files = {name:record for name,record in output['files'].items() if name in before}
    directories = [name for name in output['directories'] if name in before]
    _verify_runtime_members(run,{'files':files,'directories':directories})
    missing = {name:record for name,record in output['files'].items() if name not in before}
    absent_directories = [name for name in output['directories'] if name not in before]
    if missing or absent_directories:
        verify_members(prefix,manifest['run_path'],files)
        return _install_missing_runtime_members(prefix,manifest,output,run,before,missing,absent_directories)
    verify_outputs(prefix,manifest,output)
    if _runtime_versions(run,output) != before:
        raise ValueError('Runtime cache changed during content verification; retained')
    return before


def prepare_runtime_cache(prefix,manifest,output):
    """Prepare an exact runtime view; fill only missing paths in an existing run.

    The caller must first authorize the original stopped attempt, then recheck
    it under case control before install_runtime_cache. The returned view owns
    its journaled staging data until its cleanup has actually returned.
    """
    workspace.validate_identity(manifest)
    if not storage._use_s3():raise ValueError('Runtime rehydration requires an immutable object archive')
    output_inventory(prefix,manifest,output)
    # A cold attempt needs sealed inputs but must not invent a fresh input-only
    # run in the old executed output location.
    ensure_inputs(prefix,manifest,create_run=False)
    workspace.verify_cached_inputs(prefix,manifest)
    from .file_transactions import _path
    run=_path(storage._fs_path(prefix,'.'),manifest['run_path'])
    if run.exists():
        view=CapturedOutputView(prefix,manifest,output,run)
        view.runtime_versions=_prepare_existing_runtime_cache(prefix,manifest,output,run)
        return view
    view=captured_output_view(prefix,manifest,output,run)
    try:
        before=_runtime_versions(view.root,output)
        _verify_runtime_members(view.root,output)
        if _runtime_versions(view.root,output)!=before:
            raise ValueError('Runtime cache changed during content verification; retained')
        view.runtime_versions=before
        return view
    except BaseException:
        view.cleanup();raise


def install_runtime_cache(prefix,manifest,output,view):
    """No-overwrite installation; caller holds the revalidated case control."""
    if (not isinstance(view,CapturedOutputView) or view.runtime_versions is None or
            not view.matches(prefix,manifest,output)):
        raise ValueError('Runtime cache view belongs to another immutable archive')
    from .file_transactions import _path
    root=storage._fs_root();run=_path(storage._fs_path(prefix,'.'),manifest['run_path'])
    before=view.runtime_versions
    if _runtime_versions(view.root,output)!=before:
        raise ValueError('Verified runtime cache changed before installation; retained')
    if view.cache is None:
        if view.root!=run:raise ValueError('Previously verified runtime cache path changed')
        return

    # The durable finalization guard excludes other product writers. Preserve a
    # destination created by another process and verify it on a new request.
    move_new(view.root,run,root=root)
    installed=_runtime_versions(run,output)
    # A directory's own change time can advance on rename. Its identity,
    # contents, and member versions must still match the verified directory.
    installed_root=installed.pop('')
    verified_members={name:version for name,version in before.items() if name}
    if installed_root[:3]!=before[''][:3] or installed!=verified_members:
        raise ValueError('Installed runtime cache changed during installation; retained')
