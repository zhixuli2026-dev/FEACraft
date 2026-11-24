"""Generate stock Elmer HeatSolve controls and verify the observed run."""

import math
import re

from .attempt_workspace import checked_path
from .case_boundary_inputs import scalar
from .casedict import CASE_DICT_PATH, parse_casedict
from .compose_artifacts import record
from .elmer_joule import _by_kind, _native_sections


LINEAR = {'method','preconditioner','tolerance','maxIterations'}
NONLINEAR = {'tolerance','maxIterations'}
TIME = {'mode','timeStep','steps','outputInterval'}
ADAPTIVE = {'minStep','maxStep','errorTolerance'}


def _positive(value):
    result = scalar(value)
    if result <= 0:
        raise ValueError('Elmer numerical controls require positive finite values')
    return result


def _count(value):
    result = _positive(value)
    if not result.is_integer() or result > 2**31-1:
        raise ValueError('Elmer requires positive integer step and iteration counts')
    return int(result)


def _iteration_controls(value, fields):
    if not isinstance(value,dict) or set(value)!=fields:
        raise ValueError('Elmer needs a complete linear or nonlinear iteration configuration')
    tolerance = _positive(value['tolerance'])
    if tolerance >= 1:
        raise ValueError('Elmer convergence tolerance must be below one')
    return {'tolerance':tolerance,'max_iterations':_count(value['maxIterations'])}


def declared(value):
    keys = {'heatSolver','linear','nonlinear','timeControl'}
    if not isinstance(value,dict) or not {'heatSolver','linear'}<=value.keys()<=keys:
        raise ValueError('Elmer numerical controls require heatSolver and linear settings')
    linear = value['linear']
    if isinstance(linear,dict) and linear=={'method':'Banded'}:
        method = {'method':'Banded'}
    else:
        method = _iteration_controls(linear,LINEAR)
        if linear['method']!='CG' or linear['preconditioner'] not in ('None','Diagonal'):
            raise ValueError('Elmer heat supports Banded or CG with None or Diagonal preconditioning')
        method.update(method='CG',preconditioner=linear['preconditioner'])
    result = {'heat_solver':_count(value['heatSolver']),'linear':method}
    if 'nonlinear' in value:
        result['nonlinear'] = _iteration_controls(value['nonlinear'],NONLINEAR)
    if 'timeControl' in value:
        controls = value['timeControl']
        mode = controls.get('mode') if isinstance(controls,dict) else None
        if mode not in ('fixed','adaptive') or set(controls)!=(TIME | (ADAPTIVE if mode=='adaptive' else set())):
            raise ValueError('Elmer timeControl requires mode, timeStep, steps and outputInterval')
        step = _positive(controls['timeStep'])
        steps,output = _count(controls['steps']),_count(controls['outputInterval'])
        if output>steps or not math.isfinite(steps*step):
            raise ValueError('Elmer outputInterval must be within the configured finite time schedule')
        schedule = {'mode':mode,'time_step':step,'steps':steps,'output_interval':output}
        if mode=='adaptive':
            minimum,maximum,target = (_positive(controls[key]) for key in ('minStep','maxStep','errorTolerance'))
            if not minimum<=maximum<=step or target>=1:
                raise ValueError('Elmer adaptive controls require minStep <= maxStep <= timeStep and errorTolerance < 1')
            schedule.update(min_step=minimum,max_step=maximum,error_tolerance=target)
        result['time'] = schedule
    return result


def prepare(root, source):
    try:
        raw = checked_path(root,CASE_DICT_PATH).read_bytes()
        method = parse_casedict(raw.decode('utf-8')).get('numericalMethod')
    except (FileNotFoundError,UnicodeDecodeError,ValueError):
        return
    if isinstance(method,dict) and 'elmer' in method:
        source['numerical'] = declared(method['elmer'])
        source['files'][CASE_DICT_PATH] = record(raw)


def _configure(section, expected):
    end,native = section
    additions = []
    for name,value in expected.items():
        observed = native.get(name.lower())
        if observed is None:
            body = format(value,'.16g') if isinstance(value,float) else str(value)
            additions.append(f'  {name} = {body}\n')
            continue
        decoded = re.sub(r'^(?:logical|string|integer|real) ', '', observed).strip('"')
        matches = (scalar(decoded.replace('d','e'))==value if isinstance(value,(int,float)) else decoded==value.lower())
        if not matches:
            raise ValueError('Native Elmer '+name+' conflicts with numericalMethod.elmer')
    return end,additions


