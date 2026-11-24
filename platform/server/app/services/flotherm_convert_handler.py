"""Registered FloTHERM conversion with immutable inputs and exclusive outputs."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import uuid

from . import attempt_workspace as workspace, flotherm_convert_artifacts as artifacts
from .async_ops import finish_before_cancel
from .case_read_view import inspect, delta_source
from .durable_events import json_text
from .geometry_handler import GeometryHandler
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict

WORKER = Path(__file__).with_name('flotherm_convert_worker.py')


class FlothermConvertHandler:
    key = 'flotherm.convert'
    version = '1'
    job_type = 'convert'
    publication_kind = 'flotherm-convert'
    cancellation = dict(GeometryHandler.cancellation)
    recovery = dict(GeometryHandler.recovery)

    def publication_channel(self, descriptor):
        return 'flotherm-convert:' + hashlib.sha256(descriptor['output_dir'].casefold().encode()).hexdigest()[:40]

    async def prepare(self, db, case, user, *, parameters, budget):
        if set(parameters) != {'ir_path', 'settings_path', 'output_dir'}:
            raise ValueError('Invalid FloTHERM conversion parameters')
        ir_path = artifacts.relative(parameters['ir_path'])
        settings = artifacts.relative(parameters['settings_path']) if parameters['settings_path'] else None
        output = artifacts.relative(parameters['output_dir'])
        if Path(ir_path).suffix.lower() != '.json':
            raise ValueError('IR 必须是 .json')
        def read_inputs(root):
            source = artifacts.snapshot(root)
            ir_file = workspace.checked_path(root, ir_path)
            if ir_file.stat().st_size > 4 * 1024 * 1024:
                raise ExecutionConflict('input_too_large', 'IR 文件不能超过 4 MiB')
            json.loads(ir_file.read_bytes())
            if settings:
                workspace.checked_path(root, settings)
            try:artifacts.empty_output(root, output)
            except FileExistsError as error:
                raise ExecutionConflict('output_exists',str(error)) from error
            return source, workspace.live_roots(root)
        source, paths = await finish_before_cancel(asyncio.to_thread(inspect, case.s3_prefix, read_inputs))
        from .quota import check_job_quota
        await check_job_quota(db, user, case_id=case.id)
        identifier = str(uuid.uuid4())
        config = {'source': source, 'ir_path': ir_path, 'settings_path': settings,
            'output_dir': output, 'execution_id': identifier, 'engine_sha256': artifacts.engine_hashes()}
        spec = {'command': 'FloTHERM IR conversion', 'case_prefix': case.s3_prefix, 'runtime': 'windows',
            'argv': [sys.executable, str(WORKER), '--ir=' + ir_path, '--settings=' + (settings or ''),
                     '--output=' + output, '--execution-id=' + identifier],
            'timeout_seconds': budget['timeout_seconds'], 'max_log_bytes': budget['max_log_bytes'],
            'evidence_run_dir': None}
        self.validate_spec(spec)
        descriptor = {'handler': 'flotherm-convert.v1', 'handler_version': self.version,
            'case_id': case.id, 'source': source, 'output_dir': output, 'execution_id': identifier,
            'configuration': config, 'worker_sha256': hashlib.sha256(WORKER.read_bytes()).hexdigest()}
        return PreparedStep(self.key, self.version, self.job_type, spec, paths, None,
            self.publication_channel(descriptor), descriptor,
            generated_inputs={artifacts.CONFIG: json_text(config).encode('utf8')})

    def validate_spec(self, spec):
        try:
            args = spec['argv']
            if (spec['runtime'] != 'windows' or spec['command'] != 'FloTHERM IR conversion'
                    or spec['evidence_run_dir'] is not None or not isinstance(args, list)
                    or len(args) != 6 or args[:2] != [sys.executable, str(WORKER)]):
                raise ValueError('Invalid FloTHERM worker')
            parts = dict(value.split('=', 1) for value in args[2:])
            if set(parts) != {'--ir', '--settings', '--output', '--execution-id'}:
                raise ValueError('Invalid FloTHERM arguments')
            for name in ('--ir', '--output'):
                if artifacts.relative(parts[name]) != parts[name]:
                    raise ValueError('Invalid FloTHERM path')
            if parts['--settings'] and artifacts.relative(parts['--settings']) != parts['--settings']:
                raise ValueError('Invalid settings path')
            if (str(uuid.UUID(parts['--execution-id'])) != parts['--execution-id']
                    or spec['timeout_seconds'] <= 0 or spec['max_log_bytes'] <= 0):
                raise ValueError('Invalid FloTHERM execution identity or budget')
        except (ValueError, KeyError, TypeError) as error:
            raise ExecutionConflict('evidence_invalid', 'FloTHERM command differs from its contract') from error

    def source(self, prefix, descriptor):
        return delta_source(prefix, descriptor)

    def before_publish(self, prefix, descriptor):
        # An absent output directory is valid; explicit read-view selections
        # require existing objects, so inspect the case including its absence.
        inspect(prefix, artifacts.empty_output, descriptor['output_dir'])

    def artifacts(self, run, descriptor, output, frozen):
        return artifacts.artifacts(run, descriptor, output, frozen)
