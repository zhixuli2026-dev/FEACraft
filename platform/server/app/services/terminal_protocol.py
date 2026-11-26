"""Stdlib protocol shared by the owned terminal bridge and its waiting PTY gate."""
import base64
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path,PurePosixPath,PureWindowsPath
import re

PREFIX='VIBEFLOW_TERMINAL_BRIDGE_V1 '
MAX_LINE=128*1024
MAX_INPUT=16*1024
LINUX_SHELL=('/bin/bash','-l')


def identity_module():
    spec=importlib.util.spec_from_file_location('terminal_os_identity',Path(__file__).with_name('process_identity_native.py'))
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    return value


def proof(key,role,nonce):return hmac.new(bytes.fromhex(key),(role+':'+nonce).encode('ascii'),hashlib.sha256).hexdigest()


def pack(value):
    data=json.dumps(value,ensure_ascii=True,sort_keys=True,separators=(',',':')).encode('ascii')+b'\n'
    if len(data)>MAX_LINE:raise ValueError('Terminal frame exceeds its limit')
    return data


def read(stream):
    line=stream.readline(MAX_LINE+1)
    if not line or len(line)>MAX_LINE or not line.endswith(b'\n'):raise EOFError('Terminal transport ended')
    value=json.loads(line)
    if not isinstance(value,dict):raise ValueError('Invalid terminal frame')
    return value


def validate_linux_home(value, user_id=None):
    if (not isinstance(value, dict) or set(value) != {'path', 'device', 'inode'}
            or not isinstance(value['path'], str) or '\0' in value['path'] or len(value['path']) > 4096
            or type(value['device']) is not int or value['device'] < 0
            or type(value['inode']) is not int or value['inode'] <= 0):
        raise ValueError('Invalid Linux shell home selection')
    path = PurePosixPath(value['path'])
    if (path.root != '/' or str(path) != value['path'] or '..' in path.parts or len(path.parts) < 4
            or path.parent.name != 'users' or not re.fullmatch('[1-9][0-9]*', path.name)
            or (user_id is not None and path.name != str(user_id))):
        raise ValueError('Linux shell home crosses its selected account')
    return value


def validate(settings):
    if (not isinstance(settings,dict) or set(settings)-{'linux_launcher_sha256','linux_home'}!={'shell','cwd','command','cols','rows','nonce'}
            or settings['shell'] not in {'cmd','wsl','wsl-sandbox'}
            or not isinstance(settings['nonce'],str) or not re.fullmatch('[0-9a-f]{64}',settings['nonce'])
            or any(type(settings[key]) is not int or not 1<=settings[key]<=1000 for key in ('cols','rows'))
            or (settings['cwd'] is not None and (not isinstance(settings['cwd'],str) or '\0' in settings['cwd'] or len(settings['cwd'])>4096))):
        raise ValueError('Invalid terminal profile')
    argv=settings['command']
    if not isinstance(argv,list) or not argv or any(not isinstance(part,str) or '\0' in part for part in argv):
        raise ValueError('Invalid terminal command profile')
    if 'linux_launcher_sha256' in settings and (
        settings['shell']!='cmd' or argv!=list(LINUX_SHELL)
        or not isinstance(settings['linux_launcher_sha256'],str)
        or not re.fullmatch('[0-9a-f]{64}',settings['linux_launcher_sha256'])
    ):
        raise ValueError('Invalid Linux terminal launcher')
    if 'linux_home' in settings:
        if 'linux_launcher_sha256' not in settings:
            raise ValueError('Linux shell home requires the isolated terminal launcher')
        validate_linux_home(settings['linux_home'])
    if settings['shell']=='cmd':
        windows = len(argv)==1 and PureWindowsPath(argv[0]).name.lower()=='cmd.exe' and PureWindowsPath(argv[0]).is_absolute()
        if not windows and argv!=list(LINUX_SHELL):
            raise ValueError('The local terminal requires its fixed host interpreter')
    elif settings['shell']=='wsl':
        if argv!=['bash','-l']:raise ValueError('The WSL terminal requires its fixed interactive shell')
    else:
        prefix=['unshare','--user','--map-root-user','--mount','--net','--pid','--fork','--mount-proc','bash']
        offset=len(prefix)
        if (len(argv) not in {offset+3,offset+4,offset+5} or (len(argv)==offset+4 and argv[-1]!='--external-export')
                or (len(argv)==offset+5 and (argv[-2]!='--persistent-export'
                    or not re.fullmatch(r'/mnt/[a-z]/.+/\.terminal-export/[0-9a-f]{32}',argv[-1])))
                or argv[:offset]!=prefix or not argv[offset].startswith('/mnt/')
                or not argv[offset].endswith('/app/services/sandbox_wsl.sh') or argv[offset+1]!=settings['cwd'] or argv[offset+2]!='bash -l'):
            raise ValueError('The sandbox terminal requires its fixed launcher')
    return settings


def encode(settings):return base64.b64encode(pack(validate(settings))).decode('ascii')


def decode(encoded):
    raw=base64.b64decode(encoded,validate=True)
    if len(raw)>16*1024:raise ValueError('Terminal profile exceeds its limit')
    return validate(json.loads(raw))


def digest(settings):return hashlib.sha256(pack(validate(settings))).hexdigest()
