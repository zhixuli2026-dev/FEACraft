"""Fixed, read-only WSL identity queries with bounded native lifetime (stdlib).

The host half only lists running distributions and invokes this file's native
half. The native half cannot execute arbitrary commands or create children.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

SCHEMA='vibeflow.control-read.v1'
FILES=('control_identity_reader.py','process_identity_native.py','terminal_identity_probe.py')
LIMIT=16384
NATIVE_SECONDS=10


def module(name):
    spec=importlib.util.spec_from_file_location('vibeflow_control_'+name,Path(__file__).with_name(name+'.py'))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


def validate(request,*,directory=None):
    if (not isinstance(request,dict) or set(request)!={'schema','nonce','distribution','kind','arguments','helpers'}
            or request['schema']!=SCHEMA or not isinstance(request['nonce'],str)
            or not re.fullmatch('[0-9a-f]{64}',request['nonce'])):
        raise ValueError('Invalid control reader request')
    distro=request['distribution']
    if not isinstance(distro,str) or not 1<=len(distro)<=128 or any(ord(c)<32 for c in distro):
        raise ValueError('Invalid read-only distribution')
    if request['kind'] not in {'identity','ancestry'}:
        raise ValueError('Unknown read-only operation')
    directory=Path(__file__).parent if directory is None else Path(directory)
    helpers=request['helpers']
    if not isinstance(helpers,dict) or set(helpers)!=set(FILES):raise ValueError('Invalid reader implementation selection')
    for name,digest in helpers.items():
        if not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):raise ValueError('Invalid helper hash')
        if hashlib.sha256((directory/name).read_bytes()).hexdigest()!=digest:
            raise ValueError('Read-only implementation changed after selection')
    identity=module('process_identity_native')
    arguments=request['arguments']
    expected={'expected'} if request['kind']=='identity' else {'root','reported'}
    if not isinstance(arguments,dict) or set(arguments)!=expected:
        raise ValueError('Invalid read-only arguments')
    for value in arguments.values():
        if identity.validate(value)['platform']!='linux':raise ValueError('Native reader requires Linux identities')
    return request


def response(request,*,reader=None,result=None,error=None):
    return {'schema':SCHEMA,'nonce':request['nonce'],'kind':request['kind'],
            'reader':reader,'result':result,'error':error}


def arm_lifetime():
    import signal
    import threading
    # Kernel default disposition still applies during Python/C work. No Python
    # handler or event-loop callback is required to enforce this deadline.
    signal.signal(signal.SIGALRM,signal.SIG_DFL);signal.alarm(NATIVE_SECONDS)
    def owner_pipe():
        try:os.read(sys.stdin.fileno(),1)
        finally:os._exit(125)  # EOF or unexpected bytes: only this reader exits.
    threading.Thread(target=owner_pipe,daemon=True).start()


def native(request):
    arm_lifetime()
    validate(request)
    identity=module('process_identity_native')
    birth=identity.observe(os.getpid())
    arguments=request['arguments']
    result=(identity.probe(arguments['expected']) if request['kind']=='identity' else
            module('terminal_identity_probe').locate(arguments['root'],arguments['reported']))
    return response(request,reader=birth,result=result)


async def bounded(stream,limit):
    data=bytearray()
    while chunk:=await stream.read(min(4096,limit+1-len(data))):
        data.extend(chunk)
        if len(data)>limit:raise ValueError('Control reader output exceeds its limit')
    return bytes(data)


async def invoke(argv,*,keep_input=False,timeout=15):
    process=await asyncio.create_subprocess_exec(*argv,stdin=asyncio.subprocess.PIPE if keep_input else subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
    readers=[asyncio.create_task(bounded(process.stdout,LIMIT)),asyncio.create_task(bounded(process.stderr,4096))]
    try:
        # communicate() closes stdin; this pipe must remain open for the native
        # reader's lifetime watcher until that reader has actually exited.
        stdout,stderr,code=await asyncio.wait_for(asyncio.gather(*readers,process.wait()),timeout)
        return code,stdout,stderr
    finally:
        if process.stdin is not None:process.stdin.close()
        if process.returncode is None:
            process.kill()  # retained creation handle, never a rediscovered PID
        await process.wait()
        for reader in readers:
            if not reader.done():reader.cancel()
        await asyncio.gather(*readers,return_exceptions=True)


async def host(request):
    validate(request)
    code,stdout,_=await invoke(['wsl.exe','--list','--running','--quiet'],timeout=10)
    if code:return response(request,error='distribution_list_unavailable')
    encoding='utf-16-le' if b'\0' in stdout else 'utf-8'
    names={line.strip().lstrip('\ufeff') for line in stdout.decode(encoding).splitlines()}
    if request['distribution'] not in names:
        return response(request,error='distribution_not_running')
    path=str(Path(__file__).resolve()).replace('\\','/')
    if len(path)<3 or path[1]!=':' or not path[0].isalpha():raise ValueError('Reader requires a Windows source path')
    path='/mnt/'+path[0].lower()+path[2:]
    encoded=base64.b64encode(json.dumps(request,separators=(',',':')).encode()).decode('ascii')
    code,stdout,_=await invoke(['wsl.exe','-d',request['distribution'],'--exec','/usr/bin/python3','-I','-S','-u',
        path,'--native',encoded],keep_input=True)
    if code:return response(request,error='native_probe_failed')
    return json.loads(stdout)


def main():
    if len(sys.argv)!=3 or sys.argv[1] not in {'--host','--native'}:raise ValueError('Fixed reader mode is required')
    raw=base64.b64decode(sys.argv[2],validate=True)
    if len(raw)>LIMIT:raise ValueError('Control read request exceeds its limit')
    request=json.loads(raw)
    if sys.argv[1]=='--native':
        if not sys.platform.startswith('linux'):raise ValueError('Native reader requires Linux')
        result=native(request)
    else:
        if os.name!='nt':raise ValueError('Host reader requires Windows')
        result=asyncio.run(host(request))
    encoded=json.dumps(result,separators=(',',':'))
    if len(encoded.encode())>LIMIT:raise ValueError('Control read response exceeds its limit')
    print(encoded,flush=True)


if __name__=='__main__':main()
