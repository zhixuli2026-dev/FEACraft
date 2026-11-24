"""Explicit full finalization of an original stopped, archived execution.

The new API owns a separate NativeRequest for registered validators. It never
replaces the original worker, replays the solver or renews its execution credit.
"""
import asyncio,json,os,re,sys,uuid
from dataclasses import dataclass
from datetime import datetime,timedelta
from pathlib import Path
from sqlalchemy import select
from ..db import SessionLocal
from ..models import ArtifactPublication,DurableEvent,NativeRequest,JobLogReservation
from . import attempt_output_recovery as outputs,attempt_objects as objects,job_execution,job_finalizing_source as source
from . import execution_budget,request_deadline,native_request_records as requests
from .async_ops import finish_before_cancel
from .durable_events import json_text,now
from .workflow_state import ExecutionConflict,TERMINAL

KEY='finalization_recovery'
KIND='job.finalize'
CHECKPOINT='vibeflow.job-write-checkpoint.v5'
CERTIFICATE='vibeflow.finalizing-recovery.v1'
DRAINED='attempt.finalizing_recovery_drained'
PURPOSES={'pandoc_parse','pandoc_compile','pandoc_version','latex_probe','latex_compile','latex_probe_linux','latex_compile_linux'}


def digest(value):return outputs.digest(json_text(value))


def frozen_binding(case,job,attempt):
    from .upload_storage import binding
    output=json.loads(attempt.output_manifest_json)
    value={'case':binding(case),'job_id':job.id,'attempt_id':attempt.id,'claim':outputs.digest(attempt.claim_token),
        'input':attempt.input_manifest_json,'worker':attempt.worker_identity_json,'process':attempt.process_identity_json,
        'children':attempt.child_processes_json,'publication':attempt.publication_json,
        'inventory':outputs.inventory_digest(output),'source':output.get(source.KEY)}
    if outputs.current_contract(output.get(outputs.KEY)):
        value['io_preparation_sha256']=outputs.digest(json_text(output[outputs.KEY]))
    return digest(value)


def native_profile(value):
    fields={'job_id','attempt_id','binding_sha256','validator','argv','max_log_bytes','distro'}
    if (not isinstance(value,dict) or set(value)!=fields or type(value['job_id']) is not int or value['job_id']<=0
            or str(uuid.UUID(value['attempt_id']))!=value['attempt_id'] or not re.fullmatch('[0-9a-f]{64}',value['binding_sha256'])
            or value['validator'] not in {'none','report','latex'} or type(value['max_log_bytes']) is not int
            or not 1024<=value['max_log_bytes']<=64*1024*1024 or not isinstance(value['argv'],list)
            or any(not isinstance(s,str) or len(s)>16384 for s in value['argv']) or len(json_text(value))>65536):
        raise ValueError('Invalid job-bound finalizer profile')
    if value['validator']=='latex' and (not isinstance(value['distro'],str) or not re.fullmatch('[a-z0-9-]+',value['distro'])):
        raise ValueError('Invalid finalizer distribution')
    if value['validator']!='latex' and value['distro'] is not None:raise ValueError('Unexpected finalizer distribution')
    return value


def allowed_purposes(profile):
    if profile['validator']=='latex' and profile['distro']=='native-linux':return {'latex_probe_linux','latex_compile_linux'}
    return {'pandoc_parse','pandoc_compile','pandoc_version'} if profile['validator']=='report' else {'latex_probe','latex_compile'} if profile['validator']=='latex' else set()


def profile_for(records):
    case,job,attempt,*_=records
    value=json.loads(attempt.input_manifest_json);spec=value['execution_spec'];handler=value.get('handler',{}).get('key')
    validator='report' if handler=='report.build' else 'latex' if handler=='latex.build' else 'none'
    argv=spec.get('argv') if validator!='none' else []
    if validator!='none' and (not isinstance(argv,list) or len(argv)<2):raise ValueError('Registered validator has no frozen invocation')
    distro=next((s.split('=',1)[1] for s in argv if s.startswith('--distro=')),None) if validator=='latex' else None
    return native_profile({'job_id':job.id,'attempt_id':attempt.id,'binding_sha256':frozen_binding(case,job,attempt),
        'validator':validator,'argv':argv,'max_log_bytes':spec['max_log_bytes'],'distro':distro})


