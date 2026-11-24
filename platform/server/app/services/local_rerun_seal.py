"""Prepare live filesystem selections before a rerun's write transaction."""
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from types import SimpleNamespace
import uuid

from .. import storage
from . import attempt_seal_journal as seals, attempt_workspace as workspace
from . import local_retry_seal, tree_metadata_native
from .case_provision import check_deadline, io
from .case_write import CaseWriteConflict
from .file_stage_locations import MOUNTED_SCHEMA, locate
from .local_file_edits import _version
from .owned_staging import batch
from .request_deadline import current
from .seal_storage_budget import estimate
from .step_handlers import PreparedStep


SCHEMA='vibeflow.local-rerun-staging.v1'


def stale():
    return CaseWriteConflict('input_stale','文件系统重跑的当前输入在封存期间发生变化')


def stamp(path):
    check_deadline()
    version=_version(path)
    if version is None:
        raise stale()
    return {'size':version[2],'version':version}


def selection_versions(snapshot):
    try:
        paths=workspace.live_roots(snapshot.source) if snapshot.all_live else snapshot.paths
        directories=[]
        files=workspace.inventory(snapshot.source,paths,directories,file_record=stamp,deadline=current()) if paths else {}
        mode=_version(tree_metadata_native.checked(snapshot.source,tree_metadata_native.PATH))
        publications=tuple({
            'manifest':stamp(workspace.checked_path(item.root.parent,'manifest.json')),
            'files':{path:stamp(workspace.checked_path(item.root,path)) for path in item.files}
        } for item in snapshot.publications)
        return paths,directories,files,mode,publications
    except (FileNotFoundError,NotADirectoryError,ValueError) as error:
        raise stale() from error


@dataclass(frozen=True)
class PreparedRerun:
    id: str
    prefix: str
    directory: Path
    draft: dict
    snapshot: workspace.Snapshot
    source_versions: tuple
    copy_files: tuple
    user_peak: int
    already_counted: int
    case_command: PreparedStep | None = None

    def require_submission(self,case,spec,paths,publications,generated,*,all_live):
        workspace.require_staged_submission(self.draft,case,spec,paths,publications,generated,all_live=all_live)

    def install(self,case,attempt,manifest):
        if (case.s3_prefix!=self.prefix or attempt.id!=self.id or attempt.retry_of_attempt_id is not None
                or case.dict_revision!=self.draft['source']['dict_revision']
                or selection_versions(self.snapshot)!=self.source_versions):
            raise stale()
        return local_retry_seal.install_files(case,attempt,manifest,self.directory,self.copy_files)

    async def bind_submission(self,db,case,attempt):
        return await local_retry_seal.bind_workspace(db,case,attempt,self)


def prepare_files(case,snapshot,directory,budget,payload_counted):
    versions=selection_versions(snapshot)
    snapshot.write_files(directory,deadline=current())
    if selection_versions(snapshot)!=versions:
        raise stale()
    manifest=snapshot.describe()
    copied=local_retry_seal.copy_versions(directory,manifest)
    return PreparedRerun(manifest['attempt_id'],case.s3_prefix,directory,manifest,snapshot,versions,copied,
                         budget.user_bytes,payload_counted)


def _snapshot(case, spec, paths, publications, generated, *, all_live, command_handler):
    return workspace.Snapshot(case.s3_prefix,str(uuid.uuid4()),paths or [],publications=publications,
        all_live=all_live or command_handler is not None,generated_inputs=generated,
        provenance={'case_id':case.id,'job_id':None,'dict_revision':case.dict_revision,
                    'retry_of_attempt_id':None,'selection_mode':'case' if all_live else 'explicit','runtime':spec['runtime']})


def _plan(case, snapshot):
    check_deadline()
    draft=snapshot.describe()
    upper=SimpleNamespace(id=draft['attempt_id'],job_id=2**63-1)
    manifest={**draft,'source':{**draft['source'],'job_id':upper.job_id}}
    seals.validate(case,upper.id,seals.describe_plan(case,upper,manifest))
    return estimate(case,upper,None,manifest=manifest)


def _with_command(case, spec, staged, handler):
    if handler is None:
        return staged
    snapshot=staged.snapshot
    source={'files':snapshot.base_records,'directories':snapshot.base_directories}
    return replace(staged,case_command=handler.prepared(case.id,spec,source,snapshot.paths))


