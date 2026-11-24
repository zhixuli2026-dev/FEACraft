"""Private frozen PDF/sidecar profiles for the request-owned SyncTeX helper."""
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import uuid

from .. import storage
from . import native_execution_client as client,synctex_native
from .owned_process import wsl_path
from .attempt_workspace import checked_path,ordinary
from .fs_directory import remove_owned

NATIVE=Path(__file__).with_name('synctex_native.py')
DIRECTORY='.vibeflow-native-queries'
MAX_SETTINGS_BYTES=16*1024


def settings(profile):
    path_for=str if profile['distro']=='native-linux' else wsl_path
    return {'schema':synctex_native.SCHEMA,'pdf':path_for(Path(profile['root'])/profile['pdf']),
        **{key:profile[key] for key in ('pdf_sha256','synctex_sha256','page','x','y')}}


def profile(value):
    if (not isinstance(value,dict) or set(value)!={'root','pdf','distro','pdf_sha256','synctex_sha256','page','x','y'}
            or not isinstance(value['distro'],str) or not re.fullmatch('[a-z0-9-]+',value['distro'])
            or value['pdf']!='snapshot.pdf'):
        raise ValueError('Invalid SyncTeX request profile')
    root=Path(value['root'])
    parent=storage._fs_root().resolve()/DIRECTORY
    if root.parent!=parent or not re.fullmatch('[0-9a-f]{32}',root.name):
        raise ValueError('SyncTeX request has no private frozen directory')
    ordinary(parent);ordinary(root)
    synctex_native.command(settings(value))
    return value


def directory():
    parent=storage._fs_root().resolve()/DIRECTORY
    parent.mkdir(exist_ok=True);ordinary(parent)
    root=parent/uuid.uuid4().hex;root.mkdir(exist_ok=False)
    return root


def prepare(pdf,sidecar,distro,page,x,y,*,root):
    if len(pdf)>synctex_native.MAX_PDF or len(sidecar)>synctex_native.MAX_SIDECAR:
        raise ValueError('SyncTeX snapshot exceeds its limit')
    parent=root.parent
    try:
        (root/'snapshot.pdf').write_bytes(pdf)
        (root/'snapshot.synctex.gz').write_bytes(sidecar)
        result={'root':str(root),'pdf':'snapshot.pdf','distro':distro,'page':page,'x':x,'y':y,
            'pdf_sha256':hashlib.sha256(pdf).hexdigest(),'synctex_sha256':hashlib.sha256(sidecar).hexdigest()}
        profile(result)
        encoded=json.dumps(settings(result)).encode('utf-8')
        if len(encoded)>MAX_SETTINGS_BYTES:raise ValueError('SyncTeX configuration exceeds its limit')
        (root/'settings.json').write_bytes(encoded)
        return result
    except BaseException:
        remove_owned(root,root=parent);raise


def validate(parameters,registered):
    profile(registered)
    if not isinstance(parameters,dict) or set(parameters)!={'distro','config','sha256'} or parameters['distro']!=registered['distro']:
        raise ValueError('SyncTeX call changes its registered distribution')
    root=Path(registered['root']);config=Path(parameters['config'])
    if {path.name for path in root.iterdir()}!={'snapshot.pdf','snapshot.synctex.gz','settings.json'}:
        raise ValueError('SyncTeX snapshot contains an unselected sidecar')
    if config!=root/'settings.json':raise ValueError('SyncTeX configuration belongs to another request')
    config=checked_path(root,'settings.json')
    if config.stat().st_size>MAX_SETTINGS_BYTES:raise ValueError('SyncTeX configuration exceeds its limit')
    raw=config.read_bytes();digest=hashlib.sha256(raw).hexdigest()
    if parameters['sha256']!=digest or json.loads(raw)!=settings(registered):
        raise ValueError('SyncTeX fixed profile changed')
    for name,key,limit in (('snapshot.pdf','pdf_sha256',synctex_native.MAX_PDF),
                           ('snapshot.synctex.gz','synctex_sha256',synctex_native.MAX_SIDECAR)):
        path=checked_path(root,name)
        if path.stat().st_size>limit or hashlib.sha256(path.read_bytes()).hexdigest()!=registered[key]:
            raise ValueError('SyncTeX frozen input changed')
    return config,digest


def run(registered):
    config=Path(registered['root'])/'settings.json'
    result=client.run_native('synctex_query_linux' if registered['distro']=='native-linux' else 'synctex_query',{'distro':registered['distro'],'config':str(config),
        'sha256':hashlib.sha256(config.read_bytes()).hexdigest()},timeout=20,capture=True)
    if result is None or result.returncode:
        raise client.ChildExecutionError('Owned SyncTeX helper did not finish')
    try:
        value=json.loads(result.stdout)
        if (not isinstance(value,dict) or set(value)!={'schema','returncode','timed_out','stdout','stderr'}
                or value['schema']!='vibeflow.synctex-result.v1' or type(value['returncode']) is not int
                or not -0x80000000<=value['returncode']<=0xffffffff or type(value['timed_out']) is not bool):
            raise ValueError('Invalid SyncTeX result')
        data={key:base64.b64decode(value[key],validate=True) for key in ('stdout','stderr')}
        if len(data['stdout'])>synctex_native.STDOUT_LIMIT or len(data['stderr'])>synctex_native.STDERR_LIMIT:
            raise ValueError('SyncTeX result exceeds its bounds')
    except (ValueError,TypeError,KeyError) as error:
        raise client.ChildExecutionError('Owned SyncTeX result is invalid') from error
    if value['timed_out']:raise subprocess.TimeoutExpired('synctex',10)
    if value['returncode']:raise RuntimeError('SyncTeX query failed: '+(data['stderr'] or data['stdout']).decode('utf-8','replace')[-600:])
    return data['stdout'].decode('utf-8','replace')
