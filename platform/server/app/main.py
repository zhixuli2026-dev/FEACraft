"""FastAPI 应用入口"""
import asyncio
import logging
import time
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from .config import get_settings, validate_storage_backend
from .db import SessionLocal, init_db
from .routers import agent, auth, cadscene, calculix, cite, code_aster, codex, completion, elmer, events, files, flotherm, geometry, index, jobs, llm, logs, mesh, moose, mpi, opencode_cfg, org, paraview, postprocess, remote, remote_hpc, reports, runtime, scnet, skills, su2, terminal, udf, viz, vizproxy, workflows, workspaces
from .services import monitor_export, rate_limit
from .routers import acp, website_auth
from .routers import native_history
from .routers import manual_files
from .routers import upload_recovery
from .routers import mineru, model_preferences, viz_collaboration
from .routers import execution_submissions, material_library
from .routers import connection_tickets, voice
from .services.case_write import CaseWriteConflict
from .services.workflow_state import ExecutionConflict
from .storage import ensure_bucket


def _omit_websocket_query(record):
    # Browser WebSocket handshakes carry the session token in the URL.
    if (isinstance(record.msg, str) and 'WebSocket %s' in record.msg
            and isinstance(record.args, tuple) and len(record.args) >= 2 and isinstance(record.args[1], str)):
        client, path, *rest = record.args
        record.args = (client, path.partition('?')[0], *rest)
    return True


logging.getLogger('uvicorn.error').addFilter(_omit_websocket_query)

app = FastAPI(title="VibeFlow Platform", version="0.1.0")


@app.exception_handler(ExecutionConflict)
async def execution_conflict(_request, error):
    detail = {'code': error.code, 'message': str(error)}
    if error.job_id is not None:
        detail['job_id'] = error.job_id
    return JSONResponse({'detail': detail}, status_code=409)


@app.exception_handler(CaseWriteConflict)
async def case_write_conflict(_request, error):
    return JSONResponse({"detail": error.detail()}, status_code=409)

s = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in s.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request, call_next):
    monitor_enabled = bool(s.monitoring_export_token)
    started = time.perf_counter() if monitor_enabled else 0
    try:
        await rate_limit.check(request)
    except rate_limit.RateLimited as error:
        response = await rate_limited(request, error)
    except rate_limit.LimiterUnavailable as error:
        response = await limiter_unavailable(request, error)
    else:
        response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy",
                                "camera=(), geolocation=(), microphone=(self), display-capture=(self)")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Content-Security-Policy",
                                    "default-src 'none'; frame-ancestors 'self'; base-uri 'none'")
    if monitor_enabled:
        monitor_export.record(request, response.status_code, started)
    return response


@app.exception_handler(rate_limit.RateLimited)
async def rate_limited(_request, error):
    return JSONResponse({'detail': f'请求过于频繁（{error.category}），请稍后重试'}, status_code=429,
                        headers={'Retry-After': str(error.retry_after)})


@app.exception_handler(rate_limit.LimiterUnavailable)
async def limiter_unavailable(_request, _error):
    return JSONResponse({'detail': '暂时无法核验请求额度，请稍后重试'}, status_code=503,
                        headers={'Retry-After': '1'})


from .archive_admission import ArchiveAdmission
from .raw_download import RawDownloadLifetime
# Outside BaseHTTPMiddleware so capacity includes the actual response send.
app.add_middleware(ArchiveAdmission)
app.add_middleware(RawDownloadLifetime)


@app.on_event("startup")
async def startup():
    validate_storage_backend(s)
    from .services.llm import validate_vault_configuration
    validate_vault_configuration()
    await init_db()
    from .services.job_recovery import reconcile_startup
    app.state.job_recovery = await reconcile_startup()
    if app.state.job_recovery:
        print(f"[INFO] 作业所有者启动对账: {app.state.job_recovery}")
    async with SessionLocal() as db:
        from .services.agent_recovery import recover_orphan_tools
        interrupted_tools = await recover_orphan_tools(db)
        if interrupted_tools:
            print(f"[INFO] {interrupted_tools} 个孤儿工具调用已标记为 interrupted")
        recovered_runs = await agent.recover_interrupted_runs(db)
        if recovered_runs:
            print(f"[INFO] {recovered_runs} 个 started Agent 回合已恢复为 interrupted")
    from .services.agent_recovery import recover_startup as recover_agent_startup
    app.state.agent_recovery = await recover_agent_startup()
    # 订阅体系回填：老用户补个人组织+free 订阅（幂等）
    from .models import User
    from .services.quota import ensure_free_plan, ensure_user_org
    async with SessionLocal() as db:
        await ensure_free_plan(db)
        users = (await db.scalars(select(User))).all()
        for u in users:
            await ensure_user_org(db, u)
        await db.commit()
    try:
        ensure_bucket()
    except Exception as e:  # S3 未就绪时给出清晰提示，不静默
        print(f"[WARN] S3 bucket 初始化失败（SeaweedFS 未启动？）: {e}")
    from .routers.viz import start_viz_reaper
    app.state.viz_reaper_task = start_viz_reaper()
    from .services.eventbus import start as start_event_bus
    start_event_bus()
    from .services.durable_events import dispatch_forever
    app.state.outbox_stop = asyncio.Event()
    app.state.outbox_task = asyncio.create_task(dispatch_forever(SessionLocal, app.state.outbox_stop))
    from .services.job_delivery import recover_forever
    app.state.delivery_stop = asyncio.Event()
    app.state.delivery_task = asyncio.create_task(recover_forever(app.state.delivery_stop))
    from .services.workflow_dispatch import recover_forever as recover_workflows
    app.state.workflow_dispatch_stop=asyncio.Event()
    app.state.workflow_dispatch_task=asyncio.create_task(recover_workflows(app.state.workflow_dispatch_stop))
    from .services.workflow_fallback import recover_forever as recover_failure_fallbacks
    app.state.fallback_stop = asyncio.Event()
    app.state.fallback_task = asyncio.create_task(recover_failure_fallbacks(app.state.fallback_stop))
    from .services.website_auth import expiry_loop
    app.state.auth_expiry_stop = asyncio.Event()
    app.state.auth_expiry_task = asyncio.create_task(expiry_loop(app.state.auth_expiry_stop))
    if s.storage_maintenance_enabled:
        from .services.storage_maintenance import run_forever
        app.state.storage_maintenance_stop = asyncio.Event()
        app.state.storage_maintenance_task = asyncio.create_task(run_forever(app.state.storage_maintenance_stop))


