"""MPI file deltas and independently checked reconstructed OpenFOAM fields."""
import json
import math
import re

from . import attempt_workspace as workspace
from .case_evidence import case_input_fingerprint,file_record
from .case_file_delta import snapshot,changes,current_source,file_records
from .solve_publication import validate_fields
from .logparse import parse_solver_log
from .native_openfoam_residuals import has_nonfinite_telemetry
from .openfoam_numerical_inputs import SOLUTION, observed_configuration

CONFIG='.vibeflow/prepared/mpi.json'
RECEIPT='.vibeflow/mpi/receipt.json'
LOG='.vibeflow/mpi/native.log'
NATIVE='.vibeflow/mpi/native.json'
TIME=re.compile(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?')


def is_input(path,run_dir):
    if not path.startswith(run_dir+'/'):return False
    return path[len(run_dir)+1:].split('/')[0] in {'system','constant','0','0.orig'}


def output_times(root,run_dir):
    values={}
    for directory in sorted((root/run_dir).iterdir()):
        if directory.is_dir() and TIME.fullmatch(directory.name) and float(directory.name)!=0:
            if not math.isfinite(float(directory.name)):raise ValueError('Non-finite MPI output time')
            values[directory.name]=sorted(p for p in directory.rglob('*') if p.is_file())
    if len({float(t) for t in values})!=len(values):raise ValueError('Ambiguous MPI time directories')
    return values


def evidence(root,run_dir,execution_id,configuration=None):
    log=root/run_dir/'mpi-solve.log'
    transcript=log.read_text(encoding='utf8',errors='replace')
    parsed=parse_solver_log(transcript)
    summary=parsed['summary']
    if not summary['finished_cleanly'] or summary['fatal_errors'] or not summary['fields'] or summary['last_time'] is None:
        raise ValueError('MPI solve lacks a clean finished log and field residuals')
    times=output_times(root,run_dir)
    if not times or not any(math.isclose(float(t),float(summary['last_time']),abs_tol=1e-9,rel_tol=0) for t in times):
        raise ValueError('MPI final log time has no reconstructed fields')
    inputs=[file_record(root,p) for p in sorted((root/run_dir).rglob('*')) if p.is_file() and is_input(p.relative_to(root).as_posix(),run_dir)]
    value={'schema':'vibeflow.mpi-result.v1','execution_id':execution_id,'run_dir':run_dir,
        'case_input_sha256':case_input_fingerprint((root/'0-caseDict/caseDict').read_text(encoding='utf8')),
        'inputs':inputs,'summary':summary,'outputs':[file_record(root,p) for paths in times.values() for p in paths],'log':file_record(root,log)}
    numbers=parsed['times']+[p[k] for series in parsed['residuals_detail'].values() for p in series for k in ('initial','final')]
    if not numbers or not all(math.isfinite(v) for v in numbers):raise ValueError('MPI produced invalid solver times/residuals')
    value['parsed_log']=json.loads(json.dumps(parsed,allow_nan=False))
    settings=configuration or {}
    numerics=settings.get('numerics')
    if numerics:
        if has_nonfinite_telemetry(transcript):
            raise ValueError('MPI solver log contains non-finite residuals or times')
        value['configuration']=numerics
        value.update(observed_configuration(numerics,transcript,parsed['residuals_detail']))
    selection=settings.get('decomposition')
    if selection is not None:
        decompose_log=root/run_dir/'mpi-decompose.log'
        observations=re.findall(r'(?m)^Decomposition method (\S+) \[(\d+)\]\s',decompose_log.read_text(encoding='utf8'))
        if not observations or set(observations)!={(selection['method'],str(configuration['subdomains']))}:
            raise ValueError('MPI decomposition log differs from its sealed method and rank count')
        value['decomposition']={**selection,'subdomains':configuration['subdomains'],'log':file_record(root,decompose_log)}
    return value


def artifacts(run,descriptor,output,frozen):
    from .postprocess_artifacts import context
    identity=context(run);config=json.loads(workspace.checked_path(run,CONFIG).read_text(encoding='utf8'))
    if config!=descriptor['configuration']:raise ValueError('MPI private configuration changed')
    source=descriptor.get('origin_source',descriptor['source'])
    if {p:r for p,r in frozen.items() if p!=CONFIG}!=source['files']:raise ValueError('MPI sealed inputs differ from preparation')
    for path,expected in source['files'].items():
        if path=='0-caseDict/caseDict' or is_input(path,descriptor['run_dir']):
            if workspace.digest_file(workspace.checked_path(run,path))!={k:expected[k] for k in ('size','sha256')}:
                raise ValueError('MPI modified an original engineering input: '+path)
    if config['decompose_text'] is not None:
        path=workspace.checked_path(run,descriptor['run_dir']+'/system/decomposeParDict')
        if path.read_bytes()!=config['decompose_text'].encode('utf8'):raise ValueError('MPI generated decomposition changed')
    if 'solution_text' in config:
        path=workspace.checked_path(run,descriptor['run_dir']+'/'+SOLUTION)
        if path.read_bytes()!=config['solution_text'].encode('utf-8'):raise ValueError('MPI generated fvSolution changed')
    native=json.loads(workspace.checked_path(run,NATIVE).read_text(encoding='utf8'))
    for key in ('execution_id','distro','run_dir','solver','subdomains'):
        if native[key]!=descriptor[key]:raise ValueError('MPI native receipt identity differs')
    if (native.get('schema')!='vibeflow.mpi-native.v1' or native['helper_sha256']!=descriptor['native_sha256'] or not native['openfoam']
            or (descriptor['distro']!='native-linux' and native['uid']==0)):
        raise ValueError('MPI toolchain does not belong to this invocation')
    if set(native['processors'])!={'processor'+str(i) for i in range(descriptor['subdomains'])}:
        raise ValueError('MPI has an incomplete processor set')
    if set(native['steps'])!={'decompose','solve','reconstruct'} or any(s['returncode']!=0 for s in native['steps'].values()):
        raise ValueError('MPI pipeline did not complete all stages')
    if 'force' in config and ('-force' in native['steps']['decompose']['argv']) is not config['force']:
        raise ValueError('MPI decomposition flags differ from its sealed configuration')
    expected_environment={'PMIX_MCA_gds':config.get('pmix_gds')}
    if descriptor['distro']=='native-linux' and native['uid']==0:
        expected_environment.update(OMPI_ALLOW_RUN_AS_ROOT='1',OMPI_ALLOW_RUN_AS_ROOT_CONFIRM='1')
    if 'pmix_gds' in config and (config['pmix_gds']!='hash' or
            native['steps']['solve'].get('environment')!=expected_environment):
        raise ValueError('MPI PMIx backend differs from its sealed configuration')
    for step,label in [('decompose','mpi-decompose.log'),('solve','mpi-solve.log'),('reconstruct','mpi-reconstruct.log')]:
        if workspace.digest_file(workspace.checked_path(run,descriptor['run_dir']+'/'+label))!=native['steps'][step]['log']:
            raise ValueError('MPI pipeline transcript changed')
    observed=snapshot(run)
    user_files={p:r for p,r in output['files'].items() if p.split('/')[0].casefold()!='.vibeflow'}
    if observed['files']!=user_files:raise ValueError('MPI output changed after capture')
    for path,expected in native['changed'].items():
        if {k:user_files[path][k] for k in ('size','sha256')}!=expected:raise ValueError('MPI native output changed')
    actual=evidence(run,descriptor['run_dir'],descriptor['execution_id'],config)
    saved=json.loads(workspace.checked_path(run,descriptor['run_dir']+'/mpi-evidence.json').read_text(encoding='utf8'))
    if saved!=actual:raise ValueError('MPI evidence differs from reconstructed fields and logs')
    validate_fields(run,descriptor['run_dir'],actual,times=output_times(run,descriptor['run_dir']))
    receipt=json.loads(workspace.checked_path(run,RECEIPT).read_text(encoding='utf8'))
    records={p:user_files[p] for p in receipt['outputs']}
    deleted={p:r for p,r in source['files'].items() if p not in user_files}
    for path,record in user_files.items():
        if path not in records and source['files'].get(path)!=record:raise ValueError('MPI receipt omitted changed files')
    expected={'schema':'vibeflow.mpi-publication.v1','case_id':descriptor['case_id'],
        'job_id':identity['source']['job_id'],'attempt_id':identity['attempt_id'],'execution_id':descriptor['execution_id'],
        'inputs':source,'outputs':records,'deleted':deleted,'native':native,'worker_sha256':descriptor['worker_sha256'],
        'log':workspace.digest_file(workspace.checked_path(run,LOG))}
    if receipt!=expected:raise ValueError('MPI receipt does not match the owned invocation')
    descriptor['output_files']=records;descriptor['deleted_files']=deleted;descriptor['receipt']=receipt
    from .case_query_source import attach
    attach(descriptor,native)
    return records
