"""Local attempt inputs and independent working copies, never live-case output.

The DB manifest is authoritative. This is an application immutability boundary,
not a sandbox for arbitrary native commands or an FS/DB crash journal.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import stat
import uuid

from .. import storage
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict, normalize_path
from .fs_directory import move_new, remove_owned

SCHEMA = "vibeflow.attempt-inputs.v1"
PREFIX = ".vibeflow/attempts"
MAX_FILES = 10000
MAX_BYTES = 1024 * 1024 * 1024


def require_sealed(job, attempt):
    """Check durable ownership before delivery; byte verification stays at launch.

    A historical command or delivery event cannot reconstruct the inputs that
    its requester saw. Never silently seal today's files for that old request.
    """
    from .workflow_state import ExecutionConflict
    try:
        document = json.loads(attempt.input_manifest_json) if attempt else {}
        manifest = document.get('workspace')
        if not isinstance(manifest, dict):
            raise ValueError('No sealed attempt input manifest')
        relative = PREFIX+'/'+attempt.id
        if (manifest.get('schema') != SCHEMA or manifest.get('attempt_id') != attempt.id
                or manifest.get('inputs_path') != relative+'/inputs'
                or manifest.get('run_path') != relative+'/run'
                or not isinstance(manifest.get('files'),dict)
                or not isinstance(manifest.get('directories'),list)
                or manifest.get('source',{}).get('case_id') != job.case_id
                or manifest['source'].get('job_id',job.id) != job.id
                or manifest['source'].get('runtime') != document['execution_spec']['runtime']):
            raise ValueError('Sealed attempt input ownership differs')
    except (ValueError,TypeError,KeyError,AttributeError) as error:
        raise ExecutionConflict('recovery_required',
            'Attempt has no valid sealed inputs; review the inputs and submit a new request') from error
    return manifest


def canonical_path(value):
    value = normalize_path(value)
    if any(part.endswith((".", " ")) or PureWindowsPath(part).is_reserved() for part in value.split("/")):
        raise ValueError("Attempt paths must use portable file names")
    return value


def _reserved(path):
    from ..storage_paths import contains_file_stage

    if contains_file_stage(path):
        return True
    parts = path.replace("\\", "/").split("/")
    # Also reject Windows trailing-dot/case aliases before the directory exists.
    parts = [part.rstrip(" .").casefold() for part in parts if part not in {"", "."}]
    return len(parts) >= 2 and parts[0] == '.vibeflow' and parts[1] in {
        'attempts', 'attempt-provisions', 'publications', 'file-transactions', 'tree-transactions',
        'tree-state.json', 'uploads', 'live-store', 'job-logs', 'remote-hpc'}


def require_staged_submission(manifest,case,spec,paths,publications,generated,*,all_live):
    from .publication_inputs import normalize

    if (spec['case_prefix']!=case.s3_prefix or manifest['source']['runtime']!=spec['runtime']
            or manifest['source']['selection_mode']!=('case' if all_live else 'explicit')):
        raise ValueError('Prepared workspace differs from its input request')
    if not all_live and manifest['selection']!=sorted({canonical_path(path) for path in paths or ()}):
        raise ValueError('Prepared workspace selected different paths')
    if paths is None and not (all_live or publications or generated):
        raise ValueError('Prepared workspace requires an isolated input request')
    sources=manifest.get('publications',[])
    requests=normalize(publications) if publications else []
    if len(sources)!=len(requests) or any(
        (source['publication_id'],source['require_current'])!=(request['publication_id'],request['require_current'])
        or (request['paths'] is not None and source['paths']!=request['paths'])
        for source,request in zip(sources,requests)):
        raise ValueError('Prepared workspace selected different publication inputs')
    if sorted(generated or {})!=manifest.get('generated_inputs',[]) or any(
        (len(data),hashlib.sha256(data).hexdigest())!=(manifest['files'][path]['size'],manifest['files'][path]['sha256'])
        for path,data in (generated or {}).items()):
        raise ValueError('Prepared workspace selected different generated inputs')


def assert_mutable_path(prefix, path):
    if _reserved(path):
        raise CaseWriteConflict("input_sealed", "执行版本不可直接编辑，请修改工作副本后创建新尝试", path=path)
    if not storage._use_s3():
        root = storage._fs_path(prefix, ".")
        resolved = storage._fs_path(prefix, path)
        if _reserved(resolved.relative_to(root).as_posix()):
            raise CaseWriteConflict("input_sealed", "不能通过文件别名修改执行版本", path=path)


def ordinary(path):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or
            not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode))):
        raise ValueError("Attempt inputs cannot contain links, reparse points or special files")
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        raise ValueError("Attempt inputs cannot contain hard links")
    return info


def checked_path(root, relative):
    relative = canonical_path(relative)
    current = root
    for part in relative.split("/"):
        current = current / part
        ordinary(current)
    if not current.resolve().is_relative_to(root.resolve()):
        raise ValueError("Attempt input escaped its root")
    return current


def digest_file(path, *, deadline=None):
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            if deadline is not None:
                deadline.check()
            size += len(chunk)
            digest.update(chunk)
    return {"size": size, "sha256": digest.hexdigest()}


def live_roots(root):
    """Select user files, excluding the runtime's own attempts/logs/journals."""
    from ..storage_paths import is_file_stage

    return sorted(canonical_path(path.name) for path in root.iterdir()
        if path.name.rstrip(' .').casefold() != '.vibeflow' and not is_file_stage(path.name))


