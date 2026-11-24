"""Case-relative CCX dependencies and fresh, owned solver output records.

Path rules follow the 2.23 readinput/includefilename and restart readers:
https://dhondt.de/ccx_2.23.src.tar.bz2 . Includes resolve from the process cwd.
"""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from . import attempt_workspace as workspace
from .compose_artifacts import existing,record

CONFIG='.vibeflow/prepared/calculix.json'
RECEIPT='.vibeflow/calculix/receipt.json'
LOG='.vibeflow/calculix/solver.log'
ELEMENT_TYPES = {
    'line': {'T3D2', 'T3D3'},
    'tri': {'CPS3', 'CPS6', 'CPE3', 'CPE6', 'CAX3', 'CAX6'},
    'quad': {'CPS4', 'CPS4R', 'CPS8', 'CPS8R', 'CPE4', 'CPE4R', 'CPE8', 'CPE8R',
             'CAX4', 'CAX4R', 'CAX8', 'CAX8R'},
    'tet': {'C3D4', 'C3D10', 'C3D10T'},
    'hex': {'C3D8', 'C3D8I', 'C3D8R', 'C3D20', 'C3D20R'},
    'prism': {'C3D6', 'C3D15'},
    'shell': {'S3', 'S4', 'S4R', 'S6', 'S8', 'S8R'},
    'beam': {'B31', 'B31R', 'B32', 'B32R'},
}


def relative(value):
    path=workspace.canonical_path(value.strip('"'))
    if path.split('/')[0].casefold()=='.vibeflow':raise ValueError('CalculiX cannot select runtime metadata')
    return path


def input_path(value):
    path=relative(value)
    if not path.lower().endswith('.inp'):raise ValueError('CalculiX input must be .inp')
    if len(path.encode('utf8'))>125:raise ValueError('CalculiX input path exceeds its native filename limit')
    return path


def publication_channel(value):
    canonical = input_path(value).casefold().encode()
    return 'calculix:' + hashlib.sha256(canonical).hexdigest()[:40]


def native_input(path, managed=False):
    # CCX's C reader accepts spaces, but its Fortran output routines truncate
    # the job name at the first blank. Use a private, deterministic job stem.
    return '.vibeflow/calculix/native/model.inp' if managed or any(c.isspace() for c in path) else path


def native_digest(source):
    managed = source.get('numerical') or source.get('mechanical_loads') or source.get('elasticity') or source.get('contact')
    return managed['native_input_sha256'] if managed is not None else None


def render_input(source, original):
    if source.get('contact'):
        from .calculix_contact import render
        original = render(original,source['contact']['interfaces'])
    if source.get('elasticity'):
        from .calculix_elasticity import render
        original = render(original,source['elasticity'])
    if source.get('mechanical_loads'):
        from .calculix_boundary_inputs import render
        original = render(original,source['mechanical_loads']['loads'])
    if source.get('numerical'):
        from .calculix_numerical import render
        original = render(original,source['numerical'])
    return original


def prepare_element_families(root, source, cards):
    from .case_mesh_schema import element_families
    from .casedict import CASE_DICT_PATH, parse_casedict

    try:
        raw = workspace.checked_path(root, CASE_DICT_PATH).read_bytes()
        parsed = parse_casedict(raw.decode('utf8'))
    except (FileNotFoundError, UnicodeDecodeError, ValueError):
        return
    meta, mesh = parsed.get('caseMeta'), parsed.get('mesh')
    if (not isinstance(meta,dict) or meta.get('discipline')!='solid'
            or not isinstance(mesh,dict) or 'elementFamilies' not in mesh):
        return
    allowed = element_families(mesh['elementFamilies'])
    supported = set().union(*(ELEMENT_TYPES.get(family,set()) for family in allowed))
    if 'wedge' in allowed:
        supported.update(ELEMENT_TYPES['prism'])
    types = set()
    for card in cards:
        keyword, *parameters = card.upper().split(',')
        if keyword == '*ELEMENT':
            names = [part.removeprefix('TYPE=') for part in parameters if part.startswith('TYPE=')]
            if len(names)!=1 or not names[0]:
                raise ValueError('CalculiX *ELEMENT requires a unique TYPE')
            types.add(names[0])
    if not types:
        raise ValueError('CalculiX element family declaration requires native *ELEMENT cards')
    if types - supported:
        raise ValueError('CalculiX native element types outside mesh.elementFamilies: '+', '.join(sorted(types-supported)))
    source['element_families'] = {'declared':sorted(allowed), 'native_types':sorted(types)}
    source['files'][CASE_DICT_PATH] = record(raw)


