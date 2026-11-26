"""Registered batch analysis for an operator-provided Abaqus launcher."""
import hashlib
from pathlib import Path
import re

from . import attempt_workspace as workspace
from .commercial_batch import CommercialBatchHandler, case_input, installed_launcher


WORKER = Path(__file__).with_name('abaqus_worker.py')
PRODUCTS = ('result.odb', 'result.sta', 'result.dat', 'result.msg', 'result.log')
REQUIRED = {'result.odb', 'result.sta'}
MAX_DECK_BYTES = 1024 * 1024


def launcher():
    return installed_launcher('VIBEFLOW_ABAQUS', 'Abaqus')


def input_path(value):
    return case_input(value, '.inp', 'Abaqus')


def read_deck(root, path):
    selected = workspace.checked_path(root, input_path(path))
    with selected.open('rb') as stream:
        data = stream.read(MAX_DECK_BYTES + 1)
    if not data or len(data) > MAX_DECK_BYTES:
        raise ValueError('Abaqus 输入文件须在 1 字节至 1 MiB 之间')
    keywords = set()
    continuation = False
    for line in data.decode('ascii').splitlines():
        keyword_line = line.strip().upper()
        if not keyword_line or keyword_line.startswith('**'):
            continue
        if not continuation and not keyword_line.startswith('*'):
            continue
        name, *options = keyword_line.split(',') if not continuation else ('', *keyword_line.split(','))
        name = name.lstrip('*').replace(' ', '')
        keywords.add(name)
        if (name.startswith(('INCL', 'IMPO', 'USER', 'REST', 'CO-SIM', 'SUBMODEL', 'EXTERNALFIELD', 'PARAMETER'))
                or any(re.match(r'\s*(?:INPUT|FILE|LIBRARY|OLDJOB|GLOBALMODEL|USER)\s*(?:=|$)', option)
                       for option in options)):
            raise ValueError('Abaqus 批处理需要自包含输入，禁止引用外部文件、重启动和用户子程序')
        continuation = keyword_line.endswith(',')
    if not {'STEP', 'ENDSTEP'} <= keywords or b'\0' in data:
        raise ValueError('Abaqus 输入缺少完整分析步')
    return data


def file_record(data):
    return {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def deck_record(root, path):
    return file_record(read_deck(root, path))


def products(root):
    result = {}
    for name in PRODUCTS:
        if (root / name).exists() or (root / name).is_symlink():
            result[name] = workspace.digest_file(workspace.checked_path(root, name))
    if not REQUIRED <= result.keys() or any(not result[name]['size'] for name in REQUIRED):
        raise ValueError('Abaqus 未写出非空 ODB 和状态文件')
    with workspace.checked_path(root, 'result.sta').open('rb') as stream:
        stream.seek(max(0, result['result.sta']['size'] - 65536))
        ending = stream.read(65536).upper().splitlines()
    if not ending or not any(line.strip() == b'THE ANALYSIS HAS COMPLETED SUCCESSFULLY' for line in ending):
        raise ValueError('Abaqus 状态文件缺少成功完成分析的记录')
    return result


class AbaqusHandler(CommercialBatchHandler):
    key = 'abaqus.solve'
    publication_kind = 'abaqus'
    name = 'Abaqus'
    command = 'Abaqus analysis'
    worker = WORKER
    max_input_bytes = MAX_DECK_BYTES
    launcher = staticmethod(launcher)
    input_path = staticmethod(input_path)
    input_record = staticmethod(deck_record)
    products = staticmethod(products)

    @staticmethod
    def options(parameters):
        cpus = parameters['cpus']
        if type(cpus) is not int or not 1 <= cpus <= 16:
            raise ValueError('Abaqus CPU 数量须在 1 至 16 之间')
        return {'cpus': cpus}