def inventory(root, paths, directories=None, *, file_record=None, deadline=None):
    from ..storage_paths import contains_file_stage

    records, dirs, total = {}, set(), 0
    def visit(path):
        nonlocal total
        if deadline is not None:
            deadline.check()
        if contains_file_stage(path.relative_to(root).as_posix()):
            return
        info = ordinary(path)
        relative = canonical_path(path.relative_to(root).as_posix())
        parts = relative.split("/")
        dirs.update("/".join(parts[:length]) for length in range(1, len(parts)))
        if stat.S_ISDIR(info.st_mode):
            dirs.add(relative)
            for child in sorted(path.iterdir()):
                visit(child)
        else:
            if relative in records:
                return
            records[relative] = (file_record(path) if file_record is not None else
                {**digest_file(path,deadline=deadline), "executable":bool(info.st_mode & 0o111)})
            total += records[relative]["size"]
            if len(records) > MAX_FILES or total > MAX_BYTES:
                raise ValueError("Attempt input limit exceeded (10000 files / 1 GiB)")
    for relative in paths:
        visit(checked_path(root, relative))
    if not records and not dirs:
        raise ValueError("Attempt input selection is empty")
    if directories is not None:
        directories.extend(sorted(dirs))
    return {path: records[path] for path in sorted(records)}


