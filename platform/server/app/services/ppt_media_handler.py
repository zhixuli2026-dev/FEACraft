"""Registered narrated video export from a frozen PPTX source."""
import asyncio
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import uuid
from zipfile import BadZipFile

from . import attempt_workspace as workspace
from . import ppt_media_native as native
from .async_ops import finish_before_cancel
from .durable_events import json_text
from .openfoam_runtime import distribution, NATIVE_LINUX
from .owned_process import wsl_path
from .pdf_number_handler import require_absent
from .ppt_handler import PptHandler, source
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict


WORKER = Path(native.__file__).resolve()
CONFIG = native.CONFIG.as_posix()


def video_request(parameters):
    if set(parameters) != {'input', 'output', 'language'} or parameters['language'] not in native.VOICES:
        raise ValueError('PPT 视频需要输入、输出和支持的旁白语言')
    deck, video = (workspace.canonical_path(parameters[key]) for key in ('input', 'output'))
    if (not deck.lower().endswith('.pptx') or not video.lower().endswith('.mp4')
            or deck.split('/')[0].casefold() == '.vibeflow'):
        raise ValueError('请选择算例内的 PPTX 输入和 MP4 输出')
    return {'input': deck, 'output': video, 'language': parameters['language']}


def argv(distro, identifier):
    if distro == NATIVE_LINUX:
        return [sys.executable, '-I', '-S', '-u', str(WORKER), '--execution-id', identifier]
    return ['wsl.exe', '-d', distro, '--user', 'nobody', '--exec', 'python3', '-I', '-S', '-u',
            wsl_path(WORKER), '--execution-id', identifier]


class PptMediaHandler(PptHandler):
    key = 'ppt.video'

    async def prepare(self, db, case, user, *, parameters, budget):
        document = video_request(parameters)
        distro = distribution()
        if distro != NATIVE_LINUX and not shutil.which('wsl.exe'):
            raise ExecutionConflict('runtime_unavailable', 'PPT 视频需要配置 Linux 或 WSL 工具链')
        workspace.assert_mutable_path(case.s3_prefix, document['output'])
        resources = [document['input']]
        from .case_read_view import inspect

        def read_inputs(root):
            require_absent(root, document['output'])
            try:
                native.speaker_notes(workspace.checked_path(root, document['input']))
            except (BadZipFile, KeyError, native.Xml.ParseError) as error:
                raise ValueError('无法打开 PPTX 或读取演讲者备注') from error
            return source(root, resources)

        try:
            observed = await finish_before_cancel(asyncio.to_thread(inspect, case.s3_prefix, read_inputs))
        except FileExistsError as error:
            raise ExecutionConflict('output_exists', 'PPT 视频输出已存在') from error
        from .quota import check_job_quota
        await check_job_quota(db, user, case_id=case.id)
        payload = json_text(document).encode('utf-8')
        identifier = str(uuid.uuid4())
        spec = {'command': 'PPTX narrated video', 'case_prefix': case.s3_prefix,
                'runtime': 'windows' if distro == NATIVE_LINUX else 'wsl', 'argv': argv(distro, identifier),
                'timeout_seconds': min(1500, budget['timeout_seconds']), 'max_log_bytes': budget['max_log_bytes'],
                'evidence_run_dir': None}
        self.validate_spec(spec)
        descriptor = {'handler': 'ppt-media.v1', 'handler_version': self.version, 'case_id': case.id,
                      'output': document['output'], 'request': document, 'source': observed, 'resources': resources,
                      'request_sha256': hashlib.sha256(payload).hexdigest(), 'execution_id': identifier,
                      'worker_sha256': native.record(WORKER)['sha256']}
        return PreparedStep(self.key, self.version, self.job_type, spec, resources, None,
                            self.publication_channel(descriptor), descriptor, generated_inputs={CONFIG: payload})

    def validate_spec(self, spec):
        args, runtime = spec.get('argv'), spec.get('runtime')
        try:
            if spec.get('command') != 'PPTX narrated video' or spec.get('evidence_run_dir') is not None or not isinstance(args, list):
                raise ValueError('Unexpected PPT video command')
            identifier = str(uuid.UUID(args[-1]))
            if runtime == 'windows':
                valid = sys.platform.startswith('linux') and args == argv(NATIVE_LINUX, identifier)
            else:
                valid = (runtime == 'wsl' and args[2] != NATIVE_LINUX and re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', args[2])
                         and args == argv(args[2], identifier))
            if not valid:
                raise ValueError('Unexpected PPT video executable')
        except (IndexError, AttributeError, TypeError, ValueError) as error:
            raise ExecutionConflict('evidence_invalid', 'PPT 视频执行参数无效') from error

    def artifacts(self, run, descriptor, output, frozen_files):
        from .postprocess_artifacts import context

        attempt = context(run)
        spec = descriptor['request']
        contract = workspace.checked_path(run, CONFIG).read_bytes()
        if contract != json_text(spec).encode('utf-8') or video_request(json.loads(contract)) != spec:
            raise ValueError('PPT video request changed')
        for path, record in frozen_files.items():
            actual = workspace.digest_file(workspace.checked_path(run, path))
            if any(record[key] != value for key, value in actual.items()):
                raise ValueError('PPT video changed a frozen input')
        if source(run, descriptor['resources']) != descriptor['source']:
            raise ValueError('PPT video source changed')
        target = workspace.checked_path(run, spec['output'])
        result = workspace.digest_file(target)
        if any(output['files'][spec['output']][key] != value for key, value in result.items()):
            raise ValueError('PPT video output changed after capture')
        with target.open('rb') as stream:
            if stream.read(8)[4:] != b'ftyp':
                raise ValueError('PPT video has no MP4 header')
        identifier = descriptor['execution_id']
        receipt = json.loads(workspace.checked_path(run, f'.vibeflow/ppt-media-receipts/{identifier}.json').read_text(encoding='utf-8'))
        expected = {'schema': 'vibeflow.ppt-media.v1', 'execution_id': identifier,
                    'attempt_id': attempt['attempt_id'], 'case_id': descriptor['case_id'], 'job_id': attempt['source']['job_id'],
                    'request_sha256': descriptor['request_sha256'], 'worker_sha256': descriptor['worker_sha256'],
                    'source': descriptor['source']['files'][spec['input']], 'output': result,
                    'voice': native.VOICES[spec['language']], 'video_codec': 'h264', 'audio_codec': 'aac',
                    'width': 1280, 'height': 720}
        if any(receipt.get(key) != value for key, value in expected.items()):
            raise ValueError('PPT video lacks a matching worker receipt')
        input_notes = native.speaker_notes(workspace.checked_path(run, spec['input']))
        pages = receipt['pages']
        if ([entry['page'] for entry in pages] != list(range(1, len(input_notes) + 1))
                or [entry['narrated'] for entry in pages] != list(map(bool, input_notes))
                or not 0 < receipt['duration_seconds'] <= 725):
            raise ValueError('PPT video page timing or speech differs from the frozen deck')
        descriptor['receipt'] = receipt
        return {spec['output']: {**result, 'executable': False}}
