"""Original stopped-writer intent before evidence, log and inventory writes.

This is not full drainage, an archive receipt or permission to replay native
work. File plans precede writes; partial private files can resume only when
their exact bytes match the committed plan. Unknown targets are never replaced.
"""
import asyncio,hashlib,json,os,re,uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime,timedelta
from pathlib import Path
from sqlalchemy import select

from .. import storage
from ..db import SessionLocal
from ..models import Case,DurableEvent
from . import attempt_workspace as workspace,attempt_output_recovery as outputs,job_execution
from . import execution_budget,job_worker_identity,process_identity_native as identity
from .async_ops import finish_before_cancel
from .durable_events import append_event,json_text,lock_case_execution,now
from .workflow_state import ExecutionConflict

KEY='finalizing_stage'
RESULT='finalizing_stage_result'
SCHEMA='vibeflow.finalizing-stage.v1'
STARTED='attempt.finalizing_stage_started'
PLANNED='attempt.finalizing_file_planned'
INSTALLED='attempt.finalizing_file_installed'
PLAN='vibeflow.finalizing-file.v1'
LOG_BLOCK_BYTES = 64 * 1024
FIELDS={'schema','case_id','job_id','attempt_id','case_binding_sha256','input_sha256','worker_sha256',
 'process_sha256','children_sha256','stop','usage','files','directories','source_log_key','log','evidence_input','completion','supported','recorded_at'}


def digest(value):return outputs.digest(json_text(value))
def event_id(attempt_id,kind):return str(uuid.uuid5(uuid.UUID(attempt_id),SCHEMA+':'+kind))


@dataclass
class Stage:
    owner:object
    case_id:int
    prefix:str
    manifest:dict
    value:dict
    lease:object=None

    @property
    def sha256(self):return digest(self.value)


def validate(case,job,attempt,value):
    from .upload_storage import binding
    if (not isinstance(value,dict) or set(value)!=FIELDS or value['schema']!=SCHEMA
            or value['case_id']!=case.id or value['job_id']!=job.id or job.case_id!=case.id
            or value['attempt_id']!=attempt.id or attempt.job_id!=job.id
            or value['case_binding_sha256']!=outputs.digest(binding(case))
            or value['input_sha256']!=outputs.digest(attempt.input_manifest_json)
            or value['worker_sha256']!=outputs.digest(attempt.worker_identity_json)
            or value['process_sha256']!=outputs.digest(attempt.process_identity_json)
            or value['children_sha256']!=outputs.digest(attempt.child_processes_json)
            or type(value['supported']) is not bool or not isinstance(value['files'],dict)
            or not isinstance(value['directories'],list) or len(value['files'])>workspace.MAX_FILES
            or value['source_log_key']!=f'.vibeflow/jobs/job-{job.id}.log'):
        raise ValueError('Early finalizing intent changed its original worker, storage or input')
    workspace.require_sealed(job,attempt);outputs.closed(job,attempt,value['stop'])
    from .execution_output_checkpoint import validate_usage
    spec=json.loads(attempt.input_manifest_json)['execution_spec'];validate_usage(value['usage'],spec['max_log_bytes'])
    log=value['log']
    if (not isinstance(log,dict) or set(log)!={'size','sha256'} or type(log['size']) is not int
            or not 0<=log['size']<=spec['max_log_bytes']+65536 or not re.fullmatch('[0-9a-f]{64}',log['sha256'])):
        raise ValueError('Early intent lost its bounded original log')
    completion=value['completion']
    if (not isinstance(completion,dict) or set(completion)!={'status','exit_code','reason'}
            or completion['status'] not in {'succeeded','failed','cancelled','interrupted'}
            or type(completion['exit_code']) is not int or not isinstance(completion['reason'],str)):
        raise ValueError('Early intent lost its original native outcome')
    if completion['status']=='succeeded' and (completion['exit_code']!=0 or value['stop']['root_exit_code']!=0):
        raise ValueError('Early native success lacks actual successful process exit')
    if datetime.fromisoformat(value['recorded_at']).utcoffset()!=timedelta(0):raise ValueError('Early intent requires UTC')
    for path,record in value['files'].items():
        workspace.canonical_path(path)
        if (not isinstance(record,dict) or set(record)!={'size','sha256','executable'} or type(record['size']) is not int or record['size']<0
                or type(record['executable']) is not bool or not re.fullmatch('[0-9a-f]{64}',record['sha256'])):
            raise ValueError('Invalid early output inventory')
    if value['directories']!=sorted(set(value['directories'])):raise ValueError('Invalid early directory inventory')
    for path in value['directories']:workspace.canonical_path(path)
    return value