def copy_records(source, target, records, directories, *, sources=None, deadline=None):
    target.mkdir(parents=True, exist_ok=False)
    for relative in directories:
        if deadline is not None:
            deadline.check()
        (target / relative).mkdir(parents=True, exist_ok=True)
    for relative, expected in records.items():
        origin = checked_path((sources or {}).get(relative, source), relative)
        output = target / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        digest, size = hashlib.sha256(), 0
        with origin.open("rb") as incoming, output.open("xb") as outgoing:
            for chunk in iter(lambda: incoming.read(1024 * 1024), b""):
                if deadline is not None:
                    deadline.check()
                size += len(chunk)
                if size > expected['size']:
                    raise CaseWriteConflict('input_stale','封存期间输入大小发生变化',path=relative)
                digest.update(chunk)
                outgoing.write(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if size != expected["size"] or digest.hexdigest() != expected["sha256"]:
            raise CaseWriteConflict("input_stale", "封存期间输入发生变化，请重新提交", path=relative)
        output.chmod(0o755 if expected["executable"] else 0o644)


class Snapshot:
    def __init__(self, prefix, attempt_id, paths, *, source=None, provenance=None, publications=None,
                 references=None, selection=None, all_live=False, allow_empty=False,
                 generated_inputs=None, generated_paths=None):
        if storage._use_s3() and source is None:
            raise ValueError("S3 inputs require a version-checked local materialization")
        if str(uuid.UUID(attempt_id)) != attempt_id:
            raise ValueError("Invalid attempt identity")
        # Retrying an already verified composite snapshot may have more roots
        # than the original 256 live selections (publication roots are extra).
        if (not isinstance(paths, list) or len(paths) < (0 if publications or all_live or allow_empty or generated_inputs else 1) or
                (source is None and not all_live and len(paths) > 256)):
            raise ValueError("input_paths must select 1-256 relative files or directories")
        self.paths = sorted({canonical_path(path) for path in paths})
        self.selection = self.paths if selection is None else selection
        self.publications = publications or []
        self.references = references if references is not None else [item.reference for item in self.publications]
        if source is None and any(path.split("/")[0].casefold() == ".vibeflow" for path in self.paths):
            raise ValueError("Runtime metadata cannot be selected as attempt inputs")
        self.generated_inputs = {}
        for name,data in (generated_inputs or {}).items():
            path=canonical_path(name)
            if _reserved(path) or not isinstance(data,bytes):
                raise ValueError('Generated inputs require safe owned paths and immutable bytes')
            if path in self.generated_inputs:
                raise ValueError('Generated input paths must be unique after normalization')
            self.generated_inputs[path]=data
        self.generated_paths = sorted(self.generated_inputs) if generated_paths is None else generated_paths
        self.root = storage._fs_path(prefix, ".")
        self.provenance_prefix = prefix
        self.source = source or self.root
        self.all_live = all_live
        self.relative = f"{PREFIX}/{attempt_id}"
        self.target = self.root / self.relative
        self.stage = self.root / PREFIX / (".preparing-"+attempt_id)
        self.provenance = provenance or {}
        self.owned_stage = self.owned_target = False
        self.mode_document = None
        self.mode_document_external = False
        self.mode_hashes_verified = True
        self.mode_input = None

    def inspect(self, *, deadline=None):
        from .attempt_modes import read_local
        if not self.mode_document_external:
            self.mode_document = read_local(self.source)
        try:
            if self.all_live:
                self.paths = live_roots(self.source)
                self.selection = list(self.paths)
            directories = []
            records = inventory(self.source, self.paths, directories,deadline=deadline) if self.paths else {}
        except FileNotFoundError as error:
            raise ValueError("Selected attempt input does not exist") from error
        return self.inspect_records(records, directories)

    def inspect_records(self, records, directories):
        """Combine a frozen inventory without allocating inputs or run copies."""
        try:
            self.records, self.directories = dict(records), list(directories)
            self.base_records, self.base_directories = dict(self.records), list(self.directories)
            self.sources = {}
            for publication in self.publications:
                for path, record in publication.files.items():
                    if path in self.records:
                        raise ValueError('Publication input collides with another selected file: '+path)
                    self.records[path] = record
                    self.sources[path] = publication.root
                    parts = path.split('/')
                    self.directories.extend('/'.join(parts[:n]) for n in range(1,len(parts)))
            for path,data in self.generated_inputs.items():
                if path in self.records:raise ValueError('Generated input collides with a selected file: '+path)
                self.records[path]={'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'executable':False}
                parts=path.split('/')
                self.directories.extend('/'.join(parts[:n]) for n in range(1,len(parts)))
            self.records = dict(sorted(self.records.items()))
            self.directories = sorted(set(self.directories))
            from .attempt_modes import selected
            from .tree_metadata_native import PATH as mode_path
            self.mode_input = selected(self.mode_document, self.records, self.directories,
                                       verify_hashes=self.mode_hashes_verified)
            if self.mode_input is not None:
                self.records[mode_path]={'size':len(self.mode_input),
                    'sha256':hashlib.sha256(self.mode_input).hexdigest(),'executable':False}
                self.records=dict(sorted(self.records.items()))
                self.directories=sorted(set(self.directories)|{'.vibeflow'})
            if len(self.records)>MAX_FILES or sum(row['size'] for row in self.records.values())>MAX_BYTES:
                raise ValueError('Combined attempt input limit exceeded (10000 files / 1 GiB)')
            names = {}
            for path in [*self.records, *self.directories]:
                if path.casefold() in names:
                    raise ValueError('Input file/directory or portable case alias collision: '+path)
                names[path.casefold()] = path
        except FileNotFoundError as error:
            raise ValueError("Selected attempt input does not exist") from error
        return sum(row["size"] for row in self.records.values()) * 2

    def describe(self):
        if hasattr(self, 'manifest'):
            return self.manifest
        from .attempt_toolchain import host
        self.manifest = {"schema":SCHEMA, "attempt_id":self.target.name,
            "inputs_path":self.relative+"/inputs", "run_path":self.relative+"/run",
            "selection":self.selection, "files":self.records, "directories":self.directories, "source":self.provenance,
            "toolchain":host(self.provenance.get("runtime"))}
        if self.references:
            self.manifest['publications'] = self.references
        if self.generated_paths:
            self.manifest['generated_inputs'] = self.generated_paths
        if storage._use_s3():
            from .attempt_objects import binding
            self.manifest['storage'] = binding(self.provenance_prefix)
        return self.manifest

    def prepare(self):
        self.describe()
        parent = self.root
        for part in PREFIX.split("/"):
            parent = parent / part
            if not parent.exists():
                parent.mkdir()
            ordinary(parent)
        if self.target.exists():
            raise CaseWriteConflict("state_conflict", "Attempt directory already exists")
        self.stage.mkdir(parents=True, exist_ok=False)
        self.owned_stage = True
        self.write_files(self.stage)
        with (self.stage / "manifest.json").open("x", encoding="utf-8") as output:
            json.dump(self.manifest, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        move_new(self.stage, self.target, root=self.root)
        self.owned_stage, self.owned_target = False, True
        return self.manifest

    def write_files(self, directory, *, deadline=None):
        from .tree_metadata_native import PATH as mode_path
        selected={path:record for path,record in self.records.items() if path not in self.generated_inputs
                  and not (path==mode_path and self.mode_input is not None)}
        copy_records(self.source, directory / "inputs", selected, self.directories, sources=self.sources,deadline=deadline)
        for path,data in self.generated_inputs.items():
            if deadline is not None:deadline.check()
            with (directory/'inputs'/path).open('xb') as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
        if self.mode_input is not None:
            with (directory/'inputs'/mode_path).open('xb') as stream:
                stream.write(self.mode_input);stream.flush();os.fsync(stream.fileno())
            if not self.mode_document_external:
                from .attempt_modes import read_local
                if read_local(self.source)!=self.mode_document:
                    raise CaseWriteConflict('input_stale','封存期间文件权限记录发生变化，请重新提交')
        if self.all_live and live_roots(self.source) != self.paths:
            raise CaseWriteConflict('input_stale', '封存期间算例文件选择发生变化，请重新提交')
        directories = []
        observed = inventory(self.source, self.paths, directories,deadline=deadline) if self.paths else {}
        if observed != self.base_records or directories != self.base_directories:
            raise CaseWriteConflict("input_stale", "封存期间输入清单发生变化，请重新提交")
        for publication in self.publications:
            observed = inventory(publication.root, sorted(publication.files),deadline=deadline)
            if observed != publication.files:
                raise CaseWriteConflict('input_stale', '封存期间所引用的产物发生变化')
        copy_records(directory / "inputs", directory / "run", self.records, self.directories,deadline=deadline)

    def cleanup(self):
        if self.owned_stage:
            remove_owned(self.stage, root=self.root)
            self.owned_stage = False
        if self.owned_target:
            remove_owned(self.target, root=self.root)
            self.owned_target = False


async def seal(db, case, attempt, user, paths, *, previous=None, input_publications=None, all_live=False,
               generated_inputs=None,resume=None,resume_view=None):
    views = []
    try:
        return await _seal(db, case, attempt, user, paths, previous=previous, input_publications=input_publications,
            all_live=all_live, generated_inputs=generated_inputs, resume=resume, resume_view=resume_view, views=views)
    finally:
        for view in views:
            await finish_before_cancel(asyncio.to_thread(view.cleanup))


async def require_safe_history(db, case):
    """Default isolation requires terminal jobs with verified write drain."""
    from sqlalchemy import select
    from ..models import Job, JobAttempt
    from .job_write_checkpoint import validate as validate_checkpoint
    from .job_execution import completion_visibility
    from .workflow_state import TERMINAL

    history = (await db.execute(select(Job,JobAttempt).outerjoin(JobAttempt,JobAttempt.job_id==Job.id)
        .where(Job.case_id==case.id,Job.status.in_(TERMINAL)))).all()
    for old_job,old_attempt in history:
        try:
            if old_attempt is None:raise ValueError('Legacy terminal Job has no drain evidence')
            # A cancelled item which never claimed anything cannot write.
            if (old_job.status==old_attempt.phase=='cancelled' and old_attempt.claim_token is None
                    and old_attempt.write_checkpoint_json=='{}'
                    and old_attempt.process_identity_json=='{}' and old_attempt.child_processes_json=='{}'):
                continue
            validate_checkpoint(case,old_job,old_attempt)
            if old_job.status=='succeeded' and completion_visibility(old_attempt) in {'pending','invalid'}:
                raise ValueError('Previous result has not settled')
        except (ValueError,TypeError,KeyError,AttributeError) as error:
            raise CaseWriteConflict('recovery_required',
                '算例历史作业缺少停止或发布完成证明，请先处理恢复状态；新作业尚未执行') from error


async def _seal(db, case, attempt, user, paths, *, previous, input_publications, all_live,
                generated_inputs, resume, resume_view, views):
    """Caller holds the case row lock; a durable plan resolves unknown commits."""
    from . import attempt_seal_journal as seals
    await seals.recover_locked(db, case)
    from .case_validation import assert_writable_locked
    from .durable_events import json_text
    from .quota import check_storage_quota
    # Sealing creates private case files before Job claim. Both submission and
    # retry must pass admission before reading/copying inputs under this lock.
    await assert_writable_locked(db, case.id)
    if all_live:
        await require_safe_history(db,case)
    previous_manifest = json.loads(previous.input_manifest_json).get("workspace") if previous else None
    source = None
    publications, references, selection = None, None, None
    if previous_manifest:
        if (previous_manifest.get("attempt_id") != previous.id or
                previous_manifest.get("source", {}).get("case_id") != case.id):
            raise CaseWriteConflict("state_conflict", "Retry manifest belongs to another attempt or case")
        from .attempt_objects import sealed_input_view
        if resume_view is not None:
            input_view = resume_view.input_view
            if input_view is None or not input_view.matches(case.s3_prefix, previous_manifest):
                raise CaseWriteConflict('state_conflict', 'Continuation sealed input view differs from its source')
        else:
            selected = asyncio.create_task(asyncio.to_thread(sealed_input_view, case.s3_prefix, previous_manifest))
            try:
                input_view = await finish_before_cancel(selected)
            except asyncio.CancelledError:
                if not selected.cancelled() and selected.exception() is None:
                    views.append(selected.result())
                raise
            views.append(input_view)
        source = input_view.root
        from .publication_inputs import validate
        await validate(db, case, previous_manifest)
        paths = sorted(path.name for path in source.iterdir())
        selection = previous_manifest['selection']
        references = previous_manifest.get('publications', [])
    elif input_publications is not None:
        from .publication_inputs import resolve
        publications = await resolve(db, case, input_publications)
    if resume is not None:
        from .solve_resume_artifacts import SCHEMA as CHECKPOINT_SCHEMA,NAME
        if (previous_manifest is None or resume.get('schema')!=CHECKPOINT_SCHEMA
                or resume['source']['attempt_id']!=previous.id or resume['source']['case_id']!=case.id
                or resume['source']['input_sha256']!=hashlib.sha256(previous.input_manifest_json.encode()).hexdigest()
                or resume['source']['output_sha256']!=hashlib.sha256(previous.output_manifest_json.encode()).hexdigest()):
            raise CaseWriteConflict('state_conflict','续算来源与原封存尝试不一致')
        control=resume['run_dir']+'/system/controlDict'
        if set(generated_inputs or {})!={control,resume['run_dir']+'/'+NAME}:
            raise ValueError('Continuation must generate only its explicit controls and checkpoint record')
        # A composite snapshot streams original inputs and captured checkpoint
        # files through the same peak admission, hash copy and durable seal plan.
        excluded={control,resume['run_dir']+'/'+NAME,*resume['files']}
        paths=sorted(name for name in previous_manifest['files'] if name not in excluded)
        paths+=sorted(name for name in previous_manifest['directories'] if
            not any(path.startswith(name+'/') for path in previous_manifest['files']))
        from .publication_inputs import PublicationSource
        if resume_view is None or not resume_view.matches(case.s3_prefix,previous_manifest,json.loads(previous.output_manifest_json)):
            raise CaseWriteConflict('state_conflict','续算结果读取范围与原输出绑定不一致')
        publications=[PublicationSource(resume_view.root,resume['files'],{})]
    snapshot_factory = Snapshot
    if storage._use_s3():
        from .attempt_objects import Snapshot as snapshot_factory
    snapshot = snapshot_factory(case.s3_prefix, attempt.id, [] if paths is None else paths, source=source,
        publications=publications, references=references, selection=selection,
        all_live=all_live, allow_empty=bool(previous_manifest),
        generated_inputs=generated_inputs,generated_paths=None if resume is not None else previous_manifest.get('generated_inputs') if previous_manifest else None,
        provenance={"case_id":case.id, "job_id":attempt.job_id, "dict_revision":previous_manifest["source"]["dict_revision"] if previous_manifest else case.dict_revision,
            "retry_of_attempt_id":previous.id if previous else None,
            "selection_mode":previous_manifest['source'].get('selection_mode','explicit') if previous_manifest else 'case' if all_live else 'explicit',
            "runtime":json.loads(attempt.input_manifest_json)["execution_spec"]["runtime"]})
    try:
        await finish_before_cancel(asyncio.to_thread(snapshot.inspect))
        from .seal_storage_budget import estimate
        budget = await finish_before_cancel(asyncio.to_thread(estimate, case, attempt, snapshot))
        from .owner_storage_budget import owner_for_case
        storage_owner = await owner_for_case(db, case)
        await check_storage_quota(db, storage_owner, budget.user_bytes)
        if storage._use_s3():
            await finish_before_cancel(asyncio.to_thread(snapshot.materialize))
            actual = await finish_before_cancel(asyncio.to_thread(estimate, case, attempt, snapshot))
            if actual.user_bytes > budget.user_bytes:
                raise ValueError('Materialized seal exceeds its preflight storage estimate')
        plan = await finish_before_cancel(asyncio.to_thread(seals.prepare, case, attempt, snapshot))
        manifest = await finish_before_cancel(asyncio.to_thread(snapshot.prepare))
        if manifest.get('publications'):
            from .publication_inputs import validate
            await validate(db, case, manifest)
        original = json.loads(attempt.input_manifest_json)
        attempt.input_manifest_json = json_text({**original, "workspace":manifest})
        await db.flush()
        await seals.witness(db, case, attempt, plan)
        return manifest
    except BaseException:
        # A plan retains prepared files until the SQL outcome is known.
        if 'plan' not in locals():
            await finish_before_cancel(asyncio.to_thread(snapshot.cleanup))
        elif storage._use_s3() and snapshot.temporary:
            await finish_before_cancel(asyncio.to_thread(snapshot.temporary.cleanup))
            snapshot.temporary = None
        raise


def verify(prefix, manifest):
    """Verify sealed inputs and require an owned existing runtime directory."""
    inputs=verify_inputs(prefix,manifest)
    return inputs,checked_path(storage._fs_path(prefix,'.'),manifest['run_path'])


def verify_inputs(prefix, manifest):
    """Verify the sealed copy against DB data; never trust edited disk manifests."""
    validate_identity(manifest)
    if storage._use_s3():
        from .attempt_objects import ensure_inputs
        ensure_inputs(prefix,manifest)
    return verify_cached_inputs(prefix,manifest)


def verify_cached_inputs(prefix, manifest):
    """Verify local bytes after the caller has verified the storage authority."""
    from .request_deadline import current

    validate_identity(manifest)
    relative = f"{PREFIX}/{manifest['attempt_id']}"
    root = storage._fs_path(prefix, ".")
    inputs = checked_path(root, manifest["inputs_path"])
    selections = sorted(path.name for path in inputs.iterdir())
    directories = []
    observed = inventory(inputs, selections, directories, deadline=current()) if selections else {}
    if observed != manifest["files"] or directories != manifest["directories"]:
        raise CaseWriteConflict("input_stale", "封存输入已损坏，拒绝执行或确认成功")
    disk = json.loads(checked_path(root,relative+'/manifest.json').read_text(encoding="utf-8"))
    if disk != manifest:
        raise CaseWriteConflict("input_stale", "执行清单与数据库不一致")
    return inputs


def validate_identity(manifest):
    """Validate DB-owned paths before any local or remote attempt access."""
    if manifest.get("schema") != SCHEMA or str(uuid.UUID(manifest["attempt_id"])) != manifest["attempt_id"]:
        raise ValueError("Invalid attempt input manifest")
    relative = f"{PREFIX}/{manifest['attempt_id']}"
    if manifest.get("inputs_path") != relative+"/inputs" or manifest.get("run_path") != relative+"/run":
        raise ValueError("Attempt manifest paths do not match ownership")


def inspect_runtime(prefix, manifest, complete):
    if complete and storage._use_s3() and not storage._fs_path(prefix,manifest['run_path']).is_dir():
        raise ValueError('Executed S3 working directory is missing; cannot invent output from sealed inputs')
    _, run = verify(prefix, manifest)
    selections = sorted(path.name for path in run.iterdir())
    directories = []
    records = inventory(run, selections, directories) if selections else {}
    if not complete and (records != manifest['files'] or directories != manifest['directories']):
        raise CaseWriteConflict('input_stale', 'Attempt working copy changed before launch')
    return run, records, directories


async def runtime_workspace(owner, *, complete=False, completion=None, finalizing_source=None):
    from ..db import SessionLocal
    from ..models import Case
    from .job_execution import _owned, attempt_event
    from .durable_events import json_text
    async with SessionLocal() as db:
        job, attempt = await _owned(db, owner)
        original = attempt.input_manifest_json
        manifest = json.loads(attempt.input_manifest_json).get("workspace")
        if manifest is None:
            return None
        if (manifest.get("attempt_id") != attempt.id or
                manifest.get("source", {}).get("case_id") != job.case_id or
                manifest.get("source", {}).get("job_id", job.id) != job.id):
            raise CaseWriteConflict("state_conflict", "Input manifest belongs to another attempt or case")
        case = await db.get(Case, job.case_id)
        prefix = case.s3_prefix
    # These directories are immutable application inputs / this attempt's own
    # working copy, never editable live-case aliases. Bulk reads must not hold
    # the case control lock and block cancellation or lease renewal.
    run, records, directories = await finish_before_cancel(asyncio.to_thread(
        inspect_runtime, prefix, manifest, complete))
    remote_output = complete and storage._use_s3()
    captured_output = None
    async with SessionLocal() as db:
        job, attempt = await _owned(db, owner)
        case = await db.get(Case, job.case_id, populate_existing=True)
        if case.s3_prefix != prefix or attempt.input_manifest_json != original:
            raise CaseWriteConflict('input_stale', 'Attempt input binding changed during verification')
        from .publication_inputs import validate, PublicationInputStale
        dependency_error = None
        try:
            await validate(db, case, manifest)
        except PublicationInputStale as error:
            if not complete:
                raise
            dependency_error = error
        if complete:
            output = {"schema":"vibeflow.attempt-outputs.v1", "run_path":manifest["run_path"],
                "files":records, "directories":directories, "input_attempt_id":attempt.id}
            from . import finalizing_stage
            previous_output=json.loads(attempt.output_manifest_json)
            for field in (finalizing_stage.KEY,finalizing_stage.RESULT):
                if field in previous_output:output[field]=previous_output[field]
            if manifest.get('publications'):
                output['publications'] = manifest['publications']
                output['dependency_error'] = 'input_stale' if dependency_error else None
            if remote_output:
                from .attempt_objects import binding
                output['object_store'] = {'state':'pending','storage':binding(prefix)}
                from .attempt_output_recovery import record_capture
                await record_capture(db,case,job,attempt,owner,output,completion)
                from .job_finalizing_source import record as record_source
                await record_source(db,case,job,attempt,output,finalizing_source)
            attempt.output_manifest_json = json_text(output)
            captured_output = attempt.output_manifest_json
            await attempt_event(db, job, attempt, "attempt.outputs_captured", {"file_count":len(records)})
        await db.commit()
        if dependency_error:
            raise dependency_error
    if remote_output:
        # Preserve the complete local output inventory before any remote write.
        # A partial upload leaves a truthful pending receipt, not a lost manifest.
        from .attempt_objects import persist_outputs
        from .output_archive_storage import capacity
        async with capacity(owner,case.id,prefix,original,captured_output) as ticket:
            if ticket.ready:object_output=ticket.receipt
            else:
                ticket.started=True
                object_output = await finish_before_cancel(asyncio.to_thread(persist_outputs,prefix,manifest,run,records,directories))
        async with SessionLocal() as db:
            job, attempt = await _owned(db,owner)
            case = await db.get(Case,job.case_id,populate_existing=True)
            if (case.s3_prefix!=prefix or attempt.input_manifest_json!=original
                    or attempt.output_manifest_json!=captured_output):
                raise CaseWriteConflict('state_conflict','Output binding changed during S3 upload')
            output=json.loads(captured_output)
            output['object_store']={'state':'complete',**object_output}
            attempt.output_manifest_json=json_text(output)
            await attempt_event(db,job,attempt,'attempt.outputs_archived',{'file_count':len(records)})
            await db.commit()
    return run
