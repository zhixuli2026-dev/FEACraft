"""Typed caseDict controls for supported solver adapters."""

import math
import re

from .casedict_schema import _error


_LINEAR_METHODS={
    'PCG':('preconditioner',{'DIC','FDIC','diagonal','none'}),
    'PBiCGStab':('preconditioner',{'DILU','DIC','diagonal','none'}),
    'smoothSolver':('smoother',{'GaussSeidel','symGaussSeidel','DIC','DILU'}),
    'GAMG':('smoother',{'GaussSeidel','symGaussSeidel','DIC','DILU'}),
}
TIME_KEYS={'endTime','deltaT','writeControl','writeInterval','adjustTimeStep','maxCo','maxDeltaT'}
_WRITE_MODES={'timeStep','runTime','adjustableRunTime'}
_CORRECTORS={'outerCorrectors':'nOuterCorrectors','correctors':'nCorrectors','nonOrthogonalCorrectors':'nNonOrthogonalCorrectors'}
MOMENTUM_SCHEMES={'linear':['Gauss','linear'],'upwind':['Gauss','upwind'],
                  'linearUpwind':['Gauss','linearUpwind','grad(U)']}


def _number(value, label, *, positive=True):
    try:
        result=float(value) if not isinstance(value,bool) else math.nan
    except (TypeError,ValueError,OverflowError):
        result=math.nan
    if not math.isfinite(result) or result<0 or positive and result==0:
        raise ValueError(label+' 须为有效有限数值')
    return result


