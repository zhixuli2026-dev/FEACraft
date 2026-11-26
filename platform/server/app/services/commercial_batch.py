"""Sealed inputs and output publication for configured local batch solvers."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

from .. import storage
from . import attempt_workspace as workspace
from .case_provision import io
from .case_read_view import inspect
from .durable_events import json_text
from .flotherm_convert_artifacts import empty_output
from .geometry_handler import GeometryHandler
from .postprocess_artifacts import context
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict


REPORT = 'analysis-receipt.json'


def installed_launcher(variable, name):
    executable = Path(os.environ.get(variable, '').strip())
    if not executable.is_absolute() or not executable.is_file():
        raise FileNotFoundError(f'请将 {variable} 配置为本机厂商启动程序的绝对路径')
    executable = executable.resolve(strict=True)
    if os.name == 'nt':
        if executable.suffix.lower() not in {'.exe', '.bat', '.cmd'} or re.search(r'["%\r\n!]', str(executable)):
            raise FileNotFoundError(f'{name} 启动程序路径无效')
    elif not os.access(executable, os.X_OK):
        raise FileNotFoundError(f'{name} 启动程序不可执行')
    return str(executable)


def launcher_status(find_launcher, backend):
    try:
        find_launcher()
    except FileNotFoundError as error:
        return {'available': False, 'code': backend + '_launcher_unavailable', 'message': str(error), 'license': 'unverified'}
    return {'available': True, 'code': backend + '_launcher_configured', 'license': 'unverified'}


def case_input(value, suffix, name):
    path = workspace.canonical_path(value)
    suffixes = (suffix,) if isinstance(suffix, str) else suffix
    if path.split('/')[0] != 'C-run' or not path.casefold().endswith(suffixes):
        raise ValueError(f"请选择 C-run 下的 {name} {' 或 '.join(suffixes)} 输入文件")
    return path


def run_launcher(executable, arguments, stage, name):
    env = dict(os.environ, TEMP=str(stage), TMP=str(stage), TMPDIR=str(stage))
    argv = [executable, *arguments]
    if os.name == 'nt' and Path(executable).suffix.lower() != '.exe':
        if not all(re.fullmatch(r'[a-zA-Z0-9_,./=@-]+', arg) for arg in arguments):
            raise ValueError('批处理启动参数含命令控制字符')
        (stage / 'launch.cmd').write_text('@echo off\n"%VIBEFLOW_BATCH_LAUNCHER%" ' + ' '.join(arguments) + '\n',
                                         encoding='ascii', newline='\r\n')
        env['VIBEFLOW_BATCH_LAUNCHER'] = executable
        argv = ['cmd.exe', '/d', '/v:off', '/c', '.\\launch.cmd']
    result = subprocess.run(argv, cwd=stage, env=env, stdin=subprocess.DEVNULL, check=False)
    if result.returncode:
        raise ValueError(f'{name} 启动程序退出码 {result.returncode}，请查看作业日志和许可证状态')


def required_products(root, names, name):
    records = {}
    for product in names:
        try:
            path = workspace.checked_path(root, product)
        except FileNotFoundError as error:
            raise ValueError(f'{name} 未写出产物 {product}') from error
        if not path.is_file() or not path.stat().st_size:
            raise ValueError(f'{name} 产物为空 {product}')
        records[product] = workspace.digest_file(path)
    return records


class CommercialBatchHandler:
    version = '1'
    job_type = 'solve'
    output_stage = 'C-run'
    cancellation = dict(GeometryHandler.cancellation)
    recovery = dict(GeometryHandler.recovery)

    @property
    def config_path(self):
        return f'.vibeflow/prepared/{self.publication_kind}.json'

    def publication_channel(self, descriptor):
        folder = descriptor['configuration']['output']
        return self.publication_kind + ':' + folder.rsplit('/', 1)[-1]

    def input_paths(self, path, options):
        return [path]

    def input_records(self, root, paths):
        return {path: self.input_record(root, path) for path in paths}

    def selected_records(self, prefix, paths):
        return inspect(prefix, self.input_records, paths, paths=paths, max_file_bytes=self.max_input_bytes)

    def configured_inputs(self, config):
        return {config['input']: config['input_record'], **config.get('additional_inputs', {})}

    async def prepare(self, db, case, user, *, parameters, budget, observations=None):
        path = self.input_path(parameters['input_path'])
        options = self.options(parameters)
        executable = self.launcher()
        if executable != parameters['executable']:
            raise ValueError(f'{self.name} 启动程序与部署配置不一致')
        paths = self.input_paths(path, options)
        selected = await io(self.selected_records, case.s3_prefix, paths)
        config = {'input': path, 'input_record': selected[path], **options,
                  'launcher': executable, 'launcher_sha256': (await io(workspace.digest_file, Path(executable)))['sha256'],
                  'output': f'{self.output_stage}/{self.publication_kind}/{uuid.uuid4().hex}'}
        if len(paths) > 1:
            config['additional_inputs'] = {extra: selected[extra] for extra in paths[1:]}
        spec = {'command': self.command, 'runtime': 'windows', 'case_prefix': case.s3_prefix,
                'argv': [sys.executable, str(self.worker)], 'evidence_run_dir': None, **budget}
        self.validate_spec(spec)
        descriptor = {'handler': self.publication_kind + '.v1', 'handler_version': self.version, 'case_id': case.id,
                      'input': path, 'source': {'files': selected}, 'configuration': config,
                      'worker_sha256': (await io(workspace.digest_file, self.worker))['sha256']}
        return PreparedStep(self.key, self.version, self.job_type, spec, paths, None,
                            self.publication_channel(descriptor), descriptor,
                            generated_inputs={self.config_path: json_text(config).encode('utf8')})

    def validate_spec(self, spec):
        if (spec.get('runtime') != 'windows' or spec.get('command') != self.command
                or spec.get('argv') != [sys.executable, str(self.worker)] or spec.get('evidence_run_dir') is not None
                or not 0 < spec.get('timeout_seconds', 0) <= 86400
                or not 0 < spec.get('max_log_bytes', 0) <= 16 * 1024 * 1024):
            raise ExecutionConflict('evidence_invalid', f'{self.name} 命令与登记的作业契约不一致')

    def source(self, prefix, descriptor):
        config = descriptor['configuration']
        paths = self.input_paths(descriptor['input'], self.options(config))
        return {'files': self.selected_records(prefix, paths)}

    def before_publish(self, prefix, descriptor):
        folder = descriptor['configuration']['output']
        if storage._use_s3():
            if storage.exists(prefix, folder) or storage.list_tree(prefix, folder):
                raise ValueError(f'{self.name} 输出目录已存在')
        else:
            inspect(prefix, empty_output, folder)

    def read_config(self, root):
        return json.loads(workspace.checked_path(root, self.config_path).read_text(encoding='utf8'))

    def validate_launch(self, root, config):
        executable = self.launcher()
        paths = self.input_paths(self.input_path(config['input']), self.options(config))
        if (self.input_records(root, paths) != self.configured_inputs(config) or executable != config['launcher']
                or workspace.digest_file(Path(executable))['sha256'] != config['launcher_sha256']):
            raise ValueError(f'{self.name} 封存输入或启动程序不一致')
        return executable

    def receipt(self, root, config, output):
        identity = context(root)
        receipt = {'schema': 'vibeflow.' + self.publication_kind + '-result.v1', 'case_id': identity['source']['case_id'],
                   'job_id': identity['source']['job_id'], 'attempt_id': identity['attempt_id'],
                   'input': config['input'], 'input_sha256': config['input_record']['sha256'],
                   'launcher_sha256': config['launcher_sha256'], **self.options(config), 'products': self.products(output)}
        if config.get('additional_inputs'):
            receipt['additional_inputs'] = config['additional_inputs']
        return receipt

    def stage_products(self, root, config, stage):
        outcome = self.receipt(root, config, stage)
        output = root / workspace.canonical_path(config['output'])
        output.mkdir(parents=True, exist_ok=False)
        for product in outcome['products']:
            with workspace.checked_path(stage, product).open('rb') as source, (output / product).open('xb') as target:
                shutil.copyfileobj(source, target, length=1024 * 1024)
        with (output / REPORT).open('x', encoding='utf8') as stream:
            json.dump(outcome, stream, ensure_ascii=False, sort_keys=True)

    def artifacts(self, run, descriptor, output, frozen_files):
        config = self.read_config(run)
        paths = self.input_paths(descriptor['input'], self.options(config))
        selected = self.configured_inputs(config)
        if (config != descriptor['configuration'] or workspace.digest_file(self.worker)['sha256'] != descriptor['worker_sha256']
                or self.input_records(run, paths) != selected
                or any(frozen_files[path][key] != value for path, record in selected.items() for key, value in record.items())):
            raise ValueError(f'{self.name} 封存输入或执行程序已改变')
        folder = config['output']
        reported = json.loads(workspace.checked_path(run, f'{folder}/{REPORT}').read_text(encoding='utf8'))
        if reported != self.receipt(run, config, run / folder):
            raise ValueError(f'{self.name} 输出和执行回执不一致')
        records = {}
        for name in (*reported['products'], REPORT):
            p = f'{folder}/{name}'
            captured = output['files'][p]
            if any(captured[k] != v for k, v in workspace.digest_file(workspace.checked_path(run, p)).items()):
                raise ValueError(f'{self.name} 输出在捕获后改变')
            records[p] = captured
        descriptor.update(output_files=records, deleted_files={}, receipt=reported)
        return records
