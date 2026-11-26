"""Ansys Fluent native steady and transient batch contract."""
from pathlib import Path

from . import attempt_workspace as workspace, request_deadline
from .commercial_batch import CommercialBatchHandler, case_input, installed_launcher, required_products


CASE_FORMATS = ('.cas.h5', '.cas')
DATA_FORMATS = ('.dat.h5', '.dat')
HDF5 = b'\x89HDF\r\n\x1a\n'


def launcher():
    return installed_launcher('VIBEFLOW_FLUENT', 'Fluent')


def input_path(value):
    return case_input(value, CASE_FORMATS, 'Fluent')


def check_cff(path):
    size = path.stat().st_size
    offset = 0
    with path.open('rb') as stream:
        while offset + len(HDF5) <= size:
            stream.seek(offset)
            if stream.read(len(HDF5)) == HDF5:
                return
            # HDF5 permits a user block of 512 bytes or any larger power of two.
            offset = 512 if offset == 0 else offset * 2
    raise ValueError('Fluent Common Fluids Format 文件缺少 HDF5 标识')


class FluentHandler(CommercialBatchHandler):
    key = 'fluent.solve'
    publication_kind = 'fluent'
    name = 'Fluent'
    command = 'Fluent batch'
    worker = Path(__file__).with_name('fluent_worker.py')
    max_input_bytes = workspace.MAX_BYTES
    launcher = staticmethod(launcher)
    input_path = staticmethod(input_path)

    @staticmethod
    def options(parameters):
        cpus = parameters['cpus']
        iterations = parameters['iterations']
        time_steps = parameters.get('time_steps')
        dimension = parameters['dimension']
        initialization = parameters.get('initialization')
        data = parameters.get('data_path')
        if type(cpus) is not int or not 1 <= cpus <= 16 or dimension not in ('2d', '2ddp', '3d', '3ddp'):
            raise ValueError('Fluent 维度、精度或 CPU 数量无效')
        for count in (iterations,) if time_steps is None else (iterations, time_steps):
            if type(count) is not int or not 1 <= count <= 2**31 - 1:
                raise ValueError('Fluent 迭代次数或时间步数无效')
        if initialization not in (None, 'hybrid', 'standard'):
            raise ValueError('Fluent 初始化方式无效')
        if data is not None:
            data = case_input(data, DATA_FORMATS, 'Fluent')
        return {'cpus': cpus, 'iterations': iterations, 'time_steps': time_steps, 'dimension': dimension,
                'initialization': None if data else initialization or 'hybrid', 'data_path': data}

    def input_paths(self, path, options):
        data = options['data_path']
        if data is None:
            return [path]
        if path.casefold().endswith('.h5') != data.casefold().endswith('.h5'):
            raise ValueError('Fluent case 和 data 文件必须使用相同格式')
        return [path, data]

    @staticmethod
    def input_record(root, path):
        selected = workspace.checked_path(root, case_input(path, (*CASE_FORMATS, *DATA_FORMATS), 'Fluent'))
        if not selected.is_file() or not 0 < selected.stat().st_size <= workspace.MAX_BYTES:
            raise ValueError('Fluent 输入须为工作区容量内的非空文件')
        if selected.suffix.casefold() == '.h5':
            check_cff(selected)
        return workspace.digest_file(selected, deadline=request_deadline.current())

    @staticmethod
    def products(root):
        records = required_products(root, ('result.cas.h5', 'result.dat.h5', 'fluent.log'), 'Fluent')
        for name in ('result.cas.h5', 'result.dat.h5'):
            check_cff(workspace.checked_path(root, name))
        return records
