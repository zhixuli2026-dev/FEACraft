"""Durable artifact generations. DB heads select immutable fs or S3 bundles.

Compatibility aliases are repaired after the authoritative transaction commits.
Orphan directory reconciliation after unknown commits belongs to the journal.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
import uuid

from sqlalchemy import select

from .. import storage
from ..models import ArtifactPublication, ArtifactPublicationHead, Case, Job, JobAttempt
from . import attempt_workspace as workspace
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict, coordinate
from .durable_events import append_event, json_text, now
from .fs_directory import move_new, remove_owned

PREFIX = '.vibeflow/publications'
SCHEMA = 'vibeflow.artifact-publication.v1'


async def reserve(db, case, attempt, *, kind, descriptor):
    """Caller holds the case lock; reserve before any native computation."""
    head = await db.get(ArtifactPublicationHead, (case.id, kind), populate_existing=True)
    if head is None:
        head = ArtifactPublicationHead(case_id=case.id, kind=kind, next_generation=1, version=0)
        db.add(head)
        await db.flush()
    identifier = str(uuid.uuid4())
    row = ArtifactPublication(id=identifier, case_id=case.id, kind=kind,
        generation=head.next_generation, attempt_id=attempt.id, status='prepared',
        immutable_path=f'{PREFIX}/{identifier}', manifest_sha256='0'*64)
    db.add(row)
    head.next_generation += 1
    head.version += 1
    attempt.publication_json = json_text({'schema':SCHEMA, 'publication_id':identifier,
        'head_version':head.version, 'descriptor':descriptor})
    await db.flush()
    await append_event(db, case.id, 'artifact.reserved', {'publication_id':identifier,
        'kind':kind, 'generation':row.generation}, attempt_id=attempt.id)
    return row


def read_manifest(prefix, row):
    if storage._use_s3():
        from .publication_objects import read_manifest as remote_manifest
        return remote_manifest(prefix, row)
    root = storage._fs_path(prefix, '.')
    relative = f'{PREFIX}/{row.id}'
    if row.immutable_path != relative:
        raise ValueError('Publication path does not match its identity')
    folder = workspace.checked_path(root, relative)
    from .local_source_reads import observe
    with observe(folder,['manifest.json']):
        data = workspace.checked_path(folder, 'manifest.json').read_bytes()
    if hashlib.sha256(data).hexdigest() != row.manifest_sha256:
        raise ValueError('Publication manifest does not match the database')
    manifest = json.loads(data)
    if (manifest.get('schema') != SCHEMA or manifest.get('publication_id') != row.id or
            manifest.get('case_id') != row.case_id or manifest.get('attempt_id') != row.attempt_id or
            manifest.get('kind') != row.kind or manifest.get('generation') != row.generation):
        raise ValueError('Publication manifest ownership differs from the database')
    return folder, manifest


def manifest_for_alias_paths(prefix, row):
    """Read an authenticated publication without constructing a remote file cache."""
    if storage._use_s3():
        from .publication_objects import verified_manifest
        return verified_manifest(prefix, row)
    return read_manifest(prefix, row)[1]


def read_file(prefix, row, path):
    folder, manifest = read_manifest(prefix, row)
    expected = manifest['files'].get(path)
    if expected is None:
        raise FileNotFoundError(path)
    target = workspace.checked_path(folder / 'files', path)
    data = target.read_bytes()
    if len(data) != expected['size'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
        raise ValueError('Published artifact was modified outside the application')
    return data


async def current(db, case_id, kind):
    head = await db.get(ArtifactPublicationHead, (case_id, kind), populate_existing=True)
    if head is None or head.active_publication_id is None:
        return head, None
    row = await db.get(ArtifactPublication, head.active_publication_id, populate_existing=True)
    if row is None or row.case_id != case_id or row.kind != kind or row.status != 'committed':
        raise ValueError('Active publication pointer is inconsistent')
    job = await db.scalar(select(Job).join(JobAttempt, Job.id == JobAttempt.job_id)
        .where(JobAttempt.id == row.attempt_id))
    if job is None or job.case_id != case_id or job.status != 'succeeded':
        raise ValueError('Active publication has no successful owned Job')
    return head, row


def prepare_bundle(prefix, row, run, records, descriptor):
    from .publication_journal import prepare
    return prepare(prefix, row, run, records, descriptor, _prepare_bundle_new)


def _prepare_bundle_new(prefix, row, run, records, descriptor):
    root = storage._fs_path(prefix, '.')
    parent = root
    for part in PREFIX.split('/'):
        parent = parent / part
        if not parent.exists():
            parent.mkdir()
        workspace.ordinary(parent)
    stage = parent / ('.preparing-'+row.id)
    target = parent / row.id
    stage.mkdir(exist_ok=False)
    try:
        directories = sorted({str(Path(path).parent).replace('\\','/') for path in records
            if str(Path(path).parent) != '.'})
        workspace.copy_records(run, stage / 'files', records, directories)
        manifest = {'schema':SCHEMA, 'publication_id':row.id, 'case_id':row.case_id,
            'attempt_id':row.attempt_id, 'kind':row.kind, 'generation':row.generation,
            'files':records, 'descriptor':descriptor}
        encoded = json_text(manifest).encode('utf-8')
        with (stage / 'manifest.json').open('xb') as stream:
            import os
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        move_new(stage, target, root=root)
        return hashlib.sha256(encoded).hexdigest()
    except BaseException:
        if stage.exists():
            remove_owned(stage, root=root)
        raise


@dataclass(frozen=True)
class PublicationIdentity:
    id: str
    case_id: int
    attempt_id: str
    kind: str
    generation: int
    immutable_path: str
    manifest_sha256: str

    @classmethod
    def capture(cls, row):
        return cls(**{key:getattr(row, key) for key in cls.__dataclass_fields__})

    def matches(self, row):
        return all(getattr(row, key) == value for key, value in vars(self).items())


@dataclass
class PreparedValidation:
    """In-memory proof from one owned snapshot; never accepted from a client."""
    case_id: int
    job_id: int
    attempt_id: str
    claim_sha256: str
    prefix: str
    input_json: str
    output_json: str
    publication_json: str
    kind: str
    descriptor: dict
    publication: PublicationIdentity
    records: dict | None = None
    error: dict | None = None
    bundle_sha256: str | None = None
    verified_binding: str | None = None

    def verification_binding(self):
        """Bind a local hash proof to every mutable preparation field."""
        return json_text({key:value for key, value in vars(self).items()
            if key not in {'verified_binding', 'publication'}} | {'publication':vars(self.publication)})

    def matches(self, job, attempt, case, row):
        return (self.case_id == job.case_id == case.id and self.job_id == job.id
            and self.attempt_id == attempt.id and self.prefix == case.s3_prefix
            and self.claim_sha256 == hashlib.sha256(attempt.claim_token.encode()).hexdigest()
            and self.input_json == attempt.input_manifest_json
            and self.output_json == attempt.output_manifest_json
            and self.publication_json == attempt.publication_json and self.kind == row.kind
            and self.publication.matches(row))


async def prepare_for_finish(owner, status, process_stop=None):
    """Release the ownership transaction before any compiler/validator runs.

    Cancellation, heartbeats and other case writers must be able to commit while
    native validation runs. Reserved live-source checks follow after all writers
    drain; finalize rechecks their proof, snapshot and head under case control.
    This preparation does not grant authority to publish.
    """
    if status != 'succeeded':
        return None
    from ..db import SessionLocal
    from .job_execution import _owned
    from .workflow_state import TERMINAL, ExecutionConflict
    from .step_handlers import require_binding, publication_handler, kind_for
    async with SessionLocal() as db:
        job, attempt = await _owned(db, owner)
        if job.status in TERMINAL or attempt.phase in TERMINAL:
            return None
        if attempt.write_checkpoint_json != '{}':
            # Recovery must resume verified publication state, never restart
            # artifact builders after their uncoordinated writes were closed.
            raise ExecutionConflict('recovery_required', 'Publication preparation has already drained')
        if process_stop is not None:
            from .process_record import for_attempt
            from .process_stop import validate as validate_stop
            identity = for_attempt(json.loads(attempt.process_identity_json), job, attempt)
            validate_stop(process_stop, identity)
            if not process_stop['verified']:
                return None
        reservation = json.loads(attempt.publication_json)
        if reservation.get('schema') != SCHEMA:
            return None
        row = await db.get(ArtifactPublication, reservation['publication_id'])
        if row is None or row.case_id != job.case_id or row.attempt_id != attempt.id:
            raise CaseWriteConflict('state_conflict', 'Publication reservation belongs to another attempt')
        if row.status != 'prepared':
            return None
        try:
            await require_binding(db, job, attempt)
        except ExecutionConflict:
            # The final owned transaction persists the existing binding failure.
            return None
        case = await db.get(Case, job.case_id)
        prepared = PreparedValidation(case.id, job.id, attempt.id,
            hashlib.sha256(owner.token.encode()).hexdigest(), case.s3_prefix,
            attempt.input_manifest_json, attempt.output_manifest_json, attempt.publication_json,
            row.kind, reservation['descriptor'], PublicationIdentity.capture(row))
    # The session and its case write lock are closed before entering the thread.
    try:
        frozen = json.loads(prepared.input_json)['workspace']
        _, run = await finish_before_cancel(asyncio.to_thread(workspace.verify, prepared.prefix, frozen))
        handler = publication_handler(prepared.descriptor)
        if prepared.kind != kind_for(handler, prepared.descriptor):
            raise ValueError('Publication kind does not match its registered handler')
        from .tree_metadata_native import PATH as mode_path
        # The descriptor is an integrity-checked runtime member, not an
        # engineering input added to the handler's original source contract.
        handler_files = {path: record for path, record in frozen['files'].items() if path != mode_path}
        prepared.records = await finish_before_cancel(asyncio.to_thread(handler.artifacts, run,
            prepared.descriptor, json.loads(prepared.output_json), handler_files))
    except (OSError, ValueError, KeyError, TypeError) as error:
        prepared.error = {'type':type(error).__name__, 'message':str(error)[:2000]}
    return prepared


async def prepare_bundle_for_finish(owner, prepared, recovery_context=None):
    """Copy/fsync the validated output without holding the case write lock.

    This runs after native finalizers drain. The detached reservation has no ORM
    session; finalize still verifies ownership, input bindings and bundle bytes.
    """
    if not isinstance(prepared, PreparedValidation) or prepared.error is not None:
        return prepared
    from ..db import SessionLocal
    from .job_execution import _owned
    from .workflow_state import TERMINAL
    from fastapi import HTTPException
    from .publication_storage import capacity,quota_failure
    try:
        async with SessionLocal() as db:
            job, attempt = await _owned(db, owner)
            if job.status in TERMINAL or attempt.phase in TERMINAL:
                return prepared
            row = await db.get(ArtifactPublication, prepared.publication.id)
            case = await db.get(Case, job.case_id)
            if row is None or row.status != 'prepared' or not prepared.matches(job, attempt, case, row):
                raise ValueError('Bundle preparation no longer matches its owned reservation')
            # Historical manifests are immutable application-owned records. Copy
            # their identities in this transaction and read the files after it ends.
            previous = []
            if row.kind == 'solve':
                previous = [PublicationIdentity.capture(old) for old in await db.scalars(
                    select(ArtifactPublication).where(ArtifactPublication.case_id == case.id,
                        ArtifactPublication.kind == 'solve', ArtifactPublication.status == 'committed',
                        ArtifactPublication.generation < row.generation)
                    .order_by(ArtifactPublication.generation.desc()))]
        frozen = json.loads(prepared.input_json)['workspace']
        _, run = await finish_before_cancel(asyncio.to_thread(workspace.verify, prepared.prefix, frozen))
        if not isinstance(prepared.records, dict):
            raise ValueError('Publication validation did not produce an artifact inventory')
        descriptor = prepared.descriptor
        descriptor['generated_paths'] = sorted(set(descriptor.get('generated_paths', [])) | set(prepared.records))
        if prepared.kind == 'solve':
            descriptor['retired_aliases'] = await finish_before_cancel(asyncio.to_thread(
                retired_solve_aliases, prepared.prefix, previous, prepared.records, descriptor['run_dir']))
        async with capacity(prepared.case_id,prepared.prefix,prepared.publication,prepared.records,descriptor) as ticket:
            if recovery_context is not None:
                from .publication_preparation_recovery import record
                await record(owner,prepared,*recovery_context)
            prepared.bundle_sha256 = ticket.estimate.digest if ticket.ready else await finish_before_cancel(
                asyncio.to_thread(prepare_bundle,prepared.prefix,prepared.publication,run,prepared.records,descriptor))
    except HTTPException as error:
        quota_failure(owner,error)
        prepared.error = {'type':'PublicationCapacityError','status':error.status_code,'message':str(error.detail)[:2000]}
    except (OSError, ValueError, KeyError, TypeError) as error:
        prepared.error = {'type':type(error).__name__, 'message':str(error)[:2000]}
    return prepared


def verify_prepared_bundle(prepared):
    if not isinstance(prepared.bundle_sha256, str) or len(prepared.bundle_sha256) != 64:
        raise ValueError('Publication bundle preparation did not complete')
    identity = replace(prepared.publication, manifest_sha256=prepared.bundle_sha256)
    remote = storage._use_s3()
    if remote:
        manifest = manifest_for_alias_paths(prepared.prefix, identity)
    else:
        folder, manifest = read_manifest(prepared.prefix, identity)
    if manifest['files'] != prepared.records or manifest['descriptor'] != prepared.descriptor:
        raise ValueError('Prepared publication differs from its validated artifacts')
    if remote:
        return  # The remote verifier already authenticated every member.
    for path, expected in prepared.records.items():
        actual = workspace.digest_file(workspace.checked_path(folder / 'files', path))
        if actual != {key:expected[key] for key in ('size', 'sha256')}:
            raise ValueError('Prepared publication file changed before commit')


def verify_immutable_files(prepared):
    """Read application-immutable paths only, after all native finalizers drain.

    Live source/dependency/destination checks use a separate content reservation.
    This does not provide isolation from arbitrary native or external writes.
    """
    manifest=json.loads(prepared.input_json)['workspace']
    workspace.verify(prepared.prefix, manifest)
    if storage._use_s3():
        from .attempt_objects import verify_outputs
        verify_outputs(prepared.prefix,manifest,json.loads(prepared.output_json))
    verify_prepared_bundle(prepared)


async def verify_files_for_finish(owner, prepared):
    """Keep bulk immutable reads out of the heartbeat/cancellation transaction."""
    if not isinstance(prepared, PreparedValidation) or prepared.error is not None:
        return prepared
    from ..db import SessionLocal
    from .job_execution import _owned
    from .workflow_state import TERMINAL
    prepared.verified_binding = None
    try:
        async with SessionLocal() as db:
            job, attempt = await _owned(db, owner)
            if job.status in TERMINAL or attempt.phase in TERMINAL:
                return prepared
            row = await db.get(ArtifactPublication, prepared.publication.id)
            case = await db.get(Case, job.case_id)
            if row is None or row.status != 'prepared' or not prepared.matches(job, attempt, case, row):
                raise ValueError('Final validation no longer matches its owned reservation')
        binding = prepared.verification_binding()
        await finish_before_cancel(asyncio.to_thread(verify_immutable_files, prepared))
        if prepared.verification_binding() != binding:
            raise ValueError('Publication preparation changed during final validation')
        prepared.verified_binding = binding
    except (OSError, ValueError, KeyError, TypeError) as error:
        prepared.error = {'type':type(error).__name__, 'message':str(error)[:2000]}
    return prepared


async def finalize(db, job, attempt, status, reason, prepared=None, validation=None):
    """Called by finish in its owned transaction, before Job/attempt terminal."""
    reservation = json.loads(attempt.publication_json)
    if reservation.get('schema') != SCHEMA:
        return status, reason
    row = await db.get(ArtifactPublication, reservation['publication_id'], populate_existing=True)
    if row is None or row.case_id != job.case_id or row.attempt_id != attempt.id:
        raise CaseWriteConflict('state_conflict', 'Publication reservation belongs to another attempt')
    if row.status != 'prepared':
        return status, reason
    if status != 'succeeded':
        row.status = 'failed'
        from .publication_storage import release_failed
        await release_failed(db,row)
        _record_result(attempt, row)
        return status, reason
    case = await db.get(Case, job.case_id, populate_existing=True)
    head = await db.get(ArtifactPublicationHead, (case.id, row.kind), populate_existing=True)
    try:
        from .step_handlers import publication_handler,kind_for
        if not isinstance(prepared, PreparedValidation) or not prepared.matches(job, attempt, case, row):
            raise ValueError('Publication validation does not match the current owned snapshot')
        if prepared.error is not None:
            row.status = 'failed'
            output = json.loads(attempt.output_manifest_json)
            output['publication_error'] = prepared.error
            attempt.output_manifest_json = json_text(output)
            return 'failed', 'publication_failed'
        if prepared.verified_binding is None or prepared.verified_binding != prepared.verification_binding():
            raise ValueError('Publication immutable files have no matching final validation')
        descriptor = prepared.descriptor
        handler = publication_handler(descriptor)
        if row.kind != kind_for(handler,descriptor):
            raise ValueError('Publication kind does not match its registered handler')
        from .case_artifact_trace import sealed_source
        provenance = sealed_source(attempt, case.id)
        row.manifest_sha256 = prepared.bundle_sha256
        receipt = {'publication_id':row.id, 'generation':row.generation, 'kind':row.kind,
                   'manifest_sha256':row.manifest_sha256, 'provenance':provenance}
        if (head is None or row.generation != head.next_generation-1 or
                head.version != reservation['head_version']):
            row.status = 'superseded'
            await append_event(db, case.id, 'artifact.superseded', receipt, attempt_id=attempt.id)
            return status, reason
        if validation is None:
            raise ValueError('Publication source has no reserved final validation')
        if not await validation.require_publication(db):
            row.status = 'failed'
            return 'failed', 'input_stale'
        if not isinstance(validation.alias_plan_sha256,str) or len(validation.alias_plan_sha256)!=64:
            raise ValueError('Publication has no verified compatibility operation plan')
        output=json.loads(attempt.output_manifest_json)
        output['alias_plan_sha256']=validation.alias_plan_sha256
        attempt.output_manifest_json=json_text(output)
        row.status, row.committed_at = 'committed', now()
        head.active_publication_id = row.id
        head.version += 1
        await append_event(db, case.id, 'artifact.published', receipt, attempt_id=attempt.id)
    except (OSError, ValueError, KeyError, TypeError) as error:
        row.status = 'failed'
        output=json.loads(attempt.output_manifest_json)
        output['publication_error']={'type':type(error).__name__,'message':str(error)[:2000]}
        attempt.output_manifest_json=json_text(output)
        return 'failed', 'publication_failed'
    finally:
        _record_result(attempt, row)
    return status, reason


def _record_result(attempt, row):
    output = json.loads(attempt.output_manifest_json)
    output['publication'] = {'id':row.id, 'kind':row.kind, 'generation':row.generation,
        'status':row.status, 'manifest_sha256':row.manifest_sha256}
    attempt.output_manifest_json = json_text(output)


def retired_solve_aliases(prefix, previous, records, run_dir):
    """Only previously committed, hash-identifiable result files can be retired."""
    from .solve_artifacts import managed
    retired = {}
    for old in previous:
        try:
            manifest = manifest_for_alias_paths(prefix, old)
            if manifest['descriptor']['run_dir'] != run_dir:
                continue
            for path, record in manifest['files'].items():
                if path not in records and managed(path, manifest['descriptor']['run_dir']):
                    retired.setdefault(path, record)
        except (OSError, ValueError, KeyError, TypeError):
            # Corrupt historical evidence gives no authority to delete a file.
            continue
    return retired


def _write_aliases(prefix, row):
    if getattr(row,'alias_plan_sha256',None):
        from .publication_alias_journal import apply
        return apply(prefix,row,row.alias_plan_sha256)
    if storage._use_s3():
        raise ValueError('S3 alias repair requires its committed conditional operation plan')
    _, manifest = read_manifest(prefix, row)
    from .step_handlers import publication_handler
    writer = getattr(publication_handler(manifest['descriptor']), 'write_aliases', None)
    if writer is not None:
        return writer(prefix, row, manifest)
    for path in manifest['files']:
        # Trusted compatibility mirror: do not record this as a user override.
        storage._fs_put(prefix, path, read_file(prefix, row, path))
    changed = []
    root = storage._fs_path(prefix, '.')
    for path, expected in manifest['descriptor'].get('retired_aliases', {}).items():
        if path in manifest['files']:
            raise ValueError('Retired alias is also a current result')
        try:
            target = workspace.checked_path(root, path)
        except FileNotFoundError:
            continue
        if workspace.digest_file(target) == {key:expected[key] for key in ('size','sha256')}:
            target.unlink()
        else:
            changed.append(path)
    if changed:
        raise ValueError('Modified old results were preserved: '+', '.join(changed))


async def repair_aliases(owner):
    from .execution_storage_admission import admit
    # Each round opens and closes its own transaction. A sibling publication's
    # source fence or remote owner grant is temporary, and grants no permission
    # to bypass the immutable alias plan or overwrite an edited destination.
    return await admit(lambda:_repair_aliases_once(owner),case_contention=True)


async def _repair_aliases_once(owner):
    from ..db import SessionLocal
    from .case_write_capacity import committing
    from .publication_alias_local import stage
    async with SessionLocal() as db:
        attempt = await db.get(JobAttempt, owner.attempt_id)
        reservation = json.loads(attempt.publication_json)
        if reservation.get('schema') != SCHEMA:
            return
        row = await db.get(ArtifactPublication, reservation['publication_id'])
        if row is None or row.status != 'committed':
            return
        case = await db.get(Case, row.case_id)
        _, active = await current(db, case.id, row.kind)
        if active is None or active.id != row.id:
            return
        prefix = case.s3_prefix
        digest = json.loads(attempt.output_manifest_json).get('alias_plan_sha256')
        db.expunge(row)
        await finish_before_cancel(db.rollback())
        if digest and storage._use_s3():
            from .publication_alias_objects import stage_application
            async with stage_application(prefix,row,digest):
                pass
            return
        async with stage(prefix, row, digest) as prepared:
            case = await db.get(Case, row.case_id, populate_existing=True)
            async with committing(db, case):
                _, active = await current(db, case.id, row.kind)
                if active is None or active.id != row.id:
                    return
                if prepared is None:
                    active.alias_plan_sha256=digest
                    await finish_before_cancel(asyncio.to_thread(_write_aliases, case.s3_prefix, active))
                else:
                    await prepared.publish(db, case, active)


async def managed_paths(db, prefix, row):
    try:
        manifest = await finish_before_cancel(asyncio.to_thread(manifest_for_alias_paths, prefix, row))
        return set(manifest['files']) | set(manifest['descriptor'].get('generated_paths', []))
    except (OSError, ValueError, KeyError):
        # A damaged old bundle must not prevent users from submitting a repair.
        attempt = await db.get(JobAttempt, row.attempt_id)
        return fallback_managed_paths(row, attempt)


def fallback_managed_paths(row, attempt):
    """SQL snapshot fallback for damaged historical bundles; performs no IO."""
    reservation = json.loads(attempt.publication_json)
    if reservation['descriptor'].get('handler')=='prepared-solve.v1':
        from .solve_artifacts import managed
        descriptor=reservation['descriptor'];origin=descriptor.get('origin_source',descriptor['source'])['files']
        return {p for p,r in json.loads(attempt.output_manifest_json).get('files',{}).items()
            if p.startswith(descriptor['run_dir']+'/') and (managed(p,descriptor['run_dir']) or origin.get(p)!=r)}
    if reservation['descriptor'].get('handler') in {'workflow-udf.v1','workflow-udf-build.v1'}:
        descriptor=reservation['descriptor'];native=descriptor['native'];source=native.get('origin_source',native['source'])['files']
        files=json.loads(attempt.output_manifest_json).get('files',{})
        if descriptor['handler']=='workflow-udf-build.v1':
            from .udf_profiles import artifact
            base='UDF/'+descriptor['name']+'/'
            return set(files)&{base+artifact(native['configuration']),base+'udf-build.log',base+'udf-load.log'}
        from .mpi_artifacts import is_input
        return {p for p,r in files.items() if p.startswith(descriptor['run_dir']+'/')
            and not is_input(p,descriptor['run_dir']) and source.get(p)!=r}
    if row.kind.startswith('calculix:'):
        from .calculix_artifacts import managed
        source=reservation['descriptor']['source']
        return {path for path in json.loads(attempt.output_manifest_json).get('files',{}) if managed(path,source)}
    if row.kind.startswith('su2:'):
        from .su2_artifacts import managed
        source=reservation['descriptor']['source']
        return {path for path in json.loads(attempt.output_manifest_json).get('files',{}) if managed(path,source)}
    if row.kind.startswith('elmer:'):
        from .elmer_artifacts import managed
        source=reservation['descriptor']['source']
        return {path for path in json.loads(attempt.output_manifest_json).get('files',{}) if managed(path,source)}
    if row.kind.startswith('moose:'):
        from .moose_artifacts import output_paths
        source = reservation['descriptor']['source']
        selected = output_paths(source['input'],checkpoint=source.get('checkpoint',False))
        return set(json.loads(attempt.output_manifest_json).get('files',{})) & set(selected)
    if row.kind.startswith('code-aster:'):
        selected = reservation['descriptor']['source']['plan']['outputs']
        return set(json.loads(attempt.output_manifest_json).get('files',{})) & set(selected)
    if row.kind.startswith(('paraview:','mpi:','udf:','flotherm-convert:','flotherm-library:',
                            'flotherm-pack:','case-command:')):
        source=reservation['descriptor'].get('origin_source',reservation['descriptor']['source'])['files']
        return {path for path,record in json.loads(attempt.output_manifest_json).get('files',{}).items()
            if path.split('/')[0].casefold()!='.vibeflow' and source.get(path)!=record}
    if row.kind.startswith(('pdf-number:','ppt:')):
        return {reservation['descriptor']['output']}
    if row.kind in {'report-source', 'report-source-generated'}:
        from .compose_artifacts import directory
        descriptor = reservation['descriptor']
        folder = directory(descriptor['execution_id'])+'/'
        return {descriptor['output']} | {path for path in json.loads(attempt.output_manifest_json).get('files', {})
            if path.startswith(folder)}
    if row.kind.startswith('latex:'):
        from .latex_artifacts import paths
        return set(paths(reservation['descriptor']['main']).values())
    if row.kind == 'mesh':
        from .mesh_artifacts import managed
        return {path for path in json.loads(attempt.output_manifest_json).get('files', {})
                if managed(path, reservation['descriptor']['run_dir'])}
    if row.kind == 'solve':
        if reservation['descriptor'].get('handler')=='workflow-mpi.v1':
            from .mpi_artifacts import is_input
            descriptor=reservation['descriptor'];source=descriptor['native'].get('origin_source',descriptor['native']['source'])['files']
            return {p for p,r in json.loads(attempt.output_manifest_json).get('files',{}).items()
                    if p.split('/')[0].casefold()!='.vibeflow' and not is_input(p,descriptor['run_dir']) and source.get(p)!=r}
        from .solve_artifacts import managed
        return {path for path in json.loads(attempt.output_manifest_json).get('files', {})
                if managed(path, reservation['descriptor']['run_dir'])}
    if row.kind == 'postprocess':
        from .postprocess_artifacts import managed
        return {path for path in json.loads(attempt.output_manifest_json).get('files', {})
                if managed(path, attempt.job_id)}
    if row.kind == 'report':
        from .report_artifacts import managed
        return {path for path in json.loads(attempt.output_manifest_json).get('files', {})
                if managed(path, attempt.job_id)}
    source = reservation['descriptor']['source']['geometry_files']
    return {path for path in json.loads(attempt.output_manifest_json).get('files', {})
        if path.startswith('A-geo/') and path not in source and not path.startswith('A-geo/logs/')}


async def invalidate_aliases(db, case, paths, *, prepared=None):
    """Explicit edits to generated aliases become a draft, not a valid result."""
    if prepared is not None:
        await prepared.validate_locked(db, case)
    heads = list(await db.scalars(select(ArtifactPublicationHead).where(ArtifactPublicationHead.case_id == case.id)))
    for head in heads:
        row = await db.get(ArtifactPublication, head.active_publication_id) if head.active_publication_id else None
        if row is not None:
            managed = prepared.paths_for(head.kind) if prepared is not None else await managed_paths(db, case.s3_prefix, row)
        elif head.kind == 'geometry':
            from .geometry_publication import GENERATED
            managed = GENERATED | {path for path in paths if path.startswith('A-geo/exports/')}
        else:
            continue
        import os
        normalized = lambda values: {path.casefold() if os.name == 'nt' else path for path in values}
        if not normalized(paths).intersection(normalized(managed)):
            continue
        head.active_publication_id = None
        head.version += 1
        await append_event(db, case.id, 'artifact.invalidated', {'publication_id':row.id if row else None,
            'kind':head.kind, 'reason':'output_alias_edited'})
