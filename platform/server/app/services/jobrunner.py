"""作业执行器：子进程在算例工作区内跑，日志写存储 + 内存环形缓冲供 SSE 实时推送

桌面版对应物：VFProcessSupervisor + VibeFlowWorker（求解器作业管理）。
当前为单进程内实现（开发形态）；多副本部署时换 Redis pub/sub 扇出（见方案 §R7）。

状态持久化契约：内存注册表是实时态，DB 是持久态——start/finish/cancel 都回写 jobs 表，
服务启动逐项核对持久 worker 出生身份，不把其他实例的活作业当成孤儿。
"""
from __future__ import annotations

import asyncio
import codecs
from contextlib import nullcontext
import json
import os
from pathlib import Path
import secrets
import shlex
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from ..storage import (_fs_path, prepare_job_staging, cleanup_job_staging, _use_s3)
from .runtime_config import get_runtime_setting
from .supervisor import register as supervisor_register, unregister as supervisor_unregister
from . import job_execution
from .async_ops import finish_before_cancel

LOG_DIR_REL = ".vibeflow/jobs"  # 算例内日志目录
FinishHook = Callable[[str, int | None], Awaitable[None]]


class JobStateConflict(RuntimeError):
    def __init__(self, job_id: int, current: str, requested: str):
        self.current = current
        super().__init__(f"Job {job_id}: {current} cannot transition to {requested}")


async def _persist_status(job_id: int, status: str, exit_code: int | None = None, *, unclaimed_only=False,
                          actor_user_id=None):
    """Critical completion boundary: DB errors propagate, terminal states are immutable."""
    exit_code=job_execution.database_exit_code(exit_code)
    from ..db import SessionLocal  # 延迟导入，避免模块级循环
    from ..models import Case, Job, JobAttempt
    from sqlalchemy import select, update
    from .durable_events import append_event, reserve_case_sequence
    terminal = {"succeeded", "failed", "cancelled", "interrupted"}
    allowed = {"running": {"queued"}, "succeeded": {"running"},
               "failed": {"queued", "running"}, "cancelled": {"queued", "running"},
               "interrupted": {"queued", "running"}}
    if status not in allowed:
        raise ValueError("Unsupported Job status: " + status)
    if unclaimed_only and status != "cancelled":
        raise ValueError("Unclaimed-only transition supports cancellation only")
    async with SessionLocal() as db:
        j = await db.get(Job, job_id)
        if j is None:
            raise LookupError(f"Job {job_id} does not exist")
        if j.status == status:
            if status in terminal and j.ended_at is None:
                await db.execute(update(Job).where(Job.id == job_id, Job.status == status,
                    Job.ended_at.is_(None)).values(ended_at=datetime.now(timezone.utc)))
                await db.commit()
            return
        case = await db.get(Case, j.case_id)
        case_id = j.case_id
        values = {"status": status, "exit_code": exit_code}
        if status in terminal:
            values["ended_at"] = datetime.now(timezone.utc)
        # Serialize case control writes in the same order as workflow state writes.
        seq = await reserve_case_sequence(db, case_id)
        if unclaimed_only:
            await db.refresh(j)
            attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == job_id)
                .order_by(JobAttempt.number.desc()).limit(1))
            if j.status != "queued" or (attempt is not None and
                    (attempt.phase != "queued" or attempt.claim_token is not None)):
                raise JobStateConflict(job_id, attempt.phase if attempt is not None else j.status, status)
        await job_execution.record_external_terminal(db, j, status)
        result = await db.execute(update(Job).where(Job.id == job_id,
            Job.status.in_(allowed[status])).values(**values))
        if result.rowcount != 1:
            await db.refresh(j)
            raise JobStateConflict(job_id, j.status, status)
        await append_event(db, case_id, "jobs.updated", {"job_id":job_id,
            "case_id":case_id, "status":status, "exit_code":exit_code}, reserved_seq=seq)
        if status == 'failed':
            from .workflow_fallback import record_required
            await db.refresh(j)
            await record_required(db, j)
        if status == 'cancelled' and actor_user_id is not None:
            from . import audit
            await audit.case_event(db, case, actor_user_id, 'job.cancel.requested', 'job', job_id)
        await db.commit()
        if status == "failed" and case is not None:
            from .workflow_fallback import settle_failure_fallback
            try:
                await settle_failure_fallback(db, case, j)
            except Exception as error:
                await db.rollback()
                print(f"[WARN] 作业 #{job_id} 失败阶段回退失败: {type(error).__name__}")


def _read_summary(prefix: str, log_key: str | None) -> str:
    from .file_read import FileRead
    from .logparse import parse_log_file, parse_solver_log
    if log_key:
        parsed = parse_log_file(FileRead(prefix, log_key), False, 0, None, skip_oversized=True)
    else:
        parsed = parse_solver_log("")
    return json.dumps(parsed, ensure_ascii=False, allow_nan=False)


def _write_solve_evidence(cwd: Path, snapshot: dict, log_path: Path, job_id: int):
    from .case_evidence import complete_solve_evidence
    private_evidence = not log_path.resolve().is_relative_to(cwd.resolve())
    if private_evidence:
        from .attempt_workspace import ordinary, checked_path
        parent=cwd
        for part in ('.vibeflow','jobs'):
            parent=parent/part;parent.mkdir(exist_ok=True);ordinary(parent)
        private_log=parent/f'job-{job_id}.log'
        with private_log.open('xb') as stream:
            stream.write(log_path.read_bytes())
        log_path=private_log
        run=checked_path(cwd,snapshot['run_dir']);marker=run/(run.name+'.foam')
        if marker.exists() or marker.is_symlink():ordinary(marker)
        else:
            with marker.open('xb'):pass
    evidence = complete_solve_evidence(cwd, snapshot, log_path)
    evidence_path = log_path.with_name(f"job-{job_id}-evidence.json")
    if private_evidence:
        # A command must not pre-create or redirect trusted finalizer evidence.
        with evidence_path.open('x',encoding='utf-8') as stream:
            stream.write(json.dumps(evidence,ensure_ascii=False))
    else:
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")