async def snapshot(db,job_id,user_id,*,locked=False,allow_cancellation=False,allow_pending_log=False):
    records=await outputs.snapshot(db,job_id,user_id,locked=locked,allow_cancelling=allow_cancellation)
    case,job,attempt,manifest,output,owner,event=records
    if event is None:
        from .cancelled_archive_recovery import pending
        cancellation_only=pending(job,attempt,output) and (allow_cancellation or attempt.phase=='cancelled')
        if not cancellation_only and job.status=='cancelled' and attempt.phase=='cancelling':
            raise ExecutionConflict('cancelled_archive_pending','原任务已取消但归档尚未完成；保留原数据，先核验补传操作的实际结束状态')
        if not cancellation_only:raise ExecutionConflict('recovery_required','先完成原输出的归档补传')
    value=source.validate(case,job,attempt,output,output.get(source.KEY))
    if value['source_log_key']!=job.log_s3_key and job.log_s3_key!=manifest['run_path']+'/'+source.LOG:
        log=await db.get(JobLogReservation,job.id,populate_existing=True)
        from .upload_storage import binding
        if (log is None or log.case_id!=case.id or log.attempt_id!=attempt.id or log.binding_json!=binding(case)
                or {'size':log.size,'sha256':log.sha256}!=value['log']):
            raise ValueError('Canonical log pointer lost its exact original upload receipt')
        if log.state!='materialized' or log.remaining_bytes!=0:
            from .finalizing_io import pending_log
            if not allow_pending_log or log.state not in {'active','released'} or not await pending_log(db,case,job,attempt,log):
                raise ValueError('Canonical log has no completed upload or exact finalizing IO owner')
    source_event=source.RECOVERED_EVENT if value['schema']==source.RECOVERED_SCHEMA else source.EVENT
    events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type==source_event).limit(2)))
    if len(events)!=1 or events[0].case_id!=case.id or json.loads(events[0].payload_json)!=value:
        raise ValueError('Full finalization has no unique original source/log history')
    outputs.closed(job,attempt,output[outputs.KEY]['stop'])
    await outputs.drain.history(db,case,job,attempt,pending=attempt.phase not in TERMINAL)
    cancelling=allow_cancellation and job.status=='cancelled' and attempt.phase=='cancelling' and attempt.cancel_requested_at is not None
    if job.status not in TERMINAL and (job.status!='running' or attempt.phase!='finalizing' or attempt.cancel_requested_at is not None):
        raise ExecutionConflict('state_conflict','原收尾尝试已变化或取消')
    if (job.status in TERMINAL and not cancelling) or attempt.write_checkpoint_json!='{}':
        from .job_write_checkpoint import validate
        checkpoint=validate(case,job,attempt)
        if checkpoint['schema']!=CHECKPOINT and not (event is None and checkpoint['schema']=='vibeflow.job-write-checkpoint.v6'):
            raise ExecutionConflict('recovery_required','该收尾记录需要原有恢复入口处理')
        await verify_history(db,case,job,attempt,checkpoint)
    return records


async def require_native_release(db,request):
    profile=native_profile(json.loads(request.profile_json))
    records=await snapshot(db,profile['job_id'],request.created_by,locked=True)
    case,job,attempt,*_=records
    current=json.loads(attempt.output_manifest_json).get(KEY)
    if (job.status!='running' or attempt.write_checkpoint_json!='{}' or current!={'schema':CERTIFICATE,'request_id':request.id}
            or request.case_id!=case.id or profile!=profile_for(records)):
        raise ExecutionConflict('state_conflict','专业校验已失去原作业收尾授权')
    budget=execution_budget.current()
    if budget is None or budget.attempt_id!=attempt.id:raise ValueError('Recovery validator lost original output/deadline budget')
    budget.check()


