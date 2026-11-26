"""Compute limits inside one Linux worker's private cgroup v1 namespace."""

from contextlib import contextmanager, ExitStack
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import time


SCHEMA = 'vibeflow.linux-compute-usage.v1'
CONTROL = 'attempt.linux_compute_usage'


def _load_compute(expected=None):
    path = Path(__file__).with_name('native_compute_v1.py')
    source = path.read_bytes()
    if expected is not None and hashlib.sha256(source).hexdigest() != expected:
        raise ValueError('The Linux compute controller helper changed before launch')
    namespace = {'__name__':'vibeflow_linux_compute_v1','__file__':str(path)}
    exec(compile(source,str(path),'exec'),namespace)
    return namespace


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def selection(policy):
    path = Path(__file__)
    return encode({'policy':policy,'helper_sha256':_sha(path),
                   'compute_sha256':_sha(path.with_name('native_compute_v1.py'))})


def encode(value):
    return base64.b64encode(json.dumps(value,sort_keys=True).encode('ascii')).decode('ascii')


def selected(encoded):
    if len(encoded) > 4096:
        raise ValueError('Linux compute selection exceeds its bound')
    value = json.loads(base64.b64decode(encoded,validate=True))
    if set(value) != {'policy','helper_sha256','compute_sha256'} or value['helper_sha256'] != _sha(Path(__file__)):
        raise ValueError('Invalid Linux compute selection')
    api = _load_compute(value['compute_sha256'])
    policy = api['validate_policy'](value['policy'])
    if not any(policy.values()):
        raise ValueError('A Linux compute selection requires a resource limit')
    membership = {controller:path for row in Path('/proc/self/cgroup').read_text('ascii').splitlines()
                  for _,controllers,path in (row.split(':',2),) for controller in controllers.split(',')}
    if any(membership.get(controller) != '/' for controller in api['_controllers'](policy)):
        raise ValueError('Linux worker compute requires its private controller namespace root')
    for row in Path('/proc/self/mountinfo').read_text('ascii').splitlines():
        before,after = row.split(' - ',1)
        if after.split()[0] in {'cgroup','cgroup2'} and 'ro' not in before.split()[5].split(','):
            raise ValueError('The Linux worker must keep pre-existing controller mounts read-only')
    return value,api


def check_available(policy):
    """Reject an unavailable controller before reserving an interactive session."""
    status = Path('/proc/self/status').read_text(encoding='ascii').splitlines()
    effective = next((int(line.split()[1], 16) for line in status if line.startswith('CapEff:')), 0)
    if os.geteuid() != 0 or not effective & (1 << 21):  # CAP_SYS_ADMIN for the private controller mounts.
        raise OSError('Linux compute requires the trusted root mount capability')
    selected(selection(policy))


def _membership(encoded):
    if len(encoded) > 4096:
        raise ValueError('Linux compute membership exceeds its bound')
    value = json.loads(base64.b64decode(encoded,validate=True))
    if set(value) != {'selected','scope','launch_nonce'}:
        raise ValueError('Invalid Linux compute membership')
    selection = value['selected']
    original,api = selected(encode(selection))
    scope = api['validate'](value['scope'])
    if scope['policy'] != original['policy']:
        raise ValueError('Linux compute membership belongs to a different launch')
    return value, api


def member(encoded, nonce):
    value,api = _membership(encoded)
    if value['launch_nonce'] != nonce:
        raise ValueError('Linux compute membership belongs to a different launch')
    scope = value['scope']
    api['join'](scope)


@contextmanager
def _monitor(api,scope):
    files = {}
    with ExitStack() as stack:
        with api['_bound'](scope,retain_files=True) as (_,_,groups):
            for role,name in (('cpuacct','cpuacct.usage'),('memory','memory.oom_control')):
                if role in groups:
                    fd = os.open(name,os.O_RDONLY|api['_FILE_FLAGS'],dir_fd=groups[role])
                    stack.callback(os.close,fd)
                    files[role] = fd
        yield api['RuntimeMonitor'](files,scope['policy'])


def violation(policy, observation):
    if policy['memory_limit_bytes'] and observation.get('memory_oom_kills',0) > 0:
        return 'memory_limit'
    if policy['cpu_limit_ns'] and observation.get('cpu_used_ns',0) >= policy['cpu_limit_ns']:
        return 'cpu_limit'
    return None