async def read(db,case,job,attempt):
    value=json.loads(attempt.output_manifest_json).get(KEY)
    if value is None:return None
    validate(case,job,attempt,value)
    event=await db.get(DurableEvent,event_id(attempt.id,'started'))
    if (event is None or event.type!=STARTED or event.case_id!=case.id or event.attempt_id!=attempt.id
            or json.loads(event.payload_json)!={'schema':SCHEMA,'stage_sha256':digest(value)}):
        raise ValueError('Early stage has no exact original durable intent')
    return value


async def read_result(db,case,job,attempt,value):
    result=json.loads(attempt.output_manifest_json).get(RESULT)
    event=await db.get(DurableEvent,event_id(attempt.id,'result'))
    if result is None:
        if event is not None:raise ValueError('Early result history lost its recorded outcome')
        return None
    if (not isinstance(result,dict) or set(result) not in ({'schema','stage_sha256','completion'},{'schema','stage_sha256','completion','log'})
            or result['schema']!=SCHEMA or result['stage_sha256']!=digest(value)
            or event is None or event.type!='attempt.finalizing_stage_result' or event.case_id!=case.id or event.attempt_id!=attempt.id
            or json.loads(event.payload_json)!={'stage_sha256':digest(value),'result_sha256':digest(result)}):
        raise ValueError('Early result has no matching original durable history')
    completion=result['completion']
    if (not isinstance(completion,dict) or set(completion)!={'status','exit_code','reason'}
            or completion['status'] not in {'succeeded','failed','cancelled','interrupted'} or type(completion['exit_code']) is not int
            or not isinstance(completion['reason'],str)):
        raise ValueError('Early result has an invalid original completion')
    return result


async def begin(proc):
    """Only the original runner can bind its pre-evidence native stop/usage."""
    budget=execution_budget.current(proc.owner)
    if not proc.isolated_workspace or budget is None or proc._owned_process is None:return None
    async with SessionLocal() as db:
        job,attempt=await job_execution._owned(db,proc.owner);case=await db.get(Case,job.case_id)
        if (attempt.worker_id!=job_execution.WORKER_ID
                or (attempt.phase!='finalizing' and not (attempt.phase=='cancelling' and job.status=='cancelled'))):
            raise ValueError('Early stage is not original runner finalization')
        stop=proc._owned_process.stop_result
        try:outputs.closed(job,attempt,stop)
        except (ValueError,KeyError,TypeError,AttributeError):return None  # Missing evidence never becomes a stage grant.
        manifest=workspace.require_sealed(job,attempt);input_json=attempt.input_manifest_json
        previous=await read(db,case,job,attempt)
        if previous is not None:return Stage(proc.owner,case.id,case.s3_prefix,manifest,previous)
        prefix=case.s3_prefix
    run,files,directories=await finish_before_cancel(asyncio.to_thread(workspace.inspect_runtime,prefix,manifest,True))
    log=await finish_before_cancel(asyncio.to_thread(workspace.digest_file,proc.log_path))
    if log['size']>budget.max_log_bytes+65536:raise ValueError('Original early log exceeds its bounded allowance')
    from .upload_storage import binding
    async with SessionLocal() as db:
        await lock_case_execution(db,case.id)
        job,attempt=await job_execution._owned(db,proc.owner);case=await db.get(Case,job.case_id,populate_existing=True)
        if attempt.input_manifest_json!=input_json or await read(db,case,job,attempt) is not None:raise ExecutionConflict('state_conflict','原收尾阶段在登记前变化')
        original=job_worker_identity.document(job,attempt)
        if original['process']!=await asyncio.to_thread(identity.observe,os.getpid()):raise ValueError('Only exact original API birth may open early intent')
        value={'schema':SCHEMA,'case_id':case.id,'job_id':job.id,'attempt_id':attempt.id,
            'case_binding_sha256':outputs.digest(binding(case)),'input_sha256':outputs.digest(input_json),
            'worker_sha256':outputs.digest(attempt.worker_identity_json),'process_sha256':outputs.digest(attempt.process_identity_json),
            'children_sha256':outputs.digest(attempt.child_processes_json),'stop':stop,'usage':budget.usage(),
            'files':files,'directories':directories,'source_log_key':job.log_s3_key,'log':log,'evidence_input':proc.evidence_snapshot,
            'completion':{'status':proc.status,'exit_code':proc.exit_code,'reason':proc.failure_reason},
            'supported':proc.on_finish is None and not proc.staging_prefix,'recorded_at':now().isoformat()}
        validate(case,job,attempt,value);output=json.loads(attempt.output_manifest_json);output[KEY]=value
        attempt.output_manifest_json=json_text(output)
        await append_event(db,case.id,STARTED,{'schema':SCHEMA,'stage_sha256':digest(value)},attempt_id=attempt.id,event_id=event_id(attempt.id,'started'))
        await finish_before_cancel(db.commit())
    return Stage(proc.owner,case.id,prefix,manifest,value)


