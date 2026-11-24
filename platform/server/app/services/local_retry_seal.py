"""Prepare both filesystem retry trees before the case write transaction."""
from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import uuid

from .. import storage
from . import attempt_seal_journal as seals, attempt_workspace as workspace
from .case_provision import check_deadline, io
from .case_write import CaseWriteConflict
from .command_input_seal import retry_manifest
from .durable_events import json_text
from .file_stage_locations import MOUNTED_SCHEMA, locate
from .file_transactions import _path, _sync, _write_new
from .local_file_budget import check_quota
from .local_file_edits import _version
from .owned_staging import batch
from .request_deadline import current
from .seal_storage_budget import estimate


SCHEMA='vibeflow.local-retry-staging.v1'


def _stale(path=None):
    return CaseWriteConflict('input_stale','文件系统重试的原封存或准备副本已变化',path=path)


def _tree_versions(root,manifest,*,allow_missing=False):
    children={relative:set() for relative in ('',*manifest['directories'])}
    for relative in (*manifest['directories'],*manifest['files']):
        parent,_,name=relative.rpartition('/')
        if parent not in children:
            raise _stale(relative)
        children[parent].add(name)
    versions={}
    for relative,names in children.items():
        check_deadline()
        try:
            directory=workspace.checked_path(root,relative) if relative else root
            info=workspace.ordinary(directory)
        except FileNotFoundError:
            if allow_missing and relative:
                continue
            raise
        if not stat.S_ISDIR(info.st_mode):
            raise _stale(relative)
        missing=names.copy()
        with os.scandir(directory) as entries:
            for entry in entries:
                check_deadline()
                if entry.name not in missing:
                    raise _stale(relative)
                missing.remove(entry.name)
        if missing and not allow_missing:
            raise _stale(relative)
        versions[relative]=(info.st_dev,info.st_ino,info.st_mtime_ns,info.st_ctime_ns)
    for relative,record in manifest['files'].items():
        check_deadline()
        try:
            stamp=_version(workspace.checked_path(root,relative))
        except FileNotFoundError:
            if allow_missing:
                continue
            raise
        if stamp is None and allow_missing:
            continue
        if (stamp is None or stamp[2]!=record['size']
                or bool(stamp[-1]&0o111)!=record['executable']):
            raise _stale(relative)
        versions[relative]=stamp
    return versions


def _archive_paths(prefix,manifest):
    root=storage._fs_path(prefix,'.')
    inputs=workspace.checked_path(root,manifest['inputs_path'])
    return inputs,workspace.checked_path(root,workspace.PREFIX+'/'+manifest['attempt_id']+'/manifest.json')


def copy_versions(directory,manifest):
    return tuple(_tree_versions(workspace.checked_path(directory,side),manifest) for side in ('inputs','run'))


def install_files(case,attempt,manifest,directory,versions):
    if copy_versions(directory,manifest)!=versions:
        raise _stale()
    plan=seals.prepare_manifest(case,attempt,manifest)
    _write_new(directory/'manifest.json',json.dumps(manifest,ensure_ascii=False,sort_keys=True).encode('utf8'))
    target=_path(storage._fs_path(case.s3_prefix,'.'),workspace.PREFIX+'/'+attempt.id)
    workspace.move_new(directory,target,root=storage._fs_root())
    _sync(target.parent)
    return target,plan


async def bind_workspace(db,case,attempt,staged):
    await seals.recover_locked(db,case)
    await check_quota(db,case,staged.user_peak-staged.already_counted)
    manifest={**staged.draft,'source':{**staged.draft['source'],'job_id':attempt.job_id}}
    _,plan=await io(staged.install,case,attempt,manifest)
    original=json.loads(attempt.input_manifest_json)
    attempt.input_manifest_json=json_text({**original,'workspace':manifest})
    await db.flush()
    await seals.witness(db,case,attempt,plan)
    return manifest


@dataclass(frozen=True)
class PreparedRetry:
    id: str
    prefix: str
    directory: Path
    retry_source: dict
    draft: dict
    original_files: dict
    original_manifest: tuple
    copy_files: tuple
    user_peak: int
    already_counted: int

    def install(self,case,attempt,manifest):
        if case.s3_prefix!=self.prefix or attempt.id!=self.id or attempt.retry_of_attempt_id!=self.retry_source['attempt_id']:
            raise _stale()
        inputs,original_manifest=_archive_paths(self.prefix,self.retry_source)
        if (_version(original_manifest)!=self.original_manifest
                or _tree_versions(inputs,self.retry_source)!=self.original_files):
            raise _stale()
        return install_files(case,attempt,manifest,self.directory,self.copy_files)

    async def bind_retry(self,db,case,attempt):
        return await bind_workspace(db,case,attempt,self)

def prepare_files(case,original,draft,slot,peak,payload_counted):
    inputs,metadata=_archive_paths(case.s3_prefix,original)
    stamp=_version(metadata)
    if stamp is None or stamp[2]>seals.MAX_RECORD:
        raise _stale()
    with metadata.open('rb') as stream:
        if json.loads(stream.read(seals.MAX_RECORD+1))!=original:
            raise _stale()
    source_files=_tree_versions(inputs,original)
    ready=slot/'attempt'
    deadline=current()
    workspace.copy_records(inputs,ready/'inputs',original['files'],original['directories'],deadline=deadline)
    workspace.copy_records(ready/'inputs',ready/'run',original['files'],original['directories'],deadline=deadline)
    if _version(metadata)!=stamp or _tree_versions(inputs,original)!=source_files:
        raise _stale()
    copied=copy_versions(ready,draft)
    return PreparedRetry(draft['attempt_id'],case.s3_prefix,ready,original,draft,source_files,stamp,copied,
                         peak,payload_counted)


@asynccontextmanager
async def prepare(case,original):
    workspace.validate_identity(original)
    identifier=str(uuid.uuid4())
    draft=await io(retry_manifest,case,original,identifier)
    upper=SimpleNamespace(id=identifier,job_id=2**63-1)
    manifest={**draft,'source':{**draft['source'],'job_id':upper.job_id}}
    await io(seals.validate,case,identifier,seals.describe_plan(case,upper,manifest))
    planned=await io(lambda:estimate(case,upper,None,manifest=manifest))
    root=storage._fs_path(case.s3_prefix,'.')
    default=storage._fs_path('.','.local-retry-staging')
    location=await io(locate,root,workspace.PREFIX+'/'+identifier,default)
    schema=MOUNTED_SCHEMA if location.local else SCHEMA
    async with batch(location.scratch,planned.user_bytes,schema,
                     target_root=location.scratch if location.local else None) as directory:
        yield await io(prepare_files,case,original,draft,directory,planned.user_bytes,
                       planned.payload_bytes if location.local else 0)
