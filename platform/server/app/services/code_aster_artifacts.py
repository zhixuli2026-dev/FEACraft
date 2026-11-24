"""Code_Aster export selection, output verification and attempt receipt."""
import hashlib
import json
from pathlib import PurePosixPath
import re

from . import attempt_workspace as workspace
from . import code_aster_numerical, code_aster_state


CONFIG = '.vibeflow/prepared/code-aster.json'
LOG = '.vibeflow/code-aster/solver.log'
RECEIPT = '.vibeflow/code-aster/receipt.json'
MAX_EXPORT = 1024*1024
MAX_INPUT = 128*1024*1024
MAX_OUTPUT = 128*1024*1024
PARAMETERS = {'time_limit':(1,3600), 'memory_limit':(128,2048), 'ncpus':(1,1), 'mpi_nbcpu':(1,1)}
INPUTS = {'comm':('.comm',), 'mail':('.mail',), 'mmed':('.med','.mmed'), 'libr':('.txt','.csv','.dat','.py')}
OUTPUTS = {'mess':('.mess',), 'rmed':('.med','.rmed'), 'resu':('.resu',), 'libr':('.txt','.csv','.dat')}


def path(value):
    result = workspace.canonical_path(value)
    if result!=value or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',part) for part in result.split('/')):
        raise ValueError('Code_Aster files must use portable case-relative paths')
    return result


def input_path(value):
    selected = path(value)
    if PurePosixPath(selected).suffix!='.export':
        raise ValueError('Code_Aster requires an .export input')
    return selected


def base_archive(directory):
    return path(directory)+'_base.zip'


def selection(root, export):
    export = input_path(export)
    prefix = PurePosixPath(export).parent
    with workspace.checked_path(root,export).open('rb') as stream:
        content = stream.read(MAX_EXPORT+1)
    if len(content)>MAX_EXPORT or not content.isascii() or any(x<32 and x not in (9,10,13) for x in content):
        raise ValueError('Code_Aster export must be an ASCII file up to 1 MiB')
    settings, inputs, outputs, native_base = {}, {}, {}, {}
    names, units = {export.casefold()}, set()
    for number, line in enumerate(content.decode('ascii').splitlines(),1):
        fields = line.partition('#')[0].split()
        if not fields:
            continue
        if len(fields)==3 and fields[0]=='P':
            _, key, value = fields
            lower,upper = PARAMETERS.get(key,(0,-1))
            if key not in settings and re.fullmatch(r'[0-9]{1,5}',value) and lower<=int(value)<=upper:
                settings[key] = int(value)
                continue
        if len(fields)==5 and fields[0]=='F':
            _, kind, member, direction, unit = fields
            allowed = INPUTS if direction=='D' else OUTPUTS if direction=='R' else {}
            selected = (prefix/path(member)).as_posix()
            if (PurePosixPath(selected).suffix in allowed.get(kind,()) and selected.casefold() not in names
                    and re.fullmatch(r'[1-9][0-9]?',unit) and int(unit) not in units
                    and (kind not in {'comm','mess'} or int(unit)=={'comm':1,'mess':6}[kind])):
                names.add(selected.casefold())
                units.add(int(unit))
                (inputs if direction=='D' else outputs)[selected] = kind
                continue
        if len(fields)==5 and fields[:2]==['R','base'] and fields[-1]=='0':
            selected = (prefix/path(fields[2])).as_posix()
            directions = {'D':('input',),'R':('output',),'DR':('input','output')}.get(fields[3])
            archive = base_archive(selected)
            if (directions is not None and not (set(directions)&set(native_base))
                    and selected.casefold() not in names and ('output' not in directions or archive.casefold() not in names)):
                names.add(selected.casefold())
                native_base.update(dict.fromkeys(directions,selected))
                if 'output' in directions:
                    outputs[archive] = 'base'
                    names.add(archive.casefold())
                continue
        raise ValueError(f'Unsupported Code_Aster export record at line {number}')
    if ('time_limit' not in settings or 'memory_limit' not in settings
            or list(inputs.values()).count('comm')!=1 or list(outputs.values()).count('mess')!=1
            or len(inputs)+len(outputs)>64):
        raise ValueError('Code_Aster requires explicit time/memory limits, one command file and one message result')
    if any('/'.join(name.split('/')[:i]) in names for name in names for i in range(1,len(name.split('/')))):
        raise ValueError('Code_Aster export paths overlap')
    plan = {'input':export,'inputs':inputs,'outputs':outputs,'settings':settings,
            'export_sha256':hashlib.sha256(content).hexdigest()}
    if native_base:
        plan['base'] = native_base
    return plan


