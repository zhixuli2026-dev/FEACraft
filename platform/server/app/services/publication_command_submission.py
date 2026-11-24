"""Verify explicit publication inputs before taking the job write transaction."""
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from types import SimpleNamespace

from .. import storage
from ..db import SessionLocal
from ..models import Case, User
from . import command_input_seal, document_admission, local_rerun_seal, publication_inputs
from . import typed_command_submission
from .case_provision import io
from .case_read_capacity import file_read_deadline
from .case_write import CaseWriteConflict
from .case_read_view import remote_live_version
from .local_source_reads import LocalSourceReads, Selection
from .publication_objects import PublicationObjectReads
from .publication_read_set import PublicationReadSet
from .tree_metadata_native import PATH as mode_path


CODE = 'publication_seal'
LABEL = '封存作业发布成果输入'


def _local_versions(prefix):
    root = storage._fs_path(prefix, '.')
    return Selection(root, None).snapshot(), Selection(root, (mode_path,)).snapshot()


@dataclass(frozen=True)
class StagedPublicationInputs:
    workspace: local_rerun_seal.PreparedRerun | command_input_seal.StagedInputs
    database_reads: PublicationReadSet
    require_files: Callable[[], Awaitable[None]]

    @property
    def id(self):
        return self.workspace.id

    def require_submission(self, case, spec, paths, publications, generated, *, all_live):
        self.workspace.require_submission(case, spec, paths, publications, generated, all_live=all_live)

    async def bind_submission(self, db, case, attempt):
        manifest = await self.workspace.bind_submission(db, case, attempt)
        await self.database_reads.require_current(db)
        await self.require_files()
        return manifest


async def _inspect(case, selections, files, versions, require_archives):
    database = PublicationReadSet()
    # Some legacy freshness validators discover dependencies from the public
    # tree, including missing files and portable modes. Historical references
    # stop that traversal and must not depend on the live aliases.
    watch_live = any(item['require_current'] for item in selections)
    before = await io(versions, case.s3_prefix) if watch_live else None
    with files.collect():
        resolved = await publication_inputs.resolve(database, case, selections)
    database.seal()

    async def require_files():
        await require_archives()
        if watch_live and await io(versions, case.s3_prefix) != before:
            raise publication_inputs.PublicationInputStale('所引用成果的当前输入在封存期间发生变化')

    await require_files()
    return resolved, database, require_files


@asynccontextmanager
async def stage_local(case, user_id, spec, paths, selections):
    files = LocalSourceReads()

    async def require_archives():
        if not files.sealed:
            files.seal()
        await files.require_current()

    async with typed_command_submission.seal_request(case, user_id, CODE, LABEL):
        resolved, database, require_files = await _inspect(case, selections, files, _local_versions, require_archives)
        async with local_rerun_seal.prepare(case, spec, paths, resolved, None, all_live=False,
                check_peak=partial(typed_command_submission.check_peak, case, user_id)) as staged:
            yield StagedPublicationInputs(staged, database, require_files)


@asynccontextmanager
async def stage_remote(db, case, user_id, key, digest, spec, paths, selections):
    from .request_deadline import STAGING_CLEANUP_SECONDS

    async with file_read_deadline(CODE, LABEL, seconds=STAGING_CLEANUP_SECONDS):
        async with command_input_seal.stage_submission(db, case, user_id, key, digest,
                code=CODE, label=LABEL, probe_first=True, allow_legacy=True) as (replay, lease, owner):
            if replay is not None or lease is None:
                yield None
                return
            async with lease.control() as session:
                selected = await session.get(Case, case.id)
                session.expunge(selected)
            files = PublicationObjectReads()

            async def require_archives():
                await io(files.require_current)

            resolved, database, require_files = await _inspect(
                selected, selections, files, remote_live_version, require_archives)
            step = SimpleNamespace(input_paths=paths or [], spec=spec)
            staged = await command_input_seal.upload(lease, owner, step, selected.dict_revision, None,
                publications=resolved, recheck_live=True)
            yield StagedPublicationInputs(staged, database, require_files)


def _stale():
    return CaseWriteConflict('input_stale','算例输入在封存期间发生变化')


