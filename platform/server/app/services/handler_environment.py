"""Resolve registered portable Python workers without rewriting sealed inputs."""
import hashlib,json,platform,re,shutil,sys
from pathlib import Path
from . import process_identity_native as identity
from .workflow_state import ExecutionConflict

SCHEMA='vibeflow.handler-environment.v1'
PROGRAMS={'pdf.number':'pdf_number_worker.py','ppt.build':'ppt_worker.py',
          'geometry.preview':'geo_script.py','geometry.export':'geo_script.py',
          'flotherm.convert':'flotherm_convert_worker.py',
          'postprocess.case':'postprocess_worker.py','postprocess.run':'postprocess_worker.py',
          'report.compose':'registered_compose_worker.py',
          'report.compose-generated':'registered_compose_worker.py',
          'report.build':'registered_report_worker.py','mpi.openfoam':'mpi_worker.py',
          'latex.build':'registered_latex_worker.py'}
CONFIGURED_PYTHON={'geometry.preview','geometry.export','postprocess.case','postprocess.run',
                   'report.compose','report.compose-generated','report.build','latex.build'}


def interpreter(key):
    if key in CONFIGURED_PYTHON:
        from .runtime_config import get_runtime_setting
        return 'configured_python',get_runtime_setting('python',sys.executable)
    return 'server_python',sys.executable


def declare(key,spec,descriptor):
    from ..config import get_settings
    settings=get_settings()
    if key not in PROGRAMS or (spec.get('runtime') not in settings.job_execution_hosts
            and key not in settings.job_handler_execution_hosts):return None
    program=Path(__file__).with_name(PROGRAMS[key])
    role,python=interpreter(key)
    args=spec.get('argv')
    if (spec.get('runtime')!='windows' or not isinstance(args,list) or len(args)<2
            or args[:2]!=[python,str(program)]):
        raise ExecutionConflict('evidence_invalid','Portable handler source differs from its registered worker')
    digest=hashlib.sha256(program.read_bytes()).hexdigest()
    if digest!=descriptor.get('worker_sha256'):
        raise ExecutionConflict('evidence_invalid','Portable handler source changed before submission')
    return {'schema':SCHEMA,'origin_host_id':identity.host_id(),'argv_prefix':args[:2],
            'program':program.name,'program_sha256':digest,'interpreter':role}


def local_spec(record,spec,descriptor):
    environment=record.get('environment')
    if environment is None:return spec
    key=record.get('key');name=PROGRAMS.get(key)
    role,python=interpreter(key)
    if (name is None or not isinstance(environment,dict) or set(environment)!=
            {'schema','origin_host_id','argv_prefix','program','program_sha256','interpreter'}
            or environment['schema']!=SCHEMA or environment['program']!=name
            or environment['interpreter']!=role or spec.get('runtime')!='windows'
            or not isinstance(environment['origin_host_id'],str) or not re.fullmatch('[0-9a-f]{64}',environment['origin_host_id'])
            or not isinstance(spec.get('argv'),list) or len(spec['argv'])<2
            or environment['argv_prefix']!=spec['argv'][:2]):
        raise ExecutionConflict('evidence_invalid','Portable handler environment no longer matches its sealed request')
    program=Path(__file__).with_name(name)
    if environment['program_sha256']!=descriptor.get('worker_sha256') or hashlib.sha256(program.read_bytes()).hexdigest()!=environment['program_sha256']:
        raise ExecutionConflict('runtime_unavailable','Execution host has a different registered worker version')
    if not shutil.which(python):
        raise ExecutionConflict('runtime_unavailable','Execution host Python is unavailable')
    if environment['origin_host_id']==identity.host_id() and environment['argv_prefix']!=[python,str(program)]:
        raise ExecutionConflict('runtime_unavailable','Submission host interpreter or worker path changed')
    return {**spec,'argv':[python,str(program),*spec['argv'][2:]]}


async def execution_argv(owner,original):
    from ..db import SessionLocal
    from . import job_execution
    from .step_handlers import require_binding
    from .durable_events import now
    async with SessionLocal() as db:
        job,attempt=await job_execution._owned(db,owner)
        document=json.loads(attempt.input_manifest_json);record=document.get('handler')
        if not record or 'environment' not in record:return original
        await require_binding(db,job,attempt)
        spec=document['execution_spec']
        if spec['argv']!=original:raise ExecutionConflict('evidence_invalid','Launch differs from the original sealed arguments')
        resolved=local_spec(record,spec,json.loads(attempt.publication_json)['descriptor'])
        await job_execution.attempt_event(db,job,attempt,'attempt.execution_environment',{
            'schema':SCHEMA,'handler':record['key'],'original_execution_sha256':record['execution_sha256'],
            'declaration':record['environment'],'host_id':identity.host_id(),
            'resolved_argv':resolved['argv'],'worker_python_version':platform.python_version(),
            'observed_at':now().isoformat()})
        await db.commit()
        return resolved['argv']
