"""Simcenter STAR-CCM+ native batch input and result contract."""
from pathlib import Path

from . import attempt_workspace as workspace, request_deadline
from .commercial_batch import CommercialBatchHandler, case_input, installed_launcher, required_products


def launcher():
    return installed_launcher('VIBEFLOW_STARCCM', 'STAR-CCM+')


def input_path(value):
    return case_input(value, '.sim', 'STAR-CCM+')


class StarccmHandler(CommercialBatchHandler):
    key = 'starccm.solve'
    publication_kind = 'starccm'
    name = 'STAR-CCM+'
    command = 'STAR-CCM+ batch'
    worker = Path(__file__).with_name('starccm_worker.py')
    max_input_bytes = workspace.MAX_BYTES
    launcher = staticmethod(launcher)
    input_path = staticmethod(input_path)

    @staticmethod
    def options(parameters):
        cpus = parameters['cpus']
        operation = parameters['operation']
        if type(cpus) is not int or not 1 <= cpus <= 16:
            raise ValueError('STAR-CCM+ CPU 数量须在 1 至 16 之间')
        if operation not in ('solve', 'mesh_then_solve'):
            raise ValueError('STAR-CCM+ 批处理操作无效')
        return {'cpus': cpus, 'operation': operation}

    @staticmethod
    def input_record(root, path):
        model = workspace.checked_path(root, input_path(path))
        if not model.is_file() or not 0 < model.stat().st_size <= workspace.MAX_BYTES:
            raise ValueError('STAR-CCM+ 输入须为工作区容量内的非空 SIM 文件')
        return workspace.digest_file(model, deadline=request_deadline.current())

    @staticmethod
    def products(root):
        return required_products(root, ('result.sim',), 'STAR-CCM+')
