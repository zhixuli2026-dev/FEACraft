"""运行时能力自检（服务端版；对标原版 vibeflow-env-detect + cae.* 依赖注册表）

桌面版：客户端扫工具路径存注册表。B/S 版：能力都在服务端沙箱里，
这里做的是"探测报告"——venv Python 关键包、WSL 发行版与求解器、外部二进制（pandoc）、
存储后端、DB 连通，一份 JSON 供设置页渲染。
"""
from __future__ import annotations

import asyncio
import hmac
import os
import time
import uuid
from pathlib import Path
from sqlite3 import DatabaseError
from threading import BoundedSemaphore

from fastapi import APIRouter, Depends, HTTPException, Query, Response, Request

from ..config import get_settings
from ..db import engine, get_db
from ..deps import get_current_user
from ..models import Case, Job, Membership, User, Workspace
from ..services import audit, batch_jobs, jobrunner, monitor_export, rate_limit, supervisor
from .workspaces import get_owned_case
from ..services.async_ops import finish_before_cancel
from ..services.cli_profiles import cli_profiles
from ..services.client_errors import recent_client_errors, report_client_error
from ..services.request_deadline import scope as deadline_scope
from ..services.runtime_registry import cli_details, environment_registry, stage_checks
from ..services.runtime_config import get_runtime_settings, update_runtime_settings
from ..services.request_lifecycle import wait
from sqlalchemy import func, select, text as _text
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/api/v1/runtime", tags=["runtime"])
_CAPABILITIES_SECONDS = 65
# Each admitted request runs two independent native probes.
_capability_slots = BoundedSemaphore(2)
_monitor_slots = BoundedSemaphore(2)

async def _wsl_probe(user_id, settings) -> dict:
    from ..services.runtime_probe_execution import wsl_probe
    return await wsl_probe(user_id, settings)


def _publish_runtime_status(user: User, wsl: dict):
    if not wsl.get("reachable") or not wsl.get("openfoam") or not wsl.get("wmake") or not wsl.get("gmsh"):
        from ..services.eventbus import publish
        publish(user.id, {"type": "runtime.degraded", "runtime": "wsl",
                          "reachable": bool(wsl.get("reachable")),
                          "openfoam": bool(wsl.get("openfoam")),
                          "wmake": bool(wsl.get("wmake")),
                          "gmsh": bool(wsl.get("gmsh"))})


