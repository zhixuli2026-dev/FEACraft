"""Case-scoped external scheduler submission, polling, cancellation and artifacts."""

import hashlib
import json
from urllib.parse import quote
from uuid import UUID, uuid4

import asyncssh
from fastapi import APIRouter, Depends, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_db
from ..deps import get_current_user
from ..models import Membership, RemoteHpcRun, User
from ..services import attempt_objects, attempt_workspace, job_execution, remote_hpc as hpc
from ..services.durable_events import json_text, lock_case_execution
from ..services.remote_hpc_config import profiles
from .workspaces import get_owned_case


router = APIRouter(prefix='/api/v1', tags=['remote-hpc'])


class RestartFile(BaseModel):
    output_path: str
    input_path: str


class Restart(BaseModel):
    run_id: UUID
    files: list[RestartFile] = Field(min_length=1, max_length=32)


class Submit(BaseModel):
    profile: str = Field(min_length=1, max_length=64)
    script: str
    input_paths: list[str] = Field(min_length=1, max_length=64)
    output_paths: list[str] = Field(default_factory=list, max_length=32)
    timeout_seconds: int = Field(default=600, ge=60, le=86400)
    restart: Restart | None = None


async def _authorized_profiles(db,user_id):
    try:
        configured = profiles()
    except (OSError,TypeError,ValueError) as error:
        raise HTTPException(503,'Deployment HPC profiles are invalid') from error
    orgs = set(await db.scalars(select(Membership.org_id).where(
        Membership.user_id==user_id, Membership.role=='admin')))
    return {name:profile for name,profile in configured.items() if profile.org_id in orgs}


async def _profile(db,user_id,key, prior=None):
    profile = (await _authorized_profiles(db,user_id)).get(key)
    if profile is None:
        raise HTTPException(404,'HPC profile is unavailable')
    if prior is not None:
        try:
            matches = prior == profile.fingerprint
        except (OSError,ValueError) as error:
            raise HTTPException(503,'The deployed HPC host key is unavailable') from error
        if not matches:
            raise HTTPException(409,'The job belongs to an earlier deployment HPC profile')
    return profile


def _public_path(path):
    value = attempt_workspace.canonical_path(path)
    if value.split('/')[0].casefold()=='.vibeflow':
        raise ValueError('HPC case files cannot include private execution data')
    return value


def _selections(paths):
    names = [_public_path(path) for path in paths]
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError('HPC file selection contains duplicate names')
    return sorted(names)


def _normalized(body):
    plan = body.model_dump(mode='json')
    plan['script'] = _public_path(body.script)
    plan['input_paths'] = _selections(body.input_paths)
    plan['output_paths'] = _selections(body.output_paths)
    if plan['script'] not in plan['input_paths']:
        raise ValueError('Select the remote job script as an input')
    if set(hpc.LOGS) & set(plan['output_paths']):
        raise ValueError('The scheduler manages its stdout and stderr archive paths')
    if body.restart is not None:
        mappings = [{'input_path':_public_path(item.input_path),'output_path':_public_path(item.output_path)}
                    for item in body.restart.files]
        targets = [path.casefold() for path in plan['input_paths']]
        targets += [item['input_path'].casefold() for item in mappings]
        if len(set(targets)) != len(targets):
            raise ValueError('The imported checkpoint paths overlap other selected inputs')
        plan['restart']['files'] = sorted(mappings,key=lambda item:item['input_path'])
    return plan


async def _replay(db,case_id,user_id,key,digest):
    found = await db.scalar(select(RemoteHpcRun).where(RemoteHpcRun.case_id==case_id,
        RemoteHpcRun.user_id==user_id, RemoteHpcRun.request_key==key).execution_options(populate_existing=True))
    if found is not None and found.request_sha256!=digest:
        raise HTTPException(409,'The request key already names a different HPC submission')
    return found


@router.get('/remote/profiles')
async def profile_list(user:User=Depends(get_current_user), db:AsyncSession=Depends(get_db)):
    return [profile.public() for profile in (await _authorized_profiles(db,user.id)).values()]