def native_result(executor,error):
    prepared=getattr(executor,'prepared',None)
    data=None if prepared is None else {'kind':prepared.kind,'descriptor':prepared.descriptor,'records':prepared.records,
        'error':prepared.error,'publication':vars(prepared.publication)}
    return {'schema':CERTIFICATE,'profile_sha256':digest(executor.recovery_profile),
        'usage':executor.execution_budget.usage(),'prepared':data,'error':error}


def validate_result(value,profile):
    from .execution_output_checkpoint import validate_usage
    if (not isinstance(value,dict) or set(value)!={'schema','profile_sha256','usage','prepared','error'}
            or value['schema']!=CERTIFICATE or value['profile_sha256']!=digest(profile)
            or len(json_text(value))>16*1024*1024):raise ValueError('Invalid finalizer result binding')
    validate_usage(value['usage'],profile['max_log_bytes'])
    if value['prepared'] is not None:
        data=value['prepared']
        if not isinstance(data,dict) or set(data)!={'kind','descriptor','records','error','publication'}:
            raise ValueError('Invalid independent artifact validation receipt')
        if data['records'] is not None:
            from .publication_objects import validate_records
            validate_records(data['records'])
    return value


async def request_receipt(db,records):
    case,job,attempt,*_=records;pointer=json.loads(attempt.output_manifest_json).get(KEY)
    if not isinstance(pointer,dict) or set(pointer)!={'schema','request_id'} or pointer['schema']!=CERTIFICATE:
        raise ValueError('Missing separate recovery executor')
    row=await db.get(NativeRequest,pointer['request_id'],populate_existing=True)
    if row is None or row.kind!=KIND or row.case_id!=case.id or json.loads(row.profile_json)!=profile_for(records):
        raise ValueError('Recovery executor belongs to another original attempt')
    if row.phase=='active':raise ExecutionConflict('recovery_required','原恢复校验仍在进行或需要核验实际停止状态')
    journal=requests.document(row)
    cancelled_usage=None
    if row.phase=='cancelled':
        from .finalizing_request_cancellation import read as cancelled_receipt
        cancelled_usage=(await cancelled_receipt(db,row))['usage']
    elif row.error=='OwnerExited':
        from .finalizing_request_recovery import read as recovered_receipt
        await recovered_receipt(db,row)
    else:
        await require_request_stopped(db,row)
    value=validate_result(json.loads(row.result_json),json.loads(row.profile_json))
    if cancelled_usage is not None:value=dict(value,usage=cancelled_usage)
    original=json.loads(attempt.output_manifest_json)[outputs.KEY]['output_usage']
    if (value['usage']['observed_output_bytes']<original['observed_output_bytes']
            or any(value['usage']['by_source'].get(k,0)<n for k,n in original['by_source'].items())):
        raise ValueError('Recovery executor lost previously consumed output credit')
    from .finalizing_request_recovery import chain
    await chain(db,row,value['usage'])
    return row,value


async def require_request_stopped(db,row):
    """Keep actual stops; a terminal unstarted child needs separate proof."""
    if row.phase not in {'succeeded','failed'}:raise ValueError('Validator request is not terminal')
    for child in requests.document(row)['children'].values():
        if child['identity'] is None:
            from . import finalizing_native_scope as scopes,finalizing_native_preparation as preparation
            value=await scopes.read(db,row,child);proof=await preparation.read(db,row,child,value)
            from .finalizing_linux_scope import unreleased as linux_unreleased
            if proof is None or not (proof['schema']==preparation.TERMINAL_SCHEMA or linux_unreleased(proof) and proof['authority'] in {'normal','terminal'}):
                raise ValueError('Terminal unregistered validator has no actual non-release closure')
        else:outputs.drain.complete_stop(child['stop'],child['identity'])


