"""Versioned result publication for user-supplied, explicitly typed case commands.

A type label is not a verified professional workflow step. These publications
retain legacy command behavior without impersonating geometry/mesh/solve heads.
"""
import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import PurePosixPath

from .. import storage
from . import attempt_workspace as workspace
from .async_ops import finish_before_cancel
from .case_file_delta import snapshot, current_source, file_records, restore_event_prefix
from .compose_artifacts import existing, record
from .geometry_handler import GeometryHandler
from .s3_case_files import SourceObservation
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict

TYPES = {'mesh', 'solve', 'pvpython', 'geometry', 'convert'}


def uses_default_inputs(job_type, spec, input_paths, input_publications):
    return job_type in TYPES and spec.get('argv') is None and input_paths is None and input_publications is None


@dataclass(frozen=True)
class PreparedRemoteCommand:
    step: PreparedStep
    observation: SourceObservation

    async def verify(self, case, job_type, spec):
        from .case_provision import io
        if (self.step.job_type, self.step.spec, self.step.descriptor['case_id']) != (job_type, spec, case.id):
            raise ExecutionConflict('input_stale', 'Command submission differs from its observed source')
        await io(self.observation.verify, case.s3_prefix)

    async def verify_seal(self, case, manifest):
        from .case_provision import io
        if manifest['files'] != self.observation.source['files']:
            raise ExecutionConflict('input_stale', 'Sealed command inputs differ from the observed source')
        await io(self.observation.verify, case.s3_prefix)


async def prepare_remote_submission(case_id, job_type, spec):
    from . import case_copy_io, s3_case_files
    from .case_provision import io
    from .case_read_capacity import file_read_deadline
    from .request_deadline import STAGING_CLEANUP_SECONDS

    handler = CaseCommandHandler(job_type)
    handler.validate_spec(spec)
    async with file_read_deadline('command_source', '校验命令输入', seconds=STAGING_CLEANUP_SECONDS):
        with case_copy_io.admit(code='command_source_busy', label='校验命令输入'):
            observation = await io(s3_case_files.observe_source, spec['case_prefix'])
    source = observation.source
    paths = sorted({path.split('/')[0] for path in source['files']})
    return PreparedRemoteCommand(handler.prepared(case_id, spec, source, paths), observation)


