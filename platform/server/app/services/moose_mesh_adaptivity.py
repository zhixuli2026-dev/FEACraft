"""Match declared MOOSE refinement and read the native mesh/DOF history."""

import csv
import math
import re

from .attempt_workspace import checked_path
from .case_boundary_inputs import scalar
from .casedict import CASE_DICT_PATH, parse_casedict
from .casedict_schema import finite_vector
from .compose_artifacts import record


_NUMBER = r'([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d{1,3})?)'
NAME = r'[A-Za-z][A-Za-z0-9_]*'
GOAL_EXPRESSION = (r"'if\("+r'&'.join(rf'\({axis}>={_NUMBER}\)&\({axis}<={_NUMBER}\)' for axis in 'xyz')
                   +rf",abs\(measured-{_NUMBER}\),0\)'")
_BASE_KEYS = {'cycles','maxHLevel','min','max'}
_MODES = {'h','p','hp'}


def _geometry(mode, value):
    if not isinstance(mode,str) or mode not in _MODES:
        raise ValueError('MOOSE mesh adaptivity needs native h, p or hp refinement')
    if any(re.fullmatch('[1-9][0-9]{0,2}',str(value[key])) is None for key in ('cycles','maxHLevel')):
        raise ValueError('MOOSE mesh adaptivity cycles and maxHLevel must be positive integers up to 999')
    lower,upper = (list(finite_vector(value[key])) for key in ('min','max'))
    if any(lo>=hi for lo,hi in zip(lower,upper)):
        raise ValueError('MOOSE refinement box needs increasing bounds in metres')
    return {'mode':mode,'cycles':int(value['cycles']),'maximum_h_level':int(value['maxHLevel']),
            'min_m':lower,'max_m':upper}


def declared(mesh):
    if not isinstance(mesh,dict) or 'mooseAdaptivity' not in mesh:
        return None
    mode, value = mesh.get('adaptivity'), mesh['mooseAdaptivity']
    required = _BASE_KEYS|{'goal','nativeRefinement'} if mode=='goalOriented' else _BASE_KEYS
    if not isinstance(value,dict) or set(value)!=required:
        raise ValueError('MOOSE mesh adaptivity needs h, p or hp with cycles, maxHLevel, min and max')
    if mode!='goalOriented':
        return _geometry(mode,value)
    settings = _geometry(value['nativeRefinement'],value)
    goal = value['goal']
    if (not isinstance(goal,dict) or set(goal)!={'postprocessor','value','absoluteTolerance'}
            or not isinstance(goal.get('postprocessor'),str) or not re.fullmatch(NAME,goal['postprocessor'])):
        raise ValueError('MOOSE goal needs a native PointValue name, target value and absoluteTolerance')
    tolerance=scalar(goal['absoluteTolerance'])
    if tolerance<=0:
        raise ValueError('MOOSE refinement goal requires a positive absolute tolerance')
    return {**settings,'native_mode':settings['mode'],'mode':mode,'goal':{
        'postprocessor':goal['postprocessor'],'target':scalar(goal['value']),'absolute_tolerance':tolerance}}


def _validate_goal(sections, native, marker, selected):
    goal = selected['goal']
    point = sections.get(('Postprocessors',goal['postprocessor']),{})
    if point.get('type')!='PointValue' or point.get('force_preaux')!='true':
        raise ValueError('MOOSE goal needs native PointValue before computing its refinement error')
    variable = marker.get('variable')
    auxiliary = sections.get(('AuxVariables',variable),{})
    kernels = [values for path,values in sections.items() if len(path)==2 and path[0]=='AuxKernels'
               and values.get('variable')==variable]
    if (marker.get('type')!='ValueThresholdMarker' or marker.get('third_state')!='DO_NOTHING'
            or scalar(marker.get('refine'))!=goal['absolute_tolerance']
            or auxiliary!={'family':'MONOMIAL','order':'CONSTANT'} or len(kernels)!=1):
        raise ValueError('MOOSE goal requires a threshold marker on the declared elemental error')
    kernel=kernels[0]
    function=sections.get(('Functions',kernel.get('function')),{})
    if (kernel.get('type')!='FunctionAux' or kernel.get('execute_on')!='TIMESTEP_END'
            or function.get('type')!='ParsedFunction' or function.get('symbol_names')!="'measured'"
            or function.get('symbol_values')!=f"'{goal['postprocessor']}'"):
        raise ValueError('MOOSE refinement error must measure the declared PointValue')
    expression=re.fullmatch(GOAL_EXPRESSION,function.get('expression',''))
    expected=[coordinate for axis in zip(selected['min_m'],selected['max_m']) for coordinate in axis]
    if (expression is None or list(map(float,expression.groups()))!=[*expected,goal['target']]
            or (native['adaptivity_type'],int(native['steps']),int(native['max_h_level']))
            !=(selected['native_mode'],selected['cycles'],selected['maximum_h_level'])):
        raise ValueError('MOOSE numerical goal, bounds or native cycles differ from caseDict')


