"""Fixed runtime probes; runs in an owned host or WSL process with stdlib only."""
import argparse
import base64
import importlib
import json
from pathlib import PurePosixPath
import subprocess
import sys
import threading

PACKAGES=('vtk','trame','wslink','cadquery','numpy','matplotlib','meshio','fastapi','sqlalchemy','httpx')
FRAME='VIBEFLOW_RUNTIME_PROBE_V1 '
SCRIPT='''bashrc=$1
shift
loaded=0
foam= wmake= foam_etc= control_dict= version=
if [ -r "$bashrc" ] && source "$bashrc" >/dev/null 2>&1 &&
   selected=$(readlink -f "$bashrc") && [[ "$selected" == */etc/bashrc ]]; then
    loaded=1
    installation=${selected%/etc/bashrc}
    configured_tool() {
        local found
        found=$(command -v "$1") && found=$(readlink -f "$found") && [ -x "$found" ] || return
        case "$found" in "$installation"/*) printf '%s' "$found";; esac
    }
    foam=$(configured_tool blockMesh || true)
    wmake=$(configured_tool wmake || true)
    foam_etc=$(configured_tool foamEtcFile || true)
    version=${WM_PROJECT_VERSION:-}
    if [ -n "$foam_etc" ] && resolved=$("$foam_etc" -etc="$installation/etc" -mode=o controlDict 2>/dev/null) &&
       [ -r "$resolved" ]; then
        control_dict=$(readlink -f "$resolved")
    fi
fi
printf 'loaded=%s\\n' "$loaded"
printf 'foam=%s\\n' "$foam"
printf 'wmake=%s\\n' "$wmake"
printf 'foam_etc=%s\\n' "$foam_etc"
printf 'foam_etc_control_dict=%s\\n' "$control_dict"
printf 'foam_version=%s\\n' "$version"
printf 'latexmk=%s\\n' "$(command -v latexmk || true)"
printf 'gmsh=%s\\n' "$(command -v gmsh || true)"
'''


def validate(value):
    if not isinstance(value,dict):raise ValueError('Invalid runtime probe settings')
    if value.get('kind')=='python':
        if set(value)!={'kind','packages'} or value['packages']!=list(PACKAGES):
            raise ValueError('Runtime Python probe cannot import arbitrary packages')
    elif value.get('kind')=='wsl':
        if set(value)!={'kind','bashrc'}:raise ValueError('Invalid WSL runtime probe settings')
        path=value['bashrc']
        if (not isinstance(path,str) or len(path)>500 or any(ord(char)<32 for char in path)
                or not path.startswith('/usr/lib/openfoam/') or not path.endswith('/etc/bashrc')
                or '..' in PurePosixPath(path).parts):
            raise ValueError('WSL runtime probe requires its configured OpenFOAM bashrc')
    else:raise ValueError('Unknown runtime probe kind')
    return value


def python_probe():
    packages={}
    for name in PACKAGES:
        try:
            module=importlib.import_module(name)
            value=getattr(module,'__version__','ok') or 'ok'
            packages[name]=str(value)[:128]
        except Exception:packages[name]=None
    return {'schema':'vibeflow.runtime-python.v1','version':sys.version.split()[0],'packages':packages}


def wsl_probe(settings):
    process=subprocess.Popen(['bash','--noprofile','--norc','-c',SCRIPT,'vibeflow-runtime-probe',settings['bashrc']],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    output={};overflow=threading.Event();errors=[]
    def terminate():
        try:process.kill()
        except ProcessLookupError:pass
    def read(stream,key):
        data=bytearray()
        try:
            while True:
                chunk=stream.read(4096)
                if not chunk:break
                if len(data)+len(chunk)>64*1024:overflow.set();terminate();break
                data.extend(chunk)
        except Exception as error:errors.append(type(error).__name__);terminate()
        finally:stream.close();output[key]=bytes(data)
    readers=[threading.Thread(target=read,args=(process.stdout,'stdout'),daemon=True),
             threading.Thread(target=read,args=(process.stderr,'stderr'),daemon=True)]
    for reader in readers:reader.start()
    expired=False
    try:
        try:process.wait(timeout=15)
        except subprocess.TimeoutExpired:expired=True
    finally:
        if process.poll() is None:terminate()
        process.wait(timeout=5)
        for reader in readers:reader.join(timeout=5)
    if expired:raise TimeoutError('Runtime WSL tools exceeded their deadline')
    if overflow.is_set() or errors or any(reader.is_alive() for reader in readers):
        raise ValueError('Runtime WSL output exceeded its bounds')
    if process.returncode:raise RuntimeError('Runtime WSL shell did not finish')
    values=dict(line.split('=',1) for line in output['stdout'].decode('utf-8','replace').splitlines() if '=' in line)
    loaded=values.get('loaded')=='1'
    return {'schema':'vibeflow.runtime-wsl.v1','reachable':True,'openfoam':bool(values.get('foam')),
        'openfoam_path':values.get('foam') or None,'openfoam_version':values.get('foam_version') or None,
        'openfoam_bashrc_loaded':loaded,'wmake':bool(values.get('wmake')),'wmake_path':values.get('wmake') or None,
        'foam_etc_file':bool(values.get('foam_etc_control_dict')),'foam_etc_file_path':values.get('foam_etc') or None,
        'foam_etc_control_dict':values.get('foam_etc_control_dict') or None,
        'latexmk':bool(values.get('latexmk')),'gmsh':bool(values.get('gmsh'))}


def execute(encoded):
    raw=base64.b64decode(encoded,validate=True)
    if len(raw)>4096:raise ValueError('Runtime probe settings exceed their limit')
    settings=validate(json.loads(raw))
    result=python_probe() if settings['kind']=='python' else wsl_probe(settings)
    # A leading newline separates C-extension diagnostics from the sole frame.
    print('\n'+FRAME+json.dumps(result,sort_keys=True,ensure_ascii=True),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--settings',required=True);args=parser.parse_args()
    execute(args.settings)