async def _upload_remote_log(owner, prefix, path):
    from .job_log_storage import upload
    await upload(owner,prefix,path)


async def _persist_summary(job_id: int):
    """作业结束后自动结构化求解日志；解析失败不影响作业终态。"""
    try:
        from ..db import SessionLocal
        from ..models import Case, Job
        from ..config import get_settings
        from .request_deadline import scope
        from sqlalchemy import select, update
        async with SessionLocal() as db:
            job = await db.get(Job, job_id)
            case = await db.get(Case, job.case_id) if job is not None else None
            if job is None or case is None:
                return
            case_id, prefix, log_key = case.id, case.s3_prefix, job.log_s3_key
            previous = job.parsed_summary
        # Storage and parsing may be slow. Keep both the event loop and database
        # connection available, then conditionally publish against the same binding.
        async with scope(get_settings().storage_measurement_timeout_seconds) as deadline:
            summary = await finish_before_cancel(asyncio.to_thread(_read_summary, prefix, log_key))
            deadline.check()
        async with SessionLocal() as db:
            await db.execute(update(Job).where(Job.id == job_id, Job.case_id == case_id,
                Job.log_s3_key == log_key, Job.parsed_summary == previous,
                select(Case.id).where(Case.id == case_id, Case.s3_prefix == prefix).exists()
            ).values(parsed_summary=summary))
            await db.commit()
    except Exception as error:
        print(f"[WARN] 作业 #{job_id} 日志结构化失败: {type(error).__name__}")


def win_to_wsl_path(win_path: Path) -> str:
    """D:\\x\\y → /mnt/d/x/y"""
    p = str(win_path).replace("\\", "/")
    if len(p) >= 2 and p[1] == ":":
        p = f"/mnt/{p[0].lower()}{p[2:]}"
    return p