class CaseCommandHandler:
    version = '1'
    publication_kind = 'case-command'
    cancellation = dict(GeometryHandler.cancellation)
    recovery = dict(GeometryHandler.recovery)

    def __init__(self, job_type):
        if job_type not in TYPES:
            raise ValueError('Unsupported explicitly typed command')
        self.job_type = job_type
        self.key = 'command.'+job_type

    def publication_channel(self, descriptor):
        value = json.dumps([self.job_type, descriptor['spec']['runtime'], descriptor['spec']['command'], descriptor['spec']['evidence_run_dir']],
                           ensure_ascii=False, separators=(',', ':'))
        return 'case-command:'+hashlib.sha256(value.encode()).hexdigest()[:40]

    async def prepare(self, db, case, user, *, parameters, budget):
        if set(parameters) != {'spec'}:
            raise ValueError('Invalid case command parameters')
        spec = dict(parameters['spec'])
        self.validate_spec(spec)
        if spec['case_prefix'] != case.s3_prefix:
            raise ValueError('Case command belongs to another case')
        if storage._use_s3():
            from .s3_case_files import snapshot as remote_snapshot, require_conditions
            await finish_before_cancel(asyncio.to_thread(require_conditions,case.s3_prefix))
            source=await finish_before_cancel(asyncio.to_thread(remote_snapshot,case.s3_prefix))
            paths=sorted({p.split('/')[0] for p in source['files']})
        else:
            root = storage._fs_path(case.s3_prefix, '.')
            source = await finish_before_cancel(asyncio.to_thread(snapshot, root))
            paths=workspace.live_roots(root)
        return self.prepared(case.id, spec, source, paths)

    def prepared(self, case_id, spec, source, paths):
        descriptor = {'handler': 'case-command.v1', 'handler_version': self.version,
            'case_id': case_id, 'job_type': self.job_type, 'spec': spec, 'source': source}
        return PreparedStep(self.key, self.version, self.job_type, spec, paths, None,
            self.publication_channel(descriptor), descriptor)

    def validate_spec(self, spec):
        from .command_safety import command_block_reason
        from .case_evidence import safe_run_dir
        try:
            if (set(spec) != {'command', 'case_prefix', 'runtime', 'argv', 'timeout_seconds',
                             'max_log_bytes', 'evidence_run_dir'} or spec['argv'] is not None
                    or not isinstance(spec['command'], str) or not 1 <= len(spec['command']) <= 4000
                    or command_block_reason(spec['command'])
                    or spec['runtime'] not in {'windows', 'wsl', 'wsl-sandbox'}
                    or not math.isfinite(spec['timeout_seconds']) or not 0 < spec['timeout_seconds'] <= 86400
                    or not 1024 <= spec['max_log_bytes'] <= 64*1024*1024):
                raise ValueError('Invalid case command execution specification')
            if spec['evidence_run_dir'] is not None and (self.job_type != 'solve'
                    or safe_run_dir(spec['evidence_run_dir']) != spec['evidence_run_dir']):
                raise ValueError('Invalid legacy solve evidence directory')
        except (ValueError, KeyError, TypeError) as error:
            raise ExecutionConflict('evidence_invalid', 'Case command differs from its execution contract') from error

    def source(self, prefix, descriptor):
        if storage._use_s3():
            from .s3_case_files import current_source as remote_source
            observed=remote_source(prefix,descriptor)
        else:observed=current_source(storage._fs_path(prefix, '.'), descriptor)
        return restore_event_prefix(prefix,descriptor,observed)

    def before_publish(self, prefix, descriptor):
        if storage._use_s3():
            from .s3_case_files import read_version
            read=lambda path:read_version(prefix,path)[0]
        else:
            root = storage._fs_path(prefix, '.')
            read=lambda path:existing(root,path)
        before = file_records(descriptor['source'])
        for path in set(descriptor['output_files'])|set(descriptor['deleted_files']):
            data = read(path)
            actual = record(data) if data is not None else None
            # Legacy solve logs already live at their Job-specific path. Keep
            # that exact transcript and publish only matching evidence beside it.
            if path in descriptor.get('evidence_files', []):
                expected = {k: descriptor['output_files'][path][k] for k in ('size', 'sha256')}
                if actual is not None and actual != expected:
                    raise ValueError('Case command evidence destination changed: '+path)
            elif actual != before.get(path):
                raise ValueError('Case command destination changed: '+path)

    def artifacts(self, run, descriptor, output, frozen):
        from .postprocess_artifacts import context
        identity = context(run)
        source = descriptor['source']
        if identity['source']['case_id'] != descriptor['case_id'] or frozen != source['files']:
            raise ValueError('Case command sealed source differs')
        observed = snapshot(run)['files']
        if observed != {p: r for p, r in output['files'].items() if p.split('/')[0].casefold() != '.vibeflow'}:
            raise ValueError('Case command files changed after capture')
        from .case_events import CASE_EVENTS_PATH
        if observed.get(CASE_EVENTS_PATH)!=source['files'].get(CASE_EVENTS_PATH):
            raise ValueError('Case command changed the preserved platform timeline')
        # Refuse publication when a command changes existing initial fields or
        # originals. Newly generated initial fields are outputs.
        for path, expected in source['files'].items():
            parts = PurePosixPath(path).parts
            if (path == '0-caseDict/caseDict' or
                    (parts[0] == 'C-run' and any(p in {'0', '0.orig'} for p in parts[2:-1]))):
                if observed.get(path) != expected:
                    raise ValueError('Case command changed a preserved engineering input: '+path)
        records = {p: r for p, r in observed.items() if source['files'].get(p) != r}
        deleted = {p: r for p, r in source['files'].items() if p not in observed}
        evidence_files = []
        run_dir = descriptor['spec']['evidence_run_dir']
        if run_dir:
            from .case_evidence import capture_solve_inputs, complete_solve_evidence
            from .mpi_artifacts import output_times
            from .solve_publication import validate_fields
            job_id = identity['source']['job_id']
            log_name = f'.vibeflow/jobs/job-{job_id}.log'
            evidence_name = f'.vibeflow/jobs/job-{job_id}-evidence.json'
            expected = complete_solve_evidence(run,
                capture_solve_inputs(run.parent/'inputs', run_dir, job_id), workspace.checked_path(run, log_name))
            actual = json.loads(workspace.checked_path(run, evidence_name).read_text(encoding='utf8'))
            if actual != expected:
                raise ValueError('Case command solve evidence differs from frozen inputs and actual output')
            validate_fields(run, run_dir, actual, times=output_times(run, run_dir))
            evidence_files = [log_name, evidence_name]
            for name in evidence_files:
                value = {**workspace.digest_file(workspace.checked_path(run, name)), 'executable': False}
                if output['files'].get(name) != value:
                    raise ValueError('Case command solve evidence changed after capture')
                records[name] = value
        descriptor['output_files'] = records
        descriptor['deleted_files'] = deleted
        descriptor['evidence_files'] = evidence_files
        descriptor['result'] = {'schema': 'vibeflow.case-command-result.v1', 'case_id': descriptor['case_id'],
            'job_id': identity['source']['job_id'], 'attempt_id': identity['attempt_id'],
            'job_type': self.job_type, 'professional_workflow_proof': False,
            'legacy_solve_evidence': bool(run_dir)}
        return records
