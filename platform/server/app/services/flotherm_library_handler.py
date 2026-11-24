"""Registered FloTHERM helper builds; source copying is delayed until publication."""
import asyncio
import hashlib
from pathlib import Path, PurePosixPath
import re
import sys
import uuid

from . import attempt_workspace as workspace, flotherm_library_artifacts as artifacts
from .async_ops import finish_before_cancel
from .case_read_view import inspect, delta_source
from .compose_artifacts import existing, record
from .durable_events import json_text
from .geometry_handler import GeometryHandler
from .step_handlers import PreparedStep
from .openfoam_runtime import configured
from .workflow_state import ExecutionConflict

WORKER = Path(__file__).with_name('flotherm_library_worker.py')
NATIVE = Path(__file__).with_name('flotherm_library_native.py')
COMMON = Path(__file__).with_name('mpi_native.py')


class FlothermLibraryHandler:
    key = 'flotherm.build-library'
    version = '1'
    job_type = 'convert'
    publication_kind = 'flotherm-library'
    cancellation = dict(GeometryHandler.cancellation)
    recovery = dict(GeometryHandler.recovery)

    def publication_channel(self, descriptor):
        return 'flotherm-library:'+hashlib.sha256(descriptor['destination_dir'].casefold().encode()).hexdigest()[:40]

    async def prepare(self, db, case, user, *, parameters, budget):
        if set(parameters) != {'source_dir', 'destination_dir'}:
            raise ValueError('Invalid FloTHERM library parameters')
        source_dir = artifacts.relative(parameters['source_dir'])
        destination_dir = artifacts.relative(parameters['destination_dir'])
        if source_dir.casefold() == destination_dir.casefold():
            destination_dir = source_dir
        distro, bashrc = configured()
        def read_inputs(root):
            source = artifacts.snapshot(root)
            selected = {p: r for p, r in source['files'].items() if p.startswith(source_dir+'/')}
            if (source_dir+'/Make/files' not in selected or source_dir+'/Make/options' not in selected
                    or not any(p.endswith('.C') and PurePosixPath(p).parent.as_posix() == source_dir for p in selected)):
                raise ValueError('辅助库需要 Make/files、Make/options 和至少一个 .C 源文件')
            if source_dir != destination_dir:
                a, b = source_dir.casefold(), destination_dir.casefold()
                if a.startswith(b+'/') or b.startswith(a+'/'):
                    raise ValueError('辅助库源目录和目标目录不能互相包含')
                if (root/destination_dir).exists():
                    raise ExecutionConflict('output_exists','辅助库目标已存在，请选择新目录；已有内容不会被覆盖')
                if len(selected) > 20 or any(r['size'] > 1024*1024 for r in selected.values()):
                    raise ExecutionConflict('input_too_large', '辅助库最多 20 个源文件，单文件不能超过 1 MiB')
            return source, workspace.live_roots(root)
        source, paths = await finish_before_cancel(asyncio.to_thread(inspect, case.s3_prefix, read_inputs))
        from .quota import check_job_quota
        await check_job_quota(db, user, case_id=case.id)
        identifier = str(uuid.uuid4())
        config = {'source': source, 'source_dir': source_dir, 'destination_dir': destination_dir,
            'execution_id': identifier, 'distro': distro, 'bashrc': bashrc, 'max_log_bytes': budget['max_log_bytes'],
            'native_sha256': hashlib.sha256(NATIVE.read_bytes()).hexdigest(),
            'common_native_sha256': hashlib.sha256(COMMON.read_bytes()).hexdigest()}
        spec = {'command': 'FloTHERM helper-library compilation', 'case_prefix': case.s3_prefix, 'runtime': 'windows',
            'argv': [sys.executable, str(WORKER), '--source-dir='+source_dir, '--destination-dir='+destination_dir,
                '--distro='+distro, '--bashrc='+bashrc, '--execution-id='+identifier,
                '--timeout='+str(budget['timeout_seconds']), '--max-log-bytes='+str(budget['max_log_bytes'])],
            'timeout_seconds': budget['timeout_seconds'], 'max_log_bytes': budget['max_log_bytes'], 'evidence_run_dir': None}
        self.validate_spec(spec)
        descriptor = {'handler': 'flotherm-library.v1', 'handler_version': self.version, 'case_id': case.id,
            'source': source, 'source_dir': source_dir, 'destination_dir': destination_dir,
            'configuration': config, 'execution_id': identifier, 'distro': distro,
            'native_sha256': config['native_sha256'], 'common_native_sha256': config['common_native_sha256'],
            'worker_sha256': hashlib.sha256(WORKER.read_bytes()).hexdigest()}
        return PreparedStep(self.key, self.version, self.job_type, spec, paths, None,
            self.publication_channel(descriptor), descriptor,
            generated_inputs={artifacts.CONFIG: json_text(config).encode('utf8')})

    def validate_spec(self, spec):
        try:
            args = spec['argv']
            if (spec.get('runtime') != 'windows' or spec.get('evidence_run_dir') is not None
                    or spec.get('command') != 'FloTHERM helper-library compilation'
                    or not isinstance(args, list) or len(args) != 9 or args[:2] != [sys.executable, str(WORKER)]):
                raise ValueError('Invalid FloTHERM library worker')
            parts = dict(value.split('=', 1) for value in args[2:])
            if (set(parts) != {'--source-dir', '--destination-dir', '--distro', '--bashrc', '--execution-id', '--timeout', '--max-log-bytes'}
                    or not re.fullmatch('[a-z0-9-]+', parts['--distro'])
                    or not re.fullmatch('/usr/lib/openfoam/[A-Za-z0-9_.-]+/etc/bashrc', parts['--bashrc'])
                    or str(uuid.UUID(parts['--execution-id'])) != parts['--execution-id']
                    or int(parts['--timeout']) != spec['timeout_seconds']
                    or int(parts['--max-log-bytes']) != spec['max_log_bytes']
                    or spec['timeout_seconds'] <= 0 or spec['max_log_bytes'] <= 0):
                raise ValueError('Invalid FloTHERM compilation contract')
            for name in ('--source-dir', '--destination-dir'):
                if artifacts.relative(parts[name]) != parts[name]:
                    raise ValueError('Invalid FloTHERM compilation path')
        except (ValueError, KeyError, TypeError) as error:
            raise ExecutionConflict('evidence_invalid', 'FloTHERM compilation differs from registered contract') from error

    def source(self, prefix, descriptor):
        return delta_source(prefix, descriptor)

    def before_publish(self, prefix, descriptor):
        inspect(prefix, self._before_publish, descriptor)

    def _before_publish(self, root, descriptor):
        if descriptor['source_dir'] != descriptor['destination_dir'] and (root/descriptor['destination_dir']).exists():
            raise ValueError('FloTHERM copy destination was created by another writer')
        before = artifacts.file_records(descriptor['source'])
        for path in set(descriptor['output_files'])|set(descriptor['deleted_files']):
            data = existing(root, path)
            if (record(data) if data is not None else None) != before.get(path):
                raise ValueError('FloTHERM library destination changed: '+path)

    def artifacts(self, run, descriptor, output, frozen):
        return artifacts.artifacts(run, descriptor, output, frozen)
