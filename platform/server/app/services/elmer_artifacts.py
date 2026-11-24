"""Declarative SIF inputs and native Elmer outputs in the owned attempt."""
import hashlib
import json
import math
from pathlib import PurePosixPath
import re

from . import attempt_workspace as workspace, elmer_electrodes, elmer_electrostatic, elmer_joule, elmer_numerical

CONFIG = '.vibeflow/prepared/elmer.json'
LOG = '.vibeflow/elmer/solver.log'
RECEIPT = '.vibeflow/elmer/receipt.json'
NATIVE = '.vibeflow/elmer/native/current.sif'
MESH_MEMBERS = ('mesh.header','mesh.nodes','mesh.elements','mesh.boundary')
PROCEDURES = {('heatsolve','heatsolver'),('stresssolve','stresssolver'),('flowsolve','flowsolver'),
              elmer_electrostatic.PROCEDURE} | elmer_electrodes.PROCEDURES
GENERATED_SETTINGS = ('coupling','electrodes','electrostatic','numerical')
SECTION = re.compile(r'(header|simulation|constants|body(?: force)? \d+|equation \d+|solver \d+|material \d+|boundary condition \d+|initial condition \d+)',re.I)
KEY = re.compile(r'[A-Za-z][A-Za-z0-9 -]*(?:\(\d+(?:\s*,\s*\d+)*\))?')
VALUE = re.compile(r'[A-Za-z0-9_.,()+\- "\t]+')
PATH_OR_CODE = re.compile(r'\b(file|path|directory|mesh|include|matc|lua|python|command|procedure|function|exec|system call)\b',re.I)
SETTINGS = re.compile(r'^([A-Za-z][^=]*?)\s*=\s*(\S.*)$')


def relative(value):
    path = workspace.canonical_path(value)
    if any(not re.fullmatch(r'[a-zA-Z0-9_. -]+',part) or part.rstrip(' .').casefold()=='.vibeflow'
           for part in PurePosixPath(path).parts):
        raise ValueError('Elmer path must be a portable case-relative path')
    return path


def input_path(value):
    path = relative(value)
    if not path.lower().endswith('.sif'):
        raise ValueError('Elmer input must be .sif')
    return path


def _filename(value, suffix):
    literal = re.fullmatch(r'(?:file\s+|string\s+)?(?:"([^"]+)"|([^"\s]+))',value,re.I)
    if literal is None:
        raise ValueError('Elmer file references require a literal filename')
    name = relative(literal[1] or literal[2])
    if '/' in name or not name.lower().endswith(suffix):
        raise ValueError('Elmer requires a local '+suffix+' filename')
    return name


def model(text):
    section = None
    mesh = output_dir = None
    destinations = {}
    procedures = set()
    restart_name = restart_position = None
    binary_output = None
    extra_restart_options = set()
    for number, raw in enumerate(text.splitlines(),1):
        line = raw.split('!',1)[0].strip()
        if not line:
            continue
        error = ValueError(f'Unsupported Elmer SIF syntax at line {number}')
        if section is None:
            if not SECTION.fullmatch(line):
                raise error
            section = line.lower()
            continue
        if line.lower() == 'end':
            section = None
            continue
        if section == 'header':
            selector = re.fullmatch(r'mesh\s+db\s+"([^"]+)"\s+"([^"]+)"',line,re.I)
            results = re.fullmatch(r'results\s+directory\s+"([^"]*)"',line,re.I)
            if selector is not None and mesh is None:
                base, folder = selector.groups()
                mesh = relative(folder if base=='.' else relative(base)+'/'+relative(folder))
            elif results is not None and output_dir is None:
                value = results[1]
                output_dir = relative(value) if value and value!='.' else value
            elif not re.fullmatch(r'(check\s+keywords\s+(warn|fatal)|include\s+path\s+"")',line,re.I):
                raise error
            continue
        statement = SETTINGS.fullmatch(line)
        if statement is None or not KEY.fullmatch(statement[1]):
            raise error
        key, value = statement.groups()
        key = ' '.join(key.lower().split())
        if key == 'procedure' and section.startswith('solver '):
            fields = re.fullmatch(r'(?:file\s+)?"([A-Za-z0-9_]+)"\s+"([A-Za-z0-9_]+)"',value,re.I)
            if fields is None or tuple(x.lower() for x in fields.groups()) not in PROCEDURES:
                raise error
            procedures.add(tuple(x.lower() for x in fields.groups()))
        elif key in {'output file','post file'} and section=='simulation':
            if key in destinations:
                raise error
            destinations[key] = _filename(value,'.result' if key=='output file' else '.vtu')
        elif key=='restart file' and section=='simulation':
            if restart_name is not None:
                raise error
            restart_name = _filename(value,'.result')
        elif key=='restart position' and section=='simulation':
            position = re.fullmatch(r'(?:integer\s+)?(\d{1,10})',value,re.I)
            if position is None or restart_position is not None or int(position[1])>2147483647:
                raise error
            restart_position = int(position[1])
        elif key=='binary output' and section=='simulation':
            logical = re.fullmatch(r'(?:logical\s+)?(true|false)',value,re.I)
            if logical is None or binary_output is not None:
                raise error
            binary_output = logical[1].lower()=='true'
        elif (not VALUE.fullmatch(value) or value.count('"') not in {0,2}
                or (value.count('"')==2 and re.fullmatch(r'(?:string\s+)?"[^"]*"',value,re.I) is None)
                or (PATH_OR_CODE.search(key) and key not in {'linear system solver','exec solver'})
                or PATH_OR_CODE.search(value)):
            raise error
        elif key.startswith('restart ') or key=='meshes to restart':
            extra_restart_options.add(key)
    if section is not None or mesh is None or not destinations or not procedures:
        raise ValueError('Elmer SIF requires a complete mesh, stock solver procedure and result or VTU output')
    # Elmer writes native state under the mesh name. The header's Results
    # Directory controls visualization output independently.
    mesh_name = PurePosixPath(mesh).name
    post_dir = output_dir or mesh_name
    paths = {key:(PurePosixPath(post_dir if key=='post file' else mesh_name)/name).as_posix()
             for key,name in destinations.items()}
    if binary_output and 'output file' in paths:
        paths['output index'] = paths['output file']+'.pos'
    restart = None
    if restart_name is not None:
        if extra_restart_options - {'restart time'}:
            raise ValueError('Unsupported Elmer restart selector')
        restart_path = f'{mesh_name}/{restart_name}'
        if any(path.casefold()==restart_path.casefold() for path in paths.values()):
            raise ValueError('Elmer restart input and output must use different files')
        restart = {'path':restart_path,'position':restart_position or 0}
    return mesh,paths,restart,procedures


