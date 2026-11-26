"""Bounded request-native scopes for preparation outside case write transactions."""
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import subprocess
import sys

from .async_ops import finish_before_cancel
from .native_execution import NativeExecutor
from .native_execution_client import ChildExecutionError
from . import native_request_records as records


class RequestExecutor(NativeExecutor):
    def __init__(self,owner,cwd,kind,settings):
        if kind=='job.finalize':
            argv=settings['argv'] or [sys.executable,str(Path(__file__).with_name('registered_report_worker.py')),
                '--mode','markdown','--pandoc',sys.executable,'--execution-id',owner.id]
        elif kind=='report.prepare':
            argv=[sys.executable,str(Path(__file__).with_name('registered_report_worker.py')),
                  '--mode','markdown','--pandoc',settings['pandoc'],'--execution-id',owner.id]
        elif kind=='latex.prepare':
            argv=[sys.executable,str(Path(__file__).with_name('registered_latex_worker.py')),
                  '--distro='+settings['distro'],'--recipe='+settings['recipe']]
        else:
            argv=[sys.executable,str(Path(__file__).with_name('registered_latex_worker.py'))]
        super().__init__(owner,cwd,argv,settings['max_log_bytes'] if kind=='job.finalize' else 1024*1024)
        if kind=='job.finalize':
            from .execution_budget import current
            self.recovery_profile=dict(settings);self.execution_budget=current()
            if self.execution_budget is None or self.execution_budget.attempt_id!=settings['attempt_id']:
                raise ValueError('Independent validator requires original execution accounting')
        if kind=='synctex.query':
            self.kind='synctex';self.runtime='windows' if settings['distro']=='native-linux' else 'wsl';self.profile=dict(settings)
            if settings['distro']=='native-linux' and not sys.platform.startswith('linux'):raise ValueError('Native Linux SyncTeX is unavailable')
        if kind in records.GLOBAL_KINDS:
            from .openfoam_runtime import NATIVE_LINUX
            self.kind=kind
            self.runtime='wsl' if kind=='runtime.wsl_probe' and settings['distro']!=NATIVE_LINUX else 'windows'
            self.profile=dict(settings)
        if kind=='agent.shell':self.kind=kind;self.runtime='wsl' if settings['runtime']=='wsl' else 'windows';self.profile=dict(settings)
        if kind in {'terminal.export','terminal.recover','terminal.cleanup'}:self.kind=kind;self.runtime='wsl';self.profile=dict(settings)
        self.citation_preview=kind=='report.prepare' and settings.get('purpose')=='pandoc_cite'
        self.records=records;self.parent_nonce=owner.nonce;self.worker_closed=True

    def _command(self,purpose,parameters):
        if hasattr(self,'recovery_profile'):
            from .job_output_finalizing import allowed_purposes
            if purpose not in allowed_purposes(self.recovery_profile):raise ValueError('Recovery cannot invoke a solver or another native purpose')
            return super()._command(purpose,parameters)
        if self.kind=='terminal.cleanup':
            from .sandbox_image_cleanup import command
            if purpose!='sandbox_image_cleanup':raise ValueError('Image cleanup cannot invoke another native purpose')
            return command(self.profile,parameters)
        if self.kind=='terminal.recover':
            from .sandbox_recovery_execution import command
            if purpose!='sandbox_recover':raise ValueError('Recovery cannot invoke another native purpose')
            return command(self.profile,parameters)
        if self.kind=='terminal.export':
            from .sandbox_export_execution import command
            if purpose!='sandbox_export':raise ValueError('Sandbox exporter cannot invoke another native purpose')
            return command(self.profile,parameters)
        if self.kind=='agent.shell':
            from .agent_shell import command
            if purpose!='agent_shell':raise ValueError('Agent shell cannot invoke another native purpose')
            return command(self.profile,parameters)
        if self.kind in records.GLOBAL_KINDS:
            from .runtime_probe_execution import command,KINDS
            if purpose!=KINDS[self.kind]:raise ValueError('Global runtime requests cannot invoke another purpose')
            return command(self.kind,self.profile,parameters)
        if self.kind=='synctex':
            from . import synctex_execution
            from .owned_process import wsl_path
            native_linux=self.profile['distro']=='native-linux'
            if purpose!=('synctex_query_linux' if native_linux else 'synctex_query'):raise ValueError('SyncTeX requests cannot invoke another native purpose')
            config,digest=synctex_execution.validate(parameters,self.profile)
            if native_linux:
                from .linux_job_workspace import document_argv
                argv=[str(Path(getattr(sys,'_base_executable',sys.executable)).resolve()),'-I','-S','-u',str(synctex_execution.NATIVE),
                      '--config',str(config),'--sha256',digest]
                return document_argv(self.cwd, 'synctex', argv),20,1024*1024
            return ['wsl.exe','-d',self.profile['distro'],'--exec','python3','-I','-S','-u',wsl_path(synctex_execution.NATIVE),
                    '--config',wsl_path(config),'--sha256',digest],20,1024*1024
        if self.citation_preview:
            if purpose!='pandoc_cite':raise ValueError('Citation requests require the citation formatter')
            return super()._command(purpose,parameters)
        if purpose!=('pandoc_parse' if self.kind=='report' else ('latex_probe_linux' if self.runtime=='windows' else 'latex_probe')):
            raise ValueError('Native request cannot invoke compilation or another purpose')
        return super()._command(purpose,parameters)

    async def _prepare(self,entry,argv,env):
        if hasattr(self,'recovery_profile'):
            from . import finalizing_native_scope as scopes
            await scopes.prepare(self,entry)
            if self.runtime=='wsl':
                if argv[3:6]!=['--user','nobody','--exec']:
                    raise ValueError('Independent LaTeX validator changed its fixed unprivileged command')
                # The fixed preflight launches as root, joins its exact cgroup
                # and drops to nobody before executing the original payload.
                argv=[*argv[:3],'--user','root','--exec',*argv[6:]]
        if self.kind=='terminal.cleanup':
            from . import sandbox_image_launch as launch, sandbox_image_scope as scopes
            def check_active():
                entry.check()
                if entry.event.is_set() or not self._allowed(entry.caller):
                    raise ChildExecutionError('Parent stopped during image preparation')
            check_active()
            entry.owned.check_preparation_active=check_active
            await scopes.prepare(self.owner,entry.id,entry.owned,self.profile['distro'],check_active=check_active)
            check_active()
            async def observed(value):
                await launch.observed(self.owner,entry.id,value)
                # Persist births already observed, but do not let a delayed
                # callback advance into a later gate after owner cancellation.
                check_active()
            entry.owned.on_preparation=observed
        if self.kind=='agent.shell':
            if sys.platform == 'linux' and self.runtime != 'wsl':
                env = {name:value for name,value in env.items() if name in {'PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ'}}
                env.update(HOME=str(self.cwd), TMPDIR='/tmp')
                entry.owned.isolate_linux_network = self.profile.get('linux_network') == 'private'
                entry.owned.linux_compute_policy = self.profile.get('linux_compute_policy')
                if entry.owned.isolate_linux_network or entry.owned.linux_compute_policy:

                    async def confirm_namespace(root, init):
                        entry.check()
                        if root['namespace_init'] != init or init['pid_namespace'] == root['host']['pid_namespace']:
                            raise ValueError('Agent namespace does not match the prepared process')

                    # NativeExecutor registers this complete identity before the payload is released.
                    entry.owned.on_namespace = confirm_namespace
            return await entry.owned.prepare(argv,cwd=self.cwd,env=dict(env,PYTHONIOENCODING='utf-8'),runtime=self.runtime,
                shell=False)
        return await super()._prepare(entry,argv,env)

    async def _cleanup(self,entry):
        if self.kind=='agent.shell':
            stop = entry.owned.stop_result
            if stop and stop['verified'] and entry.owned.linux_compute_usage is not None:
                stop['linux_compute_usage'] = entry.owned.linux_compute_usage
        if hasattr(self,'recovery_profile'):
            from .finalizing_native_scope import close
            await close(self.owner,entry.id,process=entry.owned)
        if self.kind=='terminal.cleanup':
            from .sandbox_image_scope import close
            from .sandbox_image_launch import seal_unstarted
            await seal_unstarted(self.owner,entry.id,entry.owned)
            try:
                await entry.owned.retire_guardian()
            finally:
                # Even an unverified Windows guardian must not skip revoking
                # the separately owned native allocation. Missing guardian
                # evidence still prevents a complete closed record.
                await close(self.owner,entry.id,guardian_proof=entry.owned.guardian_proof)

    async def _checkpoint_usage(self,entry,diagnostics=None,*,sealed=False):
        if hasattr(self,'recovery_profile'):
            from .finalizing_native_usage import checkpoint
            await checkpoint(self,entry,diagnostics,sealed=sealed)

    async def run_sync(self,function,*args,timeout=75,**kwargs):
        """Stop actual children before joining a cancelled synchronous caller."""
        if self.request_deadline is not None:
            self.request_deadline.check()
            timeout=min(timeout,self.request_deadline.remaining())
        with self.local():
            operation=asyncio.create_task(asyncio.to_thread(function,*args,**kwargs))
        try:
            return await asyncio.wait_for(asyncio.shield(operation),timeout)
        except BaseException as error:
            self.stop('cancelled' if isinstance(error,asyncio.CancelledError) else
                'timeout' if isinstance(error,(TimeoutError,subprocess.TimeoutExpired)) else 'executor_error')
            await self._close_and_drain(operation)
            if isinstance(error,subprocess.TimeoutExpired):
                raise TimeoutError('Native request exceeded its execution deadline') from error
            raise

    async def run_finalizer(self,function,*args,timeout=300,**kwargs):
        """Stop admitted children before joining a cancelled async finalizer."""
        if not hasattr(self,'recovery_profile'):raise ValueError('Job-bound finalizer scope required')
        self.execution_budget.check()
        timeout=min(timeout,self.execution_budget.remaining())
        if self.request_deadline is not None:
            self.request_deadline.check();timeout=min(timeout,self.request_deadline.remaining())
        with self.local():operation=asyncio.create_task(function(*args,**kwargs))
        try:return await asyncio.wait_for(asyncio.shield(operation),timeout)
        except BaseException as error:
            self.stop('cancelled' if isinstance(error,asyncio.CancelledError) else 'timeout' if isinstance(error,TimeoutError) else 'executor_error')
            await self._close_and_drain(operation)
            raise

    async def _close_and_drain(self,operation):
        # Closing the real native channel can itself fail or be cancelled again.
        # Its caller thread/async finalizer still owns that channel's eventual
        # result or exception; always retrieve it before the request unwinds.
        try:
            await finish_before_cancel(self.close())
        finally:
            await finish_before_cancel(asyncio.gather(operation,return_exceptions=True))