class JobProcess:
    def __init__(self, job_id: int, command: str, cwd: Path, log_path: Path,
                 runtime: str = "windows", on_finish: FinishHook | None = None,
                 argv: list[str] | None = None, timeout_seconds: float = 86400,
                 max_log_bytes: int = 16 * 1024 * 1024,
                 staging_prefix: str | None = None, source_prefix: str | None = None,
                 owner: job_execution.Claim | None = None):
        self.job_id = job_id
        self.command = command
        self.cwd = cwd
        self.log_path = log_path
        self.runtime = runtime
        self.on_finish = on_finish
        self.argv = argv
        self.timeout_seconds = timeout_seconds
        self.max_log_bytes = max_log_bytes
        self._log_bytes = 0
        self._watchdog: asyncio.Task | None = None
        self.staging_prefix = staging_prefix
        self.source_prefix = source_prefix
        self.proc: asyncio.subprocess.Process | None = None
        self.status = "queued"           # queued|running|succeeded|failed|cancelled
        self.exit_code: int | None = None
        self.created_at = time.time()
        self.ended_at: float | None = None
        self._buf: list[str] = []        # 环形缓冲（SSE 新订阅者先补发）
        self._subscribers: list[asyncio.Queue] = []
        self._diagnostics: list[dict] = []
        self._diagnostic_bytes = 0
        self._diagnostics_sealed = False
        self.evidence_snapshot: dict | None = None
        self.isolated_workspace = False
        self.remote_workspace_prefix = None
        self.finished = False
        self.phase = "queued"
        self.completion_error: str | None = None
        self.failure_reason = ""
        self._pump_task: asyncio.Task | None = None
        self.owner = owner
        from .execution_budget import current
        self.execution_budget = current(owner)
        self._heartbeat_task: asyncio.Task | None = None
        self._log_handle = None
        self._owned_process = None
        self._launch_registered = False
        self._launch_guard = None
        self._stop_task = None
        self._root_task = None
        self._children = None
        self._native_root = None
        self.committed_result = None
        self._storage_task = None

    async def start(self):
        await job_execution.phase(self.owner, "starting")
        self.phase = "starting"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if self.execution_budget is not None and self.execution_budget.storage_policy is not None:
            from .execution_storage import Watch
            watch=Watch(self.execution_budget,self.cwd.parent if self.isolated_workspace else self.cwd)
            self.execution_budget.storage_watch=watch
            await watch.sample()
            self.execution_budget.check()
        env = dict(os.environ)
        env["VIBEFLOW_CASE_ROOT"] = str(self.cwd)
        shell=False
        if self.argv is not None:
            argv=list(self.argv)
        elif self.runtime == "wsl-sandbox":
            wsl_cwd = win_to_wsl_path(self.cwd)
            launcher = win_to_wsl_path(Path(__file__).with_name("sandbox_wsl.sh"))
            command = ("unshare --user --map-root-user --mount --net --pid --fork --mount-proc "
                       f"bash {shlex.quote(launcher)} {shlex.quote(wsl_cwd)} {shlex.quote(self.command)}")
            argv=["wsl.exe", "-d", get_runtime_setting("wsl_distro", "vibeflow-linux"), "--exec", "bash", "-lc", command]
        elif self.runtime == "wsl":
            # --exec 避免 WSL 默认 shell 在 source OpenFOAM 前展开变量。
            wsl_cwd = win_to_wsl_path(self.cwd)
            command = f"cd {shlex.quote(wsl_cwd)} && {self.command}"
            argv=["wsl.exe", "-d", get_runtime_setting("wsl_distro", "vibeflow-linux"), "--exec", "bash", "-lc", command]
        else:
            shell=os.name=='nt'
            argv=[self.command] if shell else ['/bin/sh','-c',self.command]
        from .owned_process import OwnedProcess
        from .async_ops import finish_before_cancel
        self._owned_process=OwnedProcess()
        try:
            if self.execution_budget is not None and os.name == 'nt' and self.runtime == 'windows':
                self._owned_process.windows_compute_policy = self.execution_budget.windows_compute_policy
            if self.execution_budget is not None and os.name != 'nt' and self.runtime == 'windows':
                linux_policy = self.execution_budget.linux_compute_policy
                if linux_policy and any(linux_policy.values()):
                    self._owned_process.linux_compute_policy = linux_policy
            native_policy = self.execution_budget.native_compute_policy if self.execution_budget is not None else None
            if self.runtime in {'wsl','wsl-sandbox'} and self.argv is None:
                from types import SimpleNamespace
                from ..db import SessionLocal
                from .job_scope_guardians import sealed_native_shell
                from .job_native_scopes import allocate
                async with SessionLocal() as db:
                    job,attempt=await job_execution._owned(db,self.owner)
                    scoped=sealed_native_shell(job,attempt)
                if scoped:
                    self._native_root=SimpleNamespace(id=None,owned=self._owned_process)
                    def check_active():
                        if self.execution_budget is not None:self.execution_budget.check()
                        if self.status=='cancelled':raise job_execution.JobLaunchCancelled(self.job_id)
                    await allocate(self.owner,None,self._native_root,argv[2],check_active=check_active,
                                   compute_policy=native_policy)
                    argv=[*argv[:3],'--user','root',*argv[3:]]
            from . import job_launch_registration
            await job_launch_registration.reserve(self.owner,self._owned_process)
            from .job_scope_guardians import prepare as prepare_guardian
            await prepare_guardian(self.owner,self._owned_process,seconds=self.timeout_seconds)
            self._owned_process.on_preparation=lambda value:job_launch_registration.observed(self.owner,value)
            if os.name!='nt' and self.runtime=='windows':
                from .job_linux_namespaces import prepared as namespace_prepared
                self._owned_process.on_namespace=lambda process,init:namespace_prepared(self.owner,process,init)
            from .native_execution import NativeExecutor
            if NativeExecutor.applies(self.argv):
                self._children=NativeExecutor(self.owner,self.cwd,self.argv,self.max_log_bytes)
                windows_policy=self._owned_process.windows_compute_policy
                if windows_policy and (windows_policy['user_cpu_seconds'] or windows_policy['committed_memory_bytes']):
                    from .windows_process_scope import WindowsScope
                    shared=WindowsScope(secrets.token_hex(32),observe=False)
                    self._children.windows_compute_scope=shared
                    shared.configure_compute(windows_policy['user_cpu_seconds'],windows_policy['committed_memory_bytes'])
                    self._owned_process.windows_compute_parent=shared
                linux_policy=self._owned_process.linux_compute_policy
                if linux_policy is not None:
                    from .linux_gate_compute import SharedCompute
                    self._owned_process.linux_compute_policy=None
                    self._children.linux_compute=SharedCompute(self._owned_process.nonce,linux_policy)
                    await finish_before_cancel(self._children.linux_compute.start())
                    self._children.linux_compute.bind(self._owned_process)
                env.update(await self._children.start())
            if (native_policy and any(native_policy.values()) and self._native_root is None
                    and (self._children is None or self._children.wsl_compute is None)):
                raise ValueError('WSL compute policy requires a sealed native job scope')
            # Finish identifying the waiting gates before handling cancellation.
            # The business payload is still gated by release_process below.
            # Interrupting between CreateProcess and native identity registration
            # otherwise discards the stop receipt needed to drain this attempt.
            await finish_before_cancel(self._owned_process.prepare(
                argv,cwd=self.cwd,env=env,runtime=self.runtime,shell=shell))
            self.proc=self._owned_process.proc
            if self._native_root is not None:
                from .job_native_scopes import admitted
                await admitted(self.owner,self._native_root)
            await job_launch_registration.prepared(self.owner,self._owned_process)
            await job_execution.phase(self.owner,"running",identity=self._owned_process.record)
            self._launch_registered = True
            if self._children is not None:self._children.parent_nonce=self._owned_process.nonce
            await job_execution.release_process(self.owner,self._owned_process)
        except BaseException as error:
            if self._launch_guard is not None:
                error = self._launch_guard.translate(error)
            try:
                if self._owned_process.record is not None and not self._owned_process.closed:
                    reason='cancelled' if isinstance(error,(asyncio.CancelledError,job_execution.JobLaunchCancelled)) else 'ownership_lost'
                    await finish_before_cancel(self._owned_process.stop(reason=reason,grace_ms=0))
            finally:
                try:await finish_before_cancel(self._owned_process.abort())
                finally:
                    try:
                        if self._children is not None:await finish_before_cancel(self._children.close())
                    finally:await finish_before_cancel(self._retire_native_root())
            # Read/write the authoritative registration under its original
            # claim. The callback may have committed before losing its reply.
            try:
                from .job_launch_registration import aborted
                await finish_before_cancel(aborted(self.owner,self._owned_process))
            except BaseException as persistence_error:
                error.add_note('Startup cleanup registration unavailable: '+type(persistence_error).__name__)
            raise error
        self.status = "running"
        self.phase = "running"
        self._root_task=asyncio.create_task(self._watch_root_exit())
        self._root_task.add_done_callback(lambda task:task.exception() if not task.cancelled() else None)
        self._pump_task = asyncio.create_task(self._run())
        # Local API jobs may have no waiter; still retrieve background exceptions.
        self._pump_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        self._watchdog = asyncio.create_task(self._enforce_timeout())
        self._heartbeat_task = asyncio.create_task(self._heartbeat())
        if self.execution_budget is not None and self.execution_budget.storage_watch is not None:
            self._storage_task=asyncio.create_task(self.execution_budget.storage_watch.run())

    def _stop_native(self,reason='cancelled',grace_ms=2000):
        if self._children is not None:self._children.stop(reason,worker_only=reason=='root_exit')
        if self._owned_process is not None:
            if self._stop_task is None:
                self._stop_task=asyncio.create_task(self._stop_owned(reason,grace_ms))
                self._stop_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        elif self.proc is not None and self.proc.returncode is None:
            self.proc.kill()

    async def _retire_native_root(self):
        if self._native_root is not None:
            from .job_native_scopes import retire
            await retire(self.owner,self._native_root)

    async def _stop_owned(self,reason,grace_ms):
        try:
            return await self._owned_process.stop(reason=reason,grace_ms=grace_ms)
        finally:
            from .async_ops import finish_before_cancel
            await finish_before_cancel(self._retire_native_root())

    async def _watch_root_exit(self):
        await self._owned_process.wait_root_exit()
        self._stop_native('root_exit',0)
        await asyncio.shield(self._stop_task)
        if self._children is not None:await self._children.drain_workers()

    async def _next_log_chunk(self):
        if self._root_task is None:
            return await self.proc.stdout.read(4096)
        read=asyncio.create_task(self.proc.stdout.read(4096))
        try:
            await asyncio.wait({read,self._root_task},return_when=asyncio.FIRST_COMPLETED)
            if self._root_task.done():
                await self._root_task
                return await asyncio.wait_for(read,timeout=5)
            return await read
        finally:
            if not read.done():
                read.cancel()
                await asyncio.gather(read,return_exceptions=True)

    async def _heartbeat(self):
        from ..config import get_settings
        try:
            while not self.finished:
                await asyncio.sleep(get_settings().job_heartbeat_seconds)
                status = await job_execution.heartbeat(self.owner)
                if status in {"cancelled", "interrupted"}:
                    self.cancel()
                    self.status = status
                    return
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # No further native work when durable ownership cannot be established.
            self.status = "failed"
            self.failure_reason = "persistence_failed"
            self._diagnostic(f"\n[ownership error] {type(error).__name__}\n")
            if self._children is not None or (self.proc is not None and self.proc.returncode is None):
                self._stop_native('ownership_lost',500)

    @property
    def public_status(self):
        # Legacy cancelled means the cancellation request was accepted. Attempt
        # phase/finished still distinguish draining from actual process completion.
        return "running" if self.status in {"succeeded", "failed"} and not self.finished else self.status

    async def wait(self):
        """Wait for evidence, sync and durable terminal commit, not the finish hook."""
        if self._pump_task is None:
            raise RuntimeError("Job has not started")
        await asyncio.shield(self._pump_task)
        return self.status

    def _check_success(self):
        if self.status != 'succeeded':
            raise job_execution.ExecutionConflict('state_conflict',
                'Job success was invalidated during finalization')

    async def _run(self):
        try:
            with self._children.local() if self._children is not None else nullcontext():
                await self._pump()
        except BaseException as error:
            if self.finished:
                # Cancellation of optional post-commit work cannot retract the
                # durable result or turn a successful SSE completion into an error.
                supervisor_unregister(f"job:{self.job_id}")
                raise
            self.phase = "finalization_failed"
            self.completion_error = f"Job finalization did not complete: {type(error).__name__}"
            self._diagnostic("\n[finalization error] " + self.completion_error + "\n")
            if self._watchdog is not None:
                self._watchdog.cancel()
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
            if self.proc is not None and self.proc.returncode is None:
                self._stop_native('finalization_error',500)
                if self._stop_task is not None:
                    await self._stop_task
                await asyncio.wait_for(self.proc.stdout.read(), timeout=10)
            for queue in self._subscribers:
                queue.put_nowait(None)  # Router emits error, never a successful done.
            supervisor_unregister(f"job:{self.job_id}")
            raise
        finally:
            if self._storage_task is not None:
                self._storage_task.cancel()
                from .async_ops import finish_before_cancel
                await finish_before_cancel(asyncio.gather(self._storage_task,return_exceptions=True))
            if self._log_handle is not None:
                self._log_handle.close()
            try:
                if self._stop_task is not None and not self._stop_task.done():
                    from .async_ops import finish_before_cancel
                    await finish_before_cancel(self._stop_task)
            finally:
                if self._root_task is not None and not self._root_task.done():
                    self._root_task.cancel()
                    await asyncio.gather(self._root_task,return_exceptions=True)
                try:
                    if self._children is not None:
                        from .async_ops import finish_before_cancel
                        await finish_before_cancel(self._children.close())
                finally:
                    if self._owned_process is not None:self._owned_process.close()

    async def _wait_for_budget(self, budget):
        from .execution_budget import windows_compute_violation

        owned = self._owned_process
        shared = owned.windows_compute_parent if owned is not None else None
        scope = shared or (owned.scope if owned is not None else None)
        policy = self._owned_process.windows_compute_policy if scope is not None else None
        sampling = policy and (policy['user_cpu_seconds'] or policy['committed_memory_bytes'])
        if not sampling:
            try:await asyncio.wait_for(budget.exhausted.wait(),timeout=budget.remaining())
            except asyncio.TimeoutError:budget.trip('timeout','deadline')
            return
        while not budget.exhausted.is_set():
            try:
                await asyncio.wait_for(budget.exhausted.wait(), timeout=min(.1, budget.remaining()))
            except asyncio.TimeoutError:
                if not budget.remaining():
                    budget.trip('timeout','deadline')
                elif shared is not None or (self.proc.returncode is None and self._stop_task is None):
                    try:
                        violation = windows_compute_violation(policy, scope.compute_usage())
                    except OSError:
                        violation = 'compute_unverified'
                    if violation:
                        budget.trip(violation, 'windows_job')

    async def _enforce_timeout(self):
        budget=self.execution_budget
        reason='timeout'
        if budget is None:await asyncio.sleep(self.timeout_seconds)
        else:
            await self._wait_for_budget(budget)
            reason=budget.violation or 'timeout'
        if (not self.finished and self.status not in {'cancelled','interrupted'}
                and not (budget is not None and budget.committed)):
            self.status = "failed"
            self.failure_reason = "resource_limit"
            self.exit_code = -1
            if reason=='disk_limit':
                from .execution_storage import limit_message
                detail = limit_message(budget.storage_observation)
            elif reason=='disk_unverified':
                detail = "无法确认执行目录的磁盘用量"
            elif reason=='log_limit':
                detail = f"累计执行输出超过 {self.max_log_bytes} 字节"
            elif reason=='storage_limit':
                detail = "成果复制、输出归档或日志所需存储额度不足"
            elif reason=='cpu_limit':
                if self.runtime.startswith('wsl') or budget.violation_source=='wsl_cgroup':
                    detail = "WSL 作业累计 CPU 时间超过上限"
                else:
                    detail = ("Windows 作业累计用户态 CPU 时间超过上限" if os.name == 'nt'
                              else "Linux 作业累计 CPU 时间超过上限")
            elif reason=='memory_limit':
                if self.runtime.startswith('wsl') or budget.violation_source=='wsl_cgroup':
                    detail = "WSL 作业内存与交换空间超过上限"
                else:
                    detail = ("Windows 作业申请的内存超过上限" if os.name == 'nt'
                              else "Linux 作业内存与交换空间超过上限")
            elif reason=='compute_unverified':
                detail = "无法确认作业的 CPU 和内存用量"
            else:
                detail = f"作业超过 {self.timeout_seconds:g}s 墙钟上限"
            self._diagnostic(f"\n[resource limit] {detail}，正在终止\n")
            try:
                self._stop_native(reason,500)
            except Exception:
                pass

    async def _pump(self):
        logf = open(self.log_path, "ab")
        self._log_handle = logf
        decoder=codecs.getincrementaldecoder('utf-8')('replace')
        compute_usage = None
        try:
            while True:
                chunk = await self._next_log_chunk()
                if not chunk:
                    break
                allowance=(self.execution_budget.consume_output(len(chunk),'root')
                    if self.execution_budget is not None else len(chunk))
                aggregate_exceeded=allowance<len(chunk)
                chunk=chunk[:allowance]
                if aggregate_exceeded or self._log_bytes + len(chunk) > self.max_log_bytes:
                    remaining = max(0, self.max_log_bytes - self._log_bytes)
                    chunk = chunk[:remaining]
                    self.status = "failed"
                    self.failure_reason = "resource_limit"
                    self.exit_code = -1
                    if self._log_bytes<=self.max_log_bytes and self._stop_task is None:
                        message = f"\n[resource limit] 日志超过 {self.max_log_bytes} 字节，正在终止\n"
                        self._diagnostic(message)
                    try:
                        self._stop_native('log_limit',0)
                    except Exception:
                        pass
                self._log_bytes += len(chunk)
                text = decoder.decode(chunk)
                logf.write(chunk)
                logf.flush()
                self._buf.append(text)
                if len(self._buf) > 2000:  # 环形
                    self._buf = self._buf[-1000:]
                for q in self._subscribers:
                    q.put_nowait(text)
            tail=decoder.decode(b'',final=True)
            if tail:
                self._buf.append(tail)
                for q in self._subscribers:q.put_nowait(tail)
            if self._root_task is not None:
                await self._root_task
            self.exit_code = job_execution.database_exit_code(await self.proc.wait())
            if self._stop_task is not None:
                await self._stop_task
            if self._owned_process is not None:
                await self._owned_process.finish()
                scope = self._owned_process.scope
                linux_usage = self._owned_process.linux_compute_usage
                if linux_usage is not None:
                    compute_usage = linux_usage
                    violation = linux_usage['violation']
                    if violation and self.status not in {'cancelled','interrupted'}:
                        self.execution_budget.trip(violation,'linux_cgroup')
                        self.status,self.failure_reason = 'failed','resource_limit'
                if scope is not None and self.runtime == 'windows' and self._owned_process.windows_compute_parent is None:
                    from .execution_budget import windows_compute_receipt
                    policy = self._owned_process.windows_compute_policy
                    compute_usage = windows_compute_receipt(policy,scope)
                    violation = compute_usage['violation']
                    if violation and self.status not in {'cancelled', 'interrupted'}:
                        if self.execution_budget is not None:
                            self.execution_budget.trip(violation, 'windows_job')
                        self.status, self.failure_reason = 'failed', 'resource_limit'
            if self._native_root is not None and self.execution_budget is not None:
                native=self.execution_budget.native_compute_policy
                if native and any(native.values()):
                    from .execution_budget import native_compute_violation
                    receipt=self._native_root.job_native_scope['retired']
                    observation=(receipt or {}).get('result',{}).get('compute_usage')
                    violation=native_compute_violation(native,observation) if observation else 'compute_unverified'
                    if violation and self.status not in {'cancelled','interrupted'}:
                        self.execution_budget.trip(violation,'wsl_cgroup')
                        self.status,self.failure_reason='failed','resource_limit'
            self.status = self.status if self.status in ("cancelled", "failed", "interrupted") else (
                "succeeded" if self.exit_code == 0 else "failed")
            if self.status=='failed' and not self.failure_reason and self._owned_process is not None:
                from .native_resource_failure import for_owner
                self.failure_reason=await for_owner(self.owner,self._owned_process.stop_result)
        except Exception as e:
            self.status = "failed"
            self.exit_code = -1
            self._diagnostic(f"\n[executor error] {e}\n")
            self._stop_native('executor_error',500)
            if self._stop_task is not None:
                try:
                    await self._stop_task
                except Exception as cleanup_error:
                    self.failure_reason='process_cleanup_failed'
                    self._diagnostic(f'\n[process cleanup error] {type(cleanup_error).__name__}\n')
        finally:
            self.phase = "finalizing"
            # Append after stdout EOF/stop, before any immutable log digest.
            # Late diagnostics remain outside the sealed file and its cursor.
            self._seal_diagnostics(logf)
            try:
                persisted_phase = await job_execution.phase(self.owner, "finalizing", compute_usage=compute_usage)
            except BaseException:
                logf.close()
                raise
            if persisted_phase in {"cancelled", "interrupted", "failed"}:
                self.status = persisted_phase
            logf.flush()
            from . import finalizing_stage
            early_stage=await finalizing_stage.begin(self)
            if self.on_finish:
                try:
                    await self.on_finish(self.status, self.exit_code)
                except Exception as e:
                    self.status = "failed"
                    self.failure_reason = "publication_failed"
                    self.exit_code = -1
                    message = f"\n[finish hook error] {e}\n"
                    logf.write(message.encode("utf-8", "replace"))
                    logf.flush()
                    self._buf.append(message)
                    for q in self._subscribers:
                        q.put_nowait(message)
            if self.evidence_snapshot is not None and self.status == "succeeded":
                try:
                    logf.flush()
                    if early_stage is not None and early_stage.value['supported']:
                        await finalizing_stage.write_evidence(self,early_stage)
                    else:
                        await finish_before_cancel(asyncio.to_thread(_write_solve_evidence,
                            self.cwd, self.evidence_snapshot, self.log_path, self.job_id))
                except Exception as error:
                    self.status, self.exit_code = "failed", -1
                    self.failure_reason = "evidence_invalid"
                    message = f"\n[evidence error] {error}\n"
                    logf.write(message.encode("utf-8", "replace"))
                    self._buf.append(message)
                    for q in self._subscribers:
                        q.put_nowait(message)
            if self.isolated_workspace:
                try:
                    logf.flush()
                    await finalizing_stage.result(self,early_stage)
                    capture_status,capture_code,capture_reason=self.status,self.exit_code,self.failure_reason
                    from .job_finalizing_source import capture as capture_finalizing_source
                    finalizing_source=await capture_finalizing_source(self.owner,self.cwd,self.log_path,
                        capture_status,capture_code,capture_reason)
                    from .attempt_workspace import runtime_workspace
                    await runtime_workspace(self.owner,complete=True,completion=(capture_status,capture_code,
                        self._owned_process.stop_result if self._owned_process else None),finalizing_source=finalizing_source)
                except Exception as error:
                    from .publication_inputs import PublicationInputStale
                    self.status, self.exit_code = "failed", -1
                    self.failure_reason = 'input_stale' if isinstance(error, PublicationInputStale) else 'evidence_invalid'
                    message = f"\n[attempt workspace error] {type(error).__name__}: {error}\n"
                    logf.write(message.encode("utf-8", "replace"))
                    self._buf.append(message)
            sync_succeeded = not self.staging_prefix
            log_uploaded = True
            if self.remote_workspace_prefix:
                try:
                    logf.flush()
                    await finish_before_cancel(_upload_remote_log(
                        self.owner,self.remote_workspace_prefix,self.log_path))
                except Exception as error:
                    log_uploaded=False
                    sync_succeeded=False
                    self.status,self.exit_code,self.failure_reason='failed',-1,'sync_failed'
                    message=f'\n[attempt log upload error] {type(error).__name__}; local evidence retained\n'
                    logf.write(message.encode('utf8'));logf.flush();self._buf.append(message)
            if self.staging_prefix and log_uploaded:
                try:
                    logf.flush()
                    from .staging_sync import sync_job
                    await sync_job(self.owner, self.staging_prefix, self.source_prefix)
                    sync_succeeded = True
                except Exception as error:
                    self.status = "failed"
                    self.failure_reason = "sync_failed"
                    self.exit_code = -1
                    message = (f"\n[staging error] S3 产物同步失败（{type(error).__name__}）；"
                               "已保留本地 staging，未覆盖远端更新\n")
                    logf.write(message.encode("utf-8", "replace"))
                    logf.flush()
                    self._buf.append(message)
                    for q in self._subscribers:
                        q.put_nowait(message)
                    print(f"[WARN] 作业 #{self.job_id} S3 产物上传失败: {type(error).__name__}")
            logf.close()
            await _persist_summary(self.job_id)
            try:
                completed = await job_execution.finish(self.owner, self.status, self.exit_code,
                    reason=self.failure_reason,
                    process_stop=self._owned_process.stop_result if self._owned_process else None,
                    return_result=True,check_success=self._check_success,content_drained=sync_succeeded)
            except JobStateConflict as conflict:
                # A durable cancellation/interruption may win before this callback.
                # Preserve that terminal result instead of rewriting it or hanging SSE.
                if conflict.current not in {"succeeded", "failed", "cancelled", "interrupted"}:
                    raise
                completed = await job_execution.committed_result(self.owner)
            self.committed_result = completed
            self.status, self.exit_code, self.ended_at = completed.status, completed.exit_code, completed.ended_at
            self.finished = True
            self.phase = "completed"
            if self._watchdog is not None:
                self._watchdog.cancel()
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
            for q in self._subscribers:
                q.put_nowait(None)  # 先通知 SSE 结束，不让可选 FTS 重建拖住客户端
            # Committed terminal events drive durable index refresh. The outbox
            # can finish it after this worker exits, without delaying Job.wait().
            if sync_succeeded and self.staging_prefix:
                try:
                    await finish_before_cancel(asyncio.to_thread(cleanup_job_staging, self.staging_prefix))
                except Exception as error:
                    print(f"[WARN] 作业 #{self.job_id} staging 清理失败: {type(error).__name__}")
            supervisor_unregister(f"job:{self.job_id}")

    def interrupt(self):
        """Worker shutdown stops execution; already-started finalization drains."""
        if self.finished or self.phase in {"completed", "finalizing", "finalization_failed"}:
            return
        if self.status in {"cancelled", "failed", "interrupted"}:
            return
        self.status = "interrupted"
        self.failure_reason = "recovery_required"
        self._stop_native('ownership_lost')

    def cancel(self):
        if self.finished or self.phase == "completed":
            return
        self.status = "cancelled"
        if self._launch_guard is not None:
            # Startup owns its cleanup. Do not race stop() against prepare(),
            # which may still be collecting the waiting gate's birth identity.
            self._launch_guard.cancel_launch()
            return
        try:
            self._stop_native()
        except Exception:
            pass

    def _diagnostic(self, message: str):
        remaining = 65536 - self._diagnostic_bytes
        if remaining <= 0:
            return
        text = message.encode('utf8', 'replace')[:remaining].decode('utf8', 'ignore')
        if not text:
            return
        self._diagnostic_bytes += len(text.encode('utf8'))
        self._diagnostics.append({'text': text, 'persisted': False})
        self._buf.append(text)
        for queue in self._subscribers:
            queue.put_nowait(text)

    def _seal_diagnostics(self, log):
        self._diagnostics_sealed = True
        selected = [row for row in self._diagnostics if not row['persisted']]
        for row in selected:
            log.write(row['text'].encode('utf8'))
        log.flush()
        for row in selected:
            row['persisted'] = True

    def legacy_diagnostics(self, cursor: int):
        """Only memory-only diagnostics; never re-emit bytes now in the file."""
        if not self._diagnostics_sealed and self.completion_error is None:
            return cursor, ''
        return len(self._diagnostics), ''.join(row['text'] for row in self._diagnostics[cursor:] if not row['persisted'])

    def backlog(self) -> str:
        return "".join(self._buf)

    def subscribe(self) -> tuple[asyncio.Queue, str]:
        """订阅输出。返回 (队列, 订阅瞬间的 backlog 快照)。

        asyncio 单线程内 _buf.append 与订阅者推送之间无 await，
        快照+注册是原子的，不丢增量；已结束的作业立即补结束哨兵（修复竞态：
        作业在 HTTP 订阅建立前收尾时，done 事件丢失导致 SSE 客户端挂死）。
        """
        q: asyncio.Queue = asyncio.Queue()
        snap = self.backlog()
        if self.finished or self.completion_error is not None:
            q.put_nowait(None)  # 已结束：立刻发哨兵
        else:
            self._subscribers.append(q)
        return q, snap

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)