async def _inspect_step(case, user_id, prepare, files, job_user_id):
    observations=PublicationReadSet()
    resolved=[]
    async with SessionLocal() as session:
        selected,actor=await document_admission.authorize(session,case.id,user_id)
        if (selected.s3_prefix,selected.dict_revision)!=(case.s3_prefix,case.dict_revision):
            raise _stale()
        creator=actor if job_user_id is None or job_user_id==user_id else await session.get(User,job_user_id)
        if creator is None:
            raise _stale()
        session.expunge(creator)
    with files.collect():
        # Only the final quota read in prepare() uses this session. Lineage
        # reads each close before the physical archive and case file reads.
        async with SessionLocal() as session:
            prepared=await prepare(session,case,creator,observations=observations,resolved_inputs=resolved)
        if prepared.admission is not None:
            prepared=await document_admission.complete(prepared,case.id,creator.id)
    observations.seal()
    return prepared,resolved,observations


@asynccontextmanager
async def _stage_step_local(case, user_id, prepare, registered, job_user_id, code, label):
    files=LocalSourceReads()
    async with typed_command_submission.seal_request(case,user_id,code,label):
        # A registered operation can have an older dependency handler whose
        # source reader does not expose a narrower filesystem selection.
        root=storage._fs_path(case.s3_prefix,'.')
        whole=Selection(root,None)
        baseline=await io(whole.snapshot) if registered else None
        prepared,resolved,observations=await _inspect_step(case,user_id,prepare,files,job_user_id)
        files.seal()
        legacy_draft=(prepared.key in {'report.compose','report.compose-generated'}
                      and not prepared.descriptor.get('registered_postprocess'))
        selection,expected=whole,baseline
        if (prepared.key=='report.build' and prepared.descriptor.get('registered_postprocess')
                and not files.uncovered):
            from .report_handler import live_source_paths
            paths=live_source_paths(prepared)
            selection=Selection(root,paths)
            expected={path:baseline[path] for path in paths if path in baseline}

        async def require_files():
            await files.require_current()
            if expected is not None and (files.uncovered or prepared.admission is not None or legacy_draft):
                observed=await io(selection.snapshot)
                if selection.paths is not None:
                    observed={path:observed[path] for path in selection.paths if path in observed}
                if observed!=expected:
                    raise _stale()
            if prepared.key=='report.build':
                from .report_handler import require_toolchain
                await io(require_toolchain,prepared)

        async with local_rerun_seal.prepare(case,prepared.spec,prepared.input_paths,resolved,prepared.generated_inputs,all_live=False,
                check_peak=partial(typed_command_submission.check_peak,case,user_id)) as workspace:
            if prepared.admission is not None:
                document_admission.validate_workspace(prepared,workspace.draft)
            yield prepared,StagedPublicationInputs(workspace,observations,require_files)


@asynccontextmanager
async def _stage_step_remote(db, case, user_id, key, digest, prepare, job_user_id, wait_for_capacity, validate_locked, code, label):
    async with command_input_seal.stage_submission(db,case,user_id,key,digest,code=code,label=label,
            probe_first=True,allow_legacy=True,wait_for_capacity=wait_for_capacity,
            validate_locked=validate_locked) as (replay,lease,owner):
        if replay is not None or lease is None:
            yield None
            return
        async with lease.control() as session:
            selected=await session.get(Case,case.id)
            session.expunge(selected)
        prefix=selected.s3_prefix
        baseline=await io(remote_live_version,prefix)
        files=PublicationObjectReads()
        if selected.dict_revision!=case.dict_revision:
            raise _stale()
        prepared,resolved,observations=await _inspect_step(selected,user_id,prepare,files,job_user_id)

        async def require_files():
            await io(files.require_current)
            if await io(remote_live_version,prefix)!=baseline:
                raise _stale()
            if prepared.key=='report.build':
                from .report_handler import require_toolchain
                await io(require_toolchain,prepared)

        async def verify_document(manifest):
            document_admission.validate_workspace(prepared,manifest)

        await require_files()
        verify=verify_document if prepared.admission is not None else None
        workspace=await command_input_seal.upload(lease,owner,prepared,selected.dict_revision,verify,
            publications=resolved,generated_inputs=prepared.generated_inputs,recheck_live=True)
        yield prepared,StagedPublicationInputs(workspace,observations,require_files)


def stage_step(db, case, user_id, prepare, *, registered, key=None, digest=None, job_user_id=None,
          code, label, wait_for_capacity=False, validate_locked=None):
    if storage._use_s3():
        return _stage_step_remote(db,case,user_id,key,digest,prepare,job_user_id,wait_for_capacity,validate_locked,code,label)
    return _stage_step_local(case,user_id,prepare,registered,job_user_id,code,label)
