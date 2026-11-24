"""Optional defaults for the existing registered solver plans."""

import re

from .case_numerical_method import decomposition_controls
from .casedict_schema import _error


SOLVE_TARGETS={'solve','postprocess','report'}
SOLVE_MODES={'serial':'serial','parallel':'mpi','udf':'udf'}
RESTART_POLICIES=('explicit','latestWritten','disabled')


def workflow_settings(block):
    if not isinstance(block,dict) or set(block)-{'solveMode','subdomains','udfName'}:
        raise ValueError('execution.workflow 须为包含求解方式的配置块')
    mode=block.get('solveMode')
    if not isinstance(mode,str) or mode not in SOLVE_MODES:
        raise ValueError('execution.workflow.solveMode 须为 serial、parallel 或 udf')
    settings={'solve_mode':SOLVE_MODES[mode]}
    if 'subdomains' in block:
        count=block['subdomains']
        if mode!='parallel' or not isinstance(count,str) or not re.fullmatch(r'[1-9][0-9]?',count) or int(count)>64:
            raise ValueError('subdomains 仅用于 parallel，须为 1 至 64 的整数')
        settings['subdomains']=int(count)
    if mode=='udf' or 'udfName' in block:
        name=block.get('udfName')
        if mode!='udf' or not isinstance(name,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,39}',name):
            raise ValueError('udf 需要有效的 udfName，其他求解方式无需此字段')
        settings['udf_name']=name
    return settings


def selected_workflow(case):
    execution=case.get('execution')
    if not isinstance(execution,dict):
        return {}
    settings=workflow_settings(execution['workflow']) if 'workflow' in execution else {}
    if execution.get('runMode')=='remote':
        settings.update(placement='remote')
    if settings.get('solve_mode')=='mpi':
        method=case.get('numericalMethod')
        partition=(decomposition_controls(method['decomposition'])
                   if isinstance(method,dict) and 'decomposition' in method else {})
        count=partition.get('subdomains')
        if count is not None and settings.get('subdomains',count)!=count:
            raise ValueError('execution.workflow.subdomains 与 numericalMethod.decomposition 的分区数不一致')
        settings.setdefault('subdomains',count or 2)
    return settings


def selected_interactive(case):
    execution=case.get('execution')
    if not isinstance(execution,dict) or 'interactive' not in execution:
        return {}
    block=execution['interactive']
    shell=block.get('shell') if isinstance(block,dict) else None
    if not isinstance(block,dict) or set(block)!={'shell'} or shell not in ('cmd','wsl','wsl-sandbox'):
        raise ValueError('execution.interactive.shell 须为 cmd、wsl 或 wsl-sandbox')
    return {'shell':shell}


def selected_restart(case):
    execution=case.get('execution')
    if not isinstance(execution,dict) or 'restartPolicy' not in execution:
        return None
    policy=execution['restartPolicy']
    if policy not in RESTART_POLICIES:
        raise ValueError('execution.restartPolicy 须为 explicit、latestWritten 或 disabled')
    return policy


def validate(case, errors):
    for field,reader in (('workflow',selected_workflow),('interactive',selected_interactive),
                         ('restartPolicy',selected_restart)):
        try:
            reader(case)
        except ValueError as error:
            _error(errors,'execution.'+field,'value',str(error))