def validate_certificate(case,job,attempt,value):
    from .job_write_checkpoint import FIELDS,claim_digest
    proof=value.get('recovery');output=json.loads(attempt.output_manifest_json)
    if (set(value)!=FIELDS|{'recovery'} or value.get('schema')!=CHECKPOINT or value.get('state')!='stopped'
            or value['case_id']!=case.id or value['job_id']!=job.id or value['attempt_id']!=attempt.id
            or value['case_prefix']!=case.s3_prefix or value['claim_sha256']!=claim_digest(attempt)
            or value['process_identity_json']!=attempt.process_identity_json or value['child_processes_json']!=attempt.child_processes_json
            or value['stop']!=output[outputs.KEY]['stop'] or not isinstance(proof,dict)
            or set(proof)!={'schema','binding_sha256','request_id','result_sha256','owner_exit_event_id'}
            or proof['schema']!=CERTIFICATE or proof['binding_sha256']!=frozen_binding(case,job,attempt)
            or output[KEY]!={'schema':CERTIFICATE,'request_id':proof['request_id']}):
        raise ValueError('Recovered full drain differs from the original execution')
    outputs.closed(job,attempt,value['stop'])
    if datetime.fromisoformat(value['recorded_at']).utcoffset()!=timedelta(0):raise ValueError('Recovered full drain timestamp requires UTC')
    if output.get('process_stop',value['stop'])!=value['stop']:raise ValueError('Terminal stop differs from recovered full drain')
    return value


async def verify_history(db,case,job,attempt,checkpoint):
    if checkpoint['schema']=='vibeflow.job-write-checkpoint.v6':
        from .cancelled_archive_recovery import verify_history as cancelled_history
        return await cancelled_history(db,case,job,attempt,checkpoint)
    row,result=await request_receipt(db,(case,job,attempt))
    if checkpoint['recovery']['result_sha256']!=digest(result):raise ValueError('Full drain lost its stopped validator result')
    certificate=await outputs.read_owner_exit(db,(case,job,attempt,None,None,job_execution_owner(job,attempt),None))
    if certificate is None or checkpoint['recovery']['owner_exit_event_id']!=certificate.id:raise ValueError('Full drain lost exact original owner exit')
    events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id==attempt.id,DurableEvent.type==DRAINED).limit(2)))
    if len(events)!=1 or events[0].case_id!=case.id or json.loads(events[0].payload_json)!={'checkpoint_sha256':outputs.digest(attempt.write_checkpoint_json)}:
        raise ValueError('Full drain lost its durable certificate event')
    return digest([row.id,row.phase,row.profile_json,row.children_json,row.result_json,events[0].id,events[0].payload_json])


def job_execution_owner(job,attempt):
    from .job_worker_identity import document
    return document(job,attempt)


@dataclass(frozen=True)
class Recovery:
    binding_sha256:str
    request_id:str


async def require_recovery(db,job,attempt,proof,status,reason):
    from .job_write_checkpoint import validate
    from ..models import Case
    case=await db.get(Case,job.case_id,populate_existing=True)
    checkpoint=validate(case,job,attempt)
    if not isinstance(proof,Recovery) or proof.binding_sha256!=frozen_binding(case,job,attempt) or proof.request_id!=checkpoint['recovery']['request_id']:
        raise ValueError('Full finalization commit lacks its recovered drain authority')
    await verify_history(db,case,job,attempt,checkpoint)


async def recover(job_id,user_id,attempt_id,inventory_sha256):
    from botocore.exceptions import BotoCoreError,ClientError
    async with request_deadline.scope(300):
        try:await asyncio.wait_for(outputs.SLOTS.acquire(),.1)
        except TimeoutError as error:raise ExecutionConflict('output_recovery_busy','收尾恢复正在进行，请稍后重试') from error
        try:return await _recover(job_id,user_id,attempt_id,inventory_sha256)
        except (ValueError,KeyError,TypeError,AttributeError,OSError,BotoCoreError,ClientError) as error:
            raise ExecutionConflict('finalization_recovery_unavailable','原收尾来源、日志或实际校验停止状态无法核验；保留现有数据') from error
        finally:outputs.SLOTS.release()


async def _recover(job_id,user_id,attempt_id,inventory_sha256):
    from .finalizing_io import hold
    async with hold(job_id,user_id,attempt_id,inventory_sha256) as io_guard:
        try:return await _recover_active(job_id,user_id,attempt_id,inventory_sha256)
        except BaseException as error:
            if io_guard is not None:io_guard.work_returned=True
            from .finalizing_job_cancellation import settle_if_requested
            settled=await finish_before_cancel(settle_if_requested(job_id,user_id,attempt_id,inventory_sha256))
            if settled is None or isinstance(error,asyncio.CancelledError):raise
            return settled


