"""Fixed root-management to nobody stdio handoff; never execute user code as root."""
import base64,ctypes,errno,hashlib,json,os,sys,types
from pathlib import Path

SCHEMA='vibeflow.cli-native-handoff.v1'
FILES=('cli_native_handoff.py','stdio_bridge.py','process_identity_native.py','codex_wsl_state.py','process_cgroup_native.py')
HERE=Path(__file__).resolve().parent


def helpers():return {name:hashlib.sha256((HERE/name).read_bytes()).hexdigest() for name in FILES}


def selection(scope):
    from .process_cgroup_native import validate
    return {'schema':SCHEMA,'scope':dict(validate(scope)),'helpers':helpers()}


def decode(encoded,nonce=None):
    if not isinstance(encoded,str) or len(encoded)>24576:raise ValueError('Oversized managed CLI profile')
    value=json.loads(base64.b64decode(encoded,validate=True))
    if (not isinstance(value,dict) or set(value)!={'argv','cwd','nonce','native_state','native_management'}
            or nonce is not None and value['nonce']!=nonce):raise ValueError('Invalid managed CLI profile')
    managed=value['native_management']
    if (not isinstance(managed,dict) or set(managed)!={'schema','scope','helpers'} or managed['schema']!=SCHEMA
            or not isinstance(managed['helpers'],dict) or set(managed['helpers'])!=set(FILES)):
        raise ValueError('Invalid managed CLI selection')
    sources={}
    for name in FILES:
        raw=(HERE/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=managed['helpers'][name]:raise ValueError('Managed CLI implementation changed')
        sources[name]=raw
    scope=types.ModuleType('fixed_cli_cgroup');scope.__file__=str(HERE/'process_cgroup_native.py')
    exec(compile(sources['process_cgroup_native.py'],scope.__file__,'exec'),scope.__dict__)
    scope.validate(managed['scope'])
    if managed['scope']['nonce']!=value['nonce']:raise ValueError('Managed CLI scope belongs to another launch')
    return value,scope


def prepare(arguments,nonce):
    if (os.getuid()!=0 or os.geteuid()!=0 or len(arguments)!=6
            or arguments[:4]!=['/usr/bin/python3','-I','-S','-u']
            or Path(arguments[4])!=Path(__file__).resolve()):
        raise ValueError('Managed CLI requires its fixed root entrypoint')
    value,scope=decode(arguments[5],nonce)
    selected=value['native_management']['scope']
    if (scope.observe(selected)['state']!='populated'
            or '0::'+scope.membership_path(selected) not in Path('/proc/self/cgroup').read_text().splitlines()):
        raise ValueError('Managed CLI did not inherit its selected cgroup')
    return value


def drop_privileges():
    if os.getresuid()!=(0,0,0):raise ValueError('CLI management must begin with its fixed root identity')
    library=ctypes.CDLL(None,use_errno=True)
    def prctl(option,arg):
        if library.prctl(option,arg,0,0,0)!=0:raise OSError(ctypes.get_errno(),'Unable to restrict CLI privileges')
    prctl(38,1) # PR_SET_NO_NEW_PRIVS
    prctl(8,0) # PR_SET_KEEPCAPS
    prctl(47,4) # PR_CAP_AMBIENT_CLEAR_ALL
    for capability in range(64):
        if library.prctl(24,capability,0,0,0)!=0 and ctypes.get_errno()!=errno.EINVAL:
            raise OSError(ctypes.get_errno(),'Unable to clear CLI capability bounding set')
    os.setgroups([]);os.setresgid(65534,65534,65534);os.setresuid(65534,65534,65534)
    status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    if (os.getresuid()!=(65534,)*3 or os.getresgid()!=(65534,)*3 or os.getgroups()
            or status['NoNewPrivs'].strip()!='1'
            or any(int(status[key].strip(),16) for key in ('CapInh','CapPrm','CapEff','CapBnd','CapAmb'))):
        raise ValueError('Managed CLI retained privileged identity or capabilities')


def main():
    if len(sys.argv)!=2:raise ValueError('Managed CLI accepts one frozen profile')
    prepare(['/usr/bin/python3','-I','-S','-u',str(Path(__file__).resolve()),sys.argv[1]],None)
    drop_privileges()
    # All imports/initialization and user-selected CLI execution occur only
    # after the irreversible drop. The bridge retains its original release gates.
    os.execv('/usr/bin/python3',['/usr/bin/python3','-I','-S','-u',str(HERE/'stdio_bridge.py'),sys.argv[1],'--wsl-environment'])


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,TypeError,KeyError):raise SystemExit(125)