def linear_solvers(value):
    if not isinstance(value,dict) or not value:
        raise ValueError('linearSolvers 需要至少一个具名字段')
    result={}
    for field,controls in value.items():
        if not isinstance(field,str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',field) or not isinstance(controls,dict):
            raise ValueError('线性求解器字段须为具名配置块')
        method=controls.get('solver')
        if not isinstance(method,str) or method not in _LINEAR_METHODS:
            raise ValueError(f'{field} 的 solver 须为 PCG、PBiCGStab、smoothSolver 或 GAMG')
        key,allowed=_LINEAR_METHODS[method]
        option=controls.get(key)
        required={'solver',key,'tolerance','relTol'}
        if not isinstance(option,str) or option not in allowed or not required<=controls.keys()<=required|{'maxIter'}:
            raise ValueError(f'{field} 需要兼容的 {key}、tolerance 和 relTol，可设置 maxIter')
        absolute=_number(controls['tolerance'],field+'.tolerance')
        relative=_number(controls['relTol'],field+'.relTol',positive=False)
        if relative>1:
            raise ValueError(f'{field}.relTol 须在 0 至 1 之间')
        entry={'solver':method,key:option,'tolerance':absolute,'relTol':relative}
        if 'maxIter' in controls:
            limit=_number(controls['maxIter'],field+'.maxIter')
            if not limit.is_integer() or limit>2**31-1:
                raise ValueError(f'{field}.maxIter 须为 OpenFOAM 32 位标签范围内的正整数')
            entry['maxIter']=int(limit)
        result[field]=entry
    return result


def time_switch(value):
    word=str(value).lower()
    if word in {'true','on','yes','1'}:
        return True
    if word in {'false','off','no','0'}:
        return False


def declared_time_keys(method):
    controls=method.get('timeControl')
    keys=TIME_KEYS & controls.keys() if isinstance(controls,dict) else set()
    return keys|{'writeControl'} if 'writeInterval' in keys else keys


def time_controls(value):
    if not isinstance(value,dict):
        raise ValueError('timeControl 须为配置块')
    result={key:_number(value[key],key,positive=(key!='endTime'))
            for key in ('endTime','deltaT','writeInterval','maxCo','maxDeltaT') if key in value}
    if 'adjustTimeStep' in value:
        active=time_switch(value['adjustTimeStep'])
        if active is None:
            raise ValueError('adjustTimeStep 需要明确的布尔开关')
        result['adjustTimeStep']=active
    if 'writeControl' in value:
        mode=value['writeControl']
        if not isinstance(mode,str) or mode not in _WRITE_MODES:
            raise ValueError('writeControl 须为 timeStep、runTime 或 adjustableRunTime')
        result['writeControl']=mode
    if result.get('writeControl')=='timeStep' and 'writeInterval' in result and not result['writeInterval'].is_integer():
        raise ValueError('按时间步写出时 writeInterval 须为正整数')
    return result


def convergence_required(method):
    block=method.get('convergence')
    if not isinstance(block,dict) or 'requireLinearConvergence' not in block:
        return None
    value=str(block['requireLinearConvergence']).lower()
    if value not in {'true','false'} or not method.get('linearSolvers'):
        raise ValueError('requireLinearConvergence 需要 true 或 false 及明确的 linearSolvers')
    return value=='true'


def nonlinear_controls(value):
    if (not isinstance(value,dict) or value.get('algorithm')!='PIMPLE' or 'outerCorrectors' not in value
            or set(value)-{'algorithm',*_CORRECTORS}):
        raise ValueError('非线性控制需要 PIMPLE 和明确的 outerCorrectors')
    native={}
    for field,name in _CORRECTORS.items():
        if field not in value:
            continue
        number=_number(value[field],field,positive=field!='nonOrthogonalCorrectors')
        if not number.is_integer() or number>2**31-1:
            raise ValueError(field+' 需要原生标签范围内的整数')
        native[name]=int(number)
    return {'algorithm':'PIMPLE','controls':native}


def momentum_scheme(value):
    if not isinstance(value,dict) or 'momentumConvection' not in value:
        return None
    name=value['momentumConvection']
    if not isinstance(name,str) or name not in MOMENTUM_SCHEMES:
        raise ValueError('momentumConvection 可选 linear、upwind 或 linearUpwind')
    return name


def decomposition_controls(value):
    if not isinstance(value,dict) or value.get('method') not in ('simple','scotch'):
        raise ValueError('decomposition.method 须为 simple 或 scotch')
    method=value['method']
    allowed={'method','subdomains','divisions'} if method=='simple' else {'method','subdomains'}
    if set(value)-allowed:
        raise ValueError('分解策略包含不适用的设置')
    result={'method':method}
    if 'subdomains' in value:
        ranks=_number(value['subdomains'],'subdomains')
        if not ranks.is_integer() or ranks>64:
            raise ValueError('分区数须为 1 至 64 的整数')
        result['subdomains']=int(ranks)
    if method=='simple':
        values=value.get('divisions')
        if (not isinstance(values,list) or len(values)!=3
                or any(not isinstance(part,str) or not re.fullmatch(r'[1-9][0-9]?',part) for part in values)):
            raise ValueError('simple 分解需要三个正整数，例如 divisions (2 1 1)')
        divisions=list(map(int,values))
        ranks=math.prod(divisions)
        if ranks>64 or result.get('subdomains',ranks)!=ranks:
            raise ValueError('三轴分区数的乘积须等于总分区数且不大于 64')
        result.update(divisions=divisions,subdomains=ranks)
    return result


def validate(method, errors):
    from .calculix_numerical import declared as calculix_controls
    from .code_aster_numerical import declared as aster_controls
    from .elmer_numerical import declared as elmer_controls
    from .moose_numerical import declared as moose_controls
    from .su2_numerical import declared as su2_controls

    for name,parse in (('linearSolvers',linear_solvers),('timeControl',time_controls),
                       ('nonlinear',nonlinear_controls),('stabilization',momentum_scheme),
                       ('decomposition',decomposition_controls),('calculix',calculix_controls),('elmer',elmer_controls),
                       ('su2',su2_controls),('moose',moose_controls),('codeAster',aster_controls)):
        if name in method:
            try:
                parse(method[name])
            except ValueError as error:
                _error(errors,f'numericalMethod.{name}','value',str(error))
    try:
        convergence_required(method)
    except ValueError as error:
        _error(errors,'numericalMethod.convergence.requireLinearConvergence','value',str(error))