@asynccontextmanager
async def scope(case_id,user_id,cwd,kind,settings,*,owner=None,before_release=None,acquire_owner=None):
    from .request_deadline import current
    deadline=current()
    if acquire_owner is not None and (owner is not None or kind!='job.finalize'):
        raise ValueError('Atomic finalizer admission requires exactly one owner factory')
    executor=None
    heartbeat=None;error=None;cancelled=False
    async def reserve_owner():
        nonlocal owner
        # Capture the committed owner before propagating cancellation. A
        # shielded return value alone would be lost when its caller is cancelled.
        owner=await (acquire_owner() if acquire_owner is not None else records.create(case_id,user_id,kind,settings))
    def failed(failure):
        nonlocal error,cancelled
        expired=deadline is not None and deadline.expired
        error='TimeoutError' if expired else error or type(failure).__name__
        cancelled=cancelled or (isinstance(failure,asyncio.CancelledError) and not expired)
    try:
        if deadline is not None:deadline.check()
        if owner is None:await finish_before_cancel(reserve_owner())
        if deadline is not None:deadline.check()
        executor=RequestExecutor(owner,cwd,kind,settings)
        executor.before_release=before_release
        await executor.start()
        if deadline is not None:deadline.check()
        async def renew():
            try:
                while True:
                    await asyncio.sleep(1 if kind=='job.finalize' else 5)
                    await records.heartbeat(owner)
            except asyncio.CancelledError:raise
            except Exception as failure:
                executor.failure=type(failure).__name__;executor.stop('ownership_lost')
        heartbeat=asyncio.create_task(renew())
        yield executor
        if deadline is not None:deadline.check()
        if executor.failure:raise ChildExecutionError('Native request persistence or ownership failed')
    except BaseException as failure:
        failed(failure)
        raise
    finally:
        try:
            if executor is not None:await finish_before_cancel(executor.close())
        except BaseException as failure:
            failed(failure)
            raise
        finally:
            try:
                if heartbeat is not None:
                    heartbeat.cancel()
                    await finish_before_cancel(asyncio.gather(heartbeat,return_exceptions=True))
            except BaseException as failure:
                failed(failure)
                raise
            finally:
                # Even repeated cancellation during heartbeat shutdown must
                # finish the known owner's terminal journal. No owner means
                # admission failed before this scope acquired responsibility.
                if owner is not None:
                    if deadline is not None and deadline.expired:
                        error='TimeoutError';cancelled=False
                    result=getattr(executor,'result_receipt',None)
                    if kind=='job.finalize':
                        from .job_output_finalizing import native_result,CERTIFICATE,digest
                        from .execution_budget import current
                        result=native_result(executor,error) if executor is not None else {'schema':CERTIFICATE,
                            'profile_sha256':digest(settings),'usage':current().usage(),'prepared':None,'error':error}
                    await finish_before_cancel(records.complete(owner,error or getattr(executor,'failure',None),
                        cancelled=cancelled,result=result))
