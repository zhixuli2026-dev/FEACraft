"""Authentic serial OpenFOAM checkpoints; native-worker helpers use stdlib only."""
import hashlib,json,math,re
from pathlib import Path
from .case_evidence import file_record,safe_run_dir
from .solve_artifacts import (INVOCATION,LOG,configuration,initial_snapshot,output_times)
from .udf_control import _TOKENS

SCHEMA='vibeflow.solve-checkpoint.v1'
NAME='resume-checkpoint.json'
# The previous validator sealed the same checkpoint record and file digests.
PREVIOUS_VALIDATOR_SHA256='d12e19c6639d879f7745351e8e70c0de91ac6aeadb94778d7f58ff183d98fe6b'


def time_label(value):
    if (not isinstance(value,str) or len(value)>40 or
            not re.fullmatch(r'(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?',value)
            or not math.isfinite(float(value)) or float(value)<=0):
        raise ValueError('Resume requires an explicit positive finite written time label')
    return value


def resumed_control(text,label):
    label=time_label(label)
    tokens=[match for match in _TOKENS.finditer(text) if not match.group().startswith(('//','/*'))]
    changes=[];depth=0;seen=set();skip_until=0
    for index,token in enumerate(tokens):
        if index<skip_until:continue
        value=token.group()
        if value=='{':depth+=1
        elif value=='}':depth-=1
        elif depth==0 and value in {'startFrom','startTime'}:
            if value in seen or index+2>=len(tokens) or tokens[index+2].group()!=';':
                raise ValueError('Resume requires unique literal top-level start controls')
            seen.add(value);selected=tokens[index+1]
            changes.append((selected.start(),selected.end(),'startTime' if value=='startFrom' else label))
            skip_until=index+3
    if seen!={'startFrom','startTime'}:raise ValueError('Resume requires explicit startFrom and startTime controls')
    for start,end,replacement in reversed(changes):text=text[:start]+replacement+text[end:]
    return text.encode('utf8')


def verify_records(root,records):
    root=root.resolve()
    for name,expected in records.items():
        path=root/name
        if not path.resolve(strict=True).is_relative_to(root) or any(parent.is_symlink() for parent in [path,*path.parents] if parent!=root):
            raise ValueError('Checkpoint file is linked or escapes its source')
        actual=file_record(root,path)
        if any(actual[key]!=expected[key] for key in ('size','sha256')):
            raise ValueError('Checkpoint/frozen input changed: '+name)


def require_toolchain(actual,original):
    for key in ('solver','executable','executable_sha256','openfoam','distro','python','worker_sha256','runner_sha256'):
        if not original.get(key) or actual.get(key)!=original[key]:
            raise ValueError('Resume native environment differs from its checkpoint: '+key)


def verify_frozen(root,run_dir,path,*,completed=False):
    run_dir=safe_run_dir(run_dir)
    if path!=run_dir+'/'+NAME:raise ValueError('Unexpected checkpoint record path')
    data=json.loads((root/path).read_text(encoding='utf8'))
    if data.get('schema')!=SCHEMA or data.get('run_dir')!=run_dir or time_label(data['label'])!=data['label'] or float(data['label'])!=data['time']:
        raise ValueError('Checkpoint identity is damaged')
    if data.get('validator_sha256') not in (hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                                            PREVIOUS_VALIDATOR_SHA256):
        raise ValueError('Checkpoint validator changed after preparation')
    cfg=configuration(root,run_dir,resume_time=data['time'])
    if cfg!=data['configuration'] or not 0<data['time']<cfg['end_time']:
        raise ValueError('Checkpoint start time/solver/control configuration is inconsistent')
    verify_records(root,data['frozen_files']);verify_checkpoint_files(root,data)
    times=output_times(root,run_dir)
    if not completed and set(times)!=set(data['labels']):raise ValueError('Resume inputs contain an unselected output time')
    if any(float(label)>data['time'] for label in data['labels']):raise ValueError('Checkpoint includes future fields')
    return data


def verify_checkpoint_files(root,checkpoint):
    verify_records(root,checkpoint['files'])


def inspect_source(prefix,job,attempt,descriptor,label):
    """Read captured outputs, never current-case files as original evidence."""
    from .. import storage
    from . import attempt_workspace as workspace
    if label!='latest':
        label=time_label(label)
    run_dir=safe_run_dir(descriptor['run_dir'])
    document=json.loads(attempt.input_manifest_json);manifest=document['workspace']
    output=json.loads(attempt.output_manifest_json)
    if (output.get('schema')!='vibeflow.attempt-outputs.v1' or output.get('input_attempt_id')!=attempt.id
            or output.get('run_path')!=manifest['run_path'] or output.get('dependency_error')):
        raise ValueError('Checkpoint has no complete bound runtime output inventory')
    from .attempt_objects import captured_output_view,sealed_input_view
    inputs=sealed_input_view(prefix,manifest)
    view=None
    try:
        run=None if storage._use_s3() else workspace.checked_path(storage._fs_path(prefix,'.'),manifest['run_path'])
        view=captured_output_view(prefix,manifest,output,run)
        view.input_view=inputs
        return inspect_captured(job,attempt,descriptor,label,inputs.root,view)
    except BaseException:
        if view is not None:view.cleanup()
        else:inputs.cleanup()
        raise


