"""Trusted stdlib-only launch gate. No user command runs before release.

The API starts this with the base interpreter and -I -S so a Windows venv
redirector or site hook cannot create an unassigned payload before registration.
The pipe protocol is separate from command output and contains no claim token.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time

PREFIX='VIBEFLOW_PROCESS_GATE_V1 '


def identity_module():
    path=Path(__file__).with_name('process_identity_native.py')
    spec=importlib.util.spec_from_file_location('vibeflow_native_identity',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def linux_compute_module():
    path = Path(__file__).with_name('linux_gate_compute.py')
    spec = importlib.util.spec_from_file_location('vibeflow_linux_gate_compute',path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def enter_native_scope(encoded, nonce):
    """Admit this fixed gate before HELLO; execute the exact pinned helper bytes."""
    if len(encoded) > 4096:
        raise ValueError('Native scope admission exceeds its bound')
    value = json.loads(base64.b64decode(encoded, validate=True))
    required={'scope','helper_sha256'}
    if not isinstance(value,dict) or not required<=value.keys() or value.keys()-required-{'drop_uid','compute_helper_sha256'}:
        raise ValueError('Invalid native scope admission')
    path = Path(__file__).with_name('process_cgroup_native.py')
    source = path.read_bytes()
    if value['helper_sha256'] != hashlib.sha256(source).hexdigest():
        raise ValueError('Native scope helper changed before admission')
    namespace = {'__name__': 'vibeflow_native_cgroup', '__file__': str(path)}
    exec(compile(source, str(path), 'exec'), namespace)
    descriptor = namespace['validate'](value['scope'])
    if descriptor['nonce'] != nonce:
        raise ValueError('Native scope belongs to another launch')
    namespace['join'](descriptor)
    monitor=None
    if 'compute' in descriptor:
        path=Path(__file__).with_name('native_compute_v1.py')
        source=path.read_bytes()
        if value.get('compute_helper_sha256')!=hashlib.sha256(source).hexdigest():
            raise ValueError('Native compute helper changed before admission')
        compute={'__name__':'vibeflow_native_compute','__file__':str(path)}
        exec(compile(source,str(path),'exec'),compute)
        monitor=compute['join'](descriptor['compute'],monitor=True)
    elif 'compute_helper_sha256' in value:
        raise ValueError('Native compute admission has no allocated scope')
    if 'drop_uid' in value:
        if type(value['drop_uid']) is not int or value['drop_uid']!=65534 or os.geteuid()!=0:
            raise ValueError('Invalid fixed native privilege drop')
        os.setgroups([]);os.setgid(65534);os.setuid(65534)
        if os.getuid()!=65534 or os.geteuid()!=65534:raise ValueError('Native gate retained privileged identity')
        # Privilege cannot be regained through a setuid/file-capability exec.
        # This is inherited by the namespace init and every user payload.
        library=ctypes.CDLL(None,use_errno=True)
        if library.prctl(38,1,0,0,0)!=0 or library.prctl(39,0,0,0,0)!=1:
            raise OSError(ctypes.get_errno(),'Native gate could not prevent privilege escalation')
        os.environ.update(HOME='/tmp',USER='nobody',LOGNAME='nobody')
    return monitor


class Control:
    def __init__(self,fd=None,*,terminal_writer=None,nonce=None):
        self.fd=sys.stdin.fileno() if fd is None else fd
        self.messages=queue.Queue(maxsize=8)
        self.closed=threading.Event()
        self.terminal_writer=terminal_writer;self.nonce=nonce
        self.terminal_messages=queue.Queue(maxsize=16)
        if terminal_writer is not None:threading.Thread(target=self._write_terminal,daemon=True).start()
        threading.Thread(target=self._read,daemon=True).start()

    def _write_terminal(self):
        try:
            while True:
                try:data=self.terminal_messages.get(timeout=.1)
                except queue.Empty:
                    if self.closed.is_set():return
                    continue
                view=memoryview(data)
                while view:
                    count=os.write(self.terminal_writer,view);view=view[count:]
        except OSError:self.closed.set()
        finally:os.close(self.terminal_writer)

    def _read(self):
        pending=b''
        try:
            while True:
                chunk=os.read(self.fd,4096)
                if not chunk:
                    return
                pending+=chunk
                if len(pending)>262144:
                    return
                while b'\n' in pending:
                    message,pending=pending.split(b'\n',1)
                    value=json.loads(message)
                    if isinstance(value,dict) and value.get('action')=='terminal_io':
                        if (self.terminal_writer is None or set(value)!={'action','nonce','data'} or value['nonce']!=self.nonce
                                or not isinstance(value['data'],str)):return
                        raw=base64.b64decode(value['data'],validate=True)
                        if len(raw)>128*1024:return
                        self.terminal_messages.put_nowait(raw)
                    else:
                        if len(message)>16384:return
                        self.messages.put_nowait(value)
        except (OSError,ValueError,queue.Full):
            return
        finally:
            self.closed.set()

    def release(self,nonce,timeout=45):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:
                message=self.messages.get(timeout=.05)
            except queue.Empty:
                if self.closed.is_set():
                    raise ValueError('Launch owner disconnected before registration')
                continue
            if message!={'action':'release','nonce':nonce}:
                raise ValueError('Invalid process release')
            return
        raise TimeoutError('Process registration did not finish before the launch deadline')

    def stop(self,nonce):
        try:
            message=self.messages.get_nowait()
        except queue.Empty:
            return {'action':'stop','nonce':nonce,'grace_ms':1000} if self.closed.is_set() else None
        if (not isinstance(message,dict) or set(message)!={'action','nonce','grace_ms'}
                or message['action']!='stop' or message['nonce']!=nonce
                or type(message['grace_ms']) is not int or not 0<=message['grace_ms']<=10000):
            return {'action':'stop','nonce':nonce,'grace_ms':0}
        return message


def _parent_death(parent):
    # Runs in a fresh interpreter, never in preexec_fn after a threaded fork.
    if os.name=='nt' or type(parent) is not int or parent<=0 or os.getppid()!=parent:
        os._exit(125)
    library=ctypes.CDLL(None,use_errno=True)
    if library.prctl(1,signal.SIGKILL,0,0,0)!=0:  # PR_SET_PDEATHSIG
        raise OSError(ctypes.get_errno(),'Unable to bind native supervisor lifetime')
    if os.getppid()!=parent:
        os._exit(125)


def monitor(child,control,nonce,role,graceful,force,*,margin=0,compute_monitor=None):
    """Keep the child unreaped while targeting its group/handle; never rediscover a PID."""
    report={'schema':'vibeflow.gate-exit.v1','nonce':nonce,'role':role,
        'stop_received':False,'grace_forwarded':False,'forced':False,'exit_code':None}
    deadline=None
    try:
        while child.poll() is None:
            if compute_monitor is not None:
                violation=compute_monitor.violation()
                if violation is not None:
                    force()
                    report['forced']=True
                    scope = 'WSL native scope' if role == 'wsl' else 'Linux native scope'
                    print(f'[resource limit] {scope}: {violation}',flush=True)
                    child.wait(timeout=5)
                    break
            if deadline is None:
                request=control.stop(nonce)
                if request is not None:
                    report['stop_received']=True
                    deadline=time.monotonic()+request['grace_ms']/1000+margin
                    try:
                        graceful(request)
                        report['grace_forwarded']=True
                    except (OSError,ValueError,ProcessLookupError):
                        deadline=time.monotonic()
            if deadline is not None and time.monotonic()>=deadline:
                # No intervening wait/reap occurred after poll returned None.
                force()
                report['forced']=True
                child.wait(timeout=5)
                break
            time.sleep(.02)
        report['exit_code']=child.returncode
        return child.returncode
    finally:
        if child.poll() is None:
            force()
            report['forced']=True
            child.wait(timeout=5)
        report['exit_code']=child.returncode
        print(json.dumps(report,sort_keys=True),file=sys.stderr,flush=True)


def inner(arguments,fd,nonce,terminal_fd=None,volume_fd=None,ready_fd=None):
    """Remain PID 1; exiting this init tears down even detached descendants."""
    if os.name=='nt' or os.getpid()!=1:
        raise ValueError('Native payload init requires its private PID namespace')
    if fd is None or fd<3:
        raise ValueError('Native init requires its private control descriptor')
    if terminal_fd is not None and terminal_fd<3:raise ValueError('Native terminal requires its private input descriptor')
    control=Control(fd)
    if ready_fd is not None:
        # Only this fixed PID 1 exists until the owner durably records its
        # outer-namespace birth and sends the business release.
        os.write(ready_fd,(str(Path('/proc/self/ns/pid').stat().st_ino)+'\n').encode('ascii'))
        os.close(ready_fd)
        control.release(nonce)
    if volume_fd is not None:
        if (volume_fd<3 or len(arguments)!=13 or arguments[:4]!=['python3','-I','-S','-u']
                or Path(arguments[4]) not in {Path(__file__).with_name(name).resolve() for name in ('sandbox_export_native.py','sandbox_recovery_native.py')}
                or arguments[9:12]!=['--volume-fd',str(volume_fd),'--binding']):
            raise ValueError('Only the fixed sandbox exporter may receive a pinned directory')
    child=subprocess.Popen(arguments,stdin=terminal_fd if terminal_fd is not None else subprocess.DEVNULL,
        stderr=subprocess.STDOUT,start_new_session=True,pass_fds=() if volume_fd is None else (volume_fd,))
    if volume_fd is not None:os.close(volume_fd)
    if terminal_fd is not None:os.close(terminal_fd)
    code=monitor(child,control,nonce,'init',lambda _:os.killpg(child.pid,signal.SIGTERM),
        lambda:os.killpg(child.pid,signal.SIGKILL))
    sys.stdout.flush()
    sys.stderr.flush()
    return code if code>=0 else 128-code


def execute(arguments,control,nonce,role,*,namespace,shell=False,terminal_fd=None,prepare_namespace=False,
            compute_monitor=None,linux_admission=None):
    if not namespace:
        if shell and len(arguments)!=1:
            raise ValueError('Shell launch requires one unchanged command string')
        child=subprocess.Popen(arguments[0] if shell else arguments,shell=shell,stdin=terminal_fd if terminal_fd is not None else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        if terminal_fd is not None:os.close(terminal_fd)
        return monitor(child,control,nonce,role,lambda _:child.send_signal(signal.CTRL_BREAK_EVENT),child.kill)
    # Pin in the outer namespace while the trusted gate can still identify the
    # terminal. Only a directory FD crosses into the exporter's PID namespace.
    volume=None;disk=None;recovery_payload=False
    if len(arguments)>4 and Path(arguments[4])==Path(__file__).with_name('cli_native_handoff.py').resolve():
        spec=importlib.util.spec_from_file_location('gate_cli_handoff',Path(arguments[4]))
        handoff=importlib.util.module_from_spec(spec);spec.loader.exec_module(handoff)
        handoff.prepare(arguments,nonce)
        # The fixed wrapper needs the host UID mapping only long enough to
        # drop to nobody. It never launches the bridge or CLI while privileged.
        recovery_payload=True
    if len(arguments)>4 and Path(arguments[4])==Path(__file__).with_name('sandbox_image_cleanup_native.py').resolve():
        spec=importlib.util.spec_from_file_location('gate_image_cleanup',Path(arguments[4]))
        cleanup=importlib.util.module_from_spec(spec);spec.loader.exec_module(cleanup)
        # Fixed deletion checks the original births here, in the outer PID
        # namespace. The supervised inner child only verifies the saved receipt.
        cleanup.prepare(arguments)
        recovery_payload=True
    if len(arguments)>4 and Path(arguments[4])==Path(__file__).with_name('sandbox_recovery_native.py').resolve():
        spec=importlib.util.spec_from_file_location('gate_sandbox_recovery',Path(arguments[4]))
        recovery=importlib.util.module_from_spec(spec);spec.loader.exec_module(recovery)
        volume,disk,arguments=recovery.prepare(arguments)
        recovery_payload=True
    if len(arguments)>4 and Path(arguments[4])==Path(__file__).with_name('sandbox_export_native.py').resolve():
        spec=importlib.util.spec_from_file_location('gate_sandbox_export',Path(arguments[4]))
        exporter=importlib.util.module_from_spec(spec);spec.loader.exec_module(exporter)
        volume,arguments=exporter.prepare_descriptor(arguments)
    if len(arguments)==6 and arguments[:4]==['python3','-I','-S','-u'] and Path(arguments[4])==Path(__file__).with_name('terminal_bridge.py').resolve():
        spec=importlib.util.spec_from_file_location('gate_terminal_protocol',Path(__file__).with_name('terminal_protocol.py'))
        protocol=importlib.util.module_from_spec(spec);spec.loader.exec_module(protocol)
        selected=protocol.decode(arguments[5])
        if '--persistent-export' in selected['command']:
            spec=importlib.util.spec_from_file_location('gate_sandbox_disk',Path(__file__).with_name('sandbox_disk_native.py'))
            disks=importlib.util.module_from_spec(spec);spec.loader.exec_module(disks)
            disk=disks.Image.create(selected['command'][-1])
            try:disk.attach()
            except BaseException:disk.close();raise
    reader,writer=os.pipe()
    ready_reader,ready_writer=os.pipe() if prepare_namespace else (None,None)
    # The fixed recovery copier needs DAC access to files owned by the sandbox
    # UID. It runs no user program and stays in a supervised private PID/mount
    # namespace; only this validated invocation omits an additional user namespace.
    command=['unshare',*(() if recovery_payload else ('--user','--map-current-user')),'--mount','--pid','--fork','--mount-proc',
        '--kill-child=SIGKILL',sys.executable,'-I','-S','-u',str(Path(__file__).resolve()),
        '--inner','--control-fd',str(reader),'--nonce',nonce]
    if terminal_fd is not None:command+=['--terminal-fd',str(terminal_fd)]
    if volume is not None:command+=['--volume-fd',str(volume.fd)]
    if ready_writer is not None:command+=['--ready-fd',str(ready_writer)]
    command+=['--',*arguments]
    # The process is our unreaped direct child when pidfd_open is called; the
    # handle is retained before any poll/wait can permit PID reuse.
    child=None
    descriptor=None
    try:
        admission = ['--nonce',nonce,'--linux-compute-member',linux_admission] if linux_admission else []
        child=subprocess.Popen([sys.executable,'-I','-S','-u',str(Path(__file__).resolve()),
            '--parent-pid',str(os.getpid()),*admission,'--',*command],stdin=subprocess.DEVNULL,
            pass_fds=(reader,)+(() if terminal_fd is None else (terminal_fd,))+(() if volume is None else (volume.fd,))+
                (() if ready_writer is None else (ready_writer,)))
        if ready_writer is not None:os.close(ready_writer);ready_writer=None
        if volume is not None:volume.close()
        os.close(reader)
        reader=None
        if terminal_fd is not None:os.close(terminal_fd);terminal_fd=None
        descriptor=os.pidfd_open(child.pid)
        if ready_reader is not None:
            import select
            if not select.select([ready_reader],[],[],15)[0]:
                raise TimeoutError('Namespace init did not prepare')
            data=os.read(ready_reader,128)
            if not re.fullmatch(rb'[1-9][0-9]{0,19}\n',data):
                raise ValueError('Invalid namespace init preparation')
            children=Path('/proc')/str(child.pid)/'task'/str(child.pid)/'children'
            pids=children.read_text('ascii').split()
            if len(pids)!=1:raise ValueError('Namespace launch has an unexpected init tree')
            birth=identity_module().observe(int(pids[0]))
            status=(Path('/proc')/pids[0]/'status').read_text('ascii')
            nsline=next(line for line in status.splitlines() if line.startswith('NSpid:'))
            if birth['pid_namespace']!=data.decode('ascii').strip() or nsline.split()[-1]!='1':
                raise ValueError('Namespace birth is not the prepared PID 1')
            if birth['pid_namespace']==str(Path('/proc/self/ns/pid').stat().st_ino):
                raise ValueError('Namespace init did not enter a private PID namespace')
            print(PREFIX+json.dumps({'nonce':nonce,'role':'linux_init','identity':birth},sort_keys=True),flush=True)
            control.release(nonce)
            os.write(writer,(json.dumps({'action':'release','nonce':nonce})+'\n').encode('ascii'))
        def forward(request):
            os.write(writer,(json.dumps(request)+'\n').encode('ascii'))
        return monitor(child,control,nonce,role,forward,
            lambda:signal.pidfd_send_signal(descriptor,signal.SIGKILL),margin=1,compute_monitor=compute_monitor)
    finally:
        if ready_reader is not None:os.close(ready_reader)
        if ready_writer is not None:os.close(ready_writer)
        os.close(writer)
        if reader is not None:
            os.close(reader)
        if terminal_fd is not None:os.close(terminal_fd)
        if child is not None and child.poll() is None:
            if descriptor is not None:
                signal.pidfd_send_signal(descriptor,signal.SIGKILL)
            else:
                # No poll/wait occurred before the failed pidfd acquisition, so
                # this is still our unreaped child rather than a reused PID.
                child.kill()
            child.wait(timeout=10)
        if descriptor is not None:
            os.close(descriptor)
        if volume is not None:volume.close()
        if disk is not None:disk.close()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--nonce')
    parser.add_argument('--role',choices=('host','wsl'),default='host')
    parser.add_argument('--forward-control',action='store_true')
    parser.add_argument('--cwd')
    parser.add_argument('--inner',action='store_true')
    parser.add_argument('--control-fd',type=int)
    parser.add_argument('--terminal-io',action='store_true')
    parser.add_argument('--terminal-fd',type=int)
    parser.add_argument('--volume-fd',type=int)
    parser.add_argument('--ready-fd',type=int)
    parser.add_argument('--prepare-namespace',action='store_true')
    parser.add_argument('--parent-pid',type=int)
    parser.add_argument('--shell',action='store_true')
    parser.add_argument('--native-scope')
    parser.add_argument('--linux-compute')
    parser.add_argument('--linux-compute-member')
    parser.add_argument('--linux-compute-shared')
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args()
    if args.prepare_namespace and (os.name=='nt' or args.role!='host' or args.inner or args.parent_pid is not None or args.forward_control or args.shell):
        raise ValueError('Namespace preparation is only for a native Linux host gate')
    if args.ready_fd is not None and (not args.inner or args.ready_fd<3):
        raise ValueError('Namespace readiness requires the fixed init')
    if not args.command or args.command[0]!='--' or len(args.command)<2:
        raise ValueError('A fixed payload command is required')
    arguments=args.command[1:]
    if args.native_scope is not None and (os.name=='nt' or args.role!='wsl' or args.inner
            or args.parent_pid is not None or args.forward_control or args.shell):
        raise ValueError('Only the outer native gate may enter its persisted scope')
    if args.linux_compute is not None and (not args.prepare_namespace or args.native_scope is not None):
        raise ValueError('Linux compute requires a prepared host PID namespace')
    if args.linux_compute_shared is not None and (not args.prepare_namespace or args.native_scope is not None
            or args.linux_compute is not None or args.linux_compute_member is not None):
        raise ValueError('Shared Linux compute requires a prepared host PID namespace')
    if args.linux_compute_member is not None and (os.name=='nt' or args.parent_pid is None or args.nonce is None):
        raise ValueError('Linux compute membership requires the trusted namespace supervisor')
    if args.parent_pid is not None:
        _parent_death(args.parent_pid)
        if args.linux_compute_member is not None:
            linux_compute_module().member(args.linux_compute_member,args.nonce)
        os.execvp(arguments[0],arguments)
    if not args.nonce or not re.fullmatch(r'[0-9a-f]{64}',args.nonce):
        raise ValueError('Invalid process launch nonce')
    if args.inner:
        return inner(arguments,args.control_fd,args.nonce,args.terminal_fd,args.volume_fd,args.ready_fd)
    if args.forward_control and (os.name!='nt' or args.role!='host'):
        raise ValueError('Only the Windows host gate can forward native startup control')
    if args.shell and (os.name!='nt' or args.forward_control or args.role!='host'):
        raise ValueError('Shell mode is available only for a Windows host command')
    if os.name!='nt' and os.getsid(0)!=os.getpid():
        os.setsid()
    if args.cwd is not None:
        os.chdir(args.cwd)
    compute_monitor=enter_native_scope(args.native_scope,args.nonce) if args.native_scope is not None else None
    ready={'nonce':args.nonce,'role':args.role,'identity':identity_module().observe(os.getpid())}
    print(PREFIX+json.dumps(ready,sort_keys=True),flush=True)
    if args.forward_control:
        # Do not buffer or read ahead: the next release belongs to the native
        # gate. No user code runs in this intermediate, fixed helper command.
        data=b''
        while not data.endswith(b'\n'):
            part=os.read(sys.stdin.fileno(),1)
            if not part or len(data)>16384:
                raise ValueError('Host launch owner disconnected')
            data+=part
        if json.loads(data)!={'action':'release','nonce':args.nonce}:
            raise ValueError('Invalid host release')
        child=subprocess.Popen(arguments,stdin=sys.stdin.fileno())
        return child.wait()
    terminal_fd,terminal_writer=os.pipe() if args.terminal_io else (None,None)
    control=Control(terminal_writer=terminal_writer,nonce=args.nonce)
    try:
        control.release(args.nonce)
        if args.linux_compute is not None:
            def report(value):
                print(json.dumps(value,sort_keys=True),file=sys.stderr,flush=True)
            with linux_compute_module().budget(args.linux_compute,args.nonce,report) as (admission,monitor):
                return execute(arguments,control,args.nonce,args.role,namespace=True,terminal_fd=terminal_fd,
                    prepare_namespace=True,compute_monitor=monitor,linux_admission=admission)
        if args.linux_compute_shared is not None:
            with linux_compute_module().monitor_shared(args.linux_compute_shared,args.nonce) as monitor:
                return execute(arguments,control,args.nonce,args.role,namespace=True,terminal_fd=terminal_fd,
                    prepare_namespace=True,compute_monitor=monitor,linux_admission=args.linux_compute_shared)
        return execute(arguments,control,args.nonce,args.role,namespace=os.name!='nt',shell=args.shell,
            terminal_fd=terminal_fd,prepare_namespace=args.prepare_namespace,compute_monitor=compute_monitor)
    finally:
        if compute_monitor is not None:
            compute_monitor.close()


if __name__=='__main__':
    try:
        code=main()
    except (OSError,ValueError,TimeoutError,subprocess.SubprocessError) as error:
        print('Process launch gate failed: '+type(error).__name__,file=sys.stderr,flush=True)
        code=125
    raise SystemExit(code if code>=0 else 128-code)