@asynccontextmanager
async def prepare(case,spec,paths,publications,generated,*,all_live,command_handler=None,check_peak=None):
    snapshot=_snapshot(case,spec,paths,publications,generated,all_live=all_live,command_handler=command_handler)
    await io(partial(snapshot.inspect,deadline=current()))
    if command_handler is not None and not 1 <= len(snapshot.paths) <= 256:
        raise ValueError('input_paths must select 1-256 relative files or directories')
    budget=await io(_plan,case,snapshot)
    if check_peak is not None:
        await check_peak(budget.user_bytes)
    root=storage._fs_path(case.s3_prefix,'.')
    default=storage._fs_path('.','.local-rerun-staging')
    location=await io(locate,root,snapshot.relative,default)
    schema=MOUNTED_SCHEMA if location.local else SCHEMA
    async with batch(location.scratch,budget.user_bytes,schema,
                     target_root=location.scratch if location.local else None) as slot:
        staged=await io(prepare_files,case,snapshot,slot/'attempt',budget,budget.payload_bytes if location.local else 0)
        yield _with_command(case,spec,staged,command_handler)


def _batch_snapshots(case, specs, handler):
    snapshots=[_snapshot(case,spec,None,None,None,all_live=handler is None,command_handler=handler) for spec in specs]
    first=snapshots[0]
    first.inspect(deadline=current())
    if handler is not None and not 1 <= len(first.paths) <= 256:
        raise ValueError('input_paths must select 1-256 relative files or directories')
    for snapshot in snapshots[1:]:
        snapshot.paths=list(first.paths)
        snapshot.selection=list(first.selection)
        snapshot.mode_document=first.mode_document
        snapshot.inspect_records(first.base_records,first.base_directories)
    return snapshots, [_plan(case,snapshot) for snapshot in snapshots]


def _batch_files(case, specs, snapshots, budgets, slot, local, handler):
    first=snapshots[0]
    initial=prepare_files(case,first,slot/first.target.name,budgets[0],budgets[0].payload_bytes if local else 0)
    result=[_with_command(case,specs[0],initial,handler)]
    for spec,snapshot,budget in zip(specs[1:],snapshots[1:],budgets[1:]):
        directory=slot/snapshot.target.name
        inputs=directory/'inputs'
        workspace.copy_records(initial.directory/'inputs',inputs,snapshot.records,snapshot.directories,deadline=current())
        workspace.copy_records(inputs,directory/'run',snapshot.records,snapshot.directories,deadline=current())
        manifest=snapshot.describe()
        staged=PreparedRerun(manifest['attempt_id'],case.s3_prefix,directory,manifest,snapshot,
            initial.source_versions,local_retry_seal.copy_versions(directory,manifest),budget.user_bytes,
            budget.payload_bytes if local else 0)
        result.append(_with_command(case,spec,staged,handler))
    if selection_versions(first)!=initial.source_versions:
        raise stale()
    return result


@asynccontextmanager
async def _batch_slot(case, snapshots, budgets, check_peak):
    peak=sum(budget.user_bytes for budget in budgets)
    await check_peak(peak)
    root=storage._fs_path(case.s3_prefix,'.')
    default=storage._fs_path('.','.local-rerun-staging')
    location=await io(locate,root,snapshots[0].relative,default)
    schema=MOUNTED_SCHEMA if location.local else SCHEMA
    async with batch(location.scratch,peak,schema,target_root=location.scratch if location.local else None) as slot:
        yield slot, location.local


@asynccontextmanager
async def prepare_batch(case, specs, handler, *, check_peak):
    snapshots,budgets=await io(_batch_snapshots,case,specs,handler)
    async with _batch_slot(case,snapshots,budgets,check_peak) as (slot,local):
        yield await io(_batch_files,case,specs,snapshots,budgets,slot,local,handler)


def _operation_snapshots(case, steps, verify):
    snapshots=[_snapshot(case,step.spec,step.input_paths,None,step.generated_inputs,
                         all_live=False,command_handler=None) for step in steps]
    first=snapshots[0]
    first.inspect(deadline=current())
    for snapshot in snapshots[1:]:
        if snapshot.paths!=first.paths:
            raise stale()
        snapshot.mode_document=first.mode_document
        snapshot.inspect_records(first.base_records,first.base_directories)
    for step,snapshot in zip(steps,snapshots):
        verify(step,snapshot.describe())
    return snapshots, [_plan(case,snapshot) for snapshot in snapshots]


@asynccontextmanager
async def prepare_operations(case, steps, *, verify, check_peak):
    snapshots,budgets=await io(_operation_snapshots,case,steps,verify)
    async with _batch_slot(case,snapshots,budgets,check_peak) as (slot,local):
        # Each registered member has its own generated configuration, even
        # when all members read the same public source tree.
        staged=[]
        for snapshot,budget in zip(snapshots,budgets):
            staged.append(await io(prepare_files,case,snapshot,slot/snapshot.target.name,budget,
                                   budget.payload_bytes if local else 0))
        yield staged
