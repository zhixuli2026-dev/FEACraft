"""Prepare and verify supervised FloTHERM Pack exports."""
import asyncio
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import uuid
import zipfile

from . import attempt_workspace as workspace, flotherm_export
from .async_ops import finish_before_cancel
from .case_file_delta import snapshot
from .case_read_view import delta_source, inspect
from .durable_events import json_text
from .flotherm_convert_artifacts import empty_output, relative
from .geometry_handler import GeometryHandler
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict


CONFIG = '.vibeflow/prepared/flotherm-pack.json'
WORKER = Path(__file__).with_name('flotherm_pack_worker.py')
LOGS = ('flotherm.stdout.log', 'flotherm.stderr.log')
RESULT = 'export-manifest.json'
PRODUCTS = (*flotherm_export.EXPORTS, *LOGS, RESULT)


def configured_launcher():
    if os.name != 'nt':
        raise ExecutionConflict('flotherm_export_unavailable', '工作台 Pack 导出需要 Windows FloTHERM 执行环境')
    try:
        return flotherm_export.find_launcher(None)
    except (OSError, ValueError) as error:
        raise ExecutionConflict('flotherm_export_unavailable', '请在 Windows 服务中通过 FLOTHERM_BAT 配置已安装的 FloTHERM') from error


def digest(path):
    return workspace.digest_file(path)['sha256']


class FlothermPackHandler:
    key = 'flotherm.export-pack'
    version = '1'
    job_type = 'convert'
    publication_kind = 'flotherm-pack'
    cancellation = dict(GeometryHandler.cancellation)
    recovery = dict(GeometryHandler.recovery)

    def publication_channel(self, descriptor):
        channel = hashlib.sha256(descriptor['output_dir'].casefold().encode()).hexdigest()[:40]
        return f'{self.publication_kind}:{channel}'

    async def prepare(self, db, case, user, *, parameters, budget):
        if set(parameters) != {'pack_path', 'output_dir'}:
            raise ValueError('Pack 导出参数无效')
        pack, output = (relative(parameters[name]) for name in ('pack_path', 'output_dir'))
        if PurePosixPath(pack).suffix.lower() != '.pack' or pack.casefold().startswith(output.casefold() + '/'):
            raise ValueError('请选择 .pack 文件及独立的输出目录')
        launcher = configured_launcher()

        def read_inputs(root):
            source = snapshot(root)
            selected = workspace.checked_path(root, pack)
            if not zipfile.is_zipfile(selected):
                raise ValueError('FloTHERM 导出需要可读取的 .pack 归档')
            empty_output(root, output)
            return source, workspace.live_roots(root)

        source, paths = await finish_before_cancel(asyncio.to_thread(inspect, case.s3_prefix, read_inputs))
        from .quota import check_job_quota
        await check_job_quota(db, user, case_id=case.id)
        config = {'source': source, 'pack_path': pack, 'output_dir': output, 'execution_id': str(uuid.uuid4()),
                  'launcher': str(launcher), 'launcher_sha256': digest(launcher),
                  'engine_sha256': digest(Path(flotherm_export.__file__))}
        spec = {'command': 'FloTHERM Pack export', 'case_prefix': case.s3_prefix, 'runtime': 'windows',
                'argv': [sys.executable, str(WORKER)], 'timeout_seconds': budget['timeout_seconds'],
                'max_log_bytes': budget['max_log_bytes'], 'evidence_run_dir': None}
        self.validate_spec(spec)
        descriptor = {'handler': 'flotherm-pack.v1', 'handler_version': self.version, 'case_id': case.id,
                      'source': source, 'output_dir': output, 'configuration': config,
                      'worker_sha256': digest(WORKER)}
        return PreparedStep(self.key, self.version, self.job_type, spec, paths, None,
                            self.publication_channel(descriptor), descriptor,
                            generated_inputs={CONFIG: json_text(config).encode('utf8')})

    def validate_spec(self, spec):
        if (spec.get('runtime') != 'windows' or spec.get('command') != 'FloTHERM Pack export'
                or spec.get('argv') != [sys.executable, str(WORKER)] or spec.get('evidence_run_dir') is not None
                or spec.get('timeout_seconds', 0) <= 0 or spec.get('max_log_bytes', 0) <= 0):
            raise ExecutionConflict('evidence_invalid', 'Pack 导出执行参数与作业登记不一致')

    def source(self, prefix, descriptor):
        return delta_source(prefix, descriptor)

    def before_publish(self, prefix, descriptor):
        inspect(prefix, empty_output, descriptor['output_dir'])

    def artifacts(self, run, descriptor, output, frozen):
        from .postprocess_artifacts import context
        identity = context(run)
        config = json.loads(workspace.checked_path(run, CONFIG).read_text(encoding='utf8'))
        source = descriptor['source']['files']
        captured = {p: r for p, r in output['files'].items() if p.split('/')[0].casefold() != '.vibeflow'}
        if (config != descriptor['configuration'] or digest(WORKER) != descriptor['worker_sha256']
                or digest(Path(flotherm_export.__file__)) != config['engine_sha256']
                or {p: r for p, r in frozen.items() if p != CONFIG} != source
                or snapshot(run)['files'] != captured or any(captured.get(p) != r for p, r in source.items())):
            raise ValueError('Pack 导出封存输入或捕获结果不一致')
        target = descriptor['output_dir']
        records = {p: r for p, r in captured.items() if p not in source}
        if set(records) != {f'{target}/{name}' for name in PRODUCTS}:
            raise ValueError('Pack 导出必须只写入已登记的输出文件')
        folder = run / target
        receipt = json.loads(workspace.checked_path(run, f'{target}/{RESULT}').read_text(encoding='utf8'))
        expected = {'schema': 'vibeflow.flotherm-pack.v1', 'case_id': descriptor['case_id'],
                    'job_id': identity['source']['job_id'], 'attempt_id': identity['attempt_id'],
                    'execution_id': config['execution_id'], 'pack_path': config['pack_path'],
                    'pack_sha256': source[config['pack_path']]['sha256'],
                    'exported': list(flotherm_export.EXPORTS)}
        return_code = receipt.pop('return_code')
        if receipt != expected or type(return_code) is not int:
            raise ValueError('Pack 导出回执与当前作业不一致')
        flotherm_export.validate_products(folder, return_code)
        descriptor.update(output_files=records, deleted_files={}, receipt={**receipt, 'return_code': return_code})
        return records
