"""API-owned native child scopes for registered document workers and finalizers."""
import asyncio
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import shlex
import sys
import time
import uuid

from .async_ops import finish_before_cancel
from .native_execution_client import ADDRESS,CAPABILITY,ChildExecutionError,NativeComputeLimit,NativeStorageLimit,context,latex_probe_timeout
from .owned_process import OwnedProcess,wsl_path
from . import native_child_records as records

LOCAL_EXECUTOR=ContextVar('owned_native_finalizer',default=None)
WSL_WORKERS={'registered_latex_worker.py','mpi_worker.py','udf_worker.py','flotherm_library_worker.py',
             'moose_worker.py','code_aster_worker.py'}


async def drain_finalizers():
    """Close and await any native call left behind by a failed sync transport."""
    executor=LOCAL_EXECUTOR.get()
    if executor is None:return
    executor.stop('finalization_error')
    pending=[task for task,caller in tuple(executor.tasks) if caller=='finalizer']
    if pending:await finish_before_cancel(asyncio.gather(*pending,return_exceptions=True))
    if executor.linux_compute is not None or executor.windows_compute_scope is not None:
        await finish_before_cancel(executor.finish_compute())


class Entry:
    def __init__(self,caller,deadline=None):
        self.id=str(uuid.uuid4());self.caller=caller;self.owned=OwnedProcess()
        self.event=asyncio.Event();self.reason=None;self.deadline=deadline

    def stop(self,reason):
        if self.reason is None:
            self.reason='timeout' if self.deadline is not None and time.monotonic()>=self.deadline else reason
        self.event.set()

    def check(self):
        if self.deadline is not None and time.monotonic()>=self.deadline:self.stop('timeout')
        if self.event.is_set():
            if self.reason=='timeout':raise TimeoutError('Native call exhausted its queue/preparation/execution deadline')
            raise ChildExecutionError('Native call stopped: '+str(self.reason))