@contextmanager
def budget(encoded,nonce,report,*,drain_timeout=3):
    original,api = selected(encoded)
    scope = api['allocate'](nonce,original['policy'])
    try:
        with _monitor(api,scope) as handle:
            yield admission(original,scope,nonce),handle
    finally:
        # The namespace init may be reaped a tick after the wait on its parent.
        deadline = time.monotonic()+drain_timeout
        while True:
            try:
                usage = api['retire'](scope)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise
            except ValueError as error:
                if str(error) != 'Native compute scopes still contain processes' or time.monotonic() >= deadline:
                    raise
            time.sleep(.02)
        report(receipt(api,scope,usage))


def admission(original,scope,nonce):
    return encode({'scope':scope,'selected':original,'launch_nonce':nonce})


@contextmanager
def monitor_shared(encoded,nonce):
    value,api = _membership(encoded)
    if value['launch_nonce'] != nonce:
        raise ValueError('Shared compute membership belongs to a different launch')
    with _monitor(api,value['scope']) as handle:
        yield handle


def receipt(api,scope,usage):
    api['validate_usage'](scope,usage)
    return {'schema':SCHEMA,'launch_nonce':scope['nonce'],'scope':scope,'usage':usage,
            'violation':violation(scope['policy'],usage)}


class SharedCompute:
    """Keep the accounting group alive across the root and registered tool scopes."""

    def __init__(self,nonce,policy):
        self.nonce = nonce
        self.policy = policy
        self.encoded = selection(policy)
        self.process = None
        self.scope = None
        self.result = None

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,'-I','-S','-u',str(Path(__file__).resolve()),self.encoded,self.nonce,
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        scope = json.loads(await asyncio.wait_for(self.process.stdout.readline(),10))
        from .native_compute_v1 import validate
        if validate(scope)['nonce'] != self.nonce or scope['policy'] != self.policy:
            raise ValueError('Shared compute scope changed its launch policy')
        self.scope = scope

    def bind(self,process):
        if self.scope is None or self.process.returncode is not None or self.process.stdin.is_closing():
            raise ValueError('Shared Linux compute scope is unavailable')
        original = json.loads(base64.b64decode(self.encoded,validate=True))
        process.linux_compute_admission = admission(original,self.scope,process.nonce)

    async def close(self):
        if self.process is None or self.result is not None:
            return self.result
        self.process.stdin.close()
        raw = await asyncio.wait_for(self.process.stdout.readline(),47)
        code = await asyncio.wait_for(self.process.wait(),5)
        if self.scope is None:
            return None
        if code != 0:
            raise ValueError('Shared Linux compute scope did not retire')
        result = validate_report(json.loads(raw),self.nonce,self.policy)
        if result['scope'] != self.scope:
            raise ValueError('Shared Linux compute scope identity changed during retirement')
        self.result = result
        return result


def validate_report(value, nonce, policy):
    from .native_compute_v1 import validate, validate_usage
    if (not isinstance(value,dict) or set(value) != {'schema','launch_nonce','scope','usage','violation'}
            or value['schema'] != SCHEMA or value['launch_nonce'] != nonce):
        raise ValueError('Linux compute receipt changed its launch')
    scope = validate(value['scope'])
    if scope['nonce'] != nonce or scope['policy'] != policy:
        raise ValueError('Linux compute receipt changed its frozen limits')
    usage = validate_usage(scope,value['usage'])
    if value['violation'] != violation(policy,usage):
        raise ValueError('Linux compute receipt changed its quota outcome')
    return value


if __name__ == '__main__':
    encoded,nonce = sys.argv[1:]
    with budget(encoded,nonce,lambda value: print(json.dumps(value),flush=True),drain_timeout=45) as (encoded_admission,_):
        descriptor = json.loads(base64.b64decode(encoded_admission,validate=True))['scope']
        print(json.dumps(descriptor),flush=True)
        # The owner holds the only write end. EOF also retires the group after
        # owner loss, once the existing launch pipes have drained its members.
        if sys.stdin.buffer.read(1):
            raise ValueError('Invalid shared compute control')