# 进程注册表（job_id → JobProcess）
_registry: dict[int, JobProcess] = {}


def get_proc(job_id: int) -> JobProcess | None:
    return _registry.get(job_id)


async def request_cancel(job_id: int, *, actor_user_id=None):
    """Commit the owner-visible cancellation before waking any live process."""
    from . import distributed_queue

    await _persist_status(job_id, 'cancelled', actor_user_id=actor_user_id)
    proc = get_proc(job_id)
    if proc is not None:
        proc.cancel()
    if distributed_queue.distributed_enabled():
        try:
            await distributed_queue.cancel(job_id)
        except Exception as error:
            print(f"[WARN] 已持久化取消，Redis 唤醒失败: {type(error).__name__}")


async def start_job(job_id: int, command: str, case_s3_prefix: str,
                    runtime: str = "windows", on_finish: FinishHook | None = None,
                    argv: list[str] | None = None, timeout_seconds: float = 86400,
                    max_log_bytes: int = 16 * 1024 * 1024,
                    evidence_run_dir: str | None = None) -> JobProcess:
    try:
        return await _start_job(job_id, command, case_s3_prefix, runtime=runtime,
            on_finish=on_finish, argv=argv, timeout_seconds=timeout_seconds,
            max_log_bytes=max_log_bytes, evidence_run_dir=evidence_run_dir)
    except job_execution.ExecutionConflict as error:
        # Admission has a durable Job even when claiming, staging or launch
        # registration fails. Preserve the conflict subtype and cleanup rules;
        # HTTP callers can locate the exact attempt without submitting again.
        error.job_id = job_id
        raise


