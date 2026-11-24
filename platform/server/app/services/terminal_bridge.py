"""ConPTY/Linux PTY bridge. Runs only below a registered OwnedProcess root."""
import base64
import importlib.util
import queue
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time


def protocol_module():
    spec=importlib.util.spec_from_file_location('terminal_protocol',Path(__file__).with_name('terminal_protocol.py'))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


class Bridge:
    def __init__(self,protocol,settings):
        self.protocol=protocol;self.settings=settings;self.output_lock=threading.Lock()
        self.pty=None;self.child=None;self.master=None;self.gate=None;self.gate_listener=None;self.reader=None
        self.pidfd=None;self.closed=threading.Event()

    def send(self,message):
        with self.output_lock:
            sys.stdout.buffer.write(self.protocol.pack(message));sys.stdout.buffer.flush()

    def prepare(self):
        protocol=self.protocol;settings=self.settings;nonce=settings['nonce']
        self.gate_listener=socket.socket();self.gate_listener.bind(('127.0.0.1',0));self.gate_listener.listen(1);self.gate_listener.settimeout(15)
        key=secrets.token_hex(32)
        env=dict(os.environ,VIBEFLOW_PTY_GATE_PORT=str(self.gate_listener.getsockname()[1]),VIBEFLOW_PTY_GATE_KEY=key)
        # Windows venv python.exe can redirect to a second process. The waiting
        # gate is stdlib-only, so create its actual interpreter directly.
        argv=[getattr(sys,'_base_executable',sys.executable),'-I','-S','-u',str(Path(__file__).with_name('terminal_pty_gate.py')),protocol.encode(settings)]
        if os.name=='nt':
            from winpty import PtyProcess
            self.pty=PtyProcess.spawn(argv,dimensions=(settings['rows'],settings['cols']),cwd=settings['cwd'],env=env)
            pid=self.pty.pid
        else:
            import fcntl,pty,termios
            self.master,slave=pty.openpty()
            try:
                fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',settings['rows'],settings['cols'],0,0))
                self.child=subprocess.Popen(argv,stdin=slave,stdout=slave,stderr=slave,env=env,start_new_session=True)
                self.pidfd=os.pidfd_open(self.child.pid);pid=self.child.pid
            finally:os.close(slave)
        self.gate,_=self.gate_listener.accept();self.gate.settimeout(45);self.gate_listener.close();self.gate_listener=None
        with self.gate.makefile('rb') as stream:birth=protocol.read(stream)
        if (set(birth)!={'proof','identity','profile_sha256'} or birth['proof']!=protocol.proof(key,'gate',nonce)
                or birth['profile_sha256']!=protocol.digest(settings) or birth['identity']['pid']!=pid):
            raise ValueError('PTY birth does not belong to its waiting gate')
        protocol.identity_module().validate(birth['identity'])
        self.send({'type':'gated','nonce':nonce,'identity':birth['identity'],'profile_sha256':birth['profile_sha256']})

    def alive(self):return self.pty.isalive() if self.pty is not None else self.child is not None and self.child.poll() is None

    def output(self):
        try:
            while not self.closed.is_set():
                data=self.pty.read(4096).encode('utf-8') if self.pty is not None else os.read(self.master,4096)
                if not data:break
                self.send({'type':'output','data':base64.b64encode(data).decode('ascii')})
        except (OSError,EOFError):pass
        finally:self.closed.set()

    def run(self,stream):
        protocol=self.protocol;nonce=self.settings['nonce']
        if protocol.read(stream)!={'action':'release','nonce':nonce}:raise ValueError('Terminal was not durably released')
        self.gate.sendall(protocol.pack({'action':'release','nonce':nonce}));self.gate.close();self.gate=None
        self.send({'type':'released','nonce':nonce})
        self.reader=threading.Thread(target=self.output,daemon=True,name='terminal-output');self.reader.start()
        incoming=queue.Queue(maxsize=16);input_closed=threading.Event()
        def read_input():
            try:
                while True:incoming.put_nowait(protocol.read(stream))
            except (OSError,EOFError,ValueError,queue.Full):pass
            finally:input_closed.set()
        threading.Thread(target=read_input,daemon=True,name='terminal-input').start()
        while self.alive() and not self.closed.is_set():
            try:message=incoming.get(timeout=.2)
            except queue.Empty:
                if input_closed.is_set():break
                continue
            action=message.get('action') if isinstance(message,dict) else None
            if action=='input' and set(message)=={'action','data','id'}:
                if type(message['id']) is not int or message['id']<=0:raise ValueError('Invalid terminal input sequence')
                value=base64.b64decode(message['data'],validate=True)
                if len(value)>protocol.MAX_INPUT:raise ValueError('Terminal input exceeds its limit')
                if self.pty is not None:self.pty.write(value.decode('utf-8','strict'))
                else:
                    view=memoryview(value)
                    while view:
                        count=os.write(self.master,view);view=view[count:]
                self.send({'type':'input_ack','id':message['id']})
            elif action=='resize' and set(message)=={'action','cols','rows','id'}:
                if type(message['id']) is not int or message['id']<=0:raise ValueError('Invalid terminal resize sequence')
                cols,rows=message['cols'],message['rows']
                if any(type(value) is not int or not 1<=value<=1000 for value in (cols,rows)):raise ValueError('Invalid terminal dimensions')
                if self.pty is not None:self.pty.setwinsize(rows,cols)
                else:
                    import fcntl,termios
                    fcntl.ioctl(self.master,termios.TIOCSWINSZ,struct.pack('HHHH',rows,cols,0,0))
                self.send({'type':'resize_ack','id':message['id']})
            else:raise ValueError('Unsupported terminal operation')
        if self.closed.is_set():
            deadline=time.monotonic()+1
            while self.alive() and time.monotonic()<deadline:time.sleep(.01)
        if self.alive():return
        code=self.pty.exitstatus if self.pty is not None else self.child.returncode
        # A ConPTY library reader can outlive its root. The API's owned scope
        # independently waits for every member; EOF is never the exit proof.
        if self.reader is not None:self.reader.join(timeout=1)
        self.send({'type':'exit','code':code if type(code) is int else -1})

    def close(self):
        self.closed.set()
        for value in (self.gate,self.gate_listener):
            if value is not None:value.close()
        if self.child is not None:
            if self.child.poll() is None:
                import signal
                # Retained pidfd, opened before any reap, never a PID signal.
                if self.pidfd is not None:signal.pidfd_send_signal(self.pidfd,signal.SIGKILL)
                else:self.child.kill()
            self.child.wait(timeout=5)
        if self.master is not None:os.close(self.master);self.master=None
        if self.pidfd is not None:os.close(self.pidfd);self.pidfd=None
        # Windows closure is owned by the API's retained Job object. Do not call
        # pywinpty.terminate(), which resolves and signals a bare PID.
        if self.pty is not None:
            self.pty.fileobj.close();self.pty._server.close()
            try:self.pty.pty.cancel_io()
            except Exception:pass
            self.pty.closed=True


def main():
    protocol=protocol_module();settings=protocol.decode(sys.argv[1])
    bridge=Bridge(protocol,settings)
    try:
        bridge.prepare();bridge.run(sys.stdin.buffer)
    finally:bridge.close()


if __name__=='__main__':main()
