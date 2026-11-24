"""Seal declarative MOOSE inputs and independently verify its native outputs."""
import csv
import hashlib
import json
import math
from pathlib import PurePosixPath
import re

from . import attempt_workspace as workspace
from . import moose_checkpoint, moose_mesh_adaptivity, moose_numerical


CONFIG = '.vibeflow/prepared/moose.json'
LOG = '.vibeflow/moose/solver.log'
RECEIPT = '.vibeflow/moose/receipt.json'
NAME = r'[A-Za-z][A-Za-z0-9_]*'
REAL = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?'
POINT = rf"'{REAL} +{REAL} +{REAL}'"
GROUPS = {'Variables', 'Kernels', 'BCs', 'Postprocessors', 'Functions', 'AuxVariables', 'AuxKernels'}
STEPPER_RULES = {'type':'IterationAdaptiveDT','dt':REAL,'optimal_iterations':'[1-9][0-9]{0,5}',
                 'growth_factor':REAL,'cutback_factor':REAL}
MARKER_RULES = {'type':'BoxMarker|ValueThresholdMarker','bottom_left':POINT,'top_right':POINT,'inside':'REFINE',
                'outside':'DO_NOTHING','third_state':'DO_NOTHING','variable':NAME,'refine':REAL}
NESTED_RULES = {('Executioner','TimeStepper'):STEPPER_RULES,('Adaptivity','Markers'): {}}
RULES = {
    'Mesh': {'type': 'GeneratedMesh', 'dim': '[123]', **{key: '[1-9][0-9]{0,5}' for key in ('nx', 'ny', 'nz')},
             **{key: REAL for key in ('xmin', 'xmax', 'ymin', 'ymax', 'zmin', 'zmax')}},
    'Variables': {'family': 'LAGRANGE|HIERARCHIC', 'order': 'FIRST|SECOND', 'initial_condition': REAL},
    'Kernels': {'type': 'Diffusion|Reaction|TimeDerivative|BodyForce', 'variable': NAME, 'value': REAL, 'rate': REAL},
    'BCs': {'type': 'DirichletBC|NeumannBC', 'variable': NAME,
            'boundary': r"(?:[A-Za-z0-9_]+|'[A-Za-z0-9_]+(?: [A-Za-z0-9_]+)*')", 'value': REAL},
    'Executioner': {'type': 'Steady|Transient', 'solve_type': 'NEWTON|PJFNK|JFNK|LINEAR',
                    'petsc_options_iname': "'-pc_type'", 'petsc_options_value': "'lu'",
                    'l_tol':REAL, 'l_max_its':'[1-9][0-9]{0,5}', 'nl_rel_tol':REAL, 'nl_abs_tol':REAL,
                    'nl_max_its':'[1-9][0-9]{0,5}', 'dtmin':REAL, 'dtmax':REAL,
                    'dt': REAL, 'end_time': REAL, 'num_steps': '[1-9][0-9]{0,5}'},
    'Adaptivity': {'adaptivity_type':'h|p|hp','steps':'[1-9][0-9]{0,2}',
                   'max_h_level':'[1-9][0-9]{0,2}','marker':NAME},
    'Postprocessors': {'type': 'PointValue|NumElements|NumDOFs','variable':NAME,'point':POINT,
                       'elem_filter':'ACTIVE','system':'NL','force_preaux':'true'},
    'Functions': {'type':'ParsedFunction','symbol_names':"'measured'",'symbol_values':rf"'{NAME}'",
                  'expression':moose_mesh_adaptivity.GOAL_EXPRESSION},
    'AuxVariables': {'family':'MONOMIAL','order':'CONSTANT'},
    'AuxKernels': {'type':'FunctionAux','variable':NAME,'function':NAME,'execute_on':'TIMESTEP_END'},
    'Outputs': {'csv': 'true', 'exodus': 'true', 'checkpoint': 'true|false'},
}


