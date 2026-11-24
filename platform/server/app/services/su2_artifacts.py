"""SU2 8.5 configuration dependencies and owned native result receipts."""
import json
from pathlib import PurePosixPath
import re

from . import attempt_workspace as workspace, su2_numerical
from .compose_artifacts import existing,record

CONFIG='.vibeflow/prepared/su2.json'
RECEIPT='.vibeflow/su2/receipt.json'
LOG='.vibeflow/su2/solver.log'
INPUT_DEFAULTS={'ACTDISK_FILENAME':'actdiskinput.dat','INLET_FILENAME':'inlet.dat',
    'BEM_PROP_FILENAME':'prop_geom_alfclcd_data.txt','SOLUTION_FILENAME':'solution',
    'SOLUTION_ADJ_FILENAME':'solution_adj','DV_FILENAME':'surface_positions.dat',
    'DV_UNORDERED_SENS_FILENAME':'unordered_sensitivity.dat','DV_SENS_FILENAME':'surface_sensitivity.dat',
    'FEA_FILENAME':'default_element_properties','REFERENCE_GEOMETRY_FILENAME':'reference_geometry',
    'PRESTRETCH_FILENAME':'prestretch_file'}
OUTPUT_DEFAULTS={'MESH_OUT_FILENAME':'mesh_out','CONV_FILENAME':'history','BREAKDOWN_FILENAME':'forces_breakdown.dat',
    'RESTART_FILENAME':'restart','RESTART_ADJ_FILENAME':'restart_adj','VOLUME_FILENAME':'vol_solution',
    'VOLUME_ADJ_FILENAME':'adj_vol_solution','GRAD_OBJFUNC_FILENAME':'of_grad','VALUE_OBJFUNC_FILENAME':'of_func',
    'SURFACE_FILENAME':'surface','SURFACE_ADJ_FILENAME':'surface_adjoint','SURFACE_SENS_FILENAME':'surface_sens',
    'VOLUME_SENS_FILENAME':'volume_sens','HESS_OBJFUNC_FILENAME':'of_hess.dat','TOPOL_OPTIM_OUTFILE':'element_derivatives.dat'}


def relative(value):
    path=workspace.canonical_path(value)
    if path.split('/')[0].casefold()=='.vibeflow':raise ValueError('SU2 cannot select runtime metadata')
    return path


def input_path(value):
    path=relative(value)
    if not path.lower().endswith('.cfg'):raise ValueError('SU2 input must be .cfg')
    return path


def parse(data):
    options={};pending=''
    for raw in data.decode('utf-8-sig').splitlines():
        line=raw.split('%',1)[0].strip()
        if not line:continue
        if pending and '=' in line:raise ValueError('SU2 statement follows a continuation character')
        if '\\' in line:
            pending+=line.split('\\',1)[0]+' ';continue
        line=pending+line;pending=''
        if '=' not in line:raise ValueError('SU2 configuration statement has no equals sign')
        key,value=line.split('=',1);key=key.strip().upper()
        if not re.fullmatch('[A-Z][A-Z0-9_]*',key) or key in options:raise ValueError('Invalid or duplicate SU2 option: '+key)
        values=[v for v in re.split(r'[ (){}:,\s]+',value) if v]
        if not values:raise ValueError('Empty SU2 option: '+key)
        options[key]=values
    if pending:raise ValueError('Unfinished SU2 continuation')
    return options


def single(options,key,default):
    values=options.get(key,[default])
    if len(values)!=1:raise ValueError('SU2 option requires one filename/value: '+key)
    return values[0]


def family(path,base):
    path=PurePosixPath(path);base=PurePosixPath(base)
    if path.parent!=base.parent:return False
    # GetFilename strips the requested extension, then adds zone/instance/time
    # suffixes before that extension. Preserve dots in directory/base names.
    stems={base.name,base.stem} if base.suffix else {base.name}
    return any(re.fullmatch(re.escape(stem)+r'(?:_\d+)*(?:\.[^/]+)?',path.name) for stem in stems)


def select(root,base,is_family):
    if not is_family:return [base] if existing(root,base) is not None else []
    folder=root/PurePosixPath(base).parent
    if not folder.exists():return []
    workspace.checked_path(root,PurePosixPath(base).parent.as_posix()) if PurePosixPath(base).parent.as_posix()!='.' else workspace.ordinary(root)
    return sorted(p.relative_to(root).as_posix() for p in folder.iterdir() if p.is_file() and family(p.relative_to(root).as_posix(),base))


