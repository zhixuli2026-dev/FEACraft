"""Write-ahead receipts for immutable publication directories or S3 bundles.

Receipts prove file preparation only. SQL ownership, source validation and head
fences still decide whether a bundle can become a visible publication.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import uuid

from .. import storage
from . import attempt_workspace as workspace
from .durable_events import json_text
from .fs_directory import move_new, remove_owned

SCHEMA = 'vibeflow.publication-file-journal.v1'
DIRECTORY = '.vibeflow/publications/.journal'


def _directory(prefix, row):
    if storage._use_s3():
        raise ValueError('Publication file journal requires local fs storage')
    if str(uuid.UUID(row.id)) != row.id or str(uuid.UUID(row.attempt_id)) != row.attempt_id:
        raise ValueError('Invalid publication journal identity')
    if row.immutable_path != '.vibeflow/publications/'+row.id:
        raise ValueError('Publication journal target differs from reservation')
    root = storage._fs_path(prefix, '.')
    current = root
    for part in DIRECTORY.split('/'):
        current = current / part
        current.mkdir(exist_ok=True)
        workspace.ordinary(current)
    return root, current / row.id


def _flush_directory(path):
    # Windows uses file FlushFileBuffers via fsync; POSIX also persists renames.
    if os.name != 'nt':
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try: os.fsync(descriptor)
        finally: os.close(descriptor)


def _write_new(path, value):
    with path.open('xb') as stream:
        stream.write(json_text(value).encode('utf8'))
        stream.flush(); os.fsync(stream.fileno())


def _read(path):
    workspace.ordinary(path)
    return json.loads(path.read_text(encoding='utf8'))


def _install_intent(root, directory, intent):
    if not directory.exists():
        stage = directory.with_name('.intent-'+str(uuid.uuid4()))
        stage.mkdir(exist_ok=False)
        try:
            _write_new(stage/'intent.json', intent)
            _flush_directory(stage)
            try:
                move_new(stage, directory, root=root)
                _flush_directory(directory.parent)
            except ValueError:
                if not directory.exists(): raise
        finally:
            if stage.exists(): remove_owned(stage, root=root)
    workspace.ordinary(directory)
    if _read(directory/'intent.json') != intent:
        raise ValueError('Publication journal intent differs from validated output')


@contextmanager
def _lock(directory):
    path = directory/'prepare.lock'
    if path.exists(): workspace.ordinary(path)
    with path.open('a+b') as stream:
        workspace.ordinary(path)
        stream.seek(0, os.SEEK_END)
        if not stream.tell(): stream.write(b'\0'); stream.flush()
        stream.seek(0)
        acquired = False
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            yield
        finally:
            if acquired:
                stream.seek(0)
                if os.name == 'nt': msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else: fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _verify_bundle(root, folder, manifest):
    workspace.ordinary(folder)
    if _read(folder/'manifest.json') != manifest:
        raise ValueError('Existing publication bundle differs from journal intent')
    # Enumerate all output files, so neither missing nor extra files are reused.
    files = workspace.checked_path(root, (folder/'files').relative_to(root).as_posix())
    roots = sorted(p.name for p in files.iterdir())
    observed = workspace.inventory(files, roots) if roots else {}
    if observed != manifest['files']:
        raise ValueError('Existing publication bundle files differ from journal intent')


def _ready(directory, digest):
    value = {'schema':SCHEMA, 'manifest_sha256':digest, 'state':'bundle_ready'}
    path = directory/'ready.json'
    if path.exists():
        if _read(path) != value: raise ValueError('Publication readiness receipt differs')
        return
    stage = directory/('.receipt-'+str(uuid.uuid4()))
    try:
        _write_new(stage, value)
        os.replace(stage, path)
        _flush_directory(directory)
    finally: stage.unlink(missing_ok=True)


def bundle_manifest(row,records,descriptor,*,prefix=None):
    from .artifact_publication import SCHEMA as BUNDLE_SCHEMA
    manifest = {'schema':BUNDLE_SCHEMA,'publication_id':row.id,'case_id':row.case_id,
        'attempt_id':row.attempt_id,'kind':row.kind,'generation':row.generation,
        'files':records,'descriptor':descriptor}
    if storage._use_s3():
        from .attempt_objects import binding
        if prefix is None: raise ValueError('S3 publication requires a storage binding')
        manifest['storage'] = binding(prefix)
    return manifest


def record_intent(prefix, row, records, descriptor):
    """Persist the exact validated copy plan before any bundle bytes are copied."""
    manifest=bundle_manifest(row,records,descriptor,prefix=prefix)
    if storage._use_s3():
        from .publication_objects import record_intent as remote_intent
        return remote_intent(prefix,row,manifest)
    digest = hashlib.sha256(json_text(manifest).encode('utf8')).hexdigest()
    intent = {'schema':SCHEMA,'manifest_sha256':digest,'manifest':manifest}
    root, directory = _directory(prefix, row)
    _install_intent(root, directory, intent)
    return digest


def prepare(prefix, row, run, records, descriptor, create):
    """Owned publisher may retry file preparation; this never commits SQL heads."""
    if storage._use_s3():
        from .publication_objects import prepare as remote_prepare
        return remote_prepare(prefix,row,run,bundle_manifest(row,records,descriptor,prefix=prefix))
    digest=record_intent(prefix,row,records,descriptor)
    root,directory=_directory(prefix,row)
    manifest=bundle_manifest(row,records,descriptor)
    target = root/row.immutable_path
    stage = target.with_name('.preparing-'+row.id)
    with _lock(directory):
        for candidate in (target,stage):
            if os.path.lexists(candidate): workspace.ordinary(candidate)
        if target.exists():
            _verify_bundle(root, target, manifest)
        else:
            if run is None:
                raise ValueError('Verified publication disappeared before readiness recovery')
            if stage.exists():
                workspace.ordinary(stage)
                try:
                    _verify_bundle(root, stage, manifest)
                except (OSError, ValueError, KeyError, TypeError):
                    # Preserve interrupted copies for diagnosis; only a caller
                    # with the original validated intent can prepare a replacement.
                    quarantine = directory/('interrupted-'+str(uuid.uuid4()))
                    move_new(stage, quarantine, root=root)
                    _flush_directory(directory)
                else:
                    move_new(stage, target, root=root)
                    _flush_directory(target.parent)
            if not target.exists():
                if create(prefix,row,run,records,descriptor) != digest:
                    raise ValueError('Prepared publication differs from its journal')
                _flush_directory(target.parent)
            _verify_bundle(root, target, manifest)
        _ready(directory, digest)
    return digest


def inspect(prefix, row):
    """Read-only file diagnosis; a prepared SQL row never implies visibility."""
    if storage._use_s3():
        from .publication_objects import inspect as remote_inspect
        return remote_inspect(prefix,row)
    root = storage._fs_path(prefix,'.')
    if str(uuid.UUID(row.id)) != row.id:
        raise ValueError('Invalid publication identity')
    directory = root/DIRECTORY/row.id
    if not directory.exists(): return {'state':'legacy_unjournaled'}
    try:
        workspace.checked_path(root, directory.relative_to(root).as_posix())
        intent = _read(directory/'intent.json')
        manifest = intent['manifest']
        digest = hashlib.sha256(json_text(manifest).encode('utf8')).hexdigest()
        from .artifact_publication import SCHEMA as BUNDLE_SCHEMA
        if (intent.get('schema')!=SCHEMA or intent.get('manifest_sha256')!=digest or manifest.get('schema')!=BUNDLE_SCHEMA
                or any(manifest.get(key)!=getattr(row, field) for key,field in
                    [('publication_id','id'),('case_id','case_id'),('attempt_id','attempt_id'),('kind','kind'),('generation','generation')])
                or row.immutable_path!='.vibeflow/publications/'+row.id):
            raise ValueError('Journal identity differs from database reservation')
        target = root/row.immutable_path
        if not target.exists(): return {'state':'copy_incomplete','manifest_sha256':digest}
        _verify_bundle(root,target,manifest)
        if row.manifest_sha256 not in ({digest} if row.status=='committed' else {'0'*64,digest}):
            raise ValueError('Database digest differs from journal bundle')
        return {'state':'bundle_verified','manifest_sha256':digest}
    except (OSError,ValueError,KeyError,TypeError):
        return {'state':'requires_attention'}


def read_intent(prefix, row):
    if storage._use_s3():
        from .publication_objects import read_intent as remote_intent
        return remote_intent(prefix,row)
    root = storage._fs_path(prefix,'.')
    directory = workspace.checked_path(root,DIRECTORY+'/'+row.id)
    return _read(directory/'intent.json')


async def diagnose(job_id, user_id):
    """Authorized snapshot/read/recheck; file diagnosis grants no execution authority."""
    import asyncio
    from fastapi import HTTPException
    from sqlalchemy import select
    from ..db import SessionLocal
    from ..models import ArtifactPublication, ArtifactPublicationHead, Job, JobAttempt, User
    from ..routers.workspaces import get_owned_case
    from .async_ops import finish_before_cancel

    async def snapshot():
        async with SessionLocal() as db:
            job = await db.get(Job,job_id)
            user = await db.get(User,user_id)
            if job is None or user is None: raise HTTPException(404,'作业不存在')
            case = await get_owned_case(job.case_id,user,db,write=False)
            attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job_id)
                .order_by(JobAttempt.number.desc()).limit(1))
            if attempt is None: return None
            row = await db.scalar(select(ArtifactPublication).where(ArtifactPublication.attempt_id==attempt.id))
            if row is None: return None
            if row.case_id!=case.id: raise HTTPException(409,'产物归属记录不一致')
            head = await db.get(ArtifactPublicationHead,(case.id,row.kind))
            binding = {'job_status':job.status,'phase':attempt.phase,'reservation':attempt.publication_json,
                'prefix':case.s3_prefix,'publication':{key:getattr(row,key) for key in
                    ('id','case_id','attempt_id','kind','generation','immutable_path','manifest_sha256','status')},
                'active':head.active_publication_id if head else None,'head_version':head.version if head else None}
            return row,binding

    before = await snapshot()
    if before is None: return {'job_id':job_id,'state':'no_publication','automatic_replay':False}
    row,binding = before
    result = await finish_before_cancel(asyncio.to_thread(inspect,binding['prefix'],row))
    after = await snapshot()
    if after is None or after[1]!=binding:
        return {'job_id':job_id,'state':'state_changed','automatic_replay':False}
    if result['state']=='bundle_verified':
        action = 'retain_committed' if row.status=='committed' else 'retain_for_finalizer' if row.status=='prepared' else 'retain_historical'
    else:
        action = 'review' if result['state'] in {'requires_attention','legacy_unjournaled'} else 'resume_owned_preparation'
    return {'job_id':job_id,'publication_id':row.id,**result,'database_status':row.status,
        'active':binding['active']==row.id,'next_step':action,'automatic_replay':False}