def _storage_probe(settings=None) -> dict:
    """真实存储写读删探测；只使用本次专用临时文件并立即清理。"""
    settings = settings or get_settings()
    started = time.perf_counter()
    result = {"backend": settings.storage_backend, "fs_root": settings.storage_fs_root}
    try:
        if settings.storage_backend == "fs":
            directory = Path(settings.storage_fs_root) / ".vibeflow" / "capabilities"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"probe-{uuid.uuid4().hex}.tmp"
            payload = b"vibeflow-storage-probe"
            try:
                path.write_bytes(payload)
                if path.read_bytes() != payload:
                    raise OSError("存储回读内容不一致")
            finally:
                path.unlink(missing_ok=True)
        else:
            from ..storage import ensure_bucket
            ensure_bucket()
        result.update({"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
    except Exception as error:
        result.update({"ok": False, "error": f"{type(error).__name__}: {str(error)[:160]}",
                       "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
    return result


@router.post("/client-error")
def accept_client_error(payload: dict, user: User = Depends(get_current_user)):
    try:
        entry = report_client_error(payload)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"ok": True, "id": entry["id"], "created_at": entry["created_at"]}


@router.get("/client-errors")
async def get_client_errors(limit: int = Query(20, ge=1, le=100),
                            user: User = Depends(get_current_user),
                            db: AsyncSession = Depends(get_db)):
    admin = await db.scalar(select(Membership.id).where(
        Membership.user_id == user.id, Membership.role == "admin").limit(1))
    if admin is None:
        raise HTTPException(403, "仅组织 admin 可读取客户端错误日志")
    return {"items": recent_client_errors(limit)}


@router.get("/processes")
async def list_processes(case_id: int = 0, user: User = Depends(get_current_user),
                         db: AsyncSession = Depends(get_db)):
    if case_id:
        await get_owned_case(case_id, user, db, write=False)
        return {"processes": await supervisor.active_processes(db,{case_id})}
    cases = select(Case.id).join(Workspace).where(Workspace.user_id == user.id)
    owned = set((await db.scalars(cases)).all())
    return {"processes": await supervisor.active_processes(db, owned)}


@router.post("/processes:cancel")
async def cancel_processes(body: dict, user: User = Depends(get_current_user),
                           db: AsyncSession = Depends(get_db)):
    case_id = body.get("case_id")
    if type(case_id) is not int:
        raise HTTPException(400, "case_id 必须为整数")
    case = await get_owned_case(case_id, user, db)
    jobs = await supervisor.active_jobs(db, {case_id})
    await audit.case_event(db, case, user.id, 'runtime.case.cancel.requested', 'case', case_id)
    await db.commit()

    accepted = 0
    errors = []
    for job in jobs:
        try:
            await jobrunner.request_cancel(int(job['id'].removeprefix('job:')), actor_user_id=user.id)
        except jobrunner.JobStateConflict:
            continue
        except Exception as error:
            errors.append(f"{job['id']}: {type(error).__name__}")
        else:
            accepted += 1

    result = await supervisor.cancel_case_wait(case_id, excluded={job['id'] for job in jobs})
    await batch_jobs.cancel_case(case_id)
    if accepted:
        result['cancelled']['job'] = result['cancelled'].get('job', 0) + accepted
    result['errors'].extend(errors)
    result['ok'] = not result['errors']
    result['remaining'] = await supervisor.active_processes(db, {case_id})
    return result


@router.get("/cli-profiles")
def get_cli_profiles(user: User = Depends(get_current_user)):
    return cli_profiles()


@router.get("/config")
def get_setup(user: User = Depends(get_current_user)):
    return get_runtime_settings()


@router.patch("/config")
async def update_setup(patch: dict, user: User = Depends(get_current_user),
                       db: AsyncSession = Depends(get_db)):
    admin_org_id = await db.scalar(select(Membership.org_id).where(
        Membership.user_id == user.id, Membership.role == "admin").limit(1))
    if admin_org_id is None:
        raise HTTPException(403, "仅组织 admin 可修改运行时 Setup")
    try:
        await audit.append(db, admin_org_id, user.id, "runtime.config.updated", "runtime_config", "global",
                           {"keys": ",".join(sorted(patch))})
        result = update_runtime_settings(patch)
        await db.commit()
        return result
    except ValueError as error:
        raise HTTPException(400, str(error)) from error


async def _monitor_admin(db, user_id):
    admin = await db.scalar(select(Membership.id).where(
        Membership.user_id == user_id, Membership.role == "admin").limit(1))
    if admin is None:
        raise HTTPException(403, "仅组织 admin 可读取运行时监控")
    await db.rollback()


async def _monitor_report(request, user, db):
    await _monitor_admin(db, user.id)
    return await _admitted_monitor(request, db)


async def _admitted_monitor(request, db):
    if not _monitor_slots.acquire(blocking=False):
        raise HTTPException(429, {'code':'runtime_monitor_busy','message':'运行时监控繁忙，请稍后重试'},
                            headers={'Retry-After':'1'})
    try:
        async with deadline_scope(get_settings().storage_measurement_timeout_seconds) as deadline:
            report = await wait(_monitor_snapshot(db), request=request)
            deadline.check()
            return report
    except TimeoutError as error:
        raise HTTPException(504, {'code':'runtime_monitor_timeout','message':'运行时监控探测超时'}) from error
    finally:
        _monitor_slots.release()


async def _monitor_snapshot(db: AsyncSession) -> dict:
    storage = await finish_before_cancel(asyncio.to_thread(_storage_probe))
    started = time.perf_counter()
    try:
        async with engine.connect() as conn:
            await conn.execute(_text("SELECT 1"))
        database = {"ok": True, "latency_ms": round((time.perf_counter() - started) * 1000, 2)}
    except Exception as error:
        database = {"ok": False, "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                    "error": type(error).__name__}
    rows = (await db.execute(select(Job.status, func.count(Job.id)).group_by(Job.status))).all()
    jobs = {status: count for status, count in rows}
    processes = {}
    job_lease_states = {}
    for item in await supervisor.active_processes(db):
        processes[item["kind"]] = processes.get(item["kind"], 0) + 1
        if lease_state := item.get("lease_state"):
            job_lease_states[lease_state] = job_lease_states.get(lease_state, 0) + 1
    alerts = []
    if not database["ok"]:
        alerts.append({"level": "critical", "code": "database.down"})
    if not storage["ok"]:
        alerts.append({"level": "critical", "code": "storage.degraded"})
    if jobs.get("queued", 0) > 100:
        alerts.append({"level": "warning", "code": "jobs.queue.backlog"})
    for state in ("expired", "unverified"):
        if job_lease_states.get(state):
            alerts.append({"level": "warning", "code": f"jobs.owner_lease.{state}"})
    return {"schema": "vibeflow.runtime-monitor.v1", "time": time.time(),
            "database": database, "storage": storage, "jobs": jobs,
            "processes": processes, "job_lease_states": job_lease_states,
            "rate_limit": rate_limit.stats(), "alerts": alerts}


@router.get("/monitor")
async def runtime_monitor(request: Request, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    return await _monitor_report(request, user, db)


@router.get('/telemetry')
async def telemetry(request: Request, boot: str = Query(default='', max_length=64),
                    after: int = Query(default=0, ge=0), db: AsyncSession = Depends(get_db)):
    secret = get_settings().monitoring_export_token
    if not secret:
        raise HTTPException(404, '监控导出未启用')
    credential = request.headers.get('x-vibeflow-telemetry-token', '')
    if not hmac.compare_digest(credential.encode('utf-8'), secret.encode('utf-8')):
        raise HTTPException(401, '监控凭据无效')
    snapshot = await _admitted_monitor(request, db)
    return {'schema': 'vibeflow.telemetry.v1', 'sample': snapshot, 'logs': monitor_export.read(boot, after)}


async def _central_monitor(read, limit, db, user_id):
    await _monitor_admin(db, user_id)
    path = get_settings().monitoring_db_path
    if not path:
        raise HTTPException(404, '集中监控未启用')
    try:
        return await asyncio.to_thread(read, path, limit)
    except DatabaseError as error:
        raise HTTPException(503, '集中监控记录暂不可用') from error


@router.get('/monitor/history')
async def monitor_history(limit: int = Query(default=120, ge=1, le=1000),
                          user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from ..services.monitoring_store import history
    return await _central_monitor(history, limit, db, user.id)


@router.get('/monitor/logs')
async def monitor_logs(limit: int = Query(default=80, ge=1, le=256),
                       user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    from ..services.monitoring_store import logs
    return await _central_monitor(logs, limit, db, user.id)


@router.get("/metrics")
async def runtime_metrics(request: Request, user: User = Depends(get_current_user),
                          db: AsyncSession = Depends(get_db)):
    snap = await _monitor_report(request, user, db)
    lines = ["# HELP vibeflow_database_up Database connectivity", "# TYPE vibeflow_database_up gauge",
             f"vibeflow_database_up {1 if snap['database']['ok'] else 0}",
             "# TYPE vibeflow_storage_up gauge", f"vibeflow_storage_up {1 if snap['storage']['ok'] else 0}",
             "# TYPE vibeflow_database_latency_ms gauge",
             f"vibeflow_database_latency_ms {snap['database']['latency_ms']}"]
    for status, count in sorted(snap["jobs"].items()):
        lines.append(f'vibeflow_jobs_total{{status="{status}"}} {count}')
    for kind, count in sorted(snap["processes"].items()):
        lines.append(f'vibeflow_processes{{kind="{kind}"}} {count}')
    lines.append("# TYPE vibeflow_job_process_leases gauge")
    for state, count in sorted(snap["job_lease_states"].items()):
        lines.append(f'vibeflow_job_process_leases{{state="{state}"}} {count}')
    lines.append(f"vibeflow_rate_limit_buckets {snap['rate_limit']['buckets']}")
    lines.append(f"vibeflow_alerts {len(snap['alerts'])}")
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8")


@router.get("/capabilities")
async def capabilities(request: Request,user: User = Depends(get_current_user),db: AsyncSession=Depends(get_db)):
    user_id=user.id
    if not _capability_slots.acquire(blocking=False):
        raise HTTPException(429, {'code': 'runtime_capabilities_busy', 'message': '运行环境自检繁忙，请稍后重试'},
                            headers={'Retry-After': '1'})
    try:
        async with deadline_scope(_CAPABILITIES_SECONDS) as deadline:
            # Authentication must release its database connection before the
            # probes open their own ownership and heartbeat transactions.
            await db.rollback()
            result = await wait(_collect_capabilities(user_id), request=request)
            deadline.check()
            user = await db.get(User, user_id, populate_existing=True)
            if user is None or user.status != 'active':
                raise HTTPException(401, '用户不可用')
            _publish_runtime_status(user, result['wsl'])
            await db.rollback()
            return result
    except TimeoutError as error:
        raise HTTPException(504, {'code': 'runtime_capabilities_timeout', 'message': '运行环境自检超时，请重试'}) from error
    finally:
        # wait() drains native and storage work before returning or raising.
        _capability_slots.release()


async def _collect_capabilities(user_id):
    from ..services.runtime_probe_execution import python_probe
    s = get_settings()
    env = dict(os.environ)
    settings = await finish_before_cancel(asyncio.to_thread(get_runtime_settings, env))
    tasks=[asyncio.create_task(python_probe(user_id, settings)),asyncio.create_task(_wsl_probe(user_id, settings)),
           asyncio.create_task(finish_before_cancel(asyncio.to_thread(_storage_probe)))]
    try:
        python,wsl,storage=await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():task.cancel()
        await finish_before_cancel(asyncio.gather(*tasks,return_exceptions=True))
    pkgs=python['packages']
    # DB 连通
    try:
        async with engine.connect() as conn:
            await conn.execute(_text("SELECT 1"))
        db = {"ok": True, "url": s.database_url.split("://")[0] + "://…"}
    except Exception as e:
        db = {"ok": False, "error": str(e)[:120]}
    def discover():
        details = cli_details(env)
        return details, environment_registry(env, settings=settings, cli=details, wsl=wsl)

    details, registry = await finish_before_cancel(asyncio.to_thread(discover))
    tools = {row['tool']: row for row in registry}
    pandoc = tools['pandoc']
    return {
        "python": python,
        "database": db,
        "storage": storage,
        "binaries": {"pandoc": pandoc['value'] if pandoc['available'] else None},
        "wsl": wsl,
        "cli_agents": {name: info['path'] for name, info in details.items() if info and info.get('path')},
        "cli_agent_details": details,
        "environment_registry": registry,
        "stage_checks": stage_checks(wsl, {name: value is not None for name, value in pkgs.items()}, tools=tools),
    }
