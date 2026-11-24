"""Bind UDF source, native load evidence and reconstructed integration fields."""
import json

from . import attempt_workspace as workspace, udf_profiles
from .case_file_delta import snapshot,changes,current_source,file_records
from .case_evidence import file_record,case_input_fingerprint
from .logparse import parse_solver_log
from .mpi_artifacts import output_times
from .solve_publication import validate_fields

CONFIG='.vibeflow/prepared/udf.json'
NATIVE='.vibeflow/udf/native.json'
RECEIPT='.vibeflow/udf/receipt.json'
LOG='.vibeflow/udf/native.log'


def evidence(root,run_dir,execution_id):
    log=root/run_dir/'udf-solve.log';parsed=parse_solver_log(log.read_text(encoding='utf8',errors='replace'))
    summary=parsed['summary'];times=output_times(root,run_dir)
    if not summary['finished_cleanly'] or summary['fatal_errors'] or not summary['fields'] or summary['last_time'] is None:
        raise ValueError('UDF integration lacks a clean solve and residuals')
    if not any(abs(float(t)-summary['last_time'])<1e-9 for t in times):raise ValueError('UDF final time has no fields')
    return json.loads(json.dumps({'schema':'vibeflow.udf-solve.v1','execution_id':execution_id,'run_dir':run_dir,
        'case_input_sha256':case_input_fingerprint((root/'0-caseDict/caseDict').read_text(encoding='utf8')),
        'summary':summary,'outputs':[file_record(root,p) for paths in times.values() for p in paths],
        'log':file_record(root,log),'parsed_log':parsed},allow_nan=False))


def artifacts(run,descriptor,output,frozen):
    from .postprocess_artifacts import context
    identity=context(run);configuration=json.loads(workspace.checked_path(run,CONFIG).read_text(encoding='utf8'))
    if configuration!=descriptor['configuration']:raise ValueError('UDF private configuration changed')
    source=descriptor.get('origin_source',descriptor['source']);mode=descriptor['mode'];udf_dir='UDF/'+descriptor['name'];run_dir=descriptor['run_dir']
    artifact_path=udf_dir+'/'+udf_profiles.artifact(configuration)
    if {p:r for p,r in frozen.items() if p!=CONFIG}!=source['files']:raise ValueError('UDF sealed source changed')
    # Compiler caches are outputs; original C/C++/Make inputs, mesh and fields at
    # zero remain source. Only the planned integration control edit is permitted.
    for path,expected in source['files'].items():
        udf_input=(path.startswith(udf_dir+'/') and (
            path.rsplit('/',1)[-1] in {'files','options'} or path.endswith(('.C','.H','.cpp','.h','.c','.hpp','.md')))
            and not path.startswith(udf_dir+'/lnInclude/'))
        scientific=run_dir and path.startswith(run_dir+'/') and path[len(run_dir)+1:].split('/')[0] in {'system','constant','0','0.orig'}
        protected=(path=='0-caseDict/caseDict' or path==udf_dir+'/'+udf_profiles.MANIFEST or udf_input
                   or scientific or mode=='integrate' and path==artifact_path)
        if mode=='integrate' and path==run_dir+'/system/controlDict':
            if workspace.checked_path(run,path).read_bytes()!=configuration['control_text'].encode('utf8'):
                raise ValueError('UDF control differs from prepared integration')
        elif protected and workspace.digest_file(workspace.checked_path(run,path))!={k:expected[k] for k in ('size','sha256')}:
            raise ValueError('UDF modified an original input: '+path)
    native=json.loads(workspace.checked_path(run,NATIVE).read_text(encoding='utf8'))
    for key in ('execution_id','mode','name','run_dir','distro'):
        if native[key]!=descriptor[key]:raise ValueError('UDF native invocation differs')
    if native.get('udf_kind')!=configuration.get('udf_kind'):raise ValueError('UDF native component type differs')
    if (native.get('schema')!='vibeflow.udf-native.v1' or (descriptor['distro']!='native-linux' and native['uid']==0) or not native['openfoam']
            or native['helper_sha256']!=descriptor['native_sha256'] or native['common_native_sha256']!=descriptor['common_native_sha256']):
        raise ValueError('UDF native identity/toolchain differs')
    steps={'build':udf_dir+'/udf-build.log','load':udf_dir+'/udf-load.log'} if mode=='build' else {
        'load':configuration.get('load_log',udf_dir+'/udf-load.log'),'solve':run_dir+'/udf-solve.log'}
    if set(native['steps'])!=set(steps):raise ValueError('UDF missing native stage')
    for step,path in steps.items():
        value=native['steps'][step]
        if value['returncode']!=0 or workspace.digest_file(workspace.checked_path(run,path))!=value['log']:
            raise ValueError('UDF stage transcript changed')
    compiled=workspace.checked_path(run,artifact_path)
    with compiled.open('rb') as stream:
        if stream.read(4)!=b'\x7fELF':raise ValueError('UDF artifact is not ELF')
    proof_key='executable' if configuration.get('udf_kind')==udf_profiles.SOLVER else 'library'
    if workspace.digest_file(compiled)!=native[proof_key]:raise ValueError('UDF native artifact differs from output')
    if mode=='build' and artifact_path not in native['changed']:
        raise ValueError('UDF build did not produce a fresh native artifact')
    if mode=='integrate':
        actual=evidence(run,run_dir,descriptor['execution_id'])
        if json.loads(workspace.checked_path(run,run_dir+'/udf-evidence.json').read_text(encoding='utf8'))!=actual:
            raise ValueError('UDF integration evidence changed')
        validate_fields(run,run_dir,actual,times=output_times(run,run_dir))
    user_files={p:r for p,r in output['files'].items() if p.split('/')[0].casefold()!='.vibeflow'}
    if snapshot(run)['files']!=user_files:raise ValueError('UDF output changed after capture')
    for path,expected in native['changed'].items():
        if {k:user_files[path][k] for k in ('size','sha256')}!=expected:raise ValueError('UDF native result changed')
    receipt=json.loads(workspace.checked_path(run,RECEIPT).read_text(encoding='utf8'))
    records={p:user_files[p] for p in receipt['outputs']};deleted={p:r for p,r in source['files'].items() if p not in user_files}
    if any(p not in records and source['files'].get(p)!=r for p,r in user_files.items()):raise ValueError('UDF receipt omitted output')
    expected={'schema':'vibeflow.udf-publication.v1','case_id':descriptor['case_id'],'job_id':identity['source']['job_id'],
        'attempt_id':identity['attempt_id'],'execution_id':descriptor['execution_id'],'inputs':source,'outputs':records,
        'deleted':deleted,'native':native,'worker_sha256':descriptor['worker_sha256'],
        'log':workspace.digest_file(workspace.checked_path(run,LOG))}
    if receipt!=expected:raise ValueError('UDF receipt differs from this attempt')
    descriptor['output_files']=records;descriptor['deleted_files']=deleted;descriptor['receipt']=receipt
    from .case_query_source import attach
    attach(descriptor,native)
    return records