def dependencies(root, path):
    path = input_path(path)
    deck = workspace.checked_path(root,path)
    if deck.stat().st_size > 1024*1024:
        raise ValueError('Elmer SIF exceeds 1 MiB')
    raw = deck.read_bytes()
    text = raw.decode('utf-8-sig')
    mesh,outputs,restart,procedures = model(text)
    files = {path:{'size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}}
    for name in MESH_MEMBERS:
        member = f'{mesh}/{name}'
        files[member] = workspace.digest_file(workspace.checked_path(root,member))
    source = {'input':path,'mesh':mesh,'outputs':outputs,'files':files}
    if restart is not None:
        checkpoint = workspace.checked_path(root,restart['path'])
        with checkpoint.open('rb') as stream:
            header = stream.readline(64).strip()
            if re.fullmatch(rb'ASCII\s+[1-3]|BINARY\s+[23]\.[BL]',header) is None:
                raise ValueError('Elmer restart input is not a native state file')
        files[restart['path']] = workspace.digest_file(checkpoint)
        # Elmer 26.2 can restore the field but misread its time through the
        # binary .pos index. Its native sequential reader restores both.
        # Select only the .result so the index is absent from the private run.
        restart['format'] = 'binary' if header.startswith(b'BINARY') else 'ascii'
        source['restart'] = restart
    elmer_electrostatic.prepare(root,source,text)
    if elmer_electrodes.PROCEDURES & procedures or elmer_electrostatic.PROCEDURE in procedures:
        elmer_electrodes.prepare(root,source,text,procedures)
    elmer_joule.prepare(root,source,text)
    elmer_numerical.prepare(root,source)
    if has_generated_input(source):
        digest = hashlib.sha256(render_input(source,raw)).hexdigest()
        for key in GENERATED_SETTINGS:
            if key in source:
                source[key]['native_input_sha256'] = digest
    return source


def has_generated_input(source):
    return any(key in source for key in GENERATED_SETTINGS)


def render_input(source, original):
    if 'coupling' in source:
        original = elmer_joule.render(original,source['coupling'])
    if 'electrostatic' in source:
        original = elmer_electrostatic.render(original,source['electrostatic'])
    if 'electrodes' in source:
        original = elmer_electrodes.render(original,source['electrodes']['values'])
    if 'numerical' in source:
        original = elmer_numerical.render(original,source['numerical'])
    return original


def native_invocation(root, source):
    if not has_generated_input(source):
        return source['input']
    digest = workspace.digest_file(workspace.checked_path(root,NATIVE))['sha256']
    if any(source[key]['native_input_sha256'] != digest for key in GENERATED_SETTINGS if key in source):
        raise ValueError('Generated Elmer SIF differs from the sealed input')
    return NATIVE


def managed(path, source):
    names = source['outputs']
    if path in names.values():
        return True
    vtu = names.get('post file')
    return vtu is not None and re.fullmatch(re.escape(vtu[:-4])+r'_t\d+\.vtu',path,re.I) is not None


def output_directories(source):
    return sorted({PurePosixPath(path).parent.as_posix() for path in source['outputs'].values()})


def targets(root, source):
    files = {}
    for directory in output_directories(source):
        try:
            folder = root if directory=='.' else workspace.checked_path(root,directory)
        except FileNotFoundError:
            continue
        for child in sorted(folder.iterdir()):
            path = child.relative_to(root).as_posix()
            if managed(path,source):
                files[path] = workspace.digest_file(workspace.checked_path(root,path))
    return files


def validate_outputs(root, source, files):
    names = source['outputs']
    for key,name in names.items():
        matches = [path for path in files if (path.lower().endswith('.vtu') if key=='post file'
                   else path==name)]
        if not matches:
            raise ValueError('Elmer did not write the requested '+key)
        for path in matches:
            with workspace.checked_path(root,path).open('rb') as stream:
                head = stream.read(256)
            if key=='post file' and b'<VTKFile type="UnstructuredGrid"' not in head:
                raise ValueError('Elmer output is not a native VTU grid')
            if key=='output file' and not head.lstrip().startswith((b'ASCII',b'BINARY')):
                raise ValueError('Elmer output is not a native result')
            if key=='output index' and (len(head)<=5 or head[:1] not in {b'B',b'L'}):
                raise ValueError('Elmer did not write a native binary state index')
    log = workspace.checked_path(root,LOG).read_bytes()
    if b'Elmer Solver: ALL DONE' not in log:
        raise ValueError('Elmer did not report complete native execution')
    if 'electrostatic' in source:
        energies=[float(value) for value in re.findall(rb'Tot\. Electric Energy\s*:\s*(\S+)',log)]
        if not energies or any(not math.isfinite(value) or value<0 for value in energies):
            raise ValueError('Elmer did not report a valid native electrostatic energy')
    restart = source.get('restart')
    if restart is not None:
        if b'LoadRestartFile: Did not find data at the requested point' in log:
            raise ValueError('Elmer restart position is absent from the selected state')
        if b'LoadRestartFile: Reading data from file:' in log:
            reading = ('LoadRestartFile: Reading data from file: '+restart['path']).encode('ascii')
            if reading not in log.replace(b'\\',b'/') or b'LoadRestartFile: All done' not in log:
                raise ValueError('Elmer did not finish loading the selected restart state')


def receipt(root, source, execution_id, tool, worker, files):
    from .postprocess_artifacts import context
    identity = context(root)
    result = {'schema':'vibeflow.elmer-result.v1','case_id':identity['source']['case_id'],
            'job_id':identity['source']['job_id'],'attempt_id':identity['attempt_id'],
            'execution_id':execution_id,'input':source['input'],'returncode':0,'inputs':source['files'],
            'outputs':files,'executable_sha256':tool,'worker_sha256':worker,
            'log':workspace.digest_file(workspace.checked_path(root,LOG))}
    for key in GENERATED_SETTINGS:
        if key in source:
            result[key] = source[key]
    if 'numerical' in source:
        evidence = elmer_numerical.observed(source['numerical'],workspace.checked_path(root,LOG).read_bytes())
        result['numerical'] = {**source['numerical'],'observed':evidence}
    return result


def artifacts(run, descriptor, output, frozen):
    config = json.loads(workspace.checked_path(run,CONFIG).read_text(encoding='utf8'))
    if config != descriptor['configuration']:
        raise ValueError('Elmer configuration differs from its sealed request')
    for path,expected in frozen.items():
        actual = workspace.digest_file(workspace.checked_path(run,path))
        if any(expected[key]!=actual[key] for key in actual):
            raise ValueError('Elmer altered a sealed input: '+path)
    source = descriptor['source']
    native_invocation(run,source)
    files = targets(run,source)
    expected = receipt(run,source,descriptor['execution_id'],descriptor['executable_sha256'],descriptor['worker_sha256'],files)
    if json.loads(workspace.checked_path(run,RECEIPT).read_text(encoding='utf8')) != expected:
        raise ValueError('Elmer has no matching native invocation receipt')
    captured = {path:output['files'][path] for path in files}
    if any({key:captured[path][key] for key in record}!=record for path,record in files.items()):
        raise ValueError('Elmer output changed after capture')
    validate_outputs(run,source,files)
    descriptor['output_files'] = captured
    descriptor['receipt'] = expected
    retained = set(source['files'])
    if source.get('restart',{}).get('format')=='binary':
        retained.add(source['restart']['path']+'.pos')
    descriptor['retired_aliases'] = {path:old for path,old in descriptor['retired_aliases'].items()
        if path not in files and path not in retained}
    return captured
