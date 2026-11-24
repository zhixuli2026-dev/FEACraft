"""CadQuery's registered professional execution contract."""
import asyncio
import hashlib
from pathlib import Path
import shutil
import sys
import uuid

from sqlalchemy import select

from .. import storage
from ..models import ArtifactPublication
from . import geometry_publication
from .async_ops import finish_before_cancel
from .geo_script import DEFAULT_TOLERANCE, __file__ as geometry_worker, preview_tolerance
from .runtime_config import get_runtime_setting
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict


INPUT_PATHS = ('0-caseDict/caseDict', 'A-geo')


class GeometryHandler:
    version = '1'
    job_type = 'geometry'
    publication_kind = 'geometry'
    cancellation = {'request':'durable_attempt', 'local_action':'owned_process_cancel',
        'complete_process_tree':False, 'publication_after_accepted_cancel':False}
    recovery = {'automatic_replay':False, 'resume':False, 'rerun':'explicit_new_run_with_current_inputs',
        'finalization':'verified_preparation_or_committed_bundle', 'missing_proof':'requires_attention'}

    def __init__(self, mode):
        self.mode, self.key = mode, 'geometry.'+mode

    def command(self, prefix, parameters, budget):
        tolerance = DEFAULT_TOLERANCE
        if parameters:
            if (self.mode != 'preview' or set(parameters) != {'tolerance'}
                    or type(parameters['tolerance']) not in (int, float)):
                raise ExecutionConflict('invalid_request', 'Geometry preview accepts only a numeric tolerance')
            try:
                tolerance = preview_tolerance(parameters['tolerance'])
            except ValueError as error:
                raise ExecutionConflict('invalid_request', str(error)) from error
        python = get_runtime_setting('python', sys.executable)
        if not shutil.which(python):
            raise ExecutionConflict('runtime_unavailable', 'Configured geometry Python is unavailable')
        timeout, log_bytes = budget['timeout_seconds'], budget['max_log_bytes']
        if not 0 < timeout <= 86400 or not 1024 <= log_bytes <= 64*1024*1024:
            raise ExecutionConflict('resource_limit', 'Geometry resource budget is invalid')
        execution_id = str(uuid.uuid4())
        argv = [python, geometry_worker, '--case', '.', '--mode', self.mode, '--execution-id', execution_id]
        if tolerance != DEFAULT_TOLERANCE:
            argv.extend(['--tolerance', str(tolerance)])
        spec = {'command':'CadQuery '+self.mode, 'case_prefix':prefix, 'runtime':'windows',
            'argv':argv,
            'timeout_seconds':timeout, 'max_log_bytes':log_bytes, 'evidence_run_dir':None}
        return spec, execution_id, tolerance

    async def generated_paths(self, db, case):
        from .artifact_publication import managed_paths

        previous = await db.scalar(select(ArtifactPublication).where(ArtifactPublication.case_id==case.id,
            ArtifactPublication.kind=='geometry', ArtifactPublication.status=='committed')
            .order_by(ArtifactPublication.generation.desc()).limit(1))
        return sorted(await managed_paths(db, case.s3_prefix, previous)) if previous else []

    def prepared(self, command, source, generated):
        spec, execution_id, tolerance = command
        return PreparedStep(self.key, self.version, self.job_type, spec,
            list(INPUT_PATHS), None, self.publication_kind,
            {'handler':'geometry.v1','handler_version':self.version,'mode':self.mode,
             'source':source,'generated_paths':generated,'execution_id':execution_id,'tolerance':tolerance,
             'worker_sha256':hashlib.sha256(Path(geometry_worker).read_bytes()).hexdigest()})

    async def prepare(self, db, case, user, *, parameters, budget, observations=None, resolved_inputs=None):
        from .quota import check_job_quota

        command = self.command(case.s3_prefix, parameters, budget)
        if not await finish_before_cancel(asyncio.to_thread(storage.exists, case.s3_prefix, 'A-geo/geometry.py')):
            raise ValueError('A-geo/geometry.py 不存在')
        await check_job_quota(db, user, case_id=case.id)
        generated = await self.generated_paths(db if observations is None else observations, case)
        source = await finish_before_cancel(asyncio.to_thread(geometry_publication.source_snapshot,
            case.s3_prefix, generated))
        return self.prepared(command, source, generated)

    def validate_spec(self, spec):
        argv = spec.get('argv')
        if (spec.get('command') != 'CadQuery '+self.mode or spec.get('runtime') != 'windows' or
                spec.get('evidence_run_dir') is not None or not isinstance(argv,list) or len(argv) not in (8, 10) or
                not isinstance(argv[0],str) or not argv[0] or argv[1:7] !=
                [geometry_worker,'--case','.','--mode',self.mode,'--execution-id']):
            raise ExecutionConflict('evidence_invalid','Geometry command differs from its registered worker')
        try:
            if str(uuid.UUID(argv[7])) != argv[7]:
                raise ValueError('Noncanonical execution ID')
        except (ValueError, TypeError, AttributeError) as error:
            raise ExecutionConflict('evidence_invalid','Geometry execution identity is invalid') from error
        if len(argv) == 10:
            try:
                if (self.mode != 'preview' or argv[8] != '--tolerance'
                        or argv[9] != str(preview_tolerance(argv[9]))):
                    raise ValueError('Invalid preview tolerance override')
            except ValueError as error:
                raise ExecutionConflict('evidence_invalid','Geometry command has an invalid tolerance') from error

    def artifacts(self, run, descriptor, output, frozen_files):
        return geometry_publication.artifacts_for(run, descriptor, output, frozen_files)

    def source(self, prefix, descriptor):
        return geometry_publication.source_snapshot(prefix, descriptor.get('generated_paths', []))