def dependencies(root,path,*,pressure_transfer=None):
    from . import calculix_boundary_inputs, calculix_elasticity, calculix_numerical

    path=input_path(path);files={};absent=[];outputs=set();active=set();cards=[]
    declaration=calculix_boundary_inputs.selected(root,pressure_transfer)
    native_lines=[] if declaration is not None else None
    def include(name,depth=0):
        name=relative(name)
        if depth>9 or name.casefold() in active:raise ValueError('CalculiX include cycle or nesting exceeds nine levels')
        target=workspace.checked_path(root,name)
        if target.stat().st_size>16*1024*1024:raise ValueError('CalculiX include exceeds 16 MiB')
        data=target.read_bytes();files[name]=record(data);active.add(name.casefold())
        # Uploaded decks and dependency filenames use UTF-8.
        text=data.decode('utf8')
        for raw in text.splitlines():
            line=raw.replace(' ','').replace('\t','')
            if not line or line.startswith('**'):continue
            if line.upper().startswith('*INCLUDE'):
                if '=' not in line:raise ValueError('CalculiX include has no filename')
                value=line.split('=',1)[1]
                if value.startswith('"'):
                    if '"' not in value[1:]:raise ValueError('CalculiX include filename has no closing quote')
                    value=value.split('"')[1]
                include(value,depth+1)
            else:
                if line.startswith('*'):cards.append(line)
                if native_lines is not None:native_lines.append(line)
        active.remove(name.casefold())
    include(path)
    def file(name,*,optional=False):
        name=relative(name)
        try:files[name]=workspace.digest_file(workspace.checked_path(root,name))
        except FileNotFoundError:
            if not optional:raise
            absent.append(name)
    stem=path[:-4]
    # CCX probes its eigenvalue file even before reading the model cards.
    file(stem+'.eig',optional=True)
    for line in cards:
        keyword=line.split(',',1)[0].upper()
        parameters={part.split('=',1)[0].upper():part.split('=',1)[1] for part in line.split(',')[1:] if '=' in part}
        if keyword=='*RESTART' and 'READ' in line.upper().split(',')[1:]:file(stem+'.rin')
        for key in ('STIFFNESSFILE','MASSFILE'):
            if key in parameters:file(parameters[key])
        if 'INPUT' in parameters:
            name=relative(parameters['INPUT'])
            # Explicit INPUT is the exact filename in readview/readfrd; only
            # the implicit view-factor filename gains the .vwf suffix.
            file(name)
        if keyword=='*VIEWFACTOR':
            if 'READ' in line.upper().split(',')[1:] and 'INPUT' not in parameters:file(stem+'.vwf')
            if 'OUTPUT' in parameters:outputs.add(relative(parameters['OUTPUT']))
    source={'input':path,'files':dict(sorted(files.items())),'absent':sorted(set(absent)),
        'extra_outputs':sorted(outputs),'mutable_inputs':[stem+'.eig']}
    prepare_element_families(root,source,cards)
    if pressure_transfer is not None:
        source['pressure_transfer']=pressure_transfer
    from .calculix_contact import prepare
    prepare(root,source,cards)
    calculix_elasticity.prepare(root,source,cards)
    if declaration is not None:
        calculix_boundary_inputs.prepare(root,source,cards,native_lines,declaration)
        if 'elasticity' in source:
            source['elasticity']['native_input_sha256']=source['mechanical_loads']['native_input_sha256']
    calculix_numerical.prepare(root,source,cards)
    if 'numerical' in source:
        native=render_input(source,workspace.checked_path(root,path).read_bytes())
        digest=hashlib.sha256(native).hexdigest()
        for key in ('numerical','contact','mechanical_loads','elasticity'):
            if key in source:
                source[key]['native_input_sha256']=digest
    return source


def managed(path,source):
    stem=source['input'][:-4]
    return ((str(PurePosixPath(path).parent)==str(PurePosixPath(stem).parent)
        and PurePosixPath(path).name.startswith(PurePosixPath(stem).name+'.') and path!=source['input']
        and (path not in source['files'] or path in source['mutable_inputs'])) or path in source['extra_outputs'])


