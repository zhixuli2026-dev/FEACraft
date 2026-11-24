"""Validate explicit MOOSE Transient controls and the native time/iteration evidence."""

import csv
import math
import re

from .attempt_workspace import checked_path
from .case_boundary_inputs import scalar
from .casedict import CASE_DICT_PATH, parse_casedict
from .compose_artifacts import record


LINEAR = {'tolerance','maxIterations'}
NONLINEAR = {'relativeTolerance','absoluteTolerance','maxIterations'}
TIME = {'mode','timeStep','endTime'}
ADAPTIVE = {'minStep','maxStep','optimalIterations','growthFactor','cutbackFactor'}


def _positive(value):
    result=scalar(value)
    if result<=0:
        raise ValueError('MOOSE controls require positive finite values')
    return result


def _count(value):
    result=_positive(value)
    if not result.is_integer() or result>2**31-1:
        raise ValueError('MOOSE iteration limits must be positive integers')
    return int(result)


def declared(data):
    if not isinstance(data,dict) or set(data)!={'linear','nonlinear','timeControl'}:
        raise ValueError('MOOSE requires linear, nonlinear and timeControl blocks')
    linear,nonlinear,control=(data[key] for key in ('linear','nonlinear','timeControl'))
    if not isinstance(linear,dict) or set(linear)!=LINEAR or not isinstance(nonlinear,dict) or set(nonlinear)!=NONLINEAR:
        raise ValueError('MOOSE requires linear and nonlinear tolerances and iteration limits')
    ltol=_positive(linear['tolerance'])
    nlrel=_positive(nonlinear['relativeTolerance'])
    nlabs=_positive(nonlinear['absoluteTolerance'])
    if ltol>=1 or nlrel>=1:
        raise ValueError('MOOSE relative tolerances must be below one')
    mode=control.get('mode') if isinstance(control,dict) else None
    if mode not in ('fixed','adaptive') or set(control)!=(TIME | (ADAPTIVE if mode=='adaptive' else set())):
        raise ValueError('MOOSE timeControl needs mode, timeStep and endTime, plus limits for adaptive mode')
    step,end=(_positive(control[key]) for key in ('timeStep','endTime'))
    if step>end:
        raise ValueError('MOOSE initial time step must be at or below endTime')
    executioner={'type':'Transient','solve_type':'NEWTON','petsc_options_iname':"'-pc_type'",
        'petsc_options_value':"'lu'",'l_tol':ltol,'l_max_its':_count(linear['maxIterations']),
        'nl_rel_tol':nlrel,'nl_abs_tol':nlabs,'nl_max_its':_count(nonlinear['maxIterations']),'end_time':end}
    settings={'executioner':executioner,'time_mode':mode}
    if mode=='fixed':
        executioner['dt']=step
    else:
        minimum,maximum,growth,cutback=(_positive(control[key]) for key in ('minStep','maxStep','growthFactor','cutbackFactor'))
        if not (minimum<=step<=maximum<=end and 0<cutback<1<growth):
            raise ValueError('MOOSE adaptive controls require consistent time limits and cutback < 1 < growth')
        executioner.update(dtmin=minimum,dtmax=maximum)
        settings['stepper']={'type':'IterationAdaptiveDT','dt':step,'optimal_iterations':_count(control['optimalIterations']),
                             'growth_factor':growth,'cutback_factor':cutback}
    return settings


def _require(values, expected, block):
    for key,want in expected.items():
        actual=values.get(key)
        if actual is None or (scalar(actual)!=want if isinstance(want,(int,float)) else actual!=want):
            raise ValueError('Native MOOSE '+block+'.'+key+' conflicts with numericalMethod.moose')


def prepare(root,source,sections,*,with_restart):
    try:
        raw=checked_path(root,CASE_DICT_PATH).read_bytes()
        method=parse_casedict(raw.decode('utf-8')).get('numericalMethod')
    except (FileNotFoundError,UnicodeDecodeError,ValueError):
        return
    if not isinstance(method,dict) or 'moose' not in method:
        return
    if with_restart:
        raise ValueError('Declared MOOSE schedule applies to a fresh native time series')
    settings=declared(method['moose'])
    executioner=sections['Executioner',]
    stepper=sections.get(('Executioner','TimeStepper'))
    if settings['time_mode']=='fixed' and (stepper is not None or {'dtmin','dtmax'}.intersection(executioner)):
        raise ValueError('Native MOOSE TimeStepper or limits conflict with fixed time control')
    if settings['time_mode']=='adaptive':
        if 'dt' in executioner or stepper is None:
            raise ValueError('MOOSE adaptive time control requires a declared TimeStepper with its own dt')
        _require(stepper,settings['stepper'],'TimeStepper')
    _require(executioner,settings['executioner'],'Executioner')
    source['numerical']=settings
    source['files'][CASE_DICT_PATH]=record(raw)


def observed(settings, table, log):
    with table.open(encoding='ascii',newline='') as stream:
        rows=csv.reader(stream)
        if next(rows,[])[:1]!=['time']:
            raise ValueError('MOOSE numerical evidence requires its native time series')
        times=[scalar(row[0]) for row in rows if row]
    values=settings['executioner']
    if len(times)<2 or times[0]!=0 or not math.isclose(times[-1],values['end_time'],rel_tol=1e-9):
        raise ValueError('MOOSE time series did not reach the declared final time')
    steps=[right-left for left,right in zip(times,times[1:])]
    if any(dt<=0 for dt in steps):
        raise ValueError('MOOSE time series is not strictly increasing')
    if settings['time_mode']=='adaptive':
        if max(steps)>values['dtmax']*(1+1e-8) or min(steps[:-1] or steps)<values['dtmin']*(1-1e-8):
            raise ValueError('MOOSE native adaptive step violated its confirmed bounds')
    elif (any(not math.isclose(dt,values['dt'],rel_tol=1e-8) for dt in steps[:-1])
          or steps[-1]>values['dt']*(1+1e-8)):
        raise ValueError('MOOSE used unexpected fixed time steps')
    text=re.sub(rb'\x1b\[[0-9;]*m',b'',log).decode('utf-8',errors='replace')
    linear=[int(value) for value in re.findall(r'^\s*(\d+) Linear \|R\| =',text,re.M)]
    nonlinear=[int(value) for value in re.findall(r'^\s*(\d+) Nonlinear \|R\| =',text,re.M)]
    if (not linear or not nonlinear or max(linear)>values['l_max_its'] or max(nonlinear)>values['nl_max_its']
            or text.count('Solve Converged!')!=len(steps)):
        raise ValueError('MOOSE native convergence history differs from the declared iteration controls')
    return {'steps':len(steps),'last_time':times[-1],'minimum_time_step':min(steps),'maximum_time_step':max(steps),
            'maximum_linear_iterations':max(linear),'maximum_nonlinear_iterations':max(nonlinear)}