def prepare(root,source,sections):
    native = sections.get(('Adaptivity',))
    try:
        raw = checked_path(root,CASE_DICT_PATH).read_bytes()
        mesh = parse_casedict(raw.decode('utf-8')).get('mesh')
    except (FileNotFoundError,UnicodeDecodeError,ValueError):
        raw,mesh = None,None
    selected = declared(mesh)
    native_aux = any(path[0] in {'Functions','AuxVariables','AuxKernels'} for path in sections)
    if native_aux and (selected is None or 'goal' not in selected):
        raise ValueError('MOOSE auxiliary goal controls require a declared mesh.mooseAdaptivity goal')
    if native is None and selected is None:
        return
    if native is None or sections['Executioner',].get('type') != 'Steady':
        raise ValueError('Managed MOOSE mesh adaptivity requires a native steady Adaptivity block')
    marker_name = native.get('marker')
    marker = sections.get(('Adaptivity','Markers',marker_name),{})
    if set(native) != {'adaptivity_type','steps','marker','max_h_level'}:
        raise ValueError('MOOSE mesh adaptivity requires explicit native mode, cycles and marker')
    if selected is not None and 'goal' in selected:
        _validate_goal(sections,native,marker,selected)
        settings=selected
    else:
        if marker.get('type')!='BoxMarker' or marker.get('inside')!='REFINE' or marker.get('outside')!='DO_NOTHING':
            raise ValueError('MOOSE mesh adaptivity requires a literal native refinement BoxMarker')
        settings = _geometry(native['adaptivity_type'],{'cycles':native['steps'],'maxHLevel':native['max_h_level'],
            'min':'('+marker.get('bottom_left','')[1:-1]+')','max':'('+marker.get('top_right','')[1:-1]+')'})
        if selected is not None and settings!=selected:
            raise ValueError('MOOSE native refinement settings differ from caseDict mesh.mooseAdaptivity')
    if native['adaptivity_type'] in {'p','hp'} and any(values.get('family') != 'HIERARCHIC' for path,values in sections.items()
                                               if len(path)==2 and path[0]=='Variables'):
        raise ValueError('MOOSE p and hp refine HIERARCHIC variables')
    metrics = {values.get('type'):path[-1] for path,values in sections.items()
               if len(path)==2 and path[0]=='Postprocessors' and
               (values.get('type')=='NumElements' and values.get('elem_filter')=='ACTIVE'
                or values.get('type')=='NumDOFs' and values.get('system')=='NL')}
    if set(metrics) != {'NumElements','NumDOFs'}:
        raise ValueError('MOOSE refinement history needs native active element and nonlinear DOF postprocessors')
    source['mesh_adaptivity'] = {**settings,'elements_column':metrics['NumElements'],'dofs_column':metrics['NumDOFs']}
    if raw is not None and selected is not None:
        source['files'][CASE_DICT_PATH] = record(raw)


def observed(settings, path):
    def count(value):
        number = float(value)
        if not number.is_integer() or number < 0:
            raise ValueError('MOOSE mesh count must be a nonnegative integer')
        return int(number)

    goal=settings.get('goal')
    with path.open(encoding='ascii',newline='') as stream:
        try:
            rows = [(count(row['time']),count(row[settings['elements_column']]),count(row[settings['dofs_column']]),
                     float(row[goal['postprocessor']]) if goal else None)
                    for row in csv.DictReader(stream)]
        except (TypeError,KeyError) as error:
            raise ValueError('MOOSE is missing native mesh history columns') from error
    history = [(step,cells,dofs,value) for step,cells,dofs,value in rows if step>0]
    if [step for step,_,_,_ in history] != list(range(1,settings['cycles']+2)):
        raise ValueError('MOOSE did not complete the requested refinement cycles')
    cells = [cells for _,cells,_,_ in history]
    dofs = [dofs for _,_,dofs,_ in history]
    mode=settings.get('native_mode',settings['mode'])
    if (min(cells+dofs)<1 or cells!=sorted(cells) or dofs!=sorted(dofs) or not goal and dofs[0]==dofs[-1]
            or mode=='p' and cells[0]!=cells[-1] or mode=='h' and not goal and cells[0]==cells[-1]):
        raise ValueError('MOOSE observed cells and degrees of freedom do not substantiate the requested refinement')
    result={'cells':cells,'dofs':dofs}
    if goal:
        values=[value for _,_,_,value in history]
        if not all(math.isfinite(value) for value in values):
            raise ValueError('MOOSE refinement goal history contains nonfinite values')
        met=[index for index,value in enumerate(values) if abs(value-goal['target'])<=goal['absolute_tolerance']]
        if not met or met!=list(range(met[0],len(values))):
            raise ValueError('Native refinement did not reach the declared goal within tolerance')
        reached=met[0]
        if cells[reached]!=cells[-1] or dofs[reached]!=dofs[-1]:
            raise ValueError('Native mesh kept refining after reaching the declared goal')
        result['goal']={'values':values,'first_met_cycle':reached,'final_absolute_error':abs(values[-1]-goal['target'])}
    return result