def targets(root,source):
    stem=source['input'][:-4];folder=root/PurePosixPath(stem).parent
    paths=[p.relative_to(root).as_posix() for p in folder.iterdir() if p.name.startswith(PurePosixPath(stem).name+'.')]
    paths.extend(source['extra_outputs'])
    return {path:record(data) for path in sorted(set(paths)) if managed(path,source) and (data:=existing(root,path)) is not None}


def current_source(root,descriptor):
    original=descriptor['source']
    observed=dependencies(root,descriptor['input'],pressure_transfer=original.get('pressure_transfer'))
    for path in original['mutable_inputs']:
        after=descriptor.get('output_files',{}).get(path)
        if after is not None and observed['files'].get(path)=={k:after[k] for k in ('size','sha256')}:
            if path in original['files']:observed['files'][path]=original['files'][path]
            else:
                observed['files'].pop(path,None)
                observed['absent']=sorted(set(observed['absent'])|{path})
    return observed


def validate_outputs(root,source,files):
    stem=source['input'][:-4]
    for suffix in ('.dat','.sta','.cvg','.frd'):
        if stem+suffix not in files:raise ValueError('CalculiX did not produce '+suffix)
    frd=workspace.checked_path(root,stem+'.frd').read_bytes()
    # CCX always writes this final record, also for binary or no-field FRDs.
    if not frd.rstrip().endswith(b'9999'):raise ValueError('CalculiX FRD is incomplete')
    status=workspace.checked_path(root,stem+'.sta').read_text(encoding='ascii',errors='replace')
    if 'STEP' not in status.upper():raise ValueError('CalculiX status file has no native header')
    log=workspace.checked_path(root,LOG).read_text(encoding='utf8',errors='replace')
    if '*ERROR' in log.upper() or 'Job finished' not in log:
        raise ValueError('CalculiX did not report successful native completion')
    if 'numerical' in source:
        from .calculix_numerical import observed
        return observed(root,source,log)


def artifacts(run,descriptor,output,frozen):
    from .postprocess_artifacts import context
    identity=context(run)
    config=json.loads(workspace.checked_path(run,CONFIG).read_text(encoding='utf8'))
    if config!=descriptor['configuration']:raise ValueError('CalculiX configuration changed')
    for path,expected in frozen.items():
        if path in descriptor['source']['mutable_inputs']:continue
        actual=workspace.digest_file(workspace.checked_path(run,path))
        if any(expected[k]!=actual[k] for k in actual):raise ValueError('CalculiX modified a frozen input: '+path)
    files={path:value for path,value in output['files'].items() if managed(path,descriptor['source'])}
    for path,expected in files.items():
        actual=workspace.digest_file(workspace.checked_path(run,path))
        if any(expected[k]!=actual[k] for k in actual):raise ValueError('CalculiX output changed after capture')
    receipt=json.loads(workspace.checked_path(run,RECEIPT).read_text(encoding='utf8'))
    expected={'schema':'vibeflow.calculix-result.v1','case_id':descriptor['case_id'],
        'job_id':identity['source']['job_id'],'attempt_id':identity['attempt_id'],
        'execution_id':descriptor['execution_id'],'input':descriptor['input'],'returncode':0,
        'executable_sha256':descriptor['executable_sha256'],'worker_sha256':descriptor['worker_sha256'],
        'inputs':descriptor['source']['files'],'outputs':{p:{k:r[k] for k in ('size','sha256')} for p,r in files.items()},
        'log':workspace.digest_file(workspace.checked_path(run,LOG))}
    source=descriptor['source']
    if 'pressure_transfer' in source:
        from .calculix_pressure_transfer import verify_sealed

        verify_sealed(run,source['pressure_transfer'])
    digest=native_digest(source)
    if digest is not None:
        native=workspace.checked_path(run,native_input(descriptor['input'],True))
        if workspace.digest_file(native)['sha256']!=digest:
            raise ValueError('CalculiX native input changed after launch')
    if 'contact' in source:
        expected['native_contact_sha256']=source['contact']['native_input_sha256']
    if 'mechanical_loads' in source:
        expected['native_loads_sha256']=digest
    if 'elasticity' in source:
        expected['native_elasticity_sha256']=digest
    if 'element_families' in source:
        expected['element_families']=source['element_families']
    observed=validate_outputs(run,source,files)
    if observed is not None:
        expected['numerical']={**source['numerical'],'observed':observed}
    if receipt!=expected:raise ValueError('CalculiX has no matching native invocation receipt')
    descriptor['output_files']=files;descriptor['receipt']=receipt
    return files