def inspect_captured(job,attempt,descriptor,label,inputs,view):
    from . import attempt_workspace as workspace
    from .logparse import parse_solver_log
    run=view.root;run_dir=descriptor['run_dir']
    manifest=json.loads(attempt.input_manifest_json)['workspace'];output=json.loads(attempt.output_manifest_json)
    # Verify the actual captured inventory, including mesh, initial fields and
    # log; a hash in an invocation JSON alone is not checkpoint evidence.
    records=workspace.inventory(run,sorted(p.name for p in run.iterdir()))
    if records!=output['files']:raise ValueError('Captured solver outputs have changed or are missing')
    verify_records(run,manifest['files'])
    invocation=json.loads((run/run_dir/INVOCATION).read_text(encoding='utf8'))
    prior=verify_frozen(inputs,run_dir,run_dir+'/'+NAME) if descriptor['handler']=='solve.resume.v1' else None
    expected=initial_snapshot(inputs,run_dir,descriptor['execution_id'],resume_time=prior['time'] if prior else None)
    if (invocation.get('schema')!='vibeflow.solve-invocation.v1' or invocation.get('state')!='prepared'
            or invocation.get('snapshot')!=expected or invocation.get('resume')!=prior):
        raise ValueError('Partial solver invocation does not match original sealed inputs')
    toolchain=invocation['toolchain']
    from .solve_worker import __file__ as worker_file
    from . import mesh_worker
    if (toolchain.get('schema')!='vibeflow.solve-toolchain.v1' or toolchain.get('execution_id')!=descriptor['execution_id']
            or toolchain.get('solver')!=expected['configuration']['solver'] or toolchain.get('distro')!=descriptor['distro']
            or toolchain.get('worker_sha256')!=descriptor['worker_sha256']
            or toolchain['worker_sha256']!=hashlib.sha256(Path(worker_file).read_bytes()).hexdigest()
            or toolchain.get('runner_sha256')!=hashlib.sha256(Path(mesh_worker.__file__).read_bytes()).hexdigest()
            or not toolchain.get('openfoam') or not re.fullmatch('[0-9a-f]{64}',toolchain.get('executable_sha256',''))):
        raise ValueError('Checkpoint solver/toolchain is missing, changed or unsupported')
    parsed=parse_solver_log((run/run_dir/LOG).read_text(encoding='utf8',errors='replace'))
    times=output_times(run,run_dir)
    label,chosen=verified_times(run,run_dir,label,times,parsed,expected['configuration'])
    selected={path.relative_to(run).as_posix():records[path.relative_to(run).as_posix()]
        for paths in chosen.values() for path in paths}
    control=run_dir+'/system/controlDict';generated=resumed_control((inputs/control).read_text(encoding='utf8'),label)
    frozen={name:record for name,record in manifest['files'].items() if name not in selected and name not in {control,run_dir+'/'+NAME}}
    frozen[control]={'size':len(generated),'sha256':hashlib.sha256(generated).hexdigest(),'executable':False}
    cfg={**expected['configuration'],'start_time':float(label)}
    checkpoint={'schema':SCHEMA,'run_dir':run_dir,'label':label,'time':float(label),'labels':sorted(chosen,key=float),
        'source':{'job_id':job.id,'attempt_id':attempt.id,'case_id':job.case_id,'version':attempt.version,
            'input_sha256':hashlib.sha256(attempt.input_manifest_json.encode()).hexdigest(),
            'output_sha256':hashlib.sha256(attempt.output_manifest_json.encode()).hexdigest()},
        'configuration':cfg,'toolchain':toolchain,'files':selected,'frozen_files':frozen,
        'validator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'source_log':{'path':run_dir+'/'+LOG,**records[run_dir+'/'+LOG]}}
    if view.cache is not None:
        checkpoint['archive_source']={'kind':'verified_object_outputs','receipt':output['object_store']}
    return checkpoint,generated,view


def verified_times(run,run_dir,requested,times,parsed,configuration):
    from .solve_publication import validate_fields

    if not parsed['times'] or not parsed['summary']['fields']:
        raise ValueError('Checkpoint has no native solver time history')
    if any(not math.isfinite(value) for value in parsed['times']):
        raise ValueError('Checkpoint log has non-finite times')
    latest_log=max(parsed['times'])
    end=configuration['end_time']
    evidence={'summary':parsed['summary'],'configuration':configuration}

    def validate(label):
        # Carry earlier written times too, preserving multistep history.
        chosen={name:paths for name,paths in times.items() if float(name)<=float(label)}
        validate_fields(run,run_dir,evidence,times=chosen)
        return label,chosen

    if requested!='latest':
        if requested not in times or not 0<float(requested)<end or float(requested)>latest_log:
            raise ValueError('Selected checkpoint is not a written native time before the requested end')
        return validate(requested)

    start=configuration['start_time']
    candidates=sorted((label for label in times if start<float(label)<end and float(label)<=latest_log),
                      key=float)
    if candidates:
        try:
            return validate(candidates[-1])
        except (ValueError,OSError):
            pass
    # A cancelled native write can leave an incomplete final time. Search
    # prefixes of the authenticated capture, retaining the longest prefix
    # whose fields and mesh all pass the usual native validation.
    low,high=0,len(candidates)-1
    verified=None
    while low<high:
        middle=(low+high)//2
        try:
            verified=validate(candidates[middle])
        except (ValueError,OSError):
            high=middle
        else:
            low=middle+1
    if verified is None:
        raise ValueError('No complete written checkpoint after the original start time')
    return verified