@app.on_event("shutdown")
async def shutdown():
    dispatch_stop=getattr(app.state,'workflow_dispatch_stop',None)
    dispatch_task=getattr(app.state,'workflow_dispatch_task',None)
    if dispatch_stop is not None:dispatch_stop.set()
    if dispatch_task is not None:
        dispatch_task.cancel()
        await asyncio.gather(dispatch_task,return_exceptions=True)
    fallback_stop, fallback_task = getattr(app.state,'fallback_stop',None), getattr(app.state,'fallback_task',None)
    if fallback_stop is not None:
        fallback_stop.set()
    if fallback_task is not None:
        fallback_task.cancel()
        await asyncio.gather(fallback_task, return_exceptions=True)
    maintenance_stop = getattr(app.state, 'storage_maintenance_stop', None)
    maintenance_task = getattr(app.state, 'storage_maintenance_task', None)
    if maintenance_stop is not None:
        maintenance_stop.set()
    if maintenance_task is not None:
        maintenance_task.cancel()
        await asyncio.gather(maintenance_task, return_exceptions=True)
    from .services.publication_finalizing import shutdown as stop_finalization_recovery
    await stop_finalization_recovery()
    delivery_stop, delivery_task = getattr(app.state,'delivery_stop',None),getattr(app.state,'delivery_task',None)
    if delivery_stop is not None:
        delivery_stop.set()
    if delivery_task is not None:
        delivery_task.cancel()
        await asyncio.gather(delivery_task,return_exceptions=True)
    auth_stop, auth_task = getattr(app.state, 'auth_expiry_stop', None), getattr(app.state, 'auth_expiry_task', None)
    if auth_stop is not None:
        auth_stop.set()
    if auth_task is not None:
        await asyncio.gather(auth_task, return_exceptions=True)
    reaper=getattr(app.state,'viz_reaper_task',None)
    if reaper is not None:
        reaper.cancel();await asyncio.gather(reaper,return_exceptions=True)
    from .routers.terminal import close_all_terminal_sessions
    shutdown_error=None
    try:await close_all_terminal_sessions()
    except Exception as error:shutdown_error=error
    from .routers.viz import close_all_viz_sessions
    try:await close_all_viz_sessions()
    except Exception as error:shutdown_error=shutdown_error or error
    from .services.codex_session_requests import close_all as close_all_codex_startups
    try:await close_all_codex_startups()
    except Exception as error:shutdown_error=shutdown_error or error
    from .services.codex_sessions import close_all as close_all_codex
    try:await close_all_codex()
    except Exception as error:shutdown_error=shutdown_error or error
    from .services.acp_execution import close_all as close_all_acp
    try:await close_all_acp()
    except Exception as error:shutdown_error=shutdown_error or error
    stop, task = getattr(app.state, "outbox_stop", None), getattr(app.state, "outbox_task", None)
    if stop is not None:
        stop.set()
    if task is not None:
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    await rate_limit.close()
    from .services.eventbus import close as close_event_bus
    await close_event_bus()
    if shutdown_error is not None:raise shutdown_error


@app.get("/api/health")
async def health():
    return {"ok": True, "service": "vibeflow-platform", "version": "0.1.0"}


app.include_router(auth.router)
app.include_router(website_auth.router)
app.include_router(native_history.router)
app.include_router(workspaces.router)
app.include_router(material_library.router)
app.include_router(files.router)
app.include_router(manual_files.router)
app.include_router(upload_recovery.router)
app.include_router(terminal.router)
app.include_router(llm.router)
app.include_router(voice.router)
app.include_router(agent.router)
app.include_router(codex.router)
app.include_router(acp.router)
app.include_router(jobs.router)
app.include_router(execution_submissions.router)
app.include_router(workflows.router)
app.include_router(mesh.router)
from .routers import solve
app.include_router(solve.router)
app.include_router(mpi.router)
app.include_router(events.router)
app.include_router(connection_tickets.router)
app.include_router(cadscene.router)
app.include_router(geometry.router)
app.include_router(flotherm.router)
app.include_router(postprocess.router)
app.include_router(reports.router)
app.include_router(remote.router)
app.include_router(remote_hpc.router)
app.include_router(skills.router)
app.include_router(scnet.router)
app.include_router(logs.router)
app.include_router(viz.router)
app.include_router(viz_collaboration.router)
app.include_router(mineru.router)
app.include_router(model_preferences.router)
app.include_router(completion.router)
app.include_router(runtime.router)
app.include_router(vizproxy.router)
app.include_router(index.router)
app.include_router(cite.router)
app.include_router(opencode_cfg.router)
from .routers import cli_contracts
app.include_router(cli_contracts.router)
app.include_router(calculix.router)
app.include_router(su2.router)
app.include_router(elmer.router)
app.include_router(moose.router)
app.include_router(code_aster.router)
app.include_router(paraview.router)
app.include_router(udf.router)
app.include_router(org.router)