class NativeExecutor:
    def __init__(self,owner,cwd,argv,max_log_bytes):
        self.owner=owner;self.cwd=Path(cwd).resolve();self.max_log_bytes=max_log_bytes
        from .execution_budget import current
        self.execution_budget=current(owner)
        from .request_deadline import current as request_deadline
        self.request_deadline=request_deadline()
        self.kind=('flotherm-library' if argv[1]==str(Path(__file__).with_name('flotherm_library_worker.py')) else
            'udf' if argv[1]==str(Path(__file__).with_name('udf_worker.py')) else
            'mpi' if argv[1]==str(Path(__file__).with_name('mpi_worker.py')) else
            'paraview' if argv[1]==str(Path(__file__).with_name('paraview_worker.py')) else
            'su2' if argv[1]==str(Path(__file__).with_name('su2_worker.py')) else
            'elmer' if argv[1]==str(Path(__file__).with_name('elmer_worker.py')) else
            'moose' if argv[1]==str(Path(__file__).with_name('moose_worker.py')) else
            'code-aster' if argv[1]==str(Path(__file__).with_name('code_aster_worker.py')) else
            'calculix' if argv[1]==str(Path(__file__).with_name('calculix_worker.py')) else
            'latex' if argv[1]==str(Path(__file__).with_name('registered_latex_worker.py')) else 'report')
        self.runtime='wsl' if self.uses_wsl(argv) else 'windows'
        self.profile=(dict(part.split('=',1) for part in argv[2:]) if self.kind in {'latex','calculix','su2','elmer','moose','code-aster','paraview','mpi','udf','flotherm-library'} else
            {'--mode':argv[3],'--pandoc':argv[5],'--execution-id':argv[7]})
        if self.kind in {'mpi','udf','latex','flotherm-library','moose','code-aster'} and self.profile.get('--distro')=='native-linux':
            if not sys.platform.startswith('linux'):raise ValueError('Native Linux tool execution is unavailable')
            self.runtime='windows'
        self.worker_capability=secrets.token_hex(32);self.local_capability=secrets.token_hex(32)
        self.server=None;self.address=None;self.parent_nonce=None
        self.worker_closed=False;self.closed=False;self.failure=None
        policy=self.execution_budget.native_compute_policy if self.execution_budget is not None else None
        self.wsl_compute={} if self.runtime=='wsl' and policy and any(policy.values()) else None
        self.entries={};self.tasks=set();self.handlers=set()
        self.slots=asyncio.Semaphore(1 if self.wsl_compute is not None else 4)
        self.records=records
        self.before_release=None
        self.home_watch=None
        self.linux_compute=None
        self.windows_compute_scope=None

    @staticmethod
    def applies(argv):
        workers={str(Path(__file__).with_name(name)) for name in ('registered_latex_worker.py','registered_report_worker.py','calculix_worker.py','su2_worker.py','elmer_worker.py','moose_worker.py','code_aster_worker.py','paraview_worker.py','mpi_worker.py','udf_worker.py','flotherm_library_worker.py')}
        return isinstance(argv,list) and len(argv)>1 and argv[1] in workers

    @staticmethod
    def uses_wsl(argv):
        return (isinstance(argv,list) and len(argv)>1 and Path(argv[1]).name in WSL_WORKERS
                and '--distro=native-linux' not in argv[2:])

    async def start(self):
        self.server=await asyncio.start_server(self._handle,'127.0.0.1',0,limit=65536,backlog=16)
        self.address='127.0.0.1:'+str(self.server.sockets[0].getsockname()[1])
        return {ADDRESS:self.address,CAPABILITY:self.worker_capability}

    @contextmanager
    def local(self):
        token=LOCAL_EXECUTOR.set(self)
        try:
            with context(self.address,self.local_capability):yield
        finally:LOCAL_EXECUTOR.reset(token)

    def stop(self,reason,*,worker_only=False):
        self.worker_closed=True
        if not worker_only:self.closed=True
        for entry in tuple(self.entries.values()):
            if not worker_only or entry.caller=='worker':entry.stop(reason)

    async def drain_workers(self):
        self.stop('root_exit',worker_only=True)
        pending=[task for task,caller in tuple(self.tasks) if caller=='worker']
        if pending:await finish_before_cancel(asyncio.gather(*pending,return_exceptions=True))

    async def close(self):
        self.stop('finalization_error')
        if self.server is not None:
            self.server.close();await self.server.wait_closed()
        # A disconnected or non-reading client cannot hold the owner alive.
        for task in tuple(self.handlers):task.cancel()
        if self.handlers:await finish_before_cancel(asyncio.gather(*tuple(self.handlers),return_exceptions=True))
        if self.tasks:await finish_before_cancel(asyncio.gather(*(task for task,_ in tuple(self.tasks)),return_exceptions=True))
        if self.linux_compute is not None:
            await finish_before_cancel(self.linux_compute.close())
        if self.windows_compute_scope is not None:
            self.windows_compute_scope.close()

    async def finish_compute(self):
        if self.windows_compute_scope is not None:
            from .execution_budget import windows_compute_receipt
            receipt=windows_compute_receipt(self.execution_budget.windows_compute_policy,self.windows_compute_scope)
            source='windows_job'
        else:
            receipt=await self.linux_compute.close()
            source='linux_cgroup'
            if receipt is None:
                raise ValueError('Shared Linux compute receipt is unavailable')
        if receipt['violation']:
            self.execution_budget.trip(receipt['violation'],source)
        from .job_execution import phase
        await phase(self.owner,'finalizing',compute_usage=receipt)

    def _allowed(self,caller):
        return self.parent_nonce is not None and not self.closed and not (caller=='worker' and self.worker_closed)

    def _command(self,purpose,parameters):
        if self.kind=='flotherm-library':
            from .flotherm_library_artifacts import CONFIG
            from .flotherm_library_handler import NATIVE,COMMON
            from .attempt_workspace import checked_path,digest_file
            config_path=checked_path(self.cwd,CONFIG);digest=digest_file(config_path)['sha256']
            expected_purpose='flotherm_library_linux' if self.runtime=='windows' else 'flotherm_library'
            if purpose!=expected_purpose or parameters!={'distro':self.profile['--distro'],
                    'execution_id':self.profile['--execution-id'],'config_sha256':digest}:
                raise ValueError('FloTHERM library child differs from registered invocation')
            config=json.loads(config_path.read_text(encoding='utf8'))
            for key in ('distro','bashrc','execution_id','source_dir','destination_dir'):
                if config[key]!=self.profile['--'+key.replace('_','-')]:raise ValueError('FloTHERM settings differ from registration')
            if config['max_log_bytes']!=self.max_log_bytes:raise ValueError('FloTHERM log budget changed')
            if hashlib.sha256(NATIVE.read_bytes()).hexdigest()!=config['native_sha256'] or hashlib.sha256(COMMON.read_bytes()).hexdigest()!=config['common_native_sha256']:
                raise ValueError('FloTHERM native helper changed')
            native_path=str if self.runtime=='windows' else wsl_path
            command='source '+shlex.quote(self.profile['--bashrc'])+' && exec '+shlex.join(
                ['python3','-I','-S',native_path(NATIVE),'--config',native_path(config_path),'--sha256',digest])
            argv=(['/bin/bash','--noprofile','--norc','-c',command] if self.runtime=='windows' else
                ['wsl.exe','-d',self.profile['--distro'],'--user','nobody','--exec','bash','--noprofile','--norc','-c',command])
            return argv,int(self.profile['--timeout']),self.max_log_bytes
        if self.kind=='udf':
            from .udf_artifacts import CONFIG
            from .udf_handler import NATIVE,COMMON
            from .attempt_workspace import checked_path,digest_file
            config_path=checked_path(self.cwd,CONFIG);digest=digest_file(config_path)['sha256']
            expected_purpose='udf_run_linux' if self.runtime=='windows' else 'udf_run'
            if purpose!=expected_purpose or parameters!={'distro':self.profile['--distro'],
                    'execution_id':self.profile['--execution-id'],'config_sha256':digest}:
                raise ValueError('UDF child differs from registered invocation')
            config=json.loads(config_path.read_text(encoding='utf8'))
            for key in ('distro','bashrc','execution_id','run_dir','mode','name'):
                if config[key]!=self.profile['--'+key.replace('_','-')]:raise ValueError('UDF settings differ from registration')
            if config['max_log_bytes']!=self.max_log_bytes:raise ValueError('UDF log budget changed')
            if hashlib.sha256(NATIVE.read_bytes()).hexdigest()!=config['native_sha256'] or hashlib.sha256(COMMON.read_bytes()).hexdigest()!=config['common_native_sha256']:
                raise ValueError('UDF native helper changed')
            native_path=str if self.runtime=='windows' else wsl_path
            command='source '+shlex.quote(self.profile['--bashrc'])+' && exec '+shlex.join(
                ['python3','-I','-S',native_path(NATIVE),'--config',native_path(config_path),'--sha256',digest])
            argv=(['/bin/bash','--noprofile','--norc','-c',command] if self.runtime=='windows' else
                ['wsl.exe','-d',self.profile['--distro'],'--user','nobody','--exec','bash','--noprofile','--norc','-c',command])
            return argv,int(self.profile['--timeout']),self.max_log_bytes
        if self.kind=='mpi':
            from .mpi_artifacts import CONFIG
            from .mpi_handler import NATIVE
            from .attempt_workspace import checked_path,digest_file
            config_path=checked_path(self.cwd,CONFIG);digest=digest_file(config_path)['sha256']
            expected_purpose='mpi_run_linux' if self.runtime=='windows' else 'mpi_run'
            if purpose!=expected_purpose or parameters!={'distro':self.profile['--distro'],
                    'execution_id':self.profile['--execution-id'],'config_sha256':digest}:
                raise ValueError('MPI child differs from its registered invocation')
            config=json.loads(config_path.read_text(encoding='utf8'))
            for key in ('distro','bashrc','execution_id','run_dir','solver'):
                if config[key]!=self.profile['--'+key.replace('_','-')]:raise ValueError('MPI settings differ from registration')
            if config['subdomains']!=int(self.profile['--subdomains']) or config['max_log_bytes']!=self.max_log_bytes:
                raise ValueError('MPI count/log budget changed')
            if hashlib.sha256(NATIVE.read_bytes()).hexdigest()!=config['native_sha256']:raise ValueError('MPI native helper changed')
            native_path=str if self.runtime=='windows' else wsl_path
            command='source '+shlex.quote(self.profile['--bashrc'])+' && exec '+shlex.join(
                ['python3','-I','-S',native_path(NATIVE),'--config',native_path(config_path),'--sha256',digest])
            argv=(['/bin/bash','--noprofile','--norc','-c',command] if self.runtime=='windows' else
                ['wsl.exe','-d',self.profile['--distro'],'--user','nobody','--exec','bash','--noprofile','--norc','-c',command])
            if self.runtime=='windows':
                from .openfoam_runtime import isolated_argv
                argv = isolated_argv(self.cwd, argv, source_libraries=config.get('source_library_dir'))
            return argv,int(self.profile['--timeout']),self.max_log_bytes
        if self.kind=='paraview':
            if purpose!='paraview_run' or parameters!={'script':self.profile['--script'],
                    'backend':self.profile['--backend'],'execution_id':self.profile['--execution-id']}:
                raise ValueError('ParaView child differs from its registered invocation')
            executable=Path(self.profile['--executable'])
            if hashlib.sha256(executable.read_bytes()).hexdigest()!=self.profile['--executable-sha256']:
                raise ValueError('ParaView executable changed before native release')
            return [str(executable),self.profile['--script']],int(self.profile['--timeout']),self.max_log_bytes
        if self.kind=='code-aster':
            from .attempt_workspace import checked_path,digest_file
            from .code_aster import host_path,state_package_digest
            from .code_aster_artifacts import CONFIG
            from .code_aster_handler import NATIVE,STATE
            executable = self.profile['--executable']
            distro = self.profile['--distro']
            identifier = self.profile['--execution-id']
            expected = 'code_aster_solve_linux' if self.runtime=='windows' else 'code_aster_solve'
            if purpose!=expected or parameters!={'input':self.profile['--input'],'distro':distro,'execution_id':identifier}:
                raise ValueError('Code_Aster child differs from its registered invocation')
            for path,argument in ((host_path(executable,distro),'--executable-sha256'),(NATIVE,'--native-sha256')):
                if digest_file(path)['sha256']!=self.profile[argument]:
                    raise ValueError('Code_Aster runtime changed before native release')
            config_path = checked_path(self.cwd,CONFIG)
            config = json.loads(config_path.read_text(encoding='utf8'))
            if (config['source']['plan']['input'],config['distro'],config['execution_id'])!=(self.profile['--input'],distro,identifier):
                raise ValueError('Code_Aster configuration differs from the registered invocation')
            native_path = wsl_path if self.runtime=='wsl' else str
            confined = ['/usr/bin/unshare','--user','--map-root-user','--mount']
            python = sys.executable
            if self.runtime=='wsl':
                confined.append('--net')
                python = '/usr/bin/python3'
            command = [*confined,python,'-I','-S',native_path(NATIVE),'--root='+native_path(self.cwd),
                       '--config='+native_path(config_path),'--executable='+executable]
            if config['source']['plan'].get('base'):
                helper = self.profile['--state-sha256']
                if (digest_file(STATE)['sha256']!=helper
                        or state_package_digest(executable,distro)!=config['engine_sha256']):
                    raise ValueError('Code_Aster state packaging or native build changed before release')
                command.append('--state-sha256='+helper)
            if self.runtime=='wsl':
                command = ['wsl.exe','-d',distro,'--user','nobody','--exec',*command]
            return command,int(self.profile['--timeout']),self.max_log_bytes
        if self.kind=='moose':
            from .attempt_workspace import checked_path,digest_file
            from .moose import host_path
            from .moose_artifacts import CONFIG,output_base
            from .moose_checkpoint import NATIVE
            executable = self.profile['--executable']
            distro = self.profile['--distro']
            name = self.profile['--input']
            expected_purpose = 'moose_solve_linux' if self.runtime=='windows' else 'moose_solve'
            if purpose!=expected_purpose or parameters!={'input':name,'distro':distro,'execution_id':self.profile['--execution-id']}:
                raise ValueError('MOOSE child differs from its registered invocation')
            if digest_file(host_path(executable,distro))['sha256']!=self.profile['--executable-sha256']:
                raise ValueError('MOOSE executable changed before native release')
            child_env = ['/usr/bin/env','OMP_NUM_THREADS=1','OPENBLAS_NUM_THREADS=1']
            if '--native-sha256' in self.profile:
                if digest_file(NATIVE)['sha256']!=self.profile['--native-sha256']:
                    raise ValueError('MOOSE checkpoint helper changed before native release')
                config = json.loads(checked_path(self.cwd,CONFIG).read_text(encoding='utf8'))
                source = config['source']
                native_path = wsl_path if self.runtime=='wsl' else str
                if self.runtime=='wsl':
                    command = [*child_env,'/usr/bin/unshare','--user','--map-root-user','--mount',
                               '/usr/bin/python3','-I','-S',native_path(NATIVE),'--executable='+executable]
                else:
                    command = [executable]
                command.extend(['--root='+native_path(self.cwd), '--input='+name,'--base='+output_base(name),
                    '--executable-sha256='+self.profile['--executable-sha256'],'--model-sha256='+source['model_sha256']])
                if source['checkpoint']:
                    command.append('--checkpoint')
                if config.get('restart') is not None:
                    command.append('--restart='+config['restart']['checkpoint_path'])
            else:
                command = [executable,'-i',name,'Outputs/csv=true','Outputs/exodus=true',
                           'Outputs/file_base='+output_base(name)]
                if self.runtime=='wsl':
                    command[:0]=child_env
            if self.runtime=='wsl':
                command = ['wsl.exe','-d',distro,'--user','nobody','--exec',*command]
            return command,int(self.profile['--timeout']),self.max_log_bytes
        if self.kind in {'calculix','su2','elmer'}:
            if purpose!=self.kind+'_solve' or parameters!={'input':self.profile['--input'],'execution_id':self.profile['--execution-id']}:
                raise ValueError('Solver child differs from its registered invocation')
            executable=Path(self.profile['--executable'])
            if hashlib.sha256(executable.read_bytes()).hexdigest()!=self.profile['--executable-sha256']:
                raise ValueError('Solver executable changed before native release')
            if self.kind=='su2':return [str(executable),self.profile['--input']],int(self.profile['--timeout']),self.max_log_bytes
            from .attempt_workspace import checked_path,digest_file
            if self.kind=='elmer':
                from .elmer_artifacts import CONFIG,native_invocation
                config=json.loads(checked_path(self.cwd,CONFIG).read_text(encoding='utf-8'))
                if config['execution_id']!=parameters['execution_id'] or config['source']['input']!=parameters['input']:
                    raise ValueError('Elmer settings differ from the registered invocation')
                return [str(executable),native_invocation(self.cwd,config['source'])],int(self.profile['--timeout']),self.max_log_bytes
            from .calculix_artifacts import CONFIG,native_digest,native_input
            config=json.loads(checked_path(self.cwd,CONFIG).read_text(encoding='utf-8'))
            if config['execution_id']!=parameters['execution_id'] or config['source']['input']!=parameters['input']:
                raise ValueError('CalculiX settings differ from the registered invocation')
            digest=native_digest(config['source'])
            native=native_input(parameters['input'],digest is not None)
            if digest is not None and digest_file(checked_path(self.cwd,native))['sha256']!=digest:
                raise ValueError('CalculiX native input differs from the frozen declaration')
            return [str(executable),native[:-4]],int(self.profile['--timeout']),self.max_log_bytes
        if self.kind=='report':
            from . import pandoc_execution,pandoc_native
            allowed=pandoc_native.PURPOSES-{'pandoc_cite'}
            if getattr(self,'citation_preview',False):allowed={'pandoc_cite'}
            if purpose not in allowed or not isinstance(parameters,dict) or set(parameters)!={'config','sha256'}:
                raise ValueError('Unsupported registered report native call')
            settings,digest=pandoc_execution.validate(parameters['config'],self.cwd,self.profile['--pandoc'],purpose)
            if parameters['sha256']!=digest:raise ValueError('Pandoc request settings changed')
            _,timeout=pandoc_native.command(settings)
            base=str(Path(getattr(sys,'_base_executable',sys.executable)).resolve())
            return [base,'-I','-S','-u',str(pandoc_execution.NATIVE),'--config',parameters['config'],'--sha256',digest],timeout+15,1024*1024
        from . import latex_artifacts,attempt_workspace
        distro=self.profile['--distro']
        if not isinstance(parameters,dict) or parameters.get('distro')!=distro:
            raise ValueError('Native call changes the registered distribution')
        native_linux=distro=='native-linux'
        path_for=str if native_linux else wsl_path
        argv=([str(Path(getattr(sys,'_base_executable',sys.executable)).resolve()),'-I','-S',str(latex_artifacts.NATIVE)] if native_linux else
            ['wsl.exe','-d',distro,'--user','nobody','--exec','python3','-I','-S',wsl_path(latex_artifacts.NATIVE)])
        if purpose==('latex_probe_linux' if native_linux else 'latex_probe'):
            if set(parameters)!={'distro','recipe'} or parameters['recipe']!=self.profile['--recipe']:
                raise ValueError('Native probe changes the registered recipe')
            return [*argv,'--probe',parameters['recipe']],latex_probe_timeout(distro),1024*1024
        if purpose!=('latex_compile_linux' if native_linux else 'latex_compile') or set(parameters)!={'distro','config'}:
            raise ValueError('Unsupported native execution purpose')
        path=Path(parameters['config'])
        if (path.name!='settings.json' or path.parent.parent!=self.cwd.parent
                or not re.fullmatch(r'\.latex-compile-[0-9a-f]{32}',path.parent.name)):
            raise ValueError('Native compile configuration is outside this attempt')
        path=attempt_workspace.checked_path(self.cwd.parent,path.relative_to(self.cwd.parent).as_posix())
        settings=json.loads(path.read_text(encoding='utf-8'))
        if (settings['recipe']!=self.profile['--recipe'] or settings['main']!=self.profile['--main']
                or settings['epoch']!=int(self.profile['--epoch']) or settings['max_log_bytes']!=self.max_log_bytes
                or settings.get('input_aliases',{})!=json.loads(self.profile.get('--input-aliases-json','{}'))):
            raise ValueError('Native compile settings differ from the registered attempt')
        return [*argv,'--config',path_for(path)],180,self.max_log_bytes

    async def _handle(self,reader,writer):
        handler=asyncio.current_task();self.handlers.add(handler)
        operation=None;disconnect=None;entry=None
        async def send(message):
            writer.write((json.dumps(message)+'\n').encode('utf-8'))
            await writer.drain()
        try:
            if len(self.handlers)>16:raise ChildExecutionError('Native channel capacity exceeded')
            raw=await asyncio.wait_for(reader.readline(),5)
            if len(raw)>65536:raise ChildExecutionError('Native request exceeded 64 KiB')
            request=json.loads(raw)
            if set(request) not in ({'schema','capability','purpose','parameters'},
                    {'schema','capability','purpose','parameters','deadline'}) or request['schema']!='vibeflow.native-call.v1':
                raise ValueError('Invalid native call')
            received=time.monotonic()
            requested_deadline=request.get('deadline')
            if 'deadline' in request and (isinstance(requested_deadline,bool)
                    or not isinstance(requested_deadline,(int,float)) or not math.isfinite(requested_deadline)
                    or requested_deadline<=0):raise ValueError('Invalid native deadline')
            capability=request['capability']
            if not isinstance(capability,str):raise ValueError('Invalid native capability')
            if hmac.compare_digest(capability,self.worker_capability):caller='worker'
            elif hmac.compare_digest(capability,self.local_capability):caller='finalizer'
            else:raise ChildExecutionError('Native capability does not belong to this attempt')
            if not self._allowed(caller):raise ChildExecutionError('Parent no longer accepts native calls')
            purpose=request['purpose'];parameters=request['parameters']
            argv,timeout,limit=self._command(purpose,parameters)
            deadline=received+timeout
            if requested_deadline is not None:deadline=min(deadline,requested_deadline)
            if self.execution_budget is not None and not self.execution_budget.committed:
                deadline=min(deadline,self.execution_budget.deadline)
            if self.request_deadline is not None:deadline=min(deadline,self.request_deadline.deadline)
            entry=Entry(caller,deadline);self.entries[entry.id]=entry
            async def output(data):await send({'type':'output','data':base64.b64encode(data).decode('ascii')})
            operation=asyncio.create_task(self._execute(entry,purpose,parameters,argv,timeout,limit,output))
            self.tasks.add((operation,caller))
            disconnect=asyncio.create_task(reader.read(1))
            done,_=await asyncio.wait({operation,disconnect},return_when=asyncio.FIRST_COMPLETED)
            if disconnect in done:entry.stop('cancelled')
            code=await asyncio.shield(operation)
            await send({'type':'done','returncode':code,'child_id':entry.id})
        except BaseException as error:
            if entry is not None:entry.stop('cancelled')
            if operation is not None:
                try:await finish_before_cancel(operation)
                except BaseException:pass
            if not isinstance(error,asyncio.CancelledError):
                try:await asyncio.wait_for(send({'type':'error','error':type(error).__name__}),2)
                except (OSError,asyncio.TimeoutError):pass
        finally:
            if disconnect is not None:
                disconnect.cancel();await asyncio.gather(disconnect,return_exceptions=True)
            if operation is not None:self.tasks.discard((operation,entry.caller))
            if entry is not None:self.entries.pop(entry.id,None)
            self.handlers.discard(handler)
            writer.close()
            try:await asyncio.wait_for(writer.wait_closed(),2)
            except (OSError,asyncio.TimeoutError):pass

    async def _execute(self,entry,purpose,parameters,argv,timeout,limit,output):
        owned=entry.owned;reserved=False;error=None;pump=None;root=None;stopping=None;home_monitor=None
        budget=getattr(self,'execution_budget',None)
        if entry.deadline is None:entry.deadline=time.monotonic()+timeout
        expiry=asyncio.get_running_loop().call_later(max(0,entry.deadline-time.monotonic()),entry.stop,'timeout')
        acquired=False
        def check_active():
            if budget is not None:budget.check()
            entry.check()
            if not self._allowed(entry.caller):raise ChildExecutionError('Parent no longer accepts native work')
        owned.check_preparation_active=check_active
        diagnostics=None
        def sample_diagnostics():
            if diagnostics is None:return
            diagnostics.sample()
            if budget.violation=='log_limit':
                self.stop('log_limit')
                entry.stop('log_limit')
        async def monitor_home():
            await self.home_watch.run()
            if self.home_watch.budget.violation is not None:
                entry.stop(self.home_watch.budget.violation)
        try:
            check_active()
            await asyncio.wait_for(self.slots.acquire(),max(0,entry.deadline-time.monotonic()))
            acquired=True
            try:
                if budget is not None:budget.check()
                if budget is not None and self.kind=='report':
                    from .native_diagnostics import DiagnosticMeter
                    diagnostics=DiagnosticMeter(self.cwd.parent,parameters['config'],budget,entry.caller)
                check_active()
                async def reserve():
                    nonlocal reserved
                    options={'preparation_nonce':owned.nonce} if hasattr(self,'recovery_profile') else {}
                    await self.records.reserve(self.owner,self.parent_nonce,entry.id,purpose,entry.caller,parameters,**options)
                    reserved=True
                # A durable reservation may commit before its awaited callback
                # returns. Retain cleanup responsibility even if cancelled then.
                await finish_before_cancel(reserve())
                await self._checkpoint_usage(entry,diagnostics)
                check_active()
                if self.records is records:
                    from . import child_launch_registration as registration
                    if self.runtime=='wsl':
                        from .job_native_scopes import allocate as allocate_scope, child_compute_policy
                        policy=None
                        if self.wsl_compute is not None:
                            policy=child_compute_policy(budget.native_compute_policy,self.wsl_compute)
                        await allocate_scope(self.owner,self.parent_nonce,entry,parameters['distro'],
                                             check_active=check_active,compute_policy=policy)
                    distribution=parameters.get('distro') if self.runtime=='wsl' else None
                    await registration.reserve(self.owner,self.parent_nonce,entry.id,owned,self.runtime,distribution)
                    from .job_scope_guardians import prepare as prepare_guardian
                    await prepare_guardian(self.owner,owned,seconds=max(0,entry.deadline-time.monotonic()),
                        child_id=entry.id,parent_nonce=self.parent_nonce)
                    async def observed(value):
                        await registration.observed(self.owner,self.parent_nonce,entry.id,value)
                        check_active()
                    owned.on_preparation=observed
                    if os.name!='nt' and self.runtime=='windows':
                        from .job_linux_namespaces import prepared as namespace_prepared
                        owned.on_namespace=lambda process,init:namespace_prepared(self.owner,process,init,
                            child_id=entry.id,parent_nonce=self.parent_nonce)
                env={key:value for key,value in os.environ.items() if key not in {ADDRESS,CAPABILITY}}
                if self.kind=='paraview':
                    from .paraview_artifacts import CONFIG
                    from .attempt_workspace import canonical_path, checked_path, ordinary
                    env['VIBEFLOW_CASE_ROOT']=str(self.cwd)
                    paraview_config=json.loads(checked_path(self.cwd,CONFIG).read_bytes())
                    pipeline=paraview_config.get('pipeline') or {}
                    if (os.name!='nt' and self.profile['--backend']=='pvpython'
                            and 'SLACTools' in (pipeline.get('plugins') or [])
                            and any(node.get('name')=='SamplePlaneSource' for node in pipeline['nodes'])):
                        # The serial SLAC sampler passes aliased buffers to MPI_Allreduce.
                        argv=[argv[0],'--no-mpi',*argv[1:]]
                    catalyst=paraview_config.get('catalyst')
                    if catalyst:
                        env['VIBEFLOW_CATALYST_STATE']=str(checked_path(self.cwd,catalyst['state_path']))
                        output_dir=self.cwd
                        parts=canonical_path(catalyst['folder']).split('/')
                        for part in parts[:-1]:
                            output_dir=output_dir/part
                            output_dir.mkdir(exist_ok=True)
                            ordinary(output_dir)
                        output_dir=output_dir/parts[-1]
                        output_dir.mkdir()
                        env['VIBEFLOW_CATALYST_OUTPUT']=str(output_dir)
                        env['VIBEFLOW_CATALYST_VERSION']=str(catalyst['version'])
                        if catalyst['version']==2:
                            from .paraview import catalyst_implementation
                            env['CATALYST_IMPLEMENTATION_PATHS']=catalyst_implementation(argv[0])
                            env['CATALYST_IMPLEMENTATION_NAME']='paraview'
                if self.kind=='elmer':
                    from .elmer import child_environment
                    env=child_environment(argv[0],env)
                await self._prepare(entry,argv,env)
                check_active()
                await owned.verify_registration()
                if self.records is records and getattr(entry,'job_native_scope',None) is not None:
                    from .job_native_scopes import admitted as admit_scope
                    await admit_scope(self.owner,entry)
                if self.records is records:
                    await registration.prepared(self.owner,self.parent_nonce,entry.id,owned)
                check_active()
                await self.records.register(self.owner,self.parent_nonce,entry.id,owned.record,
                    distribution if self.records is records else parameters.get('distro'))
                if self.before_release is not None:
                    # A stop flag alone cannot interrupt an awaited admission
                    # check. Cancel it at the same execution deadline; protected
                    # file/commit operations inside the callback still drain.
                    async with asyncio.timeout_at(entry.deadline):
                        await self.before_release()
                if self.home_watch is not None:
                    async with asyncio.timeout_at(entry.deadline):
                        await self.home_watch.sample()
                    if self.home_watch.budget.violation is not None:
                        entry.stop(self.home_watch.budget.violation)
                        raise NativeStorageLimit('Linux account home did not pass admission')
                await owned.verify_registration()
                async def release():
                    check_active()
                    await owned.release()
                await self.records.release(self.owner,self.parent_nonce,entry.id,owned,release)
                if self.home_watch is not None:
                    home_monitor=asyncio.create_task(monitor_home())
                async def read_output():
                    count=0
                    while True:
                        data=await owned.proc.stdout.read(4096)
                        if not data:return
                        allowed=min(len(data),max(0,limit-count))
                        count+=len(data)
                        if budget is not None:
                            allowed=min(allowed,budget.consume_output(len(data),'native.'+entry.caller))
                        await self._checkpoint_usage(entry,diagnostics)
                        exceeded=count>limit or (budget is not None and budget.violation=='log_limit')
                        if exceeded:
                            if budget is not None:
                                budget.trip('log_limit','native.'+entry.caller)
                                self.stop('log_limit')
                            entry.stop('log_limit')
                        if allowed:await output(data[:allowed])
                        if exceeded:return
                pump=asyncio.create_task(read_output())
                root=asyncio.create_task(owned.wait_root_exit())
                stopping=asyncio.create_task(entry.event.wait())
                deadline=entry.deadline
                while not root.done() and not entry.event.is_set():
                    sample_diagnostics()
                    if diagnostics is not None:await self._checkpoint_usage(entry,diagnostics)
                    pending={root,stopping}
                    if not pump.done():pending.add(pump)
                    elif pump.exception() is not None:raise pump.exception()
                    remaining=deadline-time.monotonic()
                    if remaining<=0:entry.stop('timeout');break
                    done,_=await asyncio.wait(pending,timeout=min(remaining,.05) if diagnostics is not None else remaining,
                        return_when=asyncio.FIRST_COMPLETED)
                    if not done and time.monotonic()>=deadline:entry.stop('timeout')
                if time.monotonic()>=deadline:entry.stop('timeout')
                expiry.cancel()  # Cleanup drains the retained resources, without renewing execution.
                reason=entry.reason or 'root_exit'
                await owned.stop(reason=reason,grace_ms=0 if reason=='root_exit' else 500)
                if self.kind=='agent.shell' and (owned.stop_result is None or owned.stop_result['verified'] is not True):
                    raise ChildExecutionError('Agent child cleanup was not verified')
                if home_monitor is not None:
                    home_monitor.cancel()
                    await asyncio.gather(home_monitor,return_exceptions=True)
                    home_monitor=None
                    if owned.stop_result['reason']=='root_exit':
                        await self.home_watch.sample()
                        if self.home_watch.budget.violation is not None:
                            owned.stop_result['reason']=self.home_watch.budget.violation
                await asyncio.wait_for(asyncio.shield(pump),5)
                if self.kind=='agent.shell' and owned.stop_result['reason'] in {'cpu_limit','memory_limit'}:
                    raise NativeComputeLimit('The Agent command reached its admitted Linux compute limit')
                if self.kind=='agent.shell' and owned.stop_result['reason'] in {'disk_limit','disk_unverified'}:
                    raise NativeStorageLimit('The Agent command crossed its Linux account home storage limit')
                sample_diagnostics()
                if budget is not None and budget.violation=='log_limit':
                    raise ChildExecutionError('Native output exceeded the shared allowance')
                if reason=='timeout':raise TimeoutError('Native child exceeded its deadline')
                if reason!='root_exit':raise ChildExecutionError('Native child was stopped: '+reason)
                return owned.proc.returncode
            finally:
                if acquired and self.wsl_compute is None:self.slots.release();acquired=False
        except BaseException as failure:
            error=type(failure).__name__
            raise
        finally:
            expiry.cancel()
            try:
                if owned.proc is not None and not owned.closed:
                    try:await finish_before_cancel(owned.stop(reason=entry.reason or 'executor_error',grace_ms=500))
                    except BaseException as cleanup_error:
                        error=error or type(cleanup_error).__name__
                        self.failure=error
                if reserved:
                    try:await finish_before_cancel(self._cleanup(entry))
                    except BaseException as cleanup_error:
                        error=error or type(cleanup_error).__name__
                        self.failure=error
                if reserved:
                    try:await finish_before_cancel(self.records.finish(self.owner,self.parent_nonce,entry.id,owned.record,owned.stop_result,error))
                    except BaseException as persist_error:
                        self.failure=type(persist_error).__name__
                        raise
                    if hasattr(self,'recovery_profile'):
                        # Before registration, finish() first persists the real
                        # identity and stop obtained during cancellation. The
                        # sealed channel must bind that committed stop, after
                        # physical scope closure, before terminal accounting.
                        try:
                            if pump is not None:await finish_before_cancel(asyncio.wait_for(asyncio.shield(pump),5))
                            sample_diagnostics()
                            await finish_before_cancel(self._checkpoint_usage(entry,diagnostics,sealed=True))
                        except BaseException as checkpoint_error:
                            error=error or type(checkpoint_error).__name__;self.failure=error
            finally:
                if acquired:self.slots.release()
                for task in (pump,root,stopping,home_monitor):
                    if task is not None and not task.done():task.cancel()
                await asyncio.gather(*(task for task in (pump,root,stopping,home_monitor) if task is not None),return_exceptions=True)
                owned.close()

    async def _prepare(self,entry,argv,env):
        if getattr(entry,'job_native_scope',None) is not None:
            if argv[3:6]!=['--user','nobody','--exec']:raise ValueError('Kernel-scoped native command changed its original nonroot identity')
            argv=[*argv[:3],'--user','root',*argv[5:]]
        if self.linux_compute is not None:
            self.linux_compute.bind(entry.owned)
        if sys.platform=='linux' and self.kind=='mpi' and self.runtime=='windows':
            from .openfoam_runtime import child_environment
            entry.owned.isolate_linux_network=True
            env=child_environment(env)
        if sys.platform=='linux' and self.kind=='code-aster':
            # The owned outer gate freezes the private network identity. The
            # Code_Aster helper still builds and drains its own chroot.
            entry.owned.isolate_linux_network=True
            env={key:value for key,value in env.items() if key in {'PATH','LANG','LC_ALL','LC_CTYPE','TZ'}}
        if sys.platform=='linux' and self.kind in {'calculix','su2','elmer','moose'}:
            entry.owned.isolate_linux_network=True
            launcher=Path(__file__).with_name('agent_shell_linux.py')
            options = []
            if self.kind=='elmer':
                options.append('--elmer')
            if self.kind=='moose':
                options.append('--moose')
                if '--native-sha256' in self.profile:
                    options.extend(('--moose-checkpoint-sha256',self.profile['--native-sha256']))
            argv=['/usr/bin/unshare','--user','--map-root-user','--mount','--pid','--fork','--kill-child=SIGKILL',
                  sys.executable,'-I','-S','-u',str(launcher),'--workspace',str(self.cwd),
                  '--solver-sha256',self.profile['--executable-sha256'],*options,'--',*argv]
            env={key:value for key,value in env.items()
                 if key in {'PATH','LANG','LC_ALL','LC_CTYPE','TZ'}
                 or key.startswith(('OMP_','MKL_','OPENBLAS_'))
                 or self.kind=='calculix' and (key=='CCX_SOLVER' or key.startswith('CCX_NPROC_'))}
            if self.kind=='moose':
                env.update(OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1')
        entry.owned.windows_compute_parent=self.windows_compute_scope
        return await entry.owned.prepare(argv,cwd=self.cwd,env=env,runtime=self.runtime)

    async def _checkpoint_usage(self,entry,diagnostics=None,*,sealed=False):
        """Request validators can persist read windows without changing job journals."""

    async def _cleanup(self,entry):
        """Request subclasses can drain resources allocated before a process exists."""
        from .job_native_scopes import merge_compute_usage, retire
        await retire(self.owner,entry)
        if self.wsl_compute is not None and getattr(entry,'job_native_scope',None) is not None:
            from .execution_budget import native_compute_violation
            state=entry.job_native_scope
            if state['retired'] is not None and state['allocated'] is not None:
                usage=state['retired']['result'].get('compute_usage',{})
                merge_compute_usage(self.wsl_compute,usage)
                violation=native_compute_violation(self.execution_budget.native_compute_policy,self.wsl_compute)
                if violation or (not usage and state['admitted'] is not None):
                    self.execution_budget.trip(violation or 'compute_unverified','wsl_cgroup')