@router.get('/cases/{case_id}/remote-jobs')
async def list_runs(case_id:int,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    await get_owned_case(case_id,user,db,write=False)
    runs = await db.scalars(select(RemoteHpcRun).where(RemoteHpcRun.case_id==case_id,
        RemoteHpcRun.user_id==user.id).order_by(RemoteHpcRun.created_at.desc()).limit(100))
    return [hpc.public(run) for run in runs]


async def _snapshot(db,case_id,user_id,prefix,plan):
    data = await hpc.capture(prefix,plan['input_paths'])
    if plan['restart'] is not None:
        replay = plan['restart']
        _, source = await hpc.reconnect(db,case_id,user_id,replay['run_id'])
        if source.state not in {'succeeded','cancelled'} or source.profile!=plan['profile']:
            raise ValueError('The checkpoint job has no settled result on the selected profile')
        total = sum(map(len,data.values()))
        for item in replay['files']:
            checkpoint = await hpc.member(prefix,source,item['output_path'])
            total += len(checkpoint)
            if total > hpc.MAX_TOTAL:
                raise ValueError('HPC input exceeds the total size limit')
            data[item['input_path']] = checkpoint
    return data


@router.post('/cases/{case_id}/remote-jobs', status_code=202)
async def submit(case_id:int,body:Submit,
                 key:str=Header(alias='Idempotency-Key'),user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    try:
        job_execution.validate_key(key)
        plan = _normalized(body)
        case = await get_owned_case(case_id,user,db)
        user_id,prefix = user.id,case.s3_prefix
        profile = await _profile(db,user_id,body.profile)
        if body.timeout_seconds > profile.max_seconds:
            raise ValueError('Requested wall time exceeds this profile limit')
        plan['case_prefix'] = prefix
        digest = hashlib.sha256(json_text(plan).encode('utf-8')).hexdigest()
        run = await _replay(db,case_id,user_id,key,digest)
        if run is not None and (run.scheduler_id is not None or run.state in hpc.TERMINAL or run.state=='cancelling'):
            return hpc.public(run)
        # Restoration may make the original case request unavailable before a
        # stage completes. A completed remote receipt can still be re-read.
        data = None
        if run is None:
            data = await _snapshot(db,case_id,user_id,prefix,plan)
            records = {path:attempt_objects.digest(content) for path,content in data.items()}
            await lock_case_execution(db,case_id)
            checked_case = await get_owned_case(case_id,user,db)
            if checked_case.s3_prefix!=prefix:
                raise HTTPException(409,'The case storage moved during HPC submission')
            run = await _replay(db,case_id,user_id,key,digest)
            if run is None:
                await hpc.require_capacity(db,checked_case)
                run = RemoteHpcRun(id=str(uuid4()),case_id=case_id,user_id=user_id,profile=profile.name,
                    profile_sha256=profile.fingerprint,request_key=key,request_sha256=digest,
                    request_json=json_text(plan),input_json=json_text(records),
                    source_run_id=str(body.restart.run_id) if body.restart else None)
                db.add(run)
                from ..services.audit import case_event
                await case_event(db,checked_case,user_id,'hpc.submit','remote_hpc',run.id,
                                 {'profile':profile.name,'scheduler':profile.scheduler})
            await db.commit()
        if run.profile_sha256!=profile.fingerprint:
            raise HTTPException(409,'The job belongs to an earlier deployment HPC profile')
        if run.scheduler_id is not None or run.state in hpc.TERMINAL or run.state=='cancelling':
            return hpc.public(run)
        run_id = run.id
        db.expunge(run)
        await db.rollback()

        async def read_member(path,_record):
            nonlocal data
            if data is None:
                data = await _snapshot(db,case_id,user_id,prefix,plan)
                await db.rollback()
            return data[path]

        receipt = await hpc.stage(run,profile,hpc.directory(profile,run_id),read_member)
        if receipt and receipt.get('cancelled_before_submit'):
            value = {'state':'cancelled','cancelled_before_submit':True}
        elif receipt:
            value = {'state':'queued',**receipt}
        else:
            value = {'state':'awaiting_receipt'}
        return hpc.public(await hpc._store_observation(db,case_id,user_id,run_id,value))
    except FileNotFoundError as error:
        raise HTTPException(404,'A selected HPC input file is unavailable') from error
    except (ValueError,KeyError) as error:
        raise HTTPException(409,{'code':'hpc_submission_unverified','message':str(error)}) from error
    except (OSError,TimeoutError,asyncssh.Error) as error:
        raise HTTPException(502,{'code':'hpc_transport','type':type(error).__name__}) from error


@router.get('/cases/{case_id}/remote-jobs/{run_id}')
async def inspect(case_id:int,run_id:UUID,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    await get_owned_case(case_id,user,db,write=False)
    _,run = await hpc.reconnect(db,case_id,user.id,str(run_id))
    return hpc.public(run)


async def _refresh(db,case,run,profile):
    try:
        return hpc.public(await hpc.refresh(db,case,run,profile))
    except (ValueError,KeyError) as error:
        raise HTTPException(409,{'code':'hpc_observation_unverified','message':str(error)}) from error


@router.post('/cases/{case_id}/remote-jobs/{run_id}:refresh')
async def refresh(case_id:int,run_id:UUID,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    await get_owned_case(case_id,user,db)
    case,run = await hpc.reconnect(db,case_id,user.id,str(run_id))
    if run.state in hpc.TERMINAL:
        return hpc.public(run)
    profile = await _profile(db,user.id,run.profile,run.profile_sha256)
    return await _refresh(db,case,run,profile)


@router.post('/cases/{case_id}/remote-jobs/{run_id}:cancel')
async def cancel(case_id:int,run_id:UUID,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    await get_owned_case(case_id,user,db)
    await lock_case_execution(db,case_id)
    case,run = await hpc.reconnect(db,case_id,user.id,str(run_id))
    if run.state in hpc.TERMINAL:
        return hpc.public(run)
    profile = await _profile(db,user.id,run.profile,run.profile_sha256)
    if run.state != 'cancelling':
        run.state = 'cancelling'
        run.observation_json = json_text({**json.loads(run.observation_json),'cancel_requested':True})
        from ..services.audit import case_event
        await case_event(db,case,user.id,'hpc.cancel','remote_hpc',run.id)
    await db.commit()
    return await _refresh(db,case,run,profile)


@router.get('/cases/{case_id}/remote-jobs/{run_id}/files')
async def result(case_id:int,run_id:UUID,path:str,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
    await get_owned_case(case_id,user,db,write=False)
    case,run = await hpc.reconnect(db,case_id,user.id,str(run_id))
    try:
        path = _public_path(path)
        data = await hpc.member(case.s3_prefix,run,path)
    except (ValueError,OSError) as error:
        raise HTTPException(409,'HPC job output differs from its archive receipt') from error
    return Response(data,media_type='application/octet-stream',headers={
        'Content-Disposition':"attachment; filename*=UTF-8''"+quote(path.rsplit('/',1)[-1],safe='')})