def dependencies(root,path):
    path=input_path(path);files={};configs={};selectors=[];outputs=set();active=set()
    def config(name,inherited=None):
        name=input_path(name)
        if name in active or len(active)>=16:raise ValueError('SU2 configuration cycle or excessive nesting')
        active.add(name);target=workspace.checked_path(root,name)
        if target.stat().st_size>1024*1024:raise ValueError('SU2 configuration exceeds 1 MiB')
        data=target.read_bytes();files[name]=record(data)
        local=parse(data);effective={**(inherited or {}),**local};configs[name]=effective
        children=local.get('CONFIG_LIST',[])
        if children:
            # CMultizoneOutput uses CConfig::GetCaseName, split at the first
            # dot in the driver filename, rather than CONV_FILENAME.
            if name==path:outputs.add(relative(name.split('.',1)[0]))
            if single(effective,'MULTIZONE_MESH','YES')=='YES':
                selectors.append({'base':relative(single(effective,'MESH_FILENAME','mesh')),'family':False,'required':True,'mutable':False})
            for child in children:config(child,effective)
        else:
            selectors.append({'base':relative(single(effective,'MESH_FILENAME','mesh')),'family':False,'required':True,'mutable':False})
            for key,default in INPUT_DEFAULTS.items():
                base=relative(single(effective,key,default));restart=key.startswith('SOLUTION')
                selectors.append({'base':base,'family':restart or not PurePosixPath(base).suffix,
                    'required':key=='SOLUTION_FILENAME' and single(effective,'RESTART_SOL','NO')=='YES',
                    'mutable':restart})
            for auxiliary in effective.get('FILENAMES_INTERPOLATOR',[]):
                selectors.append({'base':relative(auxiliary),'family':False,'required':True,'mutable':False})
        for key,default in OUTPUT_DEFAULTS.items():outputs.add(relative(single(effective,key,default)))
        active.remove(name)
    config(path)
    # Keep optional runtime overrides sealed too, including legacy runtime.dat.
    for name in ('runtime.dat','runtime.cfg'):
        selectors.append({'base':name,'family':False,'required':False,'mutable':False})
    selectors=sorted({json.dumps(s,sort_keys=True):s for s in selectors}.values(),key=lambda s:(s['base'],s['family'],s['required'],s['mutable']))
    for selector in selectors:
        found=select(root,selector['base'],selector['family'])
        if not found and selector['required']:raise FileNotFoundError(selector['base'])
        for name in found:files[name]=workspace.digest_file(workspace.checked_path(root,name))
    result={'input':path,'files':dict(sorted(files.items())),'configurations':configs,'selectors':selectors,'output_bases':sorted(outputs)}
    su2_numerical.prepare(root,result)
    refresh(result)
    return result


def matches_selector(path,selector):
    return family(path,selector['base']) if selector['family'] else path==selector['base']


def refresh(source):
    source['absent']=sorted({s['base'] for s in source['selectors'] if not any(matches_selector(p,s) for p in source['files'])})
    source['mutable_inputs']=sorted(p for p in source['files'] if any(s['mutable'] and matches_selector(p,s) for s in source['selectors'])
        and any(family(p,base) for base in source['output_bases']))


def managed(path,source):
    return (path not in source['configurations'] and (path not in source['files'] or path in source['mutable_inputs'])
        and any(family(path,base) for base in source['output_bases']))


def targets(root,source):
    paths=set()
    for base in source['output_bases']:paths.update(select(root,base,True))
    return {p:record(data) for p in sorted(paths) if managed(p,source) and (data:=existing(root,p)) is not None}


def current_source(root,descriptor):
    observed=dependencies(root,descriptor['input']);original=descriptor['source']
    for path,after in descriptor.get('output_files',{}).items():
        if observed['files'].get(path)=={k:after[k] for k in ('size','sha256')}:
            if path in original['files']:observed['files'][path]=original['files'][path]
            else:observed['files'].pop(path,None)
    for path in descriptor.get('retired_aliases',{}):
        if path in original['files'] and not (root/path).exists():observed['files'][path]=original['files'][path]
    refresh(observed)
    return observed


def validate_outputs(root,source,files):
    if not files:raise ValueError('SU2 produced no fresh configured results')
    log=workspace.checked_path(root,LOG).read_text(encoding='utf8',errors='replace')
    if 'Exit Success (SU2_CFD)' not in log:raise ValueError('SU2 did not report successful completion')
    for path,info in files.items():
        if not managed(path,source):raise ValueError('Unexpected SU2 result path')
        if info['size']==0:raise ValueError('SU2 produced an empty result: '+path)
    if 'numerical' in source:
        return su2_numerical.observed(root,source['numerical'],files)


def artifacts(run,descriptor,output,frozen):
    from .postprocess_artifacts import context
    identity=context(run)
    config=json.loads(workspace.checked_path(run,CONFIG).read_text(encoding='utf8'))
    if config!=descriptor['configuration']:raise ValueError('SU2 configuration changed')
    receipt=json.loads(workspace.checked_path(run,RECEIPT).read_text(encoding='utf8'))
    files={p:output['files'][p] for p in receipt['outputs']}
    for path,expected in frozen.items():
        if path in files and path in descriptor['source']['mutable_inputs']:continue
        actual=workspace.digest_file(workspace.checked_path(run,path))
        if any(expected[k]!=actual[k] for k in actual):raise ValueError('SU2 modified a sealed input: '+path)
    for path,expected in files.items():
        actual=workspace.digest_file(workspace.checked_path(run,path))
        if any(expected[k]!=actual[k] for k in actual):raise ValueError('SU2 output changed after capture')
    expected={'schema':'vibeflow.su2-result.v1','case_id':descriptor['case_id'],'job_id':identity['source']['job_id'],
        'attempt_id':identity['attempt_id'],'execution_id':descriptor['execution_id'],'input':descriptor['input'],'returncode':0,
        'executable_sha256':descriptor['executable_sha256'],'worker_sha256':descriptor['worker_sha256'],
        'inputs':descriptor['source']['files'],'outputs':{p:{k:r[k] for k in ('size','sha256')} for p,r in files.items()},
        'log':workspace.digest_file(workspace.checked_path(run,LOG))}
    observed=validate_outputs(run,descriptor['source'],files)
    if observed is not None:
        expected['numerical']={**descriptor['source']['numerical'],'observed':observed}
    if receipt!=expected:raise ValueError('SU2 has no matching native invocation receipt')
    descriptor['output_files']=files;descriptor['receipt']=receipt
    return files
