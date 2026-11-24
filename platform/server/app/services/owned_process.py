"""Prepare a process under an owned scope; release only after durable registration."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from datetime import datetime,timezone

from . import process_identity_native as identity
from .async_ops import finish_before_cancel

GATE=Path(__file__).with_name('process_gate.py').resolve()
PROBE=Path(identity.__file__).resolve()
PREFIX='VIBEFLOW_PROCESS_GATE_V1 '
SCHEMA='vibeflow.process-identity.v1'
WINDOWS_CREATION='job_list_v1'


def wsl_path(path):
    path=str(path).replace('\\','/')
    if len(path)<3 or path[1]!=':' or not path[0].isalpha():
        raise ValueError('WSL process supervision requires an absolute Windows path')
    return '/mnt/'+path[0].lower()+path[2:]


def native_payload(argv,nonce,*,terminal_io=False,native_scope=None):
    """Wrap the existing fixed WSL argv without interpreting its shell payload."""
    if Path(argv[0]).name.lower()!='wsl.exe':
        if native_scope is not None:raise ValueError('Native cgroup requires a WSL launch')
        return argv,None,False
    offset=6 if len(argv)>=7 and argv[3:6] in (['--user','nobody','--exec'],['--user','root','--exec']) else 4
    if len(argv)<=offset or argv[1]!='-d' or argv[offset-1]!='--exec':
        raise ValueError('WSL supervision requires explicit -d and --exec arguments')
    distro=argv[2]
    if not isinstance(distro,str) or not distro or len(distro)>128 or any(ord(c)<32 for c in distro):
        raise ValueError('Invalid WSL distribution')
    scope_args=[]
    if native_scope is not None:
        from . import process_cgroup_native as cgroups
        required={'scope','helper_sha256'}
        if (offset!=4 and not (offset==6 and argv[4]=='root')) or not isinstance(native_scope,dict) or (
                not required<=native_scope.keys() or native_scope.keys()-required-{'drop_uid','compute_helper_sha256'}):
            raise ValueError('Native scope requires a fixed root gate')
        if 'drop_uid' in native_scope and (type(native_scope['drop_uid']) is not int or native_scope['drop_uid']!=65534):raise ValueError('Invalid fixed native execution identity')
        descriptor=cgroups.validate(native_scope['scope'])
        if descriptor['nonce']!=nonce or native_scope['helper_sha256']!=hashlib.sha256(GATE.with_name('process_cgroup_native.py').read_bytes()).hexdigest():
            raise ValueError('Native scope admission changed its launch or helper')
        if 'compute' in descriptor:
            from .native_compute_v1 import validate as compute_scope
            compute_scope(descriptor['compute'])
            expected=hashlib.sha256(GATE.with_name('native_compute_v1.py').read_bytes()).hexdigest()
            if native_scope.get('compute_helper_sha256')!=expected:
                raise ValueError('Native compute helper differs from its launch binding')
        elif 'compute_helper_sha256' in native_scope:
            raise ValueError('Native compute helper has no allocated scope')
        scope_args=['--native-scope',base64.b64encode(json.dumps(native_scope).encode()).decode('ascii')]
    return [*argv[:offset],'python3','-I','-S','-u',wsl_path(GATE),
        '--nonce',nonce,'--role','wsl',*scope_args,*(['--terminal-io'] if terminal_io else []),'--',*argv[offset:]],distro,(offset==6 and argv[4]=='nobody') or bool(native_scope and native_scope.get('drop_uid'))


async def probe_native(distro,expected):
    """Read-only probe in the same distribution as the gated native process."""
    identity.validate(expected)
    from .control_identity import read,ReadUnavailable
    try:
        return await read(distro,'identity',{'expected':expected})
    except ReadUnavailable as error:
        return {'state':'unverified','reason':error.reason}
    except (OSError,UnicodeError,subprocess.TimeoutExpired):
        return {'state':'unverified','reason':'native_probe_unavailable'}
    except (ValueError,TypeError,KeyError):
        return {'state':'unverified','reason':'native_probe_invalid'}


class OwnedProcess:
    def __init__(self):
        self.nonce=uuid.uuid4().hex+uuid.uuid4().hex
        self.proc=None
        self.scope=None
        self.guardian=None
        self.guardian_proof=None
        self.handle=None
        self.pidfd=None
        self.record=None
        self.native=None
        self.is_native=False
        self.released=False
        self.closed=False
        self._stop_operation=None
        self._control_task=None
        self._control_data=bytearray()
        self._control_overflow=False
        self.stop_result=None
        self._record_digest=None
        self.on_preparation=None
        self.on_native_preflight=None
        self.on_namespace=None
        self.namespace_init=None
        self.check_preparation_active=None
        self.native_preflight=None
        self.preparation=None
        self.native_scope=None
        self._native_scope_digest=None
        self.windows_compute_policy=None
        self.linux_compute_policy=None
        self.linux_compute_admission=None
        self.windows_compute_parent=None
        self.linux_compute_usage=None

    async def prepare_guardian(self,root,*,max_seconds=360,on_created=None):
        if os.name!='nt' or self.scope is not None or self.proc is not None or self.closed:
            raise ValueError('Guardian requires an unused Windows launch')
        from .windows_process_scope import WindowsScope
        from .windows_scope_guard import Guardian
        self.scope=WindowsScope(self.nonce,observe=False)
        self.guardian=Guardian(self.scope,root,max_seconds=max_seconds)
        self.guardian.on_created=on_created
        await finish_before_cancel(asyncio.to_thread(self.guardian.start))

    async def retire_guardian(self):
        if self.guardian is not None and self.guardian_proof is None:
            self.guardian_proof=await finish_before_cancel(asyncio.to_thread(self.guardian.retire))
        return self.guardian_proof

    def _record(self,host,runtime,native):
        self.record={'schema':SCHEMA,'pid':host['pid'],'runtime':runtime,'launch_nonce':self.nonce,
            'host':host,'native':native,'scope':self.scope.name if self.scope else 'linux-pidfd',
            'gate_sha256':hashlib.sha256(GATE.read_bytes()).hexdigest(),
            'identity_probe_sha256':hashlib.sha256(PROBE.read_bytes()).hexdigest()}
        self._record_digest=hashlib.sha256(json.dumps(self.record,sort_keys=True).encode()).hexdigest()

    async def _preparation(self,stage,runtime,host,native=None):
        from .process_preparation import SCHEMA as PREPARATION_SCHEMA,validate
        value={'schema':PREPARATION_SCHEMA,'stage':stage,'launch_nonce':self.nonce,'runtime':runtime,
            'host':host,'native':native,'scope':None if stage=='created' else self.scope.name if self.scope else 'linux-pidfd',
            'gate_sha256':hashlib.sha256(GATE.read_bytes()).hexdigest(),
            'identity_probe_sha256':hashlib.sha256(PROBE.read_bytes()).hexdigest(),
            'observed_at':datetime.now(timezone.utc).isoformat()}
        validate(value);self.preparation=value
        if self.on_preparation is not None:
            await self.on_preparation(json.loads(json.dumps(value)))
        if self.check_preparation_active is not None:self.check_preparation_active()

    async def hello(self,role):
        line=await asyncio.wait_for(self.proc.stdout.readline(),timeout=15)
        if len(line)>16384 or not line.startswith(PREFIX.encode('ascii')):
            raise ValueError('Process did not present a trusted startup handshake')
        message=json.loads(line[len(PREFIX):])
        if set(message)!={'nonce','role','identity'} or message['nonce']!=self.nonce or message['role']!=role:
            raise ValueError('Process startup handshake belongs to another launch')
        return identity.validate(message['identity'])

    async def _release(self):
        self.proc.stdin.write((json.dumps({'action':'release','nonce':self.nonce})+'\n').encode('ascii'))
        await self.proc.stdin.drain()

    async def prepare(self,argv,*,cwd,env,runtime,shell=False,terminal_io=False):
        if (not isinstance(argv,list) or not argv or any(not isinstance(value,str) or '\0' in value for value in argv)
                or runtime not in {'windows','linux','wsl','wsl-sandbox'} or type(terminal_io) is not bool or (shell and terminal_io)):
            raise ValueError('Invalid owned process launch')
        if shell and (os.name!='nt' or runtime!='windows' or len(argv)!=1):
            raise ValueError('Invalid owned Windows shell command')
        if shell and self.native_scope is not None:raise ValueError('Shell cannot select a native scope')
        self._native_scope_digest=hashlib.sha256(json.dumps(self.native_scope,sort_keys=True).encode()).hexdigest()
        payload,distro,nonroot=(argv,None,False) if shell else native_payload(argv,self.nonce,terminal_io=terminal_io,native_scope=self.native_scope)
        if self.on_native_preflight is not None:
            dropping=self.native_scope is not None and self.native_scope.get('drop_uid')==65534
            explicit_root=argv[3:6]==['--user','root','--exec']
            if distro is None or nonroot and not (dropping and explicit_root) or self.native_scope is None or self.guardian is None:
                raise ValueError('Native preflight requires the guarded root scope')
            from . import native_process_preflight as preflight
            helper=GATE.with_name('native_process_preflight.py')
            hashes={name:hashlib.sha256(GATE.with_name(name).read_bytes()).hexdigest() for name in preflight.FILES}
            # Replace only the fixed gate entrypoint, preserving its complete argv.
            offset=6 if explicit_root else 4
            if payload[offset:offset+5]!=['python3','-I','-S','-u',wsl_path(GATE)]:
                raise ValueError('Unexpected scoped native gate command')
            payload=[*payload[:offset+4],wsl_path(helper),self.nonce,
                     base64.b64encode(json.dumps(hashes).encode()).decode('ascii'),'--',*payload[offset+5:]]
        self.is_native=distro is not None
        base=Path(getattr(sys,'_base_executable',sys.executable)).resolve()
        if not base.is_file():
            raise ValueError('Base Python interpreter is unavailable')
        options={}
        if os.name=='nt':
            from .windows_process_scope import WindowsScope
            if self.guardian is None:
                if self.scope is not None:raise ValueError('Unexpected existing launch scope')
                self.scope=WindowsScope(self.nonce)
            elif (self.scope is not self.guardian.scope or self.scope.name!='Local\\VibeFlow-'+self.nonce
                    or self.guardian.prepared is None or self.guardian.proc.poll() is not None
                    or self.scope.accounting().total_processes!=0):
                raise ValueError('Prepared guardian no longer owns an empty live launch')
        else:
            options['start_new_session']=True
        args=[str(base),'-I','-S','-u',str(GATE),'--nonce',self.nonce,'--role','host']
        if self.on_namespace is not None:
            if os.name=='nt' or distro is not None:
                raise ValueError('Namespace observation requires a native Linux host')
            args.append('--prepare-namespace')
        if self.linux_compute_policy is not None:
            from .linux_gate_compute import selection
            if self.on_namespace is None:
                raise ValueError('Linux compute requires the prepared namespace registration')
            args.extend(('--linux-compute',selection(self.linux_compute_policy)))
        if self.linux_compute_admission is not None:
            if self.on_namespace is None or self.linux_compute_policy is not None:
                raise ValueError('Shared Linux compute requires a prepared namespace')
            args.extend(('--linux-compute-shared',self.linux_compute_admission))
        if shell:
            args.append('--shell')
        if distro:
            args.append('--forward-control')
        if terminal_io:args.append('--terminal-io')
        try:
            if self.windows_compute_policy and self.scope is not None and self.windows_compute_parent is None:
                policy=self.windows_compute_policy
                self.scope.configure_compute(policy['user_cpu_seconds'],policy['committed_memory_bytes'])
            async def create():
                if os.name=='nt':
                    from .windows_process_create import create as create_in_job
                    self.proc=await create_in_job(self.scope,*args,'--',*payload,
                        cwd=str(cwd) if cwd else None,env=env,parent_scope=self.windows_compute_parent)
                else:
                    self.proc=await asyncio.create_subprocess_exec(*args,'--',*payload,cwd=str(cwd) if cwd else None,env=env,
                        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,**options)
                self._control_task=asyncio.create_task(self._read_control())
                self._control_task.add_done_callback(lambda task:task.exception() if not task.cancelled() else None)
                if os.name=='nt':
                    from .windows_process_scope import ProcessHandle
                    creation=self.proc._transport.get_extra_info('subprocess')
                    self.handle=ProcessHandle(int(creation._handle))
                    if not self.scope.contains(self.handle):
                        raise ValueError('Created host is outside its atomic Job assignment')
            # Cancellation can arrive after CreateProcess but before asyncio
            # returns the transport. Retain the actual handle before aborting.
            await finish_before_cancel(create())
            # Creation can finish after the owner stops. Keep the actual
            # transport/handle for abort, then refuse to start the next gate.
            if self.check_preparation_active is not None:self.check_preparation_active()
            created=self.handle.identity() if self.handle is not None else identity.observe(self.proc.pid)
            if self.on_native_preflight is None:
                await self._preparation('created',runtime,created)
            host=await self.hello('host')
            if host['pid']!=self.proc.pid:
                raise ValueError('Host gate is a redirector rather than the created process')
            if self.handle is not None:
                if self.handle.identity()!=host or not self.scope.contains(self.handle):
                    raise ValueError('Host handshake differs from its creation scope')
            else:
                self.pidfd=os.pidfd_open(self.proc.pid)
                if identity.probe(host)['state']!='match':
                    raise ValueError('Native host gate identity changed')
            native=None
            if not distro:self._record(host,runtime,None)
            if self.on_native_preflight is not None:
                if self.check_preparation_active is not None:self.check_preparation_active()
                await self._release()  # Only starts the read-only birth helper.
                observed=await self.hello('wsl_prepared')
                self.native={'distribution':distro,'process':observed}
                if observed['uid']!=0 or (await probe_native(distro,observed))['state']!='match':
                    raise ValueError('Prepared native birth is not the selected live root helper')
                self.native_preflight={'schema':preflight.SCHEMA,'launch_nonce':self.nonce,'host':host,
                    'native':self.native,'helpers':hashes}
                await self.on_native_preflight(json.loads(json.dumps(self.native_preflight)))
                await self._preparation('created',runtime,created)
            await self._preparation('host_ready',runtime,host)
            if self.on_namespace is not None:
                await self._release()  # Creates only the waiting trusted namespace init.
                self.namespace_init=await self.hello('linux_init')
                if (self.namespace_init['host_id']!=host['host_id'] or self.namespace_init['boot_id']!=host['boot_id']
                        or self.namespace_init['pid_namespace']==host['pid_namespace']
                        or identity.probe(self.namespace_init)['state']!='match'):
                    raise ValueError('Prepared namespace init differs from its host')
                self.record['namespace_init']=json.loads(json.dumps(self.namespace_init))
                self._record_digest=hashlib.sha256(json.dumps(self.record,sort_keys=True).encode()).hexdigest()
                await self.on_namespace(json.loads(json.dumps(self.record)),json.loads(json.dumps(self.namespace_init)))
            if distro:
                # The first release can start only this fixed, waiting native
                # helper. The user's command remains behind its second gate.
                if self.on_native_preflight is None:
                    await self._release()
                else:
                    self.proc.stdin.write((json.dumps(preflight.permit(self.nonce,self.native['process']))+'\n').encode())
                    await self.proc.stdin.drain()
                observed=await self.hello('wsl')
                if self.native_preflight is not None:
                    expected=dict(self.native_preflight['native']['process'])
                    if self.native_scope.get('drop_uid')==65534:expected['uid']=65534
                    if observed!=expected:
                        raise ValueError('Native gate differs from its durably admitted preflight birth or fixed dropped identity')
                self.native={'distribution':distro,'process':observed}
                if nonroot and observed['uid']==0:
                    raise ValueError('The nobody execution profile must not run as native root')
                if (await probe_native(distro,observed))['state']!='match':
                    raise ValueError('WSL gate birth identity could not be verified')
                native=self.native
                self._record(host,runtime,native)
                await self._preparation('native_ready',runtime,host,native)
            return self
        except BaseException as failure:
            try:
                if self.record is not None and not self.closed:
                    try:await finish_before_cancel(self.stop(reason='executor_error',grace_ms=0))
                    except BaseException as cleanup:failure.add_note('Preparation cleanup unverified: '+type(cleanup).__name__)
            finally:await finish_before_cancel(self.abort())
            raise

    async def release(self):
        if self.record is None or self.closed or self.released:
            raise ValueError('Process gate is not awaiting registration')
        await self._release()
        self.released=True

    async def verify_registration(self):
        if self.guardian is not None and (self.guardian.proc.poll() is not None or self.guardian_proof is not None):
            raise ValueError('Business release requires its selected guardian to remain live')
        if self._native_scope_digest!=hashlib.sha256(json.dumps(self.native_scope,sort_keys=True).encode()).hexdigest():
            raise ValueError('Native scope selection changed after preparation')
        if self.closed or self.record is None or self._record_digest!=hashlib.sha256(json.dumps(self.record,sort_keys=True).encode()).hexdigest():
            raise ValueError('Owned process registration changed after actual preparation')
        if self.record['launch_nonce']!=self.nonce or self.record['pid']!=self.proc.pid:raise ValueError('Owned registration crosses its launch')
        if self.handle is not None:
            if self.handle.identity()!=self.record['host'] or not self.scope.contains(self.handle):raise ValueError('Owned host is outside its retained scope')
            if self.windows_compute_parent is not None and not self.windows_compute_parent.contains(self.handle):
                raise ValueError('Owned host is outside the attempt compute scope')
        elif (await asyncio.to_thread(identity.probe,self.record['host']))['state']!='match':raise ValueError('Owned host birth changed')
        if self.namespace_init is not None and (await asyncio.to_thread(identity.probe,self.namespace_init))['state']!='match':
            raise ValueError('Prepared namespace init changed before release')
        if self.native is not None and (await probe_native(self.native['distribution'],self.native['process']))['state']!='match':raise ValueError('Owned native birth changed')

    async def _read_control(self):
        while True:
            data=await self.proc.stderr.read(4096)
            if not data:
                return
            room=max(0,8192-len(self._control_data))
            self._control_data.extend(data[:room])
            if len(data)>room:
                self._control_overflow=True

    def _control_reports(self):
        if self._control_overflow:
            return [],False
        from .linux_gate_compute import SCHEMA as COMPUTE, validate_report
        reports=[]
        compute_usage=None
        try:
            for line in bytes(self._control_data).splitlines():
                value=json.loads(line)
                if isinstance(value,dict) and value.get('schema')==COMPUTE:
                    if self.linux_compute_policy is None or compute_usage is not None:
                        return [],False
                    compute_usage=validate_report(value,self.nonce,self.linux_compute_policy)
                    continue
                if (not isinstance(value,dict) or set(value)!={'schema','nonce','role','stop_received','grace_forwarded','forced','exit_code'}
                        or value['schema']!='vibeflow.gate-exit.v1' or value['nonce']!=self.nonce
                        or value['role'] not in ({'wsl','init'} if self.is_native else {'host'} if os.name=='nt' else {'host','init'})
                        or any(type(value[key]) is not bool for key in ('stop_received','grace_forwarded','forced'))
                        or type(value['exit_code']) is not int or any(item['role']==value['role'] for item in reports)):
                    return [],False
                reports.append(value)
            if self.linux_compute_policy is not None and self.released and compute_usage is None:
                return [],False
            self.linux_compute_usage=compute_usage
            return reports,True
        except (ValueError,TypeError,KeyError):
            return [],False

    async def wait_root_exit(self,timeout=None):
        """Observe exit independently of pipe EOF; descendants may own the pipes."""
        async def wait():
            while self.proc.returncode is None:
                await asyncio.sleep(.02)
            if self.handle is not None:
                self.handle.wait_exit(0)
            return self.proc.returncode
        return await asyncio.wait_for(wait(),timeout) if timeout is not None else await wait()

    async def stop(self,*,reason='cancelled',grace_ms=2000):
        from .process_stop import REASONS
        if reason not in REASONS:
            raise ValueError('Invalid process stop reason')
        if type(grace_ms) is not int or not 0<=grace_ms<=10000:
            raise ValueError('Invalid process stop grace')
        if self._stop_operation is None:
            if self.closed or self.proc is None:
                raise ValueError('Process scope is not open')
            self._stop_operation=asyncio.create_task(self._stop(reason,grace_ms))
            self._stop_operation.add_done_callback(lambda task:task.exception() if not task.cancelled() else None)
        return await finish_before_cancel(self._stop_operation)

    async def _stop(self,reason,grace_ms):
        deadline=asyncio.get_running_loop().time()+grace_ms/1000
        result={'schema':'vibeflow.process-stop.v1','launch_nonce':self.nonce,'reason':reason,
            'grace_ms':grace_ms,'request_sent':False,'forced':False,'scope_empty':False,
            'native_state':None,'root_exit_code':None,'control_valid':False,'control':[],
            'verified':False,'error':None,'started_at':datetime.now(timezone.utc).isoformat(),'completed_at':None}
        result['scope_observation']=None
        self.stop_result=result
        try:
            if self.proc.returncode is None:
                try:
                    self.proc.stdin.write((json.dumps({'action':'stop','nonce':self.nonce,'grace_ms':grace_ms})+'\n').encode('ascii'))
                    await self.proc.stdin.drain()
                    result['request_sent']=True
                except (BrokenPipeError,ConnectionResetError):
                    pass
                try:
                    await self.wait_root_exit(grace_ms/1000+2)
                except asyncio.TimeoutError:
                    pass
            if self.proc.stdin is not None:
                self.proc.stdin.close()
            if self.scope is not None:
                # A shell may exit before its cooperating children finish.
                # Keep the remaining grace for those members too.
                while self.scope.active_processes() and asyncio.get_running_loop().time()<deadline:
                    await asyncio.sleep(.02)
                if self.guardian is not None:
                    proof=await self.retire_guardian()
                    result['forced']=proof['receipt']['forced']
                    result['scope_observation']=proof['receipt']['membership']
                else:
                    result['forced']=await finish_before_cancel(asyncio.to_thread(self.scope.retire,5))
                result['scope_empty']=True
            elif self.pidfd is not None and self.proc.returncode is None:
                import signal
                signal.pidfd_send_signal(self.pidfd,signal.SIGKILL)
                result['forced']=True
            result['root_exit_code']=await self.wait_root_exit(5)
            if self.native:
                observed=await probe_native(self.native['distribution'],self.native['process'])
                result['native_state']=observed['state']
                if observed['state']!='exited':
                    raise RuntimeError('Native supervisor exit could not be verified')
            if self._control_task is not None:
                await asyncio.wait_for(asyncio.shield(self._control_task),timeout=5)
            reports,valid=self._control_reports()
            result.update(control=reports,control_valid=valid)
            if reason == 'root_exit' and self.linux_compute_usage is not None:
                result['reason'] = self.linux_compute_usage['violation'] or reason
            result['forced']=result['forced'] or any(item['forced'] for item in reports)
            if self.scope is None:
                # Linux host completion also requires its namespace gate report.
                result['scope_empty']=valid and any(item['role']=='host' for item in reports)
            if result['root_exit_code']==0:
                required={'wsl','init'} if self.is_native else {'host'} if os.name=='nt' else {'host','init'}
                if not valid or {item['role'] for item in reports}!=required:
                    raise RuntimeError('Successful native exit lacks trusted gate completion')
            result['verified']=result['scope_empty'] and (self.native is None or result['native_state']=='exited')
            if not result['verified']:
                raise RuntimeError('Process scope exit could not be verified')
            return result
        except BaseException as error:
            result['error']=type(error).__name__
            # Accounting/control failure may happen before retire() can run.
            # Still stop the objects already owned by this launch; preserve the
            # failed verification rather than replacing it with a success claim.
            try:
                if self.proc.stdin is not None:
                    self.proc.stdin.close()
                if self.scope is not None:
                    self.scope.terminate(125)
                elif self.pidfd is not None and self.proc.returncode is None:
                    import signal
                    signal.pidfd_send_signal(self.pidfd,signal.SIGKILL)
                result['root_exit_code']=await self.wait_root_exit(5)
            except BaseException as fallback_error:
                error.add_note('Owned scope fallback did not finish: '+type(fallback_error).__name__)
            raise
        finally:
            if self.scope is not None and self.scope.observer is not None:
                result['scope_observation']=self.scope.observer.summary()
            result['completed_at']=datetime.now(timezone.utc).isoformat()

    async def finish(self):
        """The main child exited; retire any remaining members before publishing."""
        if self.proc is None or self.proc.returncode is None:
            raise ValueError('Process completion requires an observed native exit')
        return await self.stop(reason='root_exit',grace_ms=0)

    async def abort(self):
        """Close startup control, then terminate only the retained owned scope."""
        if self.closed:
            return
        try:
            if self.proc is not None:
                if self.proc.stdin is not None:
                    self.proc.stdin.close()
                # Give the native gate a bounded chance to kill its namespace.
                if self.is_native:
                    try:
                        await self.wait_root_exit(5)
                    except asyncio.TimeoutError:
                        pass
                if self.proc.returncode is None:
                    if self.scope is not None and self.handle is not None and self.scope.contains(self.handle):
                        self.scope.terminate(125)
                    elif self.pidfd is not None:
                        import signal
                        signal.pidfd_send_signal(self.pidfd,signal.SIGKILL)
                    elif os.name=='nt':
                        # asyncio retains the actual creation handle on Windows.
                        self.proc.kill()
                    else:
                        # Before a Linux pidfd exists, only pipe closure is safe.
                        await asyncio.wait_for(self.proc.wait(),timeout=5)
                # stderr belongs exclusively to the fixed gate reader.
                await asyncio.wait_for(self.proc.stdout.read(),timeout=10)
                await self.wait_root_exit(5)
                if self._control_task is not None:
                    await asyncio.wait_for(asyncio.shield(self._control_task),timeout=5)
                if self.native and (await probe_native(self.native['distribution'],self.native['process']))['state']!='exited':
                    raise RuntimeError('Native startup helper exit could not be verified')
        finally:
            try:
                await self.retire_guardian()
            finally:
                self.close()

    def close(self):
        if self.closed:
            return
        if self.proc is not None and self.proc.stdin is not None:
            self.proc.stdin.close()
        if self.guardian is not None:
            self.guardian.close()
        if self.scope is not None:
            self.scope.close()
        if self.handle is not None:
            self.handle.close()
        if self.pidfd is not None:
            os.close(self.pidfd)
            self.pidfd=None
        self.closed=True
