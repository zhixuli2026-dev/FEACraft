"""Bind SU2 flow controls to the sealed CFG and its native CSV iteration history."""

import csv
import math
from pathlib import PurePosixPath

from .attempt_workspace import checked_path
from .case_boundary_inputs import scalar
from .casedict import CASE_DICT_PATH, parse_casedict
from .compose_artifacts import record


LINEAR = {'method','preconditioner','tolerance','maxIterations'}
ADAPTIVE = {'factorDown','factorUp','minimum','maximum','linearThreshold','startIteration'}
STEADY = {'iterations','residualLimit','startIteration','requireConvergence'}
UNSTEADY = {'timeSteps','innerIterations'}


def _block(value, keys):
    if not isinstance(value,dict) or set(value)!=keys:
        raise ValueError('SU2 requires a complete control block with '+', '.join(sorted(keys)))
    return value


def _count(value, minimum=1):
    number=scalar(value)
    if not number.is_integer() or not minimum<=number<=2**31-1:
        raise ValueError('SU2 iteration counts must be integers in the supported range')
    return int(number)


def declared(value):
    if (not isinstance(value,dict) or not {'linear','cfl'}<=value.keys()
            or value.keys()-{'linear','cfl','steady','unsteady'}
            or ('steady' in value)==('unsteady' in value)):
        raise ValueError('SU2 requires linear and cfl settings and one steady or unsteady block')
    linear=_block(value['linear'],LINEAR)
    tolerance=scalar(linear['tolerance'])
    if linear['method']!='FGMRES' or linear['preconditioner']!='ILU' or not 0<tolerance<1:
        raise ValueError('SU2 requires FGMRES with ILU and a linear tolerance between zero and one')
    cfl=value['cfl']
    if not isinstance(cfl,dict) or not {'initial'}<=cfl.keys()<={'initial','adaptive'}:
        raise ValueError('SU2 CFL requires an initial value and optional adaptive controls')
    initial=scalar(cfl['initial'])
    if initial<=0:
        raise ValueError('SU2 CFL must be positive')
    cfg={'LINEAR_SOLVER':'FGMRES','LINEAR_SOLVER_PREC':'ILU','LINEAR_SOLVER_ERROR':tolerance,
         'LINEAR_SOLVER_ITER':_count(linear['maxIterations']),'CFL_NUMBER':initial,
         'CFL_ADAPT':'NO','CONV_FIELD':'RMS_DENSITY'}
    result={'options':cfg,'require_convergence':False}
    if 'adaptive' in cfl:
        adaptive=_block(cfl['adaptive'],ADAPTIVE)
        down,up,minimum,maximum,threshold=(scalar(adaptive[key]) for key in
            ('factorDown','factorUp','minimum','maximum','linearThreshold'))
        if not (0<down<1<up and 0<minimum<=initial<=maximum and 0<threshold<1):
            raise ValueError('SU2 adaptive CFL requires valid scale factors, limits and linear threshold')
        cfg.update(CFL_ADAPT='YES',CFL_ADAPT_PARAM=[down,up,minimum,maximum,threshold,_count(adaptive['startIteration'],0)])
    if 'steady' in value:
        steady=_block(value['steady'],STEADY)
        limit=scalar(steady['residualLimit'])
        start,iterations=_count(steady['startIteration'],0),_count(steady['iterations'])
        require=str(steady['requireConvergence']).lower()
        if limit>=0 or start>=iterations or require not in ('true','false'):
            raise ValueError('SU2 steady convergence needs a negative log residual, a valid start and a boolean requirement')
        cfg.update(TIME_DOMAIN='NO',ITER=iterations,CONV_RESIDUAL_MINVAL=limit,CONV_STARTITER=start)
        result['require_convergence']=require=='true'
    else:
        unsteady=value['unsteady']
        if not isinstance(unsteady,dict) or unsteady.keys() not in (UNSTEADY|{'timeStep'},UNSTEADY|{'courantNumber'}):
            raise ValueError('SU2 unsteady time requires timeSteps, innerIterations and one timeStep or courantNumber')
        adaptive_time='courantNumber' in unsteady
        dt=scalar(unsteady['courantNumber'] if adaptive_time else unsteady['timeStep'])
        steps=_count(unsteady['timeSteps'])
        if dt<=0 or not math.isfinite(dt*steps):
            raise ValueError('SU2 unsteady analysis requires a positive finite physical time schedule')
        cfg.update(TIME_DOMAIN='YES',TIME_MARCHING='DUAL_TIME_STEPPING-1ST_ORDER',TIME_STEP=0. if adaptive_time else dt,
                   UNST_CFL_NUMBER=dt if adaptive_time else 0.,TIME_ITER=steps,INNER_ITER=_count(unsteady['innerIterations']))
    return result


