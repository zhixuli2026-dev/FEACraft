"""Fixed read-only SyncTeX edit profile; standard-library WSL helper."""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path,PurePosixPath
import subprocess
import threading

SCHEMA='vibeflow.synctex-call.v1'
MAX_PDF=128*1024*1024
MAX_SIDECAR=16*1024*1024
STDOUT_LIMIT=128*1024
STDERR_LIMIT=64*1024
FIELDS={'schema','pdf','pdf_sha256','synctex_sha256','page','x','y'}


def command(settings):
    if not isinstance(settings,dict) or set(settings)!=FIELDS or settings['schema']!=SCHEMA:
        raise ValueError('Invalid SyncTeX profile')
    if not isinstance(settings['pdf'],str) or not PurePosixPath(settings['pdf']).is_absolute() or not settings['pdf'].endswith('.pdf'):
        raise ValueError('SyncTeX requires an absolute owned PDF')
    if type(settings['page']) is not int or not 1<=settings['page']<=10000:
        raise ValueError('Invalid SyncTeX page')
    for key in ('x','y'):
        if type(settings[key]) not in {int,float} or not math.isfinite(settings[key]) or not 0<settings[key]<100000:
            raise ValueError('Invalid SyncTeX coordinate')
    for key in ('pdf_sha256','synctex_sha256'):
        value=settings[key]
        if not isinstance(value,str) or len(value)!=64 or any(c not in '0123456789abcdef' for c in value):
            raise ValueError('Invalid SyncTeX frozen digest')
    return ['synctex','edit','-o',f"{settings['page']}:{settings['x']:.3f}:{settings['y']:.3f}:{settings['pdf']}"]


def execute(config,expected_sha256):
    raw=config.read_bytes()
    if len(raw)>16384 or hashlib.sha256(raw).hexdigest()!=expected_sha256:
        raise ValueError('SyncTeX configuration changed before release')
    settings=json.loads(raw);argv=command(settings)
    if {path.name for path in config.parent.iterdir()}!={'snapshot.pdf','snapshot.synctex.gz','settings.json'}:
        raise ValueError('SyncTeX snapshot contains an unselected sidecar')
    pdf=Path(settings['pdf']);sidecar=pdf.with_suffix('.synctex.gz')
    for path,limit,key in ((pdf,MAX_PDF,'pdf_sha256'),(sidecar,MAX_SIDECAR,'synctex_sha256')):
        if path.parent!=config.parent or path.is_symlink() or path.stat().st_size>limit:
            raise ValueError('SyncTeX source escaped its owned snapshot')
        if hashlib.sha256(path.read_bytes()).hexdigest()!=settings[key]:
            raise ValueError('SyncTeX input changed before release')
    # SyncTeX otherwise executes SYNCTEX_EDITOR as a command. This profile only
    # returns a location; all SyncTeX environment hints are deliberately absent.
    env={key:value for key,value in os.environ.items() if not key.upper().startswith('SYNCTEX_')}
    process=subprocess.Popen(argv,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    output={};errors=[];overflow=threading.Event()
    def terminate():
        try:process.kill()
        except ProcessLookupError:pass
    def read(stream,name,limit):
        data=bytearray()
        try:
            while True:
                chunk=stream.read(4096)
                if not chunk:break
                if len(data)+len(chunk)>limit:overflow.set();terminate();break
                data.extend(chunk)
        except BaseException as error:errors.append(type(error).__name__);terminate()
        finally:stream.close();output[name]=bytes(data)
    threads=[threading.Thread(target=read,args=(process.stdout,'stdout',STDOUT_LIMIT),daemon=True),
             threading.Thread(target=read,args=(process.stderr,'stderr',STDERR_LIMIT),daemon=True)]
    for thread in threads:thread.start()
    timed_out=False
    try:
        try:process.wait(timeout=10)
        except subprocess.TimeoutExpired:timed_out=True
    finally:
        if process.poll() is None:terminate()
        process.wait(timeout=5)
        for thread in threads:thread.join(timeout=5)
    if errors or overflow.is_set() or any(thread.is_alive() for thread in threads):
        raise ValueError('SyncTeX streams exceeded their bounds')
    print(json.dumps({'schema':'vibeflow.synctex-result.v1','returncode':process.returncode,'timed_out':timed_out,
        **{key:base64.b64encode(value).decode('ascii') for key,value in output.items()}}),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--sha256',required=True);args=parser.parse_args()
    execute(args.config,args.sha256)