def input_path(value):
    path = workspace.canonical_path(value)
    if (PurePosixPath(path).suffix != '.i'
            or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', part) for part in path.split('/'))):
        raise ValueError('MOOSE requires a portable case-relative .i path')
    return path


def output_base(path):
    return input_path(path)[:-2] + '_out'


def output_paths(path, *, checkpoint=False, adaptive_steps=0):
    base = output_base(path)
    results = (base+'.csv', base+'.e', *(f'{base}.e-s{cycle:03d}' for cycle in range(2,adaptive_steps+2)))
    return (*results, input_path(path)[:-2]+'_checkpoint.zip') if checkpoint else results


def validate_deck(text):
    sections = {}
    stack = ()
    for line_no, line in enumerate(text.splitlines(), 1):
        line = line.partition('#')[0].strip()
        if not line:
            continue
        if line == '[]' and stack:
            stack = stack[:-1]
            continue
        block = re.fullmatch(rf'\[({NAME})\]', line)
        if block is not None:
            name = block[1]
            nested = (*stack, name)
            if nested not in sections and ((not stack and name in RULES)
                    or (len(stack) == 1 and stack[0] in GROUPS) or nested==('Executioner','TimeStepper')
                    or nested==('Adaptivity','Markers') or stack==('Adaptivity','Markers')):
                sections[nested] = {}
                stack = nested
                continue
        setting = re.fullmatch(rf'({NAME})\s*=\s*(.+)', line)
        if setting is not None and stack and not (len(stack) == 1 and stack[0] in GROUPS):
            key, value = setting.groups()
            rules = NESTED_RULES.get(stack,RULES[stack[0]]) if len(stack)<3 else MARKER_RULES
            rule = rules.get(key)
            if key not in sections[stack] and rule is not None and re.fullmatch(rule, value):
                sections[stack][key] = value
                continue
        raise ValueError(f'Unsupported MOOSE block or value at line {line_no}')
    if stack or any((name,) not in sections for name in ('Mesh','Variables','Kernels','BCs','Executioner')):
        raise ValueError('MOOSE input requires a closed Mesh, Variables, Kernels, BCs and Executioner')
    mesh = sections['Mesh',]
    dim = int(mesh.get('dim','1'))
    if mesh.get('type') != 'GeneratedMesh' or math.prod(int(mesh.get('n'+axis,'1')) for axis in 'xyz'[:dim]) > 1000000:
        raise ValueError('MOOSE requires a generated mesh with at most 1,000,000 cells')
    for name, values in sections.items():
        if name[0] in {'Kernels','BCs','Postprocessors'} and len(name)==2 and 'type' not in values:
            raise ValueError(f'MOOSE object requires an explicit type: {name[-1]}')
    stepper=sections.get(('Executioner','TimeStepper'))
    if stepper is not None and (sections['Executioner',].get('type')!='Transient' or not {'type','dt'}<=stepper.keys()):
        raise ValueError('MOOSE IterationAdaptiveDT needs a transient executioner and explicit dt')
    return sections


def dependencies(root, path, with_restart=False):
    path = input_path(path)
    with workspace.checked_path(root,path).open('rb') as stream:
        data = stream.read(1024*1024+1)
    if len(data) > 1024*1024:
        raise ValueError('MOOSE input exceeds 1 MiB')
    if not data.isascii() or any(byte<32 and byte not in (9,10,13) for byte in data):
        raise ValueError('MOOSE input must use ASCII HIT syntax')
    sections = validate_deck(data.decode('ascii'))
    source = {'input':path, 'files':{path:{'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}}}
    checkpoint = sections.get(('Outputs',),{}).get('checkpoint')=='true'
    if checkpoint or with_restart:
        if sections['Executioner',].get('type')!='Transient':
            raise ValueError('MOOSE checkpoint and restart require a transient model')
        state = sorted((name,values) for name,values in sections.items() if name[0] in {'Mesh','Variables'})
        source['checkpoint'] = checkpoint
        source['model_sha256'] = hashlib.sha256(json.dumps(state,sort_keys=True).encode('ascii')).hexdigest()
    moose_numerical.prepare(root,source,sections,with_restart=with_restart)
    moose_mesh_adaptivity.prepare(root,source,sections)
    return source


def targets(root, path, *, checkpoint=False, adaptive_steps=0):
    files = {}
    for output in output_paths(path,checkpoint=checkpoint,adaptive_steps=adaptive_steps):
        try:
            file = workspace.checked_path(root,output)
        except FileNotFoundError:
            continue
        files[output] = workspace.digest_file(file)
    return files


def validate_outputs(root, source, files, executable):
    outputs = output_paths(source['input'],checkpoint=source.get('checkpoint',False))
    table,*meshes = outputs[:2]
    adaptivity = source.get('mesh_adaptivity',{})
    steps = adaptivity.get('cycles',0)
    allowed = output_paths(source['input'],checkpoint=source.get('checkpoint',False),adaptive_steps=steps)
    if not set(outputs) <= files.keys() <= set(allowed):
        raise ValueError('MOOSE did not produce the registered outputs')
    meshes.extend(path for path in allowed[2:] if path in files and path not in outputs)
    if steps and 'goal' not in adaptivity and len(meshes)<2:
        raise ValueError('MOOSE did not produce a refined Exodus mesh')
    with workspace.checked_path(root,table).open(encoding='ascii', newline='') as stream:
        rows = csv.reader(stream)
        if next(rows,[])[:1] != ['time'] or not next(rows,[]):
            raise ValueError('MOOSE CSV has no native time data')
    for mesh in meshes:
        with workspace.checked_path(root,mesh).open('rb') as stream:
            header = stream.read(8)
        if not header.startswith((b'CDF\x01',b'CDF\x02',b'CDF\x05',b'\x89HDF\r\n\x1a\n')):
            raise ValueError('MOOSE Exodus output has no NetCDF signature')
    if b'Solve Converged!' not in workspace.checked_path(root,LOG).read_bytes():
        raise ValueError('MOOSE did not report a converged solve')
    if len(outputs)==3:
        moose_checkpoint.validate(workspace.checked_path(root,outputs[-1]),source['model_sha256'],executable)


def receipt(root, source, identifier, executable_sha256, worker_sha256, files, restart=None):
    from .postprocess_artifacts import context
    identity = context(root)
    value = {'schema':'vibeflow.moose-result.v1','case_id':identity['source']['case_id'],
        'job_id':identity['source']['job_id'],'attempt_id':identity['attempt_id'],
        'execution_id':identifier,'input':source['input'],'returncode':0,'inputs':source['files'],
        'outputs':files,'executable_sha256':executable_sha256,'worker_sha256':worker_sha256,
        'log':workspace.digest_file(workspace.checked_path(root,LOG))}
    if restart is not None:
        value['restart'] = {**restart,'archive':workspace.digest_file(workspace.checked_path(root,restart['checkpoint_path']))}
    if 'numerical' in source:
        table=workspace.checked_path(root,output_base(source['input'])+'.csv')
        evidence=moose_numerical.observed(source['numerical'],table,workspace.checked_path(root,LOG).read_bytes())
        value['numerical']={**source['numerical'],'observed':evidence}
    if 'mesh_adaptivity' in source:
        table=workspace.checked_path(root,output_base(source['input'])+'.csv')
        value['mesh_adaptivity']={**source['mesh_adaptivity'],
            'observed':moose_mesh_adaptivity.observed(source['mesh_adaptivity'],table)}
    return value


def artifacts(run, descriptor, output, frozen):
    configuration = json.loads(workspace.checked_path(run,CONFIG).read_text(encoding='utf8'))
    if configuration != descriptor['configuration']:
        raise ValueError('MOOSE configuration differs from its sealed request')
    for path, expected in frozen.items():
        actual = workspace.digest_file(workspace.checked_path(run,path))
        if any(expected[key]!=actual[key] for key in actual):
            raise ValueError('MOOSE altered a sealed input: '+path)
    source = descriptor['source']
    files = targets(run,source['input'],checkpoint=source.get('checkpoint',False),
                    adaptive_steps=source.get('mesh_adaptivity',{}).get('cycles',0))
    expected = receipt(run,source,descriptor['execution_id'],descriptor['executable_sha256'],descriptor['worker_sha256'],
                       files,configuration.get('restart'))
    if json.loads(workspace.checked_path(run,RECEIPT).read_text(encoding='utf8')) != expected:
        raise ValueError('MOOSE does not have the registered native receipt')
    captured = {path:output['files'][path] for path in files}
    if any({key:captured[path][key] for key in record}!=record for path,record in files.items()):
        raise ValueError('MOOSE output changed after capture')
    validate_outputs(run,source,files,descriptor['executable_sha256'])
    descriptor.update(output_files=captured, receipt=expected)
    return captured