def prepare(root, source):
    try:
        raw=checked_path(root,CASE_DICT_PATH).read_bytes()
        method=parse_casedict(raw.decode('utf-8')).get('numericalMethod')
    except (FileNotFoundError,UnicodeDecodeError,ValueError):
        return
    if not isinstance(method,dict) or 'su2' not in method:
        return
    settings=declared(method['su2'])
    native=source['configurations'][source['input']]
    if (len(source['configurations'])!=1 or native.get('SOLVER')!=['EULER']
            or native.get('TIME_DISCRE_FLOW')!=['EULER_IMPLICIT'] or native.get('RESTART_SOL',['NO'])!=['NO']
            or source['files'].keys() & {'runtime.dat','runtime.cfg'}):
        raise ValueError('Declared SU2 controls require a single implicit Euler flow without restart or runtime overrides')
    for key,value in settings['options'].items():
        expected=value if isinstance(value,list) else [value]
        default=['NO'] if key=='TIME_DOMAIN' else []
        actual=native.get(key,default)
        if len(expected)!=len(actual) or any((scalar(got)!=want if isinstance(want,(int,float)) else got!=want)
                                             for want,got in zip(expected,actual)):
            raise ValueError('Native SU2 '+key+' conflicts with numericalMethod.su2')
    if native.get('TABULAR_FORMAT')!=['CSV'] or len(native.get('CONV_FILENAME',[]))>1:
        raise ValueError('Declared SU2 controls require one native CSV convergence history')
    base=native.get('CONV_FILENAME',['history'])[0]
    settings['history']=PurePosixPath(base).with_suffix('.csv').as_posix()
    source['numerical']=settings
    source['files'][CASE_DICT_PATH]=record(raw)


def observed(root, settings, files):
    history=settings['history']
    if history not in files:
        raise ValueError('SU2 did not publish the sealed convergence history')
    with checked_path(root,history).open(encoding='ascii',newline='') as stream:
        reader=csv.reader(stream,skipinitialspace=True)
        columns=[name.strip() for name in next(reader,[])]
        required={'Inner_Iter','rms[Rho]','Linear_Solver_Iterations','LinSolRes','Min CFL','Max CFL'}
        options=settings['options']
        transient=options['TIME_DOMAIN']=='YES'
        if transient:
            required.update({'Time_Iter','Cur_Time','Time_Step'})
        if not required<=set(columns) or len(columns)!=len(set(columns)):
            raise ValueError('Native SU2 history omits required iteration and CFL fields')
        data=[{name:scalar(value) for name,value in zip(columns,row,strict=True) if name in required} for row in reader if row]
    if not data:
        raise ValueError('Native SU2 convergence history is empty')
    last=data[-1]
    linear=max(row['Linear_Solver_Iterations'] for row in data)
    cfl_min=min(row['Min CFL'] for row in data)
    cfl_max=max(row['Max CFL'] for row in data)
    if not 0<linear<=options['LINEAR_SOLVER_ITER'] or cfl_min<=0 or cfl_max<cfl_min:
        raise ValueError('Native SU2 iteration or CFL evidence differs from its declaration')
    if options['CFL_ADAPT']=='YES':
        _,_,minimum,maximum,_,_=options['CFL_ADAPT_PARAM']
        if cfl_min<minimum*(1-1e-6) or cfl_max>maximum*(1+1e-6):
            raise ValueError('Native SU2 CFL exceeded its adaptive range')
    elif not all(math.isclose(value,options['CFL_NUMBER'],rel_tol=1e-6) for value in (cfl_min,cfl_max)):
        raise ValueError('Native SU2 fixed CFL differs from its declaration')
    result={'rows':len(data),'maximum_linear_iterations':int(linear),'final_log_density_residual':last['rms[Rho]'],
            'minimum_cfl':cfl_min,'maximum_cfl':cfl_max}
    if transient:
        steps=options['TIME_ITER']
        dt=options['TIME_STEP']
        seen={}
        for row in data:
            index=row['Time_Iter']
            pair=(row['Cur_Time'],row['Time_Step'])
            if (not index.is_integer() or not 0<=index<steps or not 0<=row['Inner_Iter']<options['INNER_ITER']
                    or pair[1]<=0 or seen.setdefault(int(index),pair)!=pair):
                raise ValueError('Native SU2 history has inconsistent physical time or inner iterations')
        if len(seen)!=steps or seen[0][0]!=0:
            raise ValueError('Native SU2 history does not complete the declared physical time schedule')
        series=[seen[index] for index in range(steps)]
        if any(not math.isclose(time,previous[0]+size,rel_tol=1e-8)
               for previous,(time,size) in zip(series,series[1:])):
            raise ValueError('Native SU2 physical time does not match the reported time steps')
        result.update(time_steps=steps,last_reported_time_s=last['Cur_Time'])
        sizes=[size for _,size in series]
        if options['UNST_CFL_NUMBER']:
            result.update(physical_cfl=options['UNST_CFL_NUMBER'],minimum_time_step=min(sizes),maximum_time_step=max(sizes))
        elif any(not math.isclose(size,dt,rel_tol=1e-6) for size in sizes):
            raise ValueError('Native SU2 physical time step differs from its declaration')
        else:
            result['time_step_s']=dt
    else:
        if not 0<=last['Inner_Iter']<options['ITER']:
            raise ValueError('Native SU2 history exceeds the configured outer iteration budget')
        reached=(last['Inner_Iter']>=options['CONV_STARTITER'] and last['rms[Rho]']<=options['CONV_RESIDUAL_MINVAL'])
        if settings['require_convergence'] and not reached:
            raise ValueError('Native SU2 did not reach the declared density residual target')
        result['density_target_reached']=reached
    return result
