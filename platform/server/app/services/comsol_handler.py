"""COMSOL native batch input and result contract."""
from pathlib import Path
import re

from . import attempt_workspace as workspace, request_deadline
from .commercial_batch import CommercialBatchHandler, case_input, installed_launcher, required_products


STUDY_TAG = r'[A-Za-z][A-Za-z0-9_]{0,63}'


def launcher():
    return installed_launcher('VIBEFLOW_COMSOL', 'COMSOL')


def input_path(value):
    return case_input(value, '.mph', 'COMSOL')


class ComsolHandler(CommercialBatchHandler):
    key = 'comsol.solve'
    publication_kind = 'comsol'
    name = 'COMSOL'
    command = 'COMSOL batch'
    worker = Path(__file__).with_name('comsol_worker.py')
    max_input_bytes = workspace.MAX_BYTES
    launcher = staticmethod(launcher)
    input_path = staticmethod(input_path)

    @staticmethod
    def options(parameters):
        cpus = parameters['cpus']
        study = parameters.get('study')
        if type(cpus) is not int or not 1 <= cpus <= 16:
            raise ValueError('COMSOL CPU 数量须在 1 至 16 之间')
        if study is not None and (not isinstance(study, str) or not re.fullmatch(STUDY_TAG, study)):
            raise ValueError('COMSOL Study 标签无效')
        return {'cpus': cpus, 'study': study}

    @staticmethod
    def input_record(root, path):
        model = workspace.checked_path(root, input_path(path))
        if not model.is_file() or not 0 < model.stat().st_size <= workspace.MAX_BYTES:
            raise ValueError('COMSOL 输入须为工作区容量内的非空 MPH 文件')
        return workspace.digest_file(model, deadline=request_deadline.current())

    @staticmethod
    def products(root):
        return required_products(root, ('result.mph', 'batch.log'), 'COMSOL')
