"""Original-runner provenance for the pre-upload finalization boundary."""
import asyncio,hashlib,json,os
from . import attempt_workspace as workspace,attempt_output_recovery as outputs,job_execution
from .async_ops import finish_before_cancel
from .durable_events import json_text

KEY='finalizing_source'
EVENT='attempt.finalizing_source_ready'
SCHEMA='vibeflow.finalizing-source.v1'
RECOVERED_SCHEMA='vibeflow.finalizing-source.v2'
RECOVERED_EVENT='attempt.finalizing_source_recovered'
LOG='.vibeflow/finalizing/root.log'
FIELDS={'schema','job_id','attempt_id','input_sha256','worker_sha256','status','exit_code','reason',
    'log_path','source_log_key','log','pre_capture_stages_returned'}


def copy_log(run,path,limit):
    from .node_storage_budget import reserve
    before=workspace.ordinary(path)
    if before.st_size>limit+65536:raise ValueError('Finalization source log exceeds its bounded snapshot allowance')
    target=run
    with reserve(before.st_size):
        for part in LOG.split('/')[:-1]:
            target=target/part;target.mkdir(exist_ok=True);workspace.ordinary(target)
        target=target/LOG.split('/')[-1]
        digest=hashlib.sha256();size=0
        with path.open('rb') as incoming,target.open('xb') as outgoing:
            for chunk in iter(lambda:incoming.read(65536),b''):
                size+=len(chunk)
                if size>before.st_size:raise ValueError('Original finalizing log grew during capture')
                digest.update(chunk);outgoing.write(chunk)
            outgoing.flush();os.fsync(outgoing.fileno())
        after=workspace.ordinary(path)
        if size!=before.st_size or (before.st_dev,before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
            raise ValueError('Original finalizing log changed during capture')
    return {'size':size,'sha256':digest.hexdigest()}


async def capture(owner,run,path,status,code,reason):
    from ..db import SessionLocal
    from ..models import Case
    from .. import storage
    from .execution_budget import current
    if current(owner) is None:return None
    async with SessionLocal() as db:
        job,attempt=await job_execution._owned(db,owner)
        case=await db.get(Case,job.case_id)
        manifest=json.loads(attempt.input_manifest_json)['workspace']
        finalizing = (job.status=='running' and attempt.phase=='finalizing') or (job.status=='cancelled' and attempt.phase=='cancelling')
        if (attempt.worker_id!=job_execution.WORKER_ID or not finalizing
                or path.resolve()!=storage._fs_path(case.s3_prefix,job.log_s3_key).resolve()
                or run.resolve()!=storage._fs_path(case.s3_prefix,manifest['run_path']).resolve()):
            raise ValueError('Finalizing source does not belong to the original runner')
        value={'schema':SCHEMA,'job_id':job.id,'attempt_id':attempt.id,'input_sha256':outputs.digest(attempt.input_manifest_json),
            'worker_sha256':outputs.digest(attempt.worker_identity_json),'status':status,'exit_code':code,'reason':reason,
            'log_path':LOG,'source_log_key':job.log_s3_key,'pre_capture_stages_returned':True}
        limit=json.loads(attempt.input_manifest_json)['execution_spec']['max_log_bytes']
        from . import finalizing_stage
        stage_value=await finalizing_stage.read(db,case,job,attempt)
        stage=finalizing_stage.Stage(owner,case.id,case.s3_prefix,manifest,stage_value) if stage_value is not None and stage_value['supported'] else None
        if not storage._use_s3() and stage is None:return None
    if stage is None:value['log']=await finish_before_cancel(asyncio.to_thread(copy_log,run,path,limit))
    else:
        data=await finish_before_cancel(asyncio.to_thread(finalizing_stage.capture_log_source,path,limit+65536))
        value['log']=await finalizing_stage.write_file(stage,LOG,data,generator='original_finalizing_log')
    return value


def validate(case,job,attempt,output,value):
    prepared=output[outputs.KEY]
    from .job_log_storage import log_key
    archived=output.get('run_path','')+'/'+LOG
    recovered=isinstance(value,dict) and value.get('schema')==RECOVERED_SCHEMA
    if (not isinstance(value,dict) or set(value)!=(FIELDS|{'recovered'} if recovered else FIELDS) or value['schema'] not in {SCHEMA,RECOVERED_SCHEMA}
            or value['job_id']!=job.id or value['attempt_id']!=attempt.id
            or value['input_sha256']!=outputs.digest(attempt.input_manifest_json)
            or value['worker_sha256']!=outputs.digest(attempt.worker_identity_json)
            or value['status']!=prepared['status'] or value['exit_code']!=prepared['exit_code']
            or value['pre_capture_stages_returned'] is not (False if recovered else True) or value['log_path']!=LOG
            or value['source_log_key'] not in {f'.vibeflow/jobs/job-{job.id}.log',log_key(job.id)}
            or job.log_s3_key not in {value['source_log_key'],log_key(job.id),archived}
            or (job.log_s3_key==archived and output.get('object_store',{}).get('state')!='complete' and 'local_store' not in output) or not isinstance(value['reason'],str)
            or len(value['reason'])>80 or any(c not in 'abcdefghijklmnopqrstuvwxyz_0123456789' for c in value['reason'])
            or value['log']!={k:output['files'][LOG][k] for k in ('size','sha256')}):
        raise ValueError('Finalizing source is missing or differs from the original captured log')
    if recovered:
        if (prepared['schema']!=outputs.RECOVERED_SCHEMA or value['recovered']!=output.get('early_recovery')
                or value['recovered'].get('stage_sha256')!=prepared['stage_sha256']):
            raise ValueError('Recovered finalizing source lost its distinct recovery provenance')
    return value


async def record(db,case,job,attempt,output,value):
    if value is None:return
    if attempt.worker_id!=job_execution.WORKER_ID or value['source_log_key']!=job.log_s3_key:
        raise ValueError('Only original runner can attest its original pre-capture log')
    validate(case,job,attempt,output,value)
    output[KEY]=value
    await job_execution.attempt_event(db,job,attempt,EVENT,value)