async def _recover_active(job_id,user_id,attempt_id,inventory_sha256):
    async with SessionLocal() as db:before=await snapshot(db,job_id,user_id,allow_pending_log=True)
    case,job,attempt,manifest,output,_,_=before
    if attempt.id!=attempt_id or outputs.inventory_digest(output)!=inventory_sha256:raise ExecutionConflict('state_conflict','原尝试或输出清单已变化')
    if job.status in TERMINAL:
        visibility=job_execution.completion_visibility(attempt)
        if visibility=='invalid':raise ExecutionConflict('recovery_required','原发布完成状态损坏；保留现场核对')
        if job.status=='succeeded' and visibility=='pending':
            from .finalizing_io import CURRENT,busy
            if CURRENT.get() is None:raise busy()
            from .publication_alias_recovery import reconcile
            repaired=await reconcile(job_id)
            if repaired not in {'publication_compatibility_recovered','publication_compatibility_already_settled'}:
                raise ExecutionConflict('recovery_required','原发布兼容路径尚未完成；保留原发布并按原证明恢复')
            async with SessionLocal() as db:before=await snapshot(db,job_id,user_id)
        return await response(before)
    observed,_,owner_event_id=await outputs.observe_original(before,user_id,persist=True)
    if observed!={'state':'exited','reason':'process_exited'} or owner_event_id is None:raise ExecutionConflict('recovery_required','原worker确切退出状态无法核验')
    owner=job_execution.Claim(job.id,attempt.id,attempt.root_job_id,attempt.number,attempt.claim_token)
    from .finalizing_request_recovery import retire
    retry=await retire(before,user_id)
    if retry:
        async with SessionLocal() as db:before=await snapshot(db,job_id,user_id,allow_pending_log=True)
        case,job,attempt,manifest,output,_,_=before
    # Early proven usage is not a full drain, but is still spent credit. Later
    # request receipts replace it only after their actual native children stop.
    budget=await execution_budget.load(owner)
    usage=output[outputs.KEY]['output_usage']
    if KEY in output:
        async with SessionLocal() as db:row,result=await request_receipt(db,before)
        usage=result['usage']
    budget.restore(usage)
    with budget.activate():
        from .execution_storage import recovery as monitor_storage
        async with monitor_storage(budget,case,attempt):
            # Same private cache primitive, revalidated before a no-overwrite
            # install. A completed v5 certificate also authorizes cold reads.
            view=None
            async def prepare_cache():
                nonlocal view
                if not objects.storage._use_s3():
                    await asyncio.to_thread(outputs.verify_local,case.s3_prefix,manifest,output)
                    return
                view=await asyncio.to_thread(objects.prepare_runtime_cache,case.s3_prefix,manifest,output)
            try:
                await finish_before_cancel(prepare_cache())
                async with SessionLocal() as db:
                    current=await snapshot(db,job_id,user_id,locked=True,allow_pending_log=True)
                    if frozen_binding(*current[:3])!=frozen_binding(*before[:3]):raise ExecutionConflict('state_conflict','恢复期间原收尾绑定变化')
                    if view is not None:await finish_before_cancel(asyncio.to_thread(objects.install_runtime_cache,case.s3_prefix,manifest,output,view))
            finally:
                if view is not None:await finish_before_cancel(asyncio.to_thread(view.cleanup))
            if KEY not in output or retry:
                from .job_log_storage import upload
                log=source.validate(case,job,attempt,output,output[source.KEY])
                path=objects.workspace.checked_path(objects.storage._fs_path(case.s3_prefix,manifest['run_path']),log['log_path'])
                if await finish_before_cancel(asyncio.to_thread(objects.workspace.digest_file,path))!=log['log']:raise ValueError('Recovered original log changed')
                archived_log=not objects.storage._use_s3()
                try:
                    if not retry and objects.storage._use_s3():await upload(owner,case.s3_prefix,path)
                except execution_budget.BudgetExceeded:
                    # Already admitted immutable archive bytes need no new
                    # upload credit. Failure settlement may retain this exact
                    # bound original log after the native deadline expires.
                    archived_log=True
                profile=profile_for(before)
                async def acquire_owner():
                    async with SessionLocal() as db:
                        current=await snapshot(db,job_id,user_id,locked=True)
                        if KEY in current[4] and (not retry or current[4][KEY]!=output[KEY]):raise ExecutionConflict('state_conflict','其他请求已开始原收尾恢复')
                        if profile_for(current)!=profile:raise ExecutionConflict('state_conflict','原收尾绑定在登记前已变化')
                        if archived_log:current[1].log_s3_key=manifest['run_path']+'/'+source.LOG
                        request_owner=await requests.create(case.id,user_id,KIND,profile,db=db)
                        from .finalizing_native_usage import initialize
                        await initialize(db,request_owner,profile,budget)
                        if retry:
                            from .finalizing_request_recovery import link
                            previous=await db.get(NativeRequest,output[KEY]['request_id'],populate_existing=True)
                            await link(db,previous,request_owner,profile)
                        current[4][KEY]={'schema':CERTIFICATE,'request_id':request_owner.id}
                        current[2].output_manifest_json=json_text(current[4]);await db.commit()
                    return request_owner
                from .native_request_execution import scope
                from .artifact_publication import prepare_for_finish
                status,code,reason=execution_budget.failure(owner,log['status'],log['exit_code'],log['reason'])
                async with scope(case.id,user_id,objects.storage._fs_path(case.s3_prefix,manifest['run_path']),KIND,profile,acquire_owner=acquire_owner) as executor:
                    # prepare_for_finish itself moves blocking handler work to
                    # a protected thread; local() supplies its actual channel.
                    if status=='succeeded':executor.prepared=await executor.run_finalizer(prepare_for_finish,owner,status,output[outputs.KEY]['stop'])
                    else:
                        with executor.local():executor.prepared=await prepare_for_finish(owner,status,output[outputs.KEY]['stop'])
            async with SessionLocal() as db:
                current=await snapshot(db,job_id,user_id)
                row,result=await request_receipt(db,current)
                archived_log=current[1].log_s3_key==manifest['run_path']+'/'+source.LOG
                if not archived_log:
                    log_row=await db.get(JobLogReservation,job.id,populate_existing=True)
                    if log_row is None or log_row.state!='materialized' or {'size':log_row.size,'sha256':log_row.sha256}!=current[4][source.KEY]['log']:
                        raise ValueError('Full finalization requires its exact materialized original log')
                    from .job_log_storage import Ticket,ready
                    ticket=Ticket(**{key:getattr(log_row,key) for key in ('job_id','attempt_id','case_id','user_id','binding_json','sha256','size','token')})
            if archived_log:
                if objects.storage._use_s3():
                    await finish_before_cancel(asyncio.to_thread(objects.verify_members,case.s3_prefix,manifest['run_path'],{source.LOG:current[4][source.KEY]['log']}))
                elif await finish_before_cancel(asyncio.to_thread(outputs.workspace.digest_file,
                        objects.storage._fs_path(case.s3_prefix,manifest['run_path']+'/'+source.LOG)))!=current[4][source.KEY]['log']:
                    raise ValueError('Original local log changed after recovered capture')
            elif not await finish_before_cancel(asyncio.to_thread(ready,case.s3_prefix,ticket)):
                raise ValueError('Original remote log is missing after recovery')
            budget.restore(result['usage'])
            return await complete(current,owner,budget,row,result,owner_event_id,user_id)