def render(original, settings):
    text = original.decode('utf-8-sig')
    sections = _native_sections(text,model='Elmer numerical controls')
    solvers = _by_kind(sections,'solver')
    index = settings['heat_solver']
    if set(solvers)!={index} or solvers[index][1].get('procedure','').removeprefix('file ')!='"heatsolve" "heatsolver"':
        raise ValueError('Elmer numerical controls require the selected sole HeatSolve solver')
    simulation = sections.get('simulation')
    if simulation is None or solvers[index][1].get('exec solver','always').strip('"')!='always':
        raise ValueError('Elmer numerical controls require a Simulation and HeatSolve active throughout')
    linear = settings['linear']
    keys = {}
    if linear['method']=='Banded':
        keys.update({'Linear System Solver':'Direct','Linear System Direct Method':'Banded'})
    else:
        keys.update({'Linear System Solver':'Iterative','Linear System Iterative Method':linear['method'],
            'Linear System Preconditioning':linear['preconditioner'],'Linear System Max Iterations':linear['max_iterations'],
            'Linear System Convergence Tolerance':linear['tolerance'],'Linear System Abort Not Converged':'True',
            'Linear System Residual Output':1})
    if 'nonlinear' in settings:
        controls = settings['nonlinear']
        keys.update({'Nonlinear System Max Iterations':controls['max_iterations'],
                     'Nonlinear System Convergence Tolerance':controls['tolerance']})
    solver_end,solver_additions = _configure(solvers[index],keys)
    schedule = settings.get('time')
    simulation_keys = {}
    if 'max output level' not in simulation[1]:
        simulation_keys['Max Output Level'] = 4
    elif scalar(simulation[1]['max output level'].removeprefix('integer '))<4:
        raise ValueError('Elmer numerical run evidence requires Max Output Level of at least 4')
    if schedule is not None:
        time_keys = {'restart file','timestep size','timestep function'}
        if time_keys.intersection(simulation[1]) or any(key.startswith(('timestep sizes(','timestep intervals(','output intervals('))
                                                       for key in simulation[1]):
            raise ValueError('Managed Elmer time control requires a single schedule without a restart selector')
        simulation_keys.update({'Simulation Type':'Transient','Timestepping Method':'BDF','BDF Order':1,
            'Timestep Sizes':schedule['time_step'],'Timestep Intervals':schedule['steps'],
            'Output Intervals':schedule['output_interval'],
            'Adaptive Timestepping':'True' if schedule['mode']=='adaptive' else 'False'})
        if schedule['mode']=='adaptive':
            simulation_keys.update({'Adaptive Time Error':schedule['error_tolerance'],
                'Adaptive Min Timestep':schedule['min_step'],'Adaptive Max Timestep':schedule['max_step']})
    simulation_end,simulation_additions = _configure(simulation,simulation_keys)
    if not solver_additions and not simulation_additions:
        return original
    additions = {solver_end:solver_additions,simulation_end:simulation_additions}
    return ''.join(''.join(additions.get(index,[]))+line+'\n' for index,line in enumerate(text.splitlines())).encode('utf-8')


def observed(settings, log):
    text = log.decode('utf-8',errors='replace').replace('\r\n','\n')
    result = {'heat_solver':settings['heat_solver']}
    linear = settings['linear']
    if linear['method']=='CG':
        rows = re.findall(r'^[ \t]*(\d+)[ \t]+([0-9.]+E[+-]\d+)[ \t]*$',text,re.M|re.I)
        if not rows or max(int(iteration) for iteration,_ in rows)>linear['max_iterations']:
            raise ValueError('Elmer did not report the configured iterative solution')
        result['maximum_linear_iterations'] = max(int(iteration) for iteration,_ in rows)
    nonlinear = settings.get('nonlinear')
    if nonlinear is not None:
        sweeps = re.split(r'HeatSolve:\s+TEMPERATURE ITERATION\s+1\b',text)[1:]
        changes = [re.findall(r'HeatSolve:[ \t]+Relative Change[ \t]*:[ \t]*(\S+)',sweep) for sweep in sweeps]
        if not changes or any(not row or len(row)>nonlinear['max_iterations'] or scalar(row[-1])>nonlinear['tolerance']
                              for row in changes):
            raise ValueError('Elmer heat iterations did not reach the declared nonlinear tolerance')
        result.update(maximum_nonlinear_iterations=max(map(len,changes)),
                      worst_final_relative_change=max(scalar(row[-1]) for row in changes))
    schedule = settings.get('time')
    if schedule is not None:
        times = re.findall(r'^MAIN: Time: (\d+)/(\d+):\s+(\S+)',text,re.M)
        if len(times)!=schedule['steps'] or any(int(index)!=sequence or int(total)!=len(times)
                 or not math.isclose(scalar(time),sequence*schedule['time_step'],rel_tol=6e-4)
                 for sequence,(index,total,time) in enumerate(times,1)):
            raise ValueError('Elmer did not complete the declared time schedule')
        trials = re.findall(r'^Adaptive\(cum,ddt,err\):[ \t]+\S+[ \t]+(\S+)[ \t]+(\S+)',text,re.M)
        if bool(trials)!=(schedule['mode']=='adaptive'):
            raise ValueError('Elmer adaptive stepping evidence disagrees with its declaration')
        result.update(steps=len(times),last_time=scalar(times[-1][2]),adaptive_trials=len(trials))
    return result
