"""Persist submission and collect exact SSH scheduler results into case storage."""

import asyncio
from contextlib import nullcontext
import json
import os
from uuid import uuid4

import asyncssh
from fastapi import HTTPException
from sqlalchemy import update

from .. import storage
from ..models import Case, RemoteHpcRun, User, Workspace
from . import attempt_objects, attempt_workspace, case_read_view, remote_exec
from . import remote_hpc_protocol as scheduler, remote_hpc_transport as transfer
from .document_admission import authorize
from .durable_events import json_text, lock_case_execution
from .file_transactions import _path
from .fs_directory import DirectoryMoveError, move_file_new


MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
TERMINAL = {'succeeded','failed','cancelled'}
LOGS = ('stdout.log', 'stderr.log')


def name(run_id):
    # PBS historically keeps only the first 15 characters of the name.
    return 'vf' + run_id.replace('-','')[:13]


def directory(profile, run_id):
    return profile.root + '/vf-' + run_id


def archived(run_id, path):
    return '.vibeflow/remote-hpc/' + run_id + '/results/' + path


def _read_members(root, selections):
    result, total = {}, 0
    for path in selections:
        target = attempt_workspace.checked_path(root,path)
        size = target.stat().st_size
        if not target.is_file() or not 0 <= size <= MAX_FILE or total + size > MAX_TOTAL:
            raise ValueError('HPC input exceeds the file or job limit')
        with target.open('rb') as stream:
            data = stream.read(size+1)
        if len(data) != size:
            raise ValueError('HPC input changed during capture')
        total += size
        result[path] = data
    return result


async def capture(prefix, paths):
    if not paths:
        return {}
    return await asyncio.to_thread(case_read_view.inspect, prefix, _read_members, paths,
                                   paths=paths, max_file_bytes=MAX_FILE)


def _persist(prefix, relative, data):
    expected = attempt_objects.digest(data)
    if storage._use_s3():
        attempt_objects.put_new(prefix, relative, data)
        return expected
    root = storage._fs_path(prefix, '.')
    path = _path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path = _path(root, relative)
    if not path.exists():
        temporary = path.with_name('.remote-result-' + uuid4().hex)
        try:
            with temporary.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                move_file_new(temporary,path,root=root)
            except DirectoryMoveError:
                if not path.is_file():
                    raise
        finally:
            temporary.unlink(missing_ok=True)
    path = attempt_workspace.checked_path(root, relative)
    if attempt_workspace.digest_file(path) != expected:
        raise ValueError('An archived HPC result already contains other bytes')
    return expected


def _read_archive(prefix, run_id, path, record):
    file_path = archived(run_id,path)
    if storage._use_s3():
        data = attempt_objects.read(prefix,file_path,limit=record['size'])
    else:
        node_root = storage._fs_path(prefix,'.')
        with attempt_workspace.checked_path(node_root,file_path).open('rb') as stream:
            data = stream.read(record['size']+1)
    if attempt_objects.digest(data) != record:
        raise ValueError('Archived HPC result no longer matches its receipt')
    return data


async def member(prefix, run, path):
    result = json.loads(run.observation_json).get('results', {}).get(path)
    if result is None:
        raise HTTPException(404,'HPC job output is unavailable')
    return await asyncio.to_thread(_read_archive,prefix,run.id,path,result)


def payload(run):
    return json.loads(run.request_json)


def public(run):
    detail = payload(run)
    observation = json.loads(run.observation_json)
    return {'id':run.id, 'case_id':run.case_id, 'profile':run.profile,'source_run_id':run.source_run_id,
            'state':run.state,'scheduler_id':run.scheduler_id, 'script':detail['script'],
            'outputs':detail['output_paths'], 'input_files':json.loads(run.input_json),
            'observation':observation, 'created_at':run.created_at, 'updated_at':run.updated_at}


async def reconnect(db, case_id, user_id, run_id):
    case = await db.get(Case, case_id, populate_existing=True)
    run = await db.get(RemoteHpcRun, run_id, populate_existing=True)
    if run is None or run.case_id != case_id or run.user_id != user_id or case is None:
        raise HTTPException(404,'HPC job not found')
    if payload(run)['case_prefix'] != case.s3_prefix:
        raise HTTPException(409,'HPC job no longer matches its case storage')
    return case, run


async def require_capacity(db,case):
    """Use the same owner lock as normal worker delivery before external submission."""
    from .job_delivery import occupied_jobs
    from .quota import occupied_remote_jobs, quotas_for

    workspace = await db.get(Workspace,case.workspace_id)
    await db.execute(update(User).where(User.id==workspace.user_id).values(id=User.id))
    owner = await db.get(User,workspace.user_id)
    limit = (await quotas_for(db,owner))['job_concurrent']
    if type(limit) is not int or limit < 1:
        raise HTTPException(429,'当前套餐没有作业并发额度')
    used = await occupied_remote_jobs(db,owner.id)
    if used < limit:
        async with occupied_jobs(db,owner.id) as jobs:
            async for _ in jobs:
                used += 1
                if used >= limit:
                    break
    if used >= limit:
        raise HTTPException(429,{'code':'job_concurrency','message':'作业并发额度已用完，请等待已有作业结束。'})


