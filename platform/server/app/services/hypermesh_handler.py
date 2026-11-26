"""Altair HyperMesh native mesh export contract."""
import json
from pathlib import Path

from . import attempt_workspace as workspace, request_deadline
from .commercial_batch import CommercialBatchHandler, installed_launcher, required_products


EXPORTS = {'optistruct': (('optistruct', 'optistruct'), 'result.fem'),
           'abaqus': (('abaqus', 'standard.3d'), 'result.inp')}
SUMMARY = 'mesh-summary.json'


def launcher():
    return installed_launcher('VIBEFLOW_HYPERMESH', 'HyperMesh')


def input_path(value):
    path = workspace.canonical_path(value)
    if path.split('/')[0] not in {'B-mesh', 'C-run'} or Path(path).suffix.casefold() != '.hm':
        raise ValueError('请选择 B-mesh 或 C-run 下的 HyperMesh .hm 输入文件')
    return path


class HypermeshHandler(CommercialBatchHandler):
    key = 'hypermesh.export'
    job_type = 'mesh'
    output_stage = 'B-mesh'
    publication_kind = 'hypermesh'
    name = 'HyperMesh'
    command = 'HyperMesh export'
    worker = Path(__file__).with_name('hypermesh_worker.py')
    max_input_bytes = workspace.MAX_BYTES
    launcher = staticmethod(launcher)
    input_path = staticmethod(input_path)

    @staticmethod
    def options(parameters):
        export_format = parameters['format']
        if not isinstance(export_format, str) or export_format not in EXPORTS:
            raise ValueError('HyperMesh 导出模板无效')
        return {'format': export_format}

    @staticmethod
    def input_record(root, path):
        model = workspace.checked_path(root, input_path(path))
        if not model.is_file() or not 0 < model.stat().st_size <= workspace.MAX_BYTES:
            raise ValueError('HyperMesh 输入须为工作区容量内的非空 HM 文件')
        return workspace.digest_file(model, deadline=request_deadline.current())

    @staticmethod
    def products(root):
        decks = [name for _, name in EXPORTS.values() if (root / name).exists()]
        if len(decks) != 1:
            raise ValueError('HyperMesh 必须写出一份求解器网格文件')
        return required_products(root, (decks[0], SUMMARY), 'HyperMesh')

    def receipt(self, root, config, output):
        response = super().receipt(root, config, output)
        _, deck = EXPORTS[config['format']]
        if deck not in response['products']:
            raise ValueError('HyperMesh 产物格式与选定模板不一致')
        with workspace.checked_path(output, SUMMARY).open('rb') as stream:
            counts = json.loads(stream.read(4096))
        if (not isinstance(counts, dict) or set(counts) != {'nodes', 'elements'}
                or any(type(count) is not int or count <= 0 for count in counts.values())):
            raise ValueError('HyperMesh 数据库节点或单元计数无效')
        response['mesh'] = counts
        return response
