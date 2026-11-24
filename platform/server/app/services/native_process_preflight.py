"""Read-only native birth handshake before admission to the business gate.

No child, cgroup join or business command runs before the birth-bound permit.
The checked gate bytes then run in this same interpreter and OS process.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys

FILES = ('native_process_preflight.py', 'process_gate.py', 'process_identity_native.py', 'process_cgroup_native.py')
LIMIT = 16384
SCHEMA = 'vibeflow.native-preflight.v1'


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def permit(nonce,birth):
    return {'action':'admit_native','nonce':nonce,'birth_sha256':digest(birth)}


def main():
    if (not sys.platform.startswith('linux') or len(sys.argv)<6 or sys.argv[3]!='--'
            or re.fullmatch('[0-9a-f]{64}',sys.argv[1]) is None or len(sys.argv[2])>4096):
        raise ValueError('Fixed native preflight arguments are required')
    nonce=sys.argv[1]
    helpers=json.loads(base64.b64decode(sys.argv[2],validate=True))
    if not isinstance(helpers,dict) or set(helpers)!=set(FILES):
        raise ValueError('Invalid native preflight helper selection')
    sources={}
    for name,expected in helpers.items():
        raw=Path(__file__).with_name(name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=expected:
            raise ValueError('Native preflight implementation changed')
        sources[name]=raw
    arguments=sys.argv[4:]
    if (arguments[:4]!=['--nonce',nonce,'--role','wsl'] or '--native-scope' not in arguments
            or '--' not in arguments or any(item in arguments[:arguments.index('--')]
                                           for item in ('--inner','--parent-pid','--forward-control','--shell'))):
        raise ValueError('Native preflight requires the fixed scoped outer gate')
    if os.getsid(0)!=os.getpid():os.setsid()
    signal.signal(signal.SIGALRM,signal.SIG_DFL)
    signal.alarm(45)
    identities={'__name__':'vibeflow_preflight_identity','__file__':str(Path(__file__).with_name('process_identity_native.py'))}
    exec(compile(sources['process_identity_native.py'],identities['__file__'],'exec'),identities)
    birth=identities['observe'](os.getpid())
    print('VIBEFLOW_PROCESS_GATE_V1 '+json.dumps({'nonce':nonce,'role':'wsl_prepared','identity':birth}),flush=True)
    # Read one line without read-ahead; final business release remains on stdin.
    raw=bytearray()
    while not raw.endswith(b'\n'):
        part=os.read(sys.stdin.fileno(),1)
        if not part or len(raw)>=LIMIT:
            raise ValueError('Native admission owner disconnected or exceeded its bound')
        raw.extend(part)
    if json.loads(raw)!=permit(nonce,birth):
        raise ValueError('Native admission does not match the observed birth')
    if identities['observe'](os.getpid())!=birth:
        raise ValueError('Native preflight birth changed before handoff')
    for name,expected in helpers.items():
        if hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()!=expected:
            raise ValueError('Native implementation changed after preparation')
    signal.alarm(330)
    gate=Path(__file__).with_name('process_gate.py')
    sys.argv=[str(gate),*arguments]
    exec(compile(sources['process_gate.py'],str(gate),'exec'),{'__name__':'__main__','__file__':str(gate)})


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,TimeoutError) as error:
        print('Native preflight failed: '+type(error).__name__,file=sys.stderr,flush=True)
        raise SystemExit(125)
