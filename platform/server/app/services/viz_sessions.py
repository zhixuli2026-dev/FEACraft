"""Owned rendering lifetime: registered gate, kernel readiness and verified close."""
import asyncio
from contextvars import Context
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from ..config import get_settings
from .async_ops import finish_before_cancel
from .case_write import CaseWriteConflict
from .execution_budget import Budget, BudgetExceeded
from . import execution_storage
from .owned_process import OwnedProcess
from . import process_identity_native as identity,viz_records as records

READY_TIMEOUT=30
HEARTBEAT_INTERVAL=5
RESOURCE_LIMIT_ERROR='VizResourceLimit'
RESOURCE_LIMIT_MESSAGES={
    'timeout':'可视化会话已达到运行时限',
    'log_limit':'可视化会话已达到累计输出限额',
    'disk_limit':'可视化工作目录用量或所在卷可用空间已达到限制',
    'disk_unverified':'无法核验可视化工作目录的磁盘用量',
    'cpu_limit':'可视化会话已达到累计 CPU 限额',
    'memory_limit':'可视化会话已达到内存限额',
    'compute_unverified':'无法启用可视化会话的 Linux 内核额度',
}


class VizSession:
    def __init__(self,case_id,user_id,selected):
        self.case_id=case_id;self.user_id=user_id;self.selected=selected;self.owned=OwnedProcess()
        self.selected['nonce']=self.owned.nonce;self.owner=None;self.id=None;self.alive=None
        self.started_at=time.time();self.last_activity=self.started_at;self.ready_birth=None;self.kernel_handle=None
        self.cleanup_verified=None;self.persisted_terminal=False;self.error=None;self.exit_code=None
        self._pump_task=None;self._watch_task=None;self._heartbeat_task=None;self._close_task=None;self._ready=None;self.logf=None
        self.finalize=None;self.on_allocated=None;self.on_closed=None
        self._startup_task=None;self._scene_task=None
        self.port_reservation=None
        self._budget=None;self._storage_task=None
        settings=get_settings()
        if sys.platform == 'linux':
            policy = {'cpu_limit_ns': settings.viz_session_linux_cpu_limit_seconds * 1_000_000_000,
                      'memory_limit_bytes': settings.viz_session_linux_memory_limit_mb * 1024**2}
            if any(policy.values()):
                self.selected['linux_compute_policy'] = policy
                self.owned.linux_compute_policy = policy
        self._lifetime=settings.viz_session_lifetime_seconds
        output_limit=settings.viz_session_max_output_bytes
        workspace_limit=settings.viz_session_workspace_limit_mb
        if self._lifetime or output_limit or workspace_limit:
            self._budget=Budget(self.owned.nonce,float('inf'),output_limit)
        if workspace_limit:
            self._budget.storage_policy=execution_storage.validate({
                'schema':execution_storage.SCHEMA,'max_workspace_bytes':workspace_limit*1024*1024,
                'min_free_bytes':settings.node_storage_min_free_mb*1024*1024,
            })
            self._budget.storage_watch=execution_storage.Watch(self._budget,selected['case_root'],scope='viz_workspace')

    @property
    def port(self):return self.selected['port']

    def touch(self):self.last_activity=time.time()

    def follow_committed_scenes(self, authorize, *, after_sequence):
        from .viz_scene_relay import relay_scenes

        if self.alive and self._close_task is None:
            self._scene_task = asyncio.create_task(relay_scenes(self, authorize, after_sequence=after_sequence),
                                                   context=Context())

    def verify_code(self):
        folder=Path(__file__).parent
        if any(hashlib.sha256((folder/(name+'.py')).read_bytes()).hexdigest()!=digest for name,digest in self.selected['code'].items()):
            raise ValueError('Visualization implementation changed before launch')

    async def allocate(self,*,db=None):
        if self.owned.linux_compute_policy is not None:
            from .linux_gate_compute import check_available
            try:
                await asyncio.to_thread(check_available, self.owned.linux_compute_policy)
            except (OSError, ValueError) as error:
                raise CaseWriteConflict('viz_compute_unverified', RESOURCE_LIMIT_MESSAGES['compute_unverified']) from error
        async def work():
            self.owner=await records.create(self.case_id,self.user_id,self.selected,db=db);self.id=self.owner.id
            if self.on_allocated is not None:self.on_allocated(self)
        await finish_before_cancel(work())

    async def start(self,reauthorize,finalize):
        self.finalize=finalize;self._startup_task=asyncio.current_task()
        try:
            if self._close_task is not None:raise RuntimeError('Visualization was closed before startup')
            if self.owner is None:await self.allocate()
            self._heartbeat_task=asyncio.create_task(self._renew())
            if self._budget is not None and self._budget.storage_watch is not None:
                await self._budget.storage_watch.sample();self._budget.check()
            await asyncio.to_thread(self.verify_code)
            log_dir=Path(self.selected['case_root'])/'.vibeflow'/'viz'
            log_name=(f'viz-{self.port}-{self.id}.log' if get_settings().viz_owner_routes else f'viz-{self.port}.log')
            def open_log():
                log_dir.mkdir(parents=True,exist_ok=True);self.logf=(log_dir/log_name).open('ab')
            await finish_before_cancel(asyncio.to_thread(open_log))
            self._ready=asyncio.get_running_loop().create_future()
            self._ready.add_done_callback(lambda future:future.exception() if not future.cancelled() else None)
            argument='--data-file' if self.selected['file_path'] is not None else '--foam'
            pipeline_scope=records.digest([self.user_id,self.case_id,argument,
                self.selected['file_path'] or self.selected['run_dir']])
            argv=self.render_command(argument,pipeline_scope)
            env = dict(os.environ)
            mesa_dir = env.get('VIBEFLOW_VTK_OSMESA_DIR')
            if os.name != 'nt' and self.selected.get('engine') != 'paraview' and mesa_dir:
                env['LD_LIBRARY_PATH'] = os.pathsep.join(filter(None, (mesa_dir, env.get('LD_LIBRARY_PATH'))))
            if self.owned.linux_compute_policy is not None:
                self.owned.on_namespace = lambda root, _init: records.register_root(self.owner, root)
            try:
                await self.owned.prepare(argv,cwd=self.selected['case_root'],env=env,runtime='windows')
            except Exception as error:
                if self.owned.on_namespace is not None and self.owned.namespace_init is None:
                    raise CaseWriteConflict('viz_compute_unverified', RESOURCE_LIMIT_MESSAGES['compute_unverified']) from error
                raise
            if self.owned.on_namespace is None:
                await records.register_root(self.owner,self.owned.record)
            await reauthorize();await records.released(self.owner);await asyncio.to_thread(self.verify_code)
            if self._budget is not None and self._budget.storage_watch is not None:
                await self._budget.storage_watch.sample();self._budget.check()
            await self.owned.release()
            if self._budget is not None:
                self._budget.attempt_id=self.id
                if self._lifetime:self._budget.deadline=time.monotonic()+self._lifetime
                if self._budget.storage_watch is not None:
                    self._storage_task=asyncio.create_task(self._budget.storage_watch.run())
            self._pump_task=asyncio.create_task(self._pump())
            if not await self._wait_running((self._ready,),READY_TIMEOUT):raise TimeoutError('Visualization readiness timed out')
            self.ready_birth=self._ready.result()
            await records.mark_ready(self.owner,self);await reauthorize()
            await self.verify_ready()
            if self._budget is not None:self._budget.check()
            if self._close_task is not None or self.owned.proc.returncode is not None:raise RuntimeError('Visualization closed during startup')
            if self._pump_task.done():self._pump_task.result();raise EOFError('Visualization output ended during startup')
            self.alive=True;self.touch();self._watch_task=asyncio.create_task(self._watch());return self
        except BaseException as error:
            # finish_before_cancel creates a cleanup task. Clear the startup
            # join target first so it cannot join the task awaiting itself.
            self._startup_task=None
            limit=self._budget.violation if isinstance(error,BudgetExceeded) else None
            if isinstance(error,CaseWriteConflict) and error.code=='viz_compute_unverified':limit='compute_unverified'
            try:await finish_before_cancel(self.close(limit or 'executor_error',error=RESOURCE_LIMIT_ERROR if limit else type(error).__name__))
            except BaseException as cleanup_error:error.add_note('Visualization startup cleanup: '+type(cleanup_error).__name__)
            usage=self.owned.linux_compute_usage
            if limit is None and self.cleanup_verified and usage:
                limit=usage['violation']
            if limit is not None:
                raise CaseWriteConflict('viz_'+limit,RESOURCE_LIMIT_MESSAGES[limit]) from error
            raise
        finally:self._startup_task=None

    def render_command(self, argument, pipeline_scope):
        selected=self.selected
        common=['--port',str(self.port),'--case-root',selected['case_root'],
                '--session-nonce',self.owned.nonce,'--profile-sha256',records.digest(selected)]
        if selected.get('engine')=='paraview':
            from .paraview import visualizer_path

            relative=Path(selected['input_path']).relative_to(selected['case_root']).as_posix()
            return [selected['python'],'--force-offscreen-rendering','-I','-u',str(Path(__file__).with_name('viz_paraview_kernel.py')),
                    '--input',relative,'--host','127.0.0.1','--timeout','0',
                    '--content',str(visualizer_path(selected['python'])),*common]
        return [selected['python'],'-I','-u',str(Path(__file__).with_name('viz_kernel.py')),
                argument,selected['input_path'],'--pipeline-scope',pipeline_scope,*common]

    async def _renew(self):
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL);await records.heartbeat(self.owner,self.last_activity)
        except asyncio.CancelledError:raise
        except Exception as error:
            task=asyncio.create_task(self.close('ownership_lost',error=type(error).__name__))
            task.add_done_callback(lambda future:future.exception() if not future.cancelled() else None)

    async def _pump(self):
        pending=b'';discard=False
        try:
            while True:
                data=await self.owned.proc.stdout.read(8192)
                if not data:break
                if self._budget is not None and self._budget.max_log_bytes:
                    allowed=self._budget.consume_output(len(data),'root')
                    if allowed<len(data):
                        if allowed:await finish_before_cancel(asyncio.to_thread(self.logf.write,data[:allowed]))
                        return
                # Drain throughout the session so a full pipe cannot block VTK.
                await finish_before_cancel(asyncio.to_thread(self.logf.write,data))
                for part in data.splitlines(keepends=True):
                    ending=part.endswith(b'\n')
                    if not discard:pending+=part
                    if len(pending)>16384:pending=b'';discard=True
                    if ending:
                        if not discard and pending.startswith(records.READY_PREFIX):
                            value=json.loads(pending[len(records.READY_PREFIX):]);records.ready_document(value,self.selected)
                            if self._ready.done():raise ValueError('Visualization sent duplicate readiness')
                            self._ready.set_result(value)
                        pending=b'';discard=False
            if not self._ready.done():raise EOFError('Visualization exited before readiness')
        except BaseException as error:
            if self._ready is not None and not self._ready.done():self._ready.set_exception(error)
            raise

    async def verify_ready(self):
        records.ready_document(self.ready_birth,self.selected);birth=self.ready_birth['process']
        if os.name=='nt':
            from .windows_process_scope import ProcessHandle
            if self.kernel_handle is None:self.kernel_handle=await finish_before_cancel(asyncio.to_thread(ProcessHandle.query,birth))
            if self.kernel_handle.identity()!=birth or not self.owned.scope.contains(self.kernel_handle):raise ValueError('Ready kernel is outside its registered process scope')
        else:
            # Map namespace-relative identity to the outside kernel PID before
            # probing; historical PID-only records are never control handles.
            from .terminal_identity_probe import locate
            actual=await asyncio.to_thread(locate,self.owned.record['host'],birth)
            if (await asyncio.to_thread(identity.probe,actual))['state']!='match':raise ValueError('Visualization birth changed')

    async def _wait_running(self,tasks,timeout=None):
        if self._budget is None:
            done,_=await asyncio.wait(tasks,timeout=timeout,return_when=asyncio.FIRST_COMPLETED)
            return done
        alarm=asyncio.create_task(self._budget.exhausted.wait())
        try:
            budget_timer=False
            if self._budget.deadline!=float('inf'):
                remaining=self._budget.remaining()
                budget_timer=timeout is None or remaining<=timeout
                timeout=remaining if timeout is None else min(timeout,remaining)
            done,_=await asyncio.wait((*tasks,alarm),timeout=timeout,return_when=asyncio.FIRST_COMPLETED)
            if not done and budget_timer:self._budget.trip('timeout','deadline')
            self._budget.check()
            return done
        finally:
            alarm.cancel()
            await asyncio.gather(alarm,return_exceptions=True)

    async def _watch(self):
        failure=None
        reason='root_exit'
        root=asyncio.create_task(self.owned.wait_root_exit())
        try:
            done=await self._wait_running((root,self._pump_task))
            if self._pump_task in done:self._pump_task.result()
            if root in done and root.result()!=0:failure='RuntimeError'
        except asyncio.CancelledError:return
        except BudgetExceeded:
            failure=RESOURCE_LIMIT_ERROR
            reason=self._budget.violation
        except Exception as error:failure=type(error).__name__
        finally:
            if not root.done():root.cancel()
            await asyncio.gather(root,return_exceptions=True)
        try:await self.close(reason,error=failure)
        except Exception:pass  # Durable failure remains visible; no successful close event.

    async def close(self,reason='cancelled',*,error=None):
        startup=self._startup_task
        if self._close_task is None and startup is not None and startup is not asyncio.current_task():
            startup.cancel();await finish_before_cancel(asyncio.gather(startup,return_exceptions=True))
        if self._close_task is None:self._close_task=asyncio.create_task(self._close(reason,error))
        return await finish_before_cancel(self._close_task)

    async def _close(self,reason,error):
        failure=error;stop=None
        try:
            if self._storage_task is not None:self._storage_task.cancel()
            if self._scene_task is not None:
                self._scene_task.cancel()
                await asyncio.gather(self._scene_task, return_exceptions=True)
                self._scene_task = None
            try:
                if self.owned.proc is not None and not self.owned.closed:stop=await self.owned.stop(reason=reason,grace_ms=500)
                else:stop=self.owned.stop_result
                if self.kernel_handle is not None:await asyncio.to_thread(self.kernel_handle.wait_exit,5)
            except BaseException as cleanup_error:
                stop=self.owned.stop_result;failure=failure or type(cleanup_error).__name__
            self.cleanup_verified=stop.get('verified') if stop is not None else None
            usage=self.owned.linux_compute_usage
            if self.cleanup_verified and usage is not None:
                stop['linux_compute_usage']=usage
                if reason in {'root_exit','executor_error'} and usage['violation']:
                    stop['reason']=usage['violation']
                    failure=RESOURCE_LIMIT_ERROR
            if self.cleanup_verified is True:self.alive=False
            elif self.owned.proc is not None:failure=failure or 'RuntimeError'
            if self._pump_task is not None:
                try:await asyncio.wait_for(asyncio.shield(self._pump_task),5)
                except BaseException as pump_error:
                    if not self._pump_task.done():self._pump_task.cancel();await asyncio.gather(self._pump_task,return_exceptions=True)
                    if not isinstance(pump_error,(EOFError,asyncio.CancelledError)):failure=failure or type(pump_error).__name__
            if self.logf is not None:await asyncio.to_thread(self.logf.close)
            if self._storage_task is not None:
                await asyncio.gather(self._storage_task,return_exceptions=True)
            if (self.cleanup_verified is True or self.owned.proc is None) and self.finalize is not None:
                try:await self.finalize(self)
                except Exception as staging_error:failure=failure or type(staging_error).__name__
            if stop is not None:
                # Raw child exit comes from the trusted gate, independently of
                # whether shutdown was requested or the process exited itself.
                replies=[item for item in stop.get('control',[]) if item['role']=='host']
                if len(replies)==1:self.exit_code=replies[0]['exit_code']
                if self.exit_code not in (None,0) and (reason=='root_exit' or not replies[0]['stop_received']):failure=failure or 'RuntimeError'
            if self.owner is not None:
                await records.complete(self.owner,self.owned.record,stop,error=failure or (None if self.cleanup_verified else 'RuntimeError'),
                    exit_code=self.exit_code,last_activity=self.last_activity)
                self.persisted_terminal=True
            self.error=failure
            if failure:raise RuntimeError('Visualization closure failed: '+failure)
            return stop
        except BaseException as error:
            self.error=self.error or type(error).__name__;raise
        finally:
            self.alive=False if self.cleanup_verified is True else None
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel();await asyncio.gather(self._heartbeat_task,return_exceptions=True)
            if self.kernel_handle is not None:self.kernel_handle.close();self.kernel_handle=None
            self.owned.close()
            if self.port_reservation is not None and (self.cleanup_verified is True or self.owned.proc is None):self.port_reservation.close()
            if self.persisted_terminal and self.cleanup_verified is True and self.on_closed is not None:self.on_closed(self)