async def complete(records,owner,budget,row,result,owner_event_id,user_id):
    from .artifact_publication import PreparedValidation,PublicationIdentity,prepare_bundle_for_finish
    case,job,attempt,_,output,*_=records;log=output[source.KEY]
    if job.status=='cancelled' and attempt.cancel_requested_at is not None:status,code,reason='cancelled',job.exit_code,'cancel_requested'
    else:
        status,code,reason=execution_budget.failure(owner,log['status'],log['exit_code'],log['reason'])
        if row.phase!='succeeded':status,code,reason='failed',-1,'publication_failed'
    prepared=None;data=result['prepared']
    if data is not None and status=='succeeded':
        prepared=PreparedValidation(case.id,job.id,attempt.id,outputs.digest(owner.token),case.s3_prefix,
            attempt.input_manifest_json,attempt.output_manifest_json,attempt.publication_json,data['kind'],
            json.loads(json_text(data['descriptor'])),PublicationIdentity(**data['publication']),records=data['records'],error=data['error'])
        try:prepared=await prepare_bundle_for_finish(owner,prepared)
        except execution_budget.BudgetExceeded:
            status,code,reason='failed',-1,'resource_limit';prepared=None
    elif attempt.publication_json!='{}' and status=='succeeded':
        status,code,reason='failed',-1,'evidence_invalid'
    async with SessionLocal() as db:
        current=await snapshot(db,job.id,user_id,locked=True,allow_cancellation=True)
        case,job,attempt,*_=current
        if job.status=='cancelled' and attempt.cancel_requested_at is not None:status,code,reason,prepared='cancelled',job.exit_code,'cancel_requested',None
        if attempt.write_checkpoint_json=='{}':
            certificate={'schema':CERTIFICATE,'binding_sha256':frozen_binding(case,job,attempt),'request_id':row.id,
                'result_sha256':digest(result),'owner_exit_event_id':owner_event_id}
            from .job_write_checkpoint import claim_digest
            checkpoint={'schema':CHECKPOINT,'case_id':case.id,'job_id':job.id,'attempt_id':attempt.id,'case_prefix':case.s3_prefix,
                'claim_sha256':claim_digest(attempt),'state':'stopped','process_identity_json':attempt.process_identity_json,
                'child_processes_json':attempt.child_processes_json,'stop':output[outputs.KEY]['stop'],'recorded_at':now().isoformat(),'recovery':certificate}
            attempt.write_checkpoint_json=json_text(checkpoint);validate_certificate(case,job,attempt,checkpoint)
            from .execution_output_checkpoint import record
            await record(db,job,attempt,owner)
            await job_execution.attempt_event(db,job,attempt,DRAINED,{'checkpoint_sha256':outputs.digest(attempt.write_checkpoint_json)})
            await db.commit()
    from .publication_source_validation import for_finish
    try:
        async with for_finish(owner,status,prepared) as (validation,prepared):
            await job_execution._finish_owned(owner,status,code,reason=reason,process_stop=output[outputs.KEY]['stop'],return_result=False,
                check_success=None,prepared=prepared,validation=validation,recovery=Recovery(frozen_binding(case,job,attempt),row.id))
    except execution_budget.BudgetExceeded:
        async with for_finish(owner,'failed',None) as (validation,unused):
            await job_execution._finish_owned(owner,'failed',-1,reason='resource_limit',process_stop=output[outputs.KEY]['stop'],return_result=False,
                check_success=None,prepared=None,validation=validation,recovery=Recovery(frozen_binding(case,job,attempt),row.id))
    async with SessionLocal() as db:final=await snapshot(db,job.id,user_id)
    return await response(final)


async def response(records):
    case,job,attempt,*_=records
    if json.loads(attempt.write_checkpoint_json).get('schema')=='vibeflow.job-write-checkpoint.v6':
        from .cancelled_archive_recovery import verify_history as cancelled_history
        async with SessionLocal() as db:await cancelled_history(db,case,job,attempt,json.loads(attempt.write_checkpoint_json))
        native_started=False
    else:
        async with SessionLocal() as db:row,result=await request_receipt(db,records)
        native_started=any(child['identity'] is not None for child in requests.document(row)['children'].values())
    return {'state':job.status,'job_id':job.id,'attempt_id':attempt.id,'exit_code':job.exit_code,
        'inventory_sha256':outputs.inventory_digest(json.loads(attempt.output_manifest_json)),
        'native_execution_started':native_started,'native_validation_started':native_started,'solver_execution_started':False,
        'finalization_pending':job_execution.completion_visibility(attempt) in {'pending','invalid'}}