async def _start_job(job_id: int, command: str, case_s3_prefix: str,
                     runtime: str = "windows", on_finish: FinishHook | None = None,
                     argv: list[str] | None = None, timeout_seconds: float = 86400,
                     max_log_bytes: int = 16 * 1024 * 1024,
                     evidence_run_dir: str | None = None) -> JobProcess:
    owner = await job_execution.claim_job(job_id, {"command":command, "case_prefix":case_s3_prefix,
        "runtime":runtime, "argv":argv, "timeout_seconds":timeout_seconds,
        "max_log_bytes":max_log_bytes, "evidence_run_dir":evidence_run_dir})
    from .job_launch_lease import LaunchLease
    from .execution_budget import load
    budget=await load(owner)
    with budget.activate():
        return await LaunchLease(owner).run(lambda guard: _launch_claimed(owner, guard, job_id,
            command, case_s3_prefix, runtime=runtime, on_finish=on_finish, argv=argv,
            timeout_seconds=timeout_seconds, max_log_bytes=max_log_bytes, evidence_run_dir=evidence_run_dir))


async def _launch_claimed(owner, guard, job_id, command, case_s3_prefix, *, runtime,
                          on_finish, argv, timeout_seconds, max_log_bytes, evidence_run_dir):
    from .async_ops import finish_before_cancel
    # Capture a completed staging allocation even if cancellation wins while
    # its non-cancellable copy is draining. No native payload exists yet.
    allocated = []
    isolated_cwd = None
    def prepare_paths():
        legacy_staging = _use_s3() and isolated_cwd is None
        prefix = prepare_job_staging(case_s3_prefix, job_id) if legacy_staging else case_s3_prefix
        if legacy_staging:
            allocated.append(prefix)
        directory = _fs_path(prefix, '.')
        directory.mkdir(parents=True, exist_ok=True)
        return prefix, directory, _fs_path(prefix, f'{LOG_DIR_REL}/job-{job_id}.log')
    try:
        await guard.checkpoint()
        from ..db import SessionLocal
        from ..models import Job
        async with SessionLocal() as db:
            job = await db.get(Job, job_id)
            if job is None:
                raise job_execution.ExecutionConflict('state_conflict', 'Owned job was removed')
            case_id = job.case_id
        from .attempt_workspace import runtime_workspace
        isolated_cwd = await runtime_workspace(owner)
        execution_prefix, cwd, log_path = await finish_before_cancel(asyncio.to_thread(prepare_paths))
        if isolated_cwd is not None:
            cwd = isolated_cwd
        await guard.checkpoint()
        from .handler_environment import execution_argv
        argv=await execution_argv(owner,argv)
    except BaseException as error:
        error = guard.translate(error)
        from .case_write import CaseWriteConflict
        if isinstance(error,CaseWriteConflict) and error.code in {'node_storage_full','node_storage_unavailable'}:
            from .execution_budget import current
            budget=current(owner)
            if budget is not None:
                budget.trip('disk_limit' if error.code=='node_storage_full' else 'disk_unverified','storage')
            error=job_execution.ExecutionConflict('resource_limit',str(error))
        async def settle_preparation():
            try:
                for prefix in allocated:
                    await asyncio.to_thread(cleanup_job_staging, prefix, synchronized=False)
            finally:
                await job_execution.finish(owner,
                    "cancelled" if isinstance(error, (asyncio.CancelledError, job_execution.JobLaunchCancelled)) else "failed", -1,
                    reason=getattr(error,'code',None) if getattr(error,'code',None) in {'input_stale','resource_limit'} else 'process_start_failed',
                    content_drained=True,not_started=True)
        await finish_before_cancel(settle_preparation())
        raise error
    jp = JobProcess(job_id, command, cwd, log_path, runtime=runtime, on_finish=on_finish,
                    argv=argv, timeout_seconds=timeout_seconds, max_log_bytes=max_log_bytes,
                    staging_prefix=execution_prefix if _use_s3() and isolated_cwd is None else None,
                    source_prefix=case_s3_prefix if _use_s3() and isolated_cwd is None else None, owner=owner)
    jp.isolated_workspace = isolated_cwd is not None
    jp.remote_workspace_prefix = case_s3_prefix if _use_s3() else None
    jp._launch_guard = guard
    _registry[job_id] = jp
    try:
        if evidence_run_dir is not None:
            from .case_evidence import capture_solve_inputs
            jp.evidence_snapshot = await finish_before_cancel(asyncio.to_thread(capture_solve_inputs, cwd, evidence_run_dir, job_id))
        await guard.checkpoint()
        await jp.start()
        jp._launch_guard = None
    except BaseException as error:
        error = guard.translate(error)
        jp._launch_guard = None
        requested_status='cancelled' if isinstance(error,(asyncio.CancelledError,job_execution.JobLaunchCancelled)) else 'failed'
        jp.status, jp.exit_code, jp.ended_at, jp.finished = requested_status, -1, time.time(), True
        message = f"[startup error] {type(error).__name__}: {error}\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(message, encoding="utf-8")
        jp._buf.append(message)
        content_drained=not jp.staging_prefix
        if jp.remote_workspace_prefix:
            try:
                await finish_before_cancel(_upload_remote_log(owner,jp.remote_workspace_prefix,log_path))
            except Exception:
                content_drained=False
        try:
            from .async_ops import finish_before_cancel
            jp.status = await finish_before_cancel(job_execution.finish(owner,requested_status,-1,
                reason='cancelled_before_launch' if requested_status=='cancelled' else 'resource_limit' if getattr(error,'code',None)=='resource_limit' else 'process_start_failed',
                process_stop=jp._owned_process.stop_result if jp._owned_process is not None else None,
                content_drained=content_drained))
            jp.phase = "completed"
        except JobStateConflict as conflict:
            jp.status = conflict.current
            jp.phase = "completed"
        except Exception as persistence_error:
            jp.finished = False
            jp.phase = "finalization_failed"
            jp.completion_error = f"Startup status could not be persisted: {type(persistence_error).__name__}"
        raise error
    supervisor_register("job", job_id, case_id, jp.cancel)
    return jp
