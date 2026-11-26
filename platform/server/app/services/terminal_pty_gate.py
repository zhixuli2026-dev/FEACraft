"""Wait on a private control socket while terminal input remains untouched."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys


def main():
    spec=importlib.util.spec_from_file_location('terminal_protocol',Path(__file__).with_name('terminal_protocol.py'))
    protocol=importlib.util.module_from_spec(spec);spec.loader.exec_module(protocol)
    settings=protocol.decode(sys.argv[1]);nonce=settings['nonce']
    port=int(os.environ.pop('VIBEFLOW_PTY_GATE_PORT'));key=os.environ.pop('VIBEFLOW_PTY_GATE_KEY')
    launcher_sha=settings.get('linux_launcher_sha256')
    if settings['command']==list(protocol.LINUX_SHELL) and launcher_sha is None:
        raise ValueError('Linux terminal requires its isolated launcher')
    if os.name!='nt' and launcher_sha is None:
        import fcntl,termios
        # start_new_session is requested by Popen before this fresh interpreter
        # starts; no Python preexec_fn runs after a threaded fork.
        if os.getsid(0)!=os.getpid():raise ValueError('PTY gate has no private session')
        fcntl.ioctl(0,termios.TIOCSCTTY,0)
    with socket.create_connection(('127.0.0.1',port),timeout=10) as channel:
        channel.settimeout(45)
        channel.sendall(protocol.pack({'proof':protocol.proof(key,'gate',nonce),
            'identity':protocol.identity_module().observe(os.getpid()),'profile_sha256':protocol.digest(settings)}))
        with channel.makefile('rb') as stream:
            message=protocol.read(stream)
        if message!={'action':'release','nonce':nonce}:raise ValueError('PTY gate was not released by its owner')
    if settings['cwd'] is not None:os.chdir(settings['cwd'])
    if launcher_sha is not None:
        launcher=Path(__file__).with_name('agent_shell_linux.py')
        if os.name!='posix' or hashlib.sha256(launcher.read_bytes()).hexdigest()!=launcher_sha:
            raise ValueError('Linux terminal launcher changed after release')
        env={key:os.environ[key] for key in ('PATH','LANG','LC_ALL','LC_CTYPE','TZ','TERM') if key in os.environ}
        env.setdefault('TERM','xterm-256color')
        argv=['/usr/bin/unshare','--user','--map-root-user','--mount','--pid','--fork','--net','--kill-child=SIGKILL',
              sys.executable,'-I','-S','-u',str(launcher),'--terminal']
        if settings['cwd'] is not None:argv+=['--workspace',settings['cwd']]
        if 'linux_home' in settings:argv+=['--home-selection',json.dumps(settings['linux_home'])]
        os.execve(argv[0],argv,env)
    if os.name!='nt':os.execvp(settings['command'][0],settings['command'])
    # Keep ConPTY's root alive until the actual command interpreter exits.
    # A callable handler keeps Ctrl-C available to the command interpreter.
    signal.signal(signal.SIGINT,lambda *_:None)
    child=subprocess.Popen(settings['command'])
    try:return child.wait()
    finally:
        if child.poll() is None:child.kill();child.wait(timeout=5)


if __name__=='__main__':raise SystemExit(main())
