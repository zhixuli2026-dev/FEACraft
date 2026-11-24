"""User-owned global capability probes, never allocated under a synthetic case."""
import base64
import json
from pathlib import Path
import re
import sys

from . import native_execution_client as client,runtime_native
from . import openfoam_runtime

NATIVE=Path(runtime_native.__file__)
KINDS={'runtime.wsl_probe':'wsl_runtime_probe','runtime.python_probe':'python_runtime_probe'}


def profile(kind,value):
    if not isinstance(value,dict):raise ValueError('Invalid runtime request profile')
    if kind=='runtime.wsl_probe':
        if (set(value)!={'distro','bashrc'} or not isinstance(value['distro'],str)
                or not re.fullmatch('[a-z0-9-]+',value['distro'])):
            raise ValueError('Invalid runtime distribution profile')
        runtime_native.validate({'kind':'wsl','bashrc':value['bashrc']})
    elif kind=='runtime.python_probe':
        if (set(value)!={'python','packages'} or not isinstance(value['python'],str)
                or not Path(value['python']).is_absolute() or not Path(value['python']).is_file()):
            raise ValueError('Invalid configured runtime Python profile')
        runtime_native.validate({'kind':'python','packages':value['packages']})
    else:raise ValueError('Unknown global runtime request profile')
    return value


def command(kind,registered,parameters):
    from .owned_process import wsl_path
    profile(kind,registered)
    if parameters!=registered:raise ValueError('Runtime call differs from its registered profile')
    settings=({'kind':'wsl','bashrc':registered['bashrc']} if kind=='runtime.wsl_probe' else
              {'kind':'python','packages':registered['packages']})
    encoded=base64.b64encode(json.dumps(settings).encode()).decode('ascii')
    if kind=='runtime.wsl_probe':
        if registered['distro']==openfoam_runtime.NATIVE_LINUX:
            if not sys.platform.startswith('linux'):raise ValueError('Native Linux runtime is unavailable')
            return [sys.executable,'-I','-S','-u',str(NATIVE),'--settings',encoded],30,1024*1024
        return ['wsl.exe','-d',registered['distro'],'--user','nobody','--exec',
            'python3','-I','-S','-u',wsl_path(NATIVE),'--settings',encoded],30,1024*1024
    return [registered['python'],'-I','-u',str(NATIVE),'--settings',encoded],45,1024*1024


def run(kind,registered):
    result=client.run_native(KINDS[kind],registered,timeout=45 if kind=='runtime.python_probe' else 30,capture=True)
    if result is None or result.returncode:raise client.ChildExecutionError('Owned runtime probe did not finish')
    frames=[line[len(runtime_native.FRAME):] for line in result.stdout.decode('utf-8','replace').splitlines()
            if line.startswith(runtime_native.FRAME)]
    if len(frames)!=1:raise client.ChildExecutionError('Owned runtime probe has no unique result frame')
    value=json.loads(frames[0])
    if kind=='runtime.python_probe':
        if (not isinstance(value,dict) or set(value)!={'schema','version','packages'} or value['schema']!='vibeflow.runtime-python.v1'
                or not isinstance(value['version'],str) or not value['version'] or len(value['version'])>128
                or not isinstance(value['packages'],dict) or set(value['packages'])!=set(runtime_native.PACKAGES)
                or any(v is not None and (not isinstance(v,str) or not v or len(v)>128) for v in value['packages'].values())):
            raise client.ChildExecutionError('Invalid Python capability result')
    else:
        flags={'openfoam','openfoam_bashrc_loaded','wmake','foam_etc_file','latexmk','gmsh'}
        paths={'openfoam_path','openfoam_version','wmake_path','foam_etc_file_path','foam_etc_control_dict'}
        fields={'schema','reachable'}|flags|paths
        if (not isinstance(value,dict) or set(value)!=fields or value['schema']!='vibeflow.runtime-wsl.v1'
                or value['reachable'] is not True
                or any(type(value[key]) is not bool for key in flags)
                or any(value[key] is not None and (not isinstance(value[key],str) or len(value[key])>4096)
                    for key in paths)):
            raise client.ChildExecutionError('Invalid WSL capability result')
    return {key:item for key,item in value.items() if key!='schema'}


async def probe(user_id,kind,registered):
    from .native_request_execution import scope
    identifier=None
    try:
        async with scope(None,user_id,NATIVE.parent,kind,registered) as executor:
            identifier=executor.owner.id
            value=await executor.run_sync(run,kind,registered,timeout=65 if kind=='runtime.python_probe' else 50)
        return {**value,'reachable':True,'request_id':identifier}
    except Exception as error:
        fallback={'version':'unavailable','packages':{key:None for key in runtime_native.PACKAGES}} if kind=='runtime.python_probe' else {}
        return {**fallback,'reachable':False,'error':type(error).__name__,'request_id':identifier}


async def python_probe(user_id, settings=None):
    if settings is None:
        from .runtime_config import get_runtime_settings
        settings=get_runtime_settings()
    return await probe(user_id,'runtime.python_probe',{'python':settings['python'] or sys.executable,
        'packages':list(runtime_native.PACKAGES)})


async def wsl_probe(user_id, settings=None):
    if settings is None:
        from .runtime_config import get_runtime_settings
        settings=get_runtime_settings()
    registered={'distro':openfoam_runtime.distribution(settings),
        'bashrc':settings['openfoam_bashrc'] or '/usr/lib/openfoam/openfoam2406/etc/bashrc'}
    return {'distro':registered['distro'],**(await probe(user_id,'runtime.wsl_probe',registered))}
