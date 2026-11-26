"""Interactive transport below OwnedProcess; PTY release follows durable birth registration."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from .async_ops import finish_before_cancel
from .owned_process import OwnedProcess,wsl_path,probe_native
from . import process_identity_native as identity,terminal_protocol as protocol

BRIDGE=Path(__file__).with_name('terminal_bridge.py')
GATE=Path(__file__).with_name('terminal_pty_gate.py')
PROBE=Path(__file__).with_name('terminal_identity_probe.py')
LINUX_LAUNCHER=Path(__file__).with_name('agent_shell_linux.py')


class OwnedTerminal:
    def __init__(self):
        self.owned=OwnedProcess();self.profile=None;self.distro=None
        self.reader=None;self.writer=None;self.write_lock=asyncio.Lock();self.gate_birth=None;self.pty_handle=None
        self.release_attempted=False;self.released=False;self.stop_operation=None;self.exit_code=None
        self.input_sequence=0;self.input_waiters={};self.input_lock=asyncio.Lock()

    def configure(self,shell,*,cwd=None,cols=80,rows=24,distro='vibeflow-linux',external_export=False,disk_root=None,
                  linux_home=None):
        native=shell in {'wsl','wsl-sandbox'}
        if native and (not isinstance(distro,str) or not re.fullmatch('[a-z0-9-]+',distro)):raise ValueError('Invalid terminal distribution')
        if shell=='cmd':
            command=([os.environ.get('COMSPEC',r'C:\Windows\System32\cmd.exe')]
                     if os.name=='nt' else list(protocol.LINUX_SHELL))
        else:
            command=['bash','-l']
        workdir=wsl_path(cwd) if native and cwd is not None else str(Path(cwd).resolve()) if cwd is not None else None
        if shell=='wsl-sandbox':
            if cwd is None:raise ValueError('Sandbox terminal requires its case directory')
            command=['unshare','--user','--map-root-user','--mount','--net','--pid','--fork','--mount-proc',
                'bash',wsl_path(BRIDGE.with_name('sandbox_wsl.sh')),workdir,'bash -l']
            if disk_root is not None:command+=['--persistent-export',wsl_path(disk_root)]
            elif external_export:command.append('--external-export')
        profile={'shell':shell,'cwd':workdir,'command':command,'cols':cols,'rows':rows,'nonce':self.owned.nonce}
        if command==list(protocol.LINUX_SHELL):
            profile['linux_launcher_sha256']=hashlib.sha256(LINUX_LAUNCHER.read_bytes()).hexdigest()
        if linux_home is not None:
            profile['linux_home']=linux_home
        self.profile=protocol.validate(profile)
        self.distro=distro if native else None
        self.cwd=cwd
        self.code={key:hashlib.sha256(path.read_bytes()).hexdigest() for key,path in self.code_paths().items()}
        return {'terminal':dict(self.profile),'distro':self.distro,'code':dict(self.code)}

    def code_paths(self):
        paths={'bridge_sha256':BRIDGE,'pty_gate_sha256':GATE,'identity_probe_sha256':PROBE,'protocol_sha256':Path(protocol.__file__)}
        if self.profile is not None and 'linux_launcher_sha256' in self.profile:paths['linux_launcher_sha256']=LINUX_LAUNCHER
        if self.profile is not None and self.profile['shell']=='wsl-sandbox':
            paths['sandbox_sha256']=BRIDGE.with_name('sandbox_wsl.sh')
            paths['tree_metadata_sha256']=BRIDGE.with_name('tree_metadata_native.py')
        if self.profile is not None and '--persistent-export' in self.profile['command']:paths['disk_sha256']=BRIDGE.with_name('sandbox_disk_native.py')
        return paths

    def check_code(self):
        if self.code!={key:hashlib.sha256(path.read_bytes()).hexdigest() for key,path in self.code_paths().items()}:
            raise ValueError('Terminal implementation changed after profile selection')

    async def prepare(self,shell,**kwargs):
        self.configure(shell,**kwargs)
        return await self.prepare_configured()

    async def prepare_configured(self,environment=None):
        self.check_code();native=self.distro is not None
        argv=([sys.executable,'-I','-u',str(BRIDGE),protocol.encode(self.profile)] if not native else
              ['wsl.exe','-d',self.distro,*(['--user','nobody'] if self.profile['shell']=='wsl' else []),'--exec','python3','-I','-S','-u',wsl_path(BRIDGE),protocol.encode(self.profile)])
        if environment is not None and native:raise ValueError('Explicit terminal environment requires Windows')
        runtime=self.profile['shell'] if native else ('windows' if os.name=='nt' else 'linux')
        await self.owned.prepare(argv,cwd=self.cwd,env=environment,runtime=runtime,terminal_io=True)
        return self.owned.record

    async def start_bridge(self):
        # The caller must commit this root's actual birth before starting the
        # bridge. Its private PTY gate still cannot execute the command shell.
        self.check_code();await self.owned.release()
        self.reader=self.owned.proc.stdout;self.writer=self.owned.proc.stdin
        gated=await self.receive(timeout=20)
        if (set(gated)!={'type','nonce','identity','profile_sha256'} or gated['type']!='gated'
                or gated['nonce']!=self.owned.nonce or gated['profile_sha256']!=protocol.digest(self.profile)):
            raise ValueError('PTY gate does not belong to its registered profile')
        reported=identity.validate(gated['identity']);self.reported_birth=reported
        if self.distro is None and os.name=='nt':
            from .windows_process_scope import ProcessHandle
            self.pty_handle=await asyncio.to_thread(ProcessHandle.query,reported)
            if not self.owned.scope.contains(self.pty_handle):raise ValueError('ConPTY gate escaped its registered Windows scope')
            birth=reported
        elif self.distro is None:
            from .terminal_identity_probe import locate
            birth=await asyncio.to_thread(locate,self.owned.record['host'],reported)
        else:
            from .control_identity import read
            birth=await read(self.distro,'ancestry',{'root':self.owned.native['process'],'reported':reported})
        self.check_code()
        self.gate_birth={'schema':'vibeflow.terminal-gate.v1','launch_nonce':self.owned.nonce,'process':birth,
            'profile_sha256':protocol.digest(self.profile),**self.code}
        return self.gate_birth

    async def verify_gate(self):
        self.check_code()
        birth=self.gate_birth['process']
        if self.distro is None and os.name=='nt':
            if (self.pty_handle is None or self.pty_handle.identity()!=birth or not self.owned.scope.contains(self.pty_handle)):
                raise ValueError('Terminal gate no longer belongs to its registered scope')
        elif self.distro is None:
            from .terminal_identity_probe import locate
            actual=await asyncio.to_thread(locate,self.owned.record['host'],self.reported_birth)
            if actual!=birth:raise ValueError('Terminal gate no longer belongs to its registered Linux scope')
        else:
            from .control_identity import read
            observed=await read(self.distro,'ancestry',{'root':self.owned.native['process'],'reported':self.reported_birth})
            if observed!=birth:raise ValueError('Native terminal gate no longer belongs to its registered root')

    async def release(self):
        if self.gate_birth is None or self.released:raise ValueError('Terminal is not awaiting durable gate registration')
        await self.verify_gate()
        self.release_attempted=True
        await self.send({'action':'release','nonce':self.owned.nonce})
        if await self.receive(timeout=10)!={'type':'released','nonce':self.owned.nonce}:raise ValueError('Terminal release was not acknowledged')
        self.released=True

    async def send(self,value):
        if self.writer is None:raise ValueError('Terminal bridge is unavailable')
        async with self.write_lock:
            message={'action':'terminal_io','nonce':self.owned.nonce,'data':base64.b64encode(protocol.pack(value)).decode('ascii')}
            self.writer.write(protocol.pack(message));await asyncio.wait_for(self.writer.drain(),5)

    async def receive(self,timeout=None):
        if self.reader is None:raise ValueError('Terminal bridge is unavailable')
        line=await asyncio.wait_for(self.reader.readline(),timeout) if timeout is not None else await self.reader.readline()
        if not line or len(line)>protocol.MAX_LINE or not line.endswith(b'\n'):raise EOFError('Terminal bridge ended')
        value=json.loads(line)
        if not isinstance(value,dict):raise ValueError('Invalid terminal frame')
        return value

    async def write(self,value):
        if not self.released:raise ValueError('Terminal has not been released')
        async with self.input_lock:
            # One acknowledged chunk keeps input bounded without blocking the
            # independent gate reader which receives stop controls.
            for offset in range(0,len(value),4096):
                chunk=value[offset:offset+4096].encode('utf-8')
                if len(chunk)>protocol.MAX_INPUT:raise ValueError('Terminal input chunk exceeds its limit')
                await self._acknowledged({'action':'input','data':base64.b64encode(chunk).decode('ascii')})

    async def _acknowledged(self,message):
        self.input_sequence+=1;identifier=self.input_sequence
        waiter=asyncio.get_running_loop().create_future();self.input_waiters[identifier]=(waiter,message['action'])
        waiter.add_done_callback(lambda future:future.exception() if not future.cancelled() else None)
        try:
            await self.send({**message,'id':identifier});await waiter
        finally:
            self.input_waiters.pop(identifier,None)
            if not waiter.done():waiter.cancel()

    def acknowledge_input(self,identifier,kind='input'):
        if type(identifier) is not int or not 0<identifier<=self.input_sequence:raise ValueError('Invalid terminal input acknowledgment')
        pending=self.input_waiters.get(identifier)
        if pending is not None:
            waiter,expected=pending
            if kind!=expected:raise ValueError('Terminal acknowledgment belongs to another operation')
            if not waiter.done():waiter.set_result(None)

    def fail_inputs(self):
        for waiter,_kind in tuple(self.input_waiters.values()):
            if not waiter.done():waiter.set_exception(EOFError('Terminal input channel ended'))

    async def resize(self,cols,rows):await self._acknowledged({'action':'resize','cols':cols,'rows':rows})

    async def stop(self,reason='cancelled'):
        if self.stop_operation is None:self.stop_operation=asyncio.create_task(self._stop(reason))
        return await finish_before_cancel(self.stop_operation)

    async def _stop(self,reason):
        try:
            # Retire the complete registered scope; transport EOF and the
            # ConPTY root alone cannot prove detached children have exited.
            result=await self.owned.stop(reason=reason,grace_ms=500)
            if self.pty_handle is not None:await asyncio.to_thread(self.pty_handle.wait_exit,5)
            if self.gate_birth is not None and self.distro is not None:
                if (await probe_native(self.distro,self.gate_birth['process']))['state']!='exited':raise RuntimeError('Native PTY gate has no exit proof')
            return result
        finally:
            self.fail_inputs()
            if self.writer is not None:
                self.writer.close()
                try:await asyncio.wait_for(self.writer.wait_closed(),5)
                except (OSError,asyncio.TimeoutError):pass
            if self.pty_handle is not None:self.pty_handle.close();self.pty_handle=None
            self.owned.close()

    async def abort(self):
        if self.owned.closed:return
        if self.owned.proc is not None:return await self.stop('executor_error')
        self.owned.close()