async def _store_observation(db, case_id, user_id, run_id, value):
    await lock_case_execution(db,case_id)
    await authorize(db,case_id,user_id)
    case,run = await reconnect(db,case_id,user_id,run_id)
    if run.state not in TERMINAL:
        remote_id = value.get('scheduler_id')
        if run.scheduler_id and remote_id not in {None,run.scheduler_id}:
            raise ValueError('Remote scheduler acknowledgement changed job identity')
        if remote_id:
            run.scheduler_id = remote_id
        requested = run.state=='cancelling'
        if remote_id is None and run.scheduler_id and value['state']=='awaiting_receipt':
            await db.commit()
            return run
        run.state = 'cancelling' if requested and value['state'] not in TERMINAL else value['state']
        prior = json.loads(run.observation_json)
        if prior.get('cancel_requested'):
            value['cancel_requested'] = True
        if prior.get('cancel_sent'):
            value['cancel_sent'] = True
        run.observation_json = json_text(value)
        if run.state in TERMINAL:
            from .audit import case_event
            await case_event(db,case,user_id,'hpc.completed','remote_hpc',run_id,{'state':run.state})
    await db.commit()
    return run


async def stage(run, profile, directory_path, read_member):
    async with asyncio.timeout(90), remote_exec.connect(profile.ssh) as connection:
        current = await transfer.receipt(connection,profile.scheduler,directory_path)
        if current is not None:
            return current
        await transfer.prepare(connection, profile, payload(run), directory_path, name(run.id),
                               json.loads(run.input_json), read_member)
        await transfer.dispatch(connection,directory_path)
        for _ in range(30):
            current = await transfer.receipt(connection,profile.scheduler,directory_path)
            if current is not None:
                return current
            await asyncio.sleep(.15)
        return None


async def _collect(db, case, run, connection, paths, directory_path):
    responses, total = {}, 0
    for path in (*LOGS,*paths):
        data = await transfer.output(connection,directory_path,path,maximum=min(MAX_FILE,MAX_TOTAL-total))
        if data is not None:
            responses[path] = data
            total += len(data)
    if not responses:
        return {}
    # Current account ownership is checked before archiving into its quota.
    from .quota import check_storage_quota
    current,_ = await authorize(db,case.id,run.user_id)
    if current.s3_prefix != case.s3_prefix:
        raise ValueError('HPC result no longer belongs to the captured case storage')
    workspace = await db.get(Workspace,current.workspace_id)
    owner = await db.get(User,workspace.user_id)
    await check_storage_quota(db,owner,total)
    await db.commit()
    from .node_storage_budget import allocation
    from .async_ops import finish_before_cancel
    receipts = {}
    async with (nullcontext() if storage._use_s3() else allocation(total)):
        for path,data in responses.items():
            receipts[path] = await finish_before_cancel(asyncio.to_thread(_persist,case.s3_prefix,archived(run.id,path),data))
    return receipts


async def refresh(db, case, run, profile):
    if run.state in TERMINAL:
        return run
    case_id, user_id, run_id = run.case_id, run.user_id, run.id
    # Keep a detached record outside the short SQL control lock.
    db.expunge(run)
    db.expunge(case)
    await db.rollback()
    remote = directory(profile,run_id)
    try:
        async with asyncio.timeout(90), remote_exec.connect(profile.ssh) as connection:
            if run.state=='cancelling':
                await transfer.request_stop(connection,remote)
            receipt = await transfer.receipt(connection,profile.scheduler,remote)
            if receipt is None:
                return await _store_observation(db,case_id,user_id,run_id,{'state':'awaiting_receipt'})
            if receipt.get('cancelled_before_submit'):
                return await _store_observation(db,case_id,user_id,run_id,
                    {'state':'cancelled','cancelled_before_submit':True})
            identifier = receipt['scheduler_id']
            observed = await scheduler.observe(connection,profile.scheduler,identifier,name(run_id),remote)
            if observed is None:
                return await _store_observation(db,case_id,user_id,run_id,
                    {'state':'awaiting_scheduler','scheduler_id':identifier})
            if run.state=='cancelling' and observed['state'] not in TERMINAL:
                observed['cancel_sent'] = await scheduler.cancel(connection,profile.scheduler,identifier)
            if observed['state'] in TERMINAL:
                if profile.scheduler=='pbs' and observed['state']=='failed' and json.loads(run.observation_json).get('cancel_sent'):
                    observed['state'] = 'cancelled'
                observed['results'] = await _collect(db,case,run,connection,payload(run)['output_paths'],remote)
                if observed['state']=='succeeded' and any(path not in observed['results'] for path in payload(run)['output_paths']):
                    observed['state'] = 'failed'
                    observed['reason'] = 'missing_results'
    except (OSError,TimeoutError,asyncssh.Error) as error:
        raise HTTPException(502,{'code':'hpc_transport','type':type(error).__name__}) from error
    return await _store_observation(db,case_id,user_id,run_id,observed)
