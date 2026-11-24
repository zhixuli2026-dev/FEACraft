"""Bounded synchronous client for one attempt's private native executor."""
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
import os
import re
import socket
import subprocess
import sys
import time

ADDRESS='VIBEFLOW_NATIVE_ADDRESS'
CAPABILITY='VIBEFLOW_NATIVE_CAPABILITY'
CURRENT=ContextVar('owned_native_executor',default=None)
REQUIRED=ContextVar('owned_native_executor_required',default=False)


class ChildExecutionError(OSError):
    pass


@contextmanager
def context(address,capability):
    token=CURRENT.set((address,capability))
    try:yield
    finally:CURRENT.reset(token)


@contextmanager
def require_supervision():
    token=REQUIRED.set(True)
    try:yield
    finally:REQUIRED.reset(token)


def latex_probe_timeout(distro):
    # WSL includes durable scope allocation, admission, and verified teardown.
    return 45 if distro=='native-linux' else 90


def run_latex(purpose,distro,*,recipe=None,config=None,capture=False):
    parameters={'distro':distro,'recipe':recipe} if purpose=='latex_probe' else {'distro':distro,'config':str(config)}
    timeout=latex_probe_timeout(distro) if purpose=='latex_probe' else 180
    return run_native(purpose+('_linux' if distro=='native-linux' else ''),parameters,timeout=timeout,capture=capture)


def supervised():
    return CURRENT.get() is not None or REQUIRED.get()


def channel():
    """Require an explicitly selected owner; environment alone is not consent."""
    connection=CURRENT.get()
    if connection is None and REQUIRED.get():
        connection=(os.environ.get(ADDRESS,''),os.environ.get(CAPABILITY,''))
    if connection is None:
        raise ChildExecutionError('An owned native execution channel is required')
    address,capability=connection
    match=re.fullmatch(r'127\.0\.0\.1:([0-9]{1,5})',address)
    if match is None or not 0<int(match[1])<65536 or not re.fullmatch('[0-9a-f]{64}',capability):
        raise ChildExecutionError('An owned native execution channel is required')
    return address,capability


def run_native(purpose,parameters,*,timeout,capture=False,sink=None):
    if isinstance(timeout,bool) or not isinstance(timeout,(int,float)) or not math.isfinite(timeout) or timeout<=0:
        raise ValueError('Native timeout must be a finite positive duration')
    deadline=time.monotonic()+timeout
    from .request_deadline import current as request_deadline
    request_budget=request_deadline()
    if request_budget is not None:deadline=min(deadline,request_budget.deadline)
    def remaining():
        value=deadline-time.monotonic()
        if value<=0:raise subprocess.TimeoutExpired(purpose,timeout)
        return value
    address,capability=channel()
    port=int(address.rsplit(':',1)[1])
    request={'schema':'vibeflow.native-call.v1','capability':capability,'purpose':purpose,'parameters':parameters,
        'deadline':deadline}
    encoded=(json.dumps(request,ensure_ascii=False)+'\n').encode('utf-8')
    if len(encoded)>65536:raise ChildExecutionError('Native request exceeded 64 KiB')
    collected=bytearray()
    try:
        with socket.create_connection(('127.0.0.1',port),timeout=min(10,remaining())) as sock:
            sock.settimeout(remaining());sock.sendall(encoded)
            pending=bytearray()
            while True:
                remaining()
                # Each actual recv uses the remaining total budget. A buffered
                # readline could otherwise renew its timeout for every byte.
                while b'\n' not in pending:
                    if len(pending)>=65536:raise ChildExecutionError('Native execution frame exceeded its bound')
                    sock.settimeout(remaining())
                    data=sock.recv(min(4096,65537-len(pending)))
                    if not data:raise ChildExecutionError('Native execution channel closed without a result')
                    pending.extend(data)
                line,_,rest=pending.partition(b'\n');pending=bytearray(rest)
                if len(line)+1>65536:raise ChildExecutionError('Native execution frame exceeded its bound')
                remaining()
                message=json.loads(line)
                if message.get('type')=='output' and set(message)=={'type','data'}:
                    data=base64.b64decode(message['data'],validate=True)
                    if sink is not None:sink(data)
                    if capture:
                        collected.extend(data)
                        if len(collected)>1024*1024:raise ChildExecutionError('Native probe output exceeded its limit')
                    else:
                        if hasattr(sys.stdout,'buffer'):sys.stdout.buffer.write(data)
                        else:sys.stdout.write(data.decode('utf-8','replace'))
                        sys.stdout.flush()
                elif message.get('type')=='done' and set(message)=={'type','returncode','child_id'}:
                    if type(message['returncode']) is not int:raise ChildExecutionError('Invalid native exit status')
                    return subprocess.CompletedProcess(purpose,message['returncode'],bytes(collected) if capture else None,b'')
                elif message.get('type')=='error':
                    if message.get('error')=='TimeoutError':raise subprocess.TimeoutExpired(purpose,timeout)
                    raise ChildExecutionError('Owned native execution failed: '+str(message.get('error','unknown')))
                else:
                    raise ChildExecutionError('Invalid native execution response')
    except TimeoutError:
        raise subprocess.TimeoutExpired(purpose,timeout) from None