async def require_authority(db,stage):
    job,attempt=await job_execution._owned(db,stage.owner);case=await db.get(Case,job.case_id,populate_existing=True)
    value=await read(db,case,job,attempt)
    if (value!=stage.value or not value['supported'] or case.s3_prefix!=stage.prefix
            or attempt.phase not in {'finalizing','cancelling'} or attempt.write_checkpoint_json!='{}'):
        raise ExecutionConflict('state_conflict','原收尾写入阶段已变化或不支持自动接续')
    actor=job_worker_identity.document(job,attempt)['process']
    if actor!=await asyncio.to_thread(identity.observe,os.getpid()):
        if stage.lease is None:raise ValueError('Early recovery write has no persistent IO ownership')
        from .storage_operations import require_context
        require_context(stage.lease);row=await stage.lease.require_locked(db)
        if row.journal_id!=attempt.id or row.plan_sha256!=stage.sha256:raise ValueError('Early IO belongs to another stage')
    return case,job,attempt


def _file_version(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _log_blocks(path, size):
    from .request_deadline import current

    deadline = current()
    selected = _file_version(workspace.ordinary(path))
    if selected[2] != size:
        raise ValueError('Original log changed length')
    with path.open('rb') as stream:
        if _file_version(os.fstat(stream.fileno())) != selected:
            raise ValueError('Original log was replaced before reading')
        for start in range(0, size, LOG_BLOCK_BYTES):
            if deadline is not None:
                deadline.check()
            length = min(LOG_BLOCK_BYTES, size - start)
            block = stream.read(length)
            if len(block) != length:
                raise ValueError('Original log was truncated')
            yield block
        if stream.read(1) or _file_version(os.fstat(stream.fileno())) != selected:
            raise ValueError('Original log changed during reading')
    if _file_version(workspace.ordinary(path)) != selected:
        raise ValueError('Original log changed during reading')


@dataclass(frozen=True)
class LogSource:
    path: Path
    record: dict
    block_hashes: tuple[bytes, ...]

    def blocks(self):
        with closing(_log_blocks(self.path, self.record['size'])) as blocks:
            for digest, block in zip(self.block_hashes, blocks, strict=True):
                if hashlib.sha256(block).digest() != digest:
                    raise ValueError('Original log differs from its captured bytes')
                yield block


def capture_log_source(path, limit, expected=None):
    size = workspace.ordinary(path).st_size
    if size > limit or (expected is not None and size != expected['size']):
        raise ValueError('Original log exceeds or differs from its captured bound')
    block_hashes = []
    digest = hashlib.sha256()
    for block in _log_blocks(path, size):
        block_hashes.append(hashlib.sha256(block).digest())
        digest.update(block)
    record = {'size': size, 'sha256': digest.hexdigest()}
    if expected is not None and record != expected:
        raise ValueError('Original log changed outside its committed outcome')
    return LogSource(path, record, tuple(block_hashes))


def append_log(stream, source, offset):
    stream.seek(0)
    remaining = offset
    with closing(source.blocks()) as blocks:
        for block in blocks:
            prefix_bytes = min(remaining, len(block))
            if prefix_bytes and stream.read(prefix_bytes) != block[:prefix_bytes]:
                raise ExecutionConflict('finalizing_file_conflict','收尾暂存内容与原计划不符；保留现场')
            remaining -= prefix_bytes
            if prefix_bytes < len(block):
                stream.write(block[prefix_bytes:])
    if remaining:
        raise ValueError('Early partial file exceeds its committed plan')
    stream.flush()
    os.fsync(stream.fileno())


def append_bytes(stream,data,offset):
    for start in range(offset,len(data),65536):stream.write(data[start:start+65536])
    stream.flush();os.fsync(stream.fileno())


def install_file(stage,plan,data):
    """Join only known matching partial bytes, then no-overwrite rename."""
    from .node_storage_budget import reserve
    from .fs_directory import move_file_new
    from .file_transactions import _sync
    root=storage._fs_path(stage.prefix,'.');run=workspace.checked_path(root,stage.manifest['run_path'])
    target=run
    for part in plan['relative'].split('/')[:-1]:
        target=target/part;target.mkdir(exist_ok=True);workspace.ordinary(target)
    target=target/plan['relative'].split('/')[-1]
    if target.exists():
        workspace.ordinary(target)
        if workspace.digest_file(target)!=plan['record']:raise ExecutionConflict('finalizing_file_conflict','收尾目标与原写入计划不符；保留现有文件')
        return
    parent=workspace.checked_path(root,stage.manifest['run_path']).parent/'finalizing-staging'
    parent.mkdir(exist_ok=True);workspace.ordinary(parent);pending=parent/(plan['id']+'.part')
    size=plan['record']['size']
    offset=0
    pending_version=None
    if pending.exists():
        pending_version=_file_version(workspace.ordinary(pending))
        if isinstance(data,LogSource):
            offset=pending_version[2]
        else:
            with pending.open('rb') as stream:
                for chunk in iter(lambda:stream.read(65536),b''):
                    if chunk!=data[offset:offset+len(chunk)]:raise ExecutionConflict('finalizing_file_conflict','收尾暂存内容与原计划不符；保留现场')
                    offset+=len(chunk)
        if offset>size:raise ValueError('Early partial file exceeds its committed plan')
    with reserve(size-offset):
        if isinstance(data,LogSource):
            with pending.open('r+b' if pending_version is not None else 'xb') as stream:
                if pending_version is not None and _file_version(os.fstat(stream.fileno()))!=pending_version:
                    raise ExecutionConflict('finalizing_file_conflict','收尾暂存已变化；保留现场')
                append_log(stream,data,offset)
        else:
            with pending.open('ab' if pending_version is not None else 'xb') as stream:
                append_bytes(stream,data,offset)
        if workspace.digest_file(pending)!=plan['record']:raise ValueError('Early staged bytes changed before installation')
        move_file_new(pending,target,root=run.parent);_sync(target.parent);_sync(parent)


async def write_file(stage,relative,data,*,generator):
    relative=workspace.canonical_path(relative)
    maximum=64*1024*1024+65536
    if isinstance(data,LogSource):
        record=data.record
    elif isinstance(data,bytes):
        if len(data)>maximum:raise ValueError('Early finalizing file exceeds its bound')
        record={'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    else:
        raise ValueError('Early finalizing file requires bytes or a captured log')
    if record['size']>maximum:raise ValueError('Early finalizing file exceeds its bound')
    if relative in stage.value['files']:raise ExecutionConflict('finalizing_file_conflict','原生程序已创建收尾专用目标；拒绝覆盖')
    identifier=event_id(stage.owner.attempt_id,'file:'+relative)
    plan={'schema':PLAN,'id':identifier,'stage_sha256':stage.sha256,'relative':relative,
        'record':record,'generator':generator}
    async with SessionLocal() as db:
        await lock_case_execution(db,stage.case_id);case,job,attempt=await require_authority(db,stage)
        existing=await db.get(DurableEvent,identifier)
        if existing is not None:
            if existing.type!=PLANNED or existing.case_id!=case.id or existing.attempt_id!=attempt.id or json.loads(existing.payload_json)!=plan:
                raise ExecutionConflict('finalizing_file_conflict','收尾文件计划已登记不同内容')
        else:
            await append_event(db,case.id,PLANNED,plan,attempt_id=attempt.id,event_id=identifier)
            await finish_before_cancel(db.commit())
    await finish_before_cancel(asyncio.to_thread(install_file,stage,plan,data))
    async with SessionLocal() as db:
        await lock_case_execution(db,stage.case_id);case,job,attempt=await require_authority(db,stage)
        installed_id=event_id(attempt.id,'installed:'+relative);value={'schema':PLAN,'plan_id':identifier,'plan_sha256':digest(plan)}
        installed=await db.get(DurableEvent,installed_id)
        if installed is None:
            await append_event(db,case.id,INSTALLED,value,attempt_id=attempt.id,event_id=installed_id);await finish_before_cancel(db.commit())
        elif installed.type!=INSTALLED or installed.case_id!=case.id or installed.attempt_id!=attempt.id or json.loads(installed.payload_json)!=value:
            raise ValueError('Early file installation history changed')
    return plan['record']


async def write_evidence(proc,stage):
    from .case_evidence import complete_solve_evidence
    data=await finish_before_cancel(asyncio.to_thread(capture_log_source,proc.log_path,
                                                    stage.value['log']['size'],stage.value['log']))
    relative=f'.vibeflow/jobs/job-{proc.job_id}.log'
    await write_file(stage,relative,data,generator='original_native_log')
    marker=stage.value['evidence_input']['run_dir'];marker=marker+'/'+marker.split('/')[-1]+'.foam'
    if marker not in stage.value['files']:await write_file(stage,marker,b'',generator='solver_view_marker')
    log=workspace.checked_path(proc.cwd,relative)
    evidence=await finish_before_cancel(asyncio.to_thread(complete_solve_evidence,proc.cwd,stage.value['evidence_input'],log))
    await write_file(stage,f'.vibeflow/jobs/job-{proc.job_id}-evidence.json',json.dumps(evidence,ensure_ascii=False).encode('utf8'),generator='solve_evidence_v1')


async def result(proc,stage):
    if stage is None or not stage.value['supported']:return
    log=await finish_before_cancel(asyncio.to_thread(workspace.digest_file,proc.log_path))
    value={'schema':SCHEMA,'stage_sha256':stage.sha256,'completion':{'status':proc.status,'exit_code':proc.exit_code,'reason':proc.failure_reason},'log':log}
    async with SessionLocal() as db:
        await lock_case_execution(db,stage.case_id);case,job,attempt=await require_authority(db,stage)
        output=json.loads(attempt.output_manifest_json)
        previous=output.get(RESULT)
        if previous is not None:
            if previous!=value:raise ValueError('Early completion already recorded a different result')
            return
        output[RESULT]=value;attempt.output_manifest_json=json_text(output)
        await append_event(db,case.id,'attempt.finalizing_stage_result',{'stage_sha256':stage.sha256,'result_sha256':digest(value)},attempt_id=attempt.id,event_id=event_id(attempt.id,'result'))
        await finish_before_cancel(db.commit())