def dependencies(root, plan):
    current = selection(root,plan['input'])
    if current!=plan:
        raise ValueError('Code_Aster export changed while reading its dependencies')
    files = {}
    for member in (plan['input'],*plan['inputs']):
        target = workspace.checked_path(root,member)
        limit = MAX_EXPORT if plan['inputs'].get(member)=='comm' else MAX_INPUT
        if target.stat().st_size>limit:
            raise ValueError('Code_Aster input exceeds its size limit: '+member)
        files[member] = workspace.digest_file(target)
    if sum(record['size'] for record in files.values())>MAX_INPUT:
        raise ValueError('Code_Aster inputs exceed 128 MiB in total')
    source={'plan':plan,'files':files}
    code_aster_numerical.prepare(root,source)
    return source


def targets(root, plan):
    files = {}
    for member in plan['outputs']:
        try:
            target = workspace.checked_path(root,member)
        except FileNotFoundError:
            continue
        if target.stat().st_size>MAX_OUTPUT:
            raise ValueError('Code_Aster output exceeds its size limit: '+member)
        files[member] = workspace.digest_file(target)
    return files


def validate_outputs(root, source, files, engine=None):
    kinds = source['plan']['outputs']
    if set(files)!=set(kinds) or any(row['size']<1 for row in files.values()):
        raise ValueError('Code_Aster did not write all declared results')
    if b'DIAGNOSTIC JOB : OK' not in workspace.checked_path(root,LOG).read_bytes():
        raise ValueError('Code_Aster did not report a successful study')
    for name,kind in kinds.items():
        if kind=='rmed':
            with workspace.checked_path(root,name).open('rb') as stream:
                if stream.read(8)!=b'\x89HDF\r\n\x1a\n':
                    raise ValueError('Code_Aster MED result has no HDF5 signature')
        elif kind=='base':
            code_aster_state.validate(workspace.checked_path(root,name),engine)


def receipt(root, source, execution_id, tool, worker, files, restart=None):
    from .postprocess_artifacts import context
    identity = context(root)
    value = {'schema':'vibeflow.code-aster-result.v1','case_id':identity['source']['case_id'],
        'job_id':identity['source']['job_id'],'attempt_id':identity['attempt_id'],'execution_id':execution_id,
        'input':source['plan']['input'],'returncode':0,'inputs':source['files'],'outputs':files,
        'executable_sha256':tool,'worker_sha256':worker,'log':workspace.digest_file(workspace.checked_path(root,LOG))}
    if restart is not None:
        archive = workspace.checked_path(root,restart['checkpoint_path'])
        value['restart'] = {**restart,'archive':workspace.digest_file(archive)}
    if 'numerical' in source:
        evidence=code_aster_numerical.observed(root,source['numerical'])
        value['numerical']={**source['numerical'],'observed':evidence}
    return value


def artifacts(run, descriptor, output, frozen):
    config = json.loads(workspace.checked_path(run,CONFIG).read_text(encoding='utf8'))
    if config!=descriptor['configuration']:
        raise ValueError('Code_Aster sealed configuration differs from its request')
    for path,expected in frozen.items():
        actual = workspace.digest_file(workspace.checked_path(run,path))
        if any(expected[key]!=actual[key] for key in actual):
            raise ValueError('Code_Aster altered a sealed input: '+path)
    source = descriptor['source']
    files = targets(run,source['plan'])
    expected = receipt(run,source,descriptor['execution_id'],descriptor['executable_sha256'],descriptor['worker_sha256'],
                       files,config.get('restart'))
    if json.loads(workspace.checked_path(run,RECEIPT).read_text(encoding='utf8'))!=expected:
        raise ValueError('Code_Aster result has no matching native receipt')
    captured = {path:output['files'][path] for path in files}
    if any({key:captured[path][key] for key in record}!=record for path,record in files.items()):
        raise ValueError('Code_Aster result changed after capture')
    validate_outputs(run,source,files,config.get('engine_sha256'))
    descriptor.update(output_files=captured,receipt=expected)
    return captured
