"""Bind typed Code_Aster controls to literal native command arguments and the message file."""

import ast
import math
import re

from .attempt_workspace import checked_path
from .case_boundary_inputs import scalar
from .casedict import CASE_DICT_PATH, parse_casedict
from .casedict_schema import interface_pair
from .compose_artifacts import record


def _tolerance(value):
    result=scalar(value)
    if not 0<result<1:
        raise ValueError('Code_Aster solver tolerances must be between zero and one')
    return result


def declared(value):
    if not isinstance(value,dict) or not {'linear'}<=value.keys()<={'linear','nonlinear'}:
        raise ValueError('Code_Aster needs linear settings and optional nonlinear static controls')
    linear=value['linear']
    if not isinstance(linear,dict) or set(linear)!={'method','relativeTolerance'} or linear['method']!='MUMPS':
        raise ValueError('Code_Aster linear control requires MUMPS and relativeTolerance')
    settings={'linear':{'method':'MUMPS','relative_tolerance':_tolerance(linear['relativeTolerance'])}}
    nonlinear=value.get('nonlinear')
    if 'nonlinear' in value:
        mandatory={'relativeTolerance','maxIterations','instants'}
        if not isinstance(nonlinear,dict) or not mandatory<=nonlinear.keys()<=mandatory|{'adaptive'}:
            raise ValueError('Code_Aster nonlinear settings require relativeTolerance, maxIterations and instants')
        limit=scalar(nonlinear['maxIterations'])
        if not limit.is_integer() or not 1<=limit<=2**31-1:
            raise ValueError('Code_Aster maxIterations must be a positive integer')
        items=interface_pair(nonlinear['instants'])
        if not isinstance(items,list) or not 2<=len(items)<=10000:
            raise ValueError('Code_Aster requires a finite list of at least two instants')
        instants=[scalar(item) for item in items]
        if instants[0]!=0 or any(right<=left for left,right in zip(instants,instants[1:])):
            raise ValueError('Code_Aster instants must start at zero and strictly increase')
        settings['nonlinear']={'relative_tolerance':_tolerance(nonlinear['relativeTolerance']),
                               'max_iterations':int(limit),'instants':instants}
        if 'adaptive' in nonlinear:
            native=nonlinear['adaptive']
            names=('initialStep','minimumStep','maximumStep','growthPercent')
            if not isinstance(native,dict) or set(native)!=set(names):
                raise ValueError('Code_Aster adaptive controls need initialStep, minimumStep, maximumStep and growthPercent')
            initial,minimum,maximum,growth=(scalar(native[name]) for name in names)
            if not 0<minimum<=initial<=maximum<=instants[-1] or initial!=instants[1] or growth<=0:
                raise ValueError('Code_Aster adaptive bounds must include the initial step, which must be the first positive instant')
            settings['nonlinear']['adaptive']={'initial_step':initial,'minimum_step':minimum,
                                               'maximum_step':maximum,'growth_percent':growth}
    return settings


def _call(value, name):
    return isinstance(value,ast.Call) and isinstance(value.func,ast.Name) and value.func.id==name


def _keywords(call):
    if call.args or any(item.arg is None for item in call.keywords):
        raise ValueError('Code_Aster numerical controls need explicit native keyword arguments')
    values={item.arg:item.value for item in call.keywords}
    if len(values)!=len(call.keywords):
        raise ValueError('Duplicate Code_Aster numerical keyword')
    return values


def _factor(arguments, name):
    call=arguments.get(name)
    if not _call(call,'_F'):
        raise ValueError('Code_Aster numerical control needs a literal '+name+' factor')
    return _keywords(call)


def _match(values, expected, name):
    for key,value in expected.items():
        try:
            node=values.get(key)
            actual=ast.literal_eval(node) if node is not None else None
            matches=(scalar(actual)==value if isinstance(value,(int,float)) else actual==value)
        except (ValueError,TypeError):
            matches=False
        if not matches:
            raise ValueError('Native Code_Aster '+name+'.'+key+' conflicts with numericalMethod.codeAster')


def _native_settings(raw, settings):
    try:
        program=ast.parse(raw)
    except SyntaxError as error:
        raise ValueError('Code_Aster numerical controls require a valid native command file') from error
    operations=[node for node in ast.walk(program) if any(_call(node,name) for name in ('MECA_STATIQUE','STAT_NON_LINE'))]
    command='STAT_NON_LINE' if 'nonlinear' in settings else 'MECA_STATIQUE'
    if len(operations)!=1 or not _call(operations[0],command):
        raise ValueError('Code_Aster numerical controls require one native '+command+' command')
    operation,=operations
    assignments={item.targets[0].id:item.value for item in program.body if isinstance(item,ast.Assign)
                 and len(item.targets)==1 and isinstance(item.targets[0],ast.Name)}
    if operation not in assignments.values():
        raise ValueError('Selected Code_Aster command must be a top-level assignment')
    arguments=_keywords(operation)
    linear=settings['linear']
    _match(_factor(arguments,'SOLVEUR'),{'METHODE':linear['method'],'RESI_RELA':linear['relative_tolerance']},'SOLVEUR')
    nonlinear=settings.get('nonlinear')
    if nonlinear is not None:
        targets={'ITER_GLOB_MAXI':nonlinear['max_iterations'],'RESI_GLOB_RELA':nonlinear['relative_tolerance']}
        _match(_factor(arguments,'CONVERGENCE'),targets,'CONVERGENCE')
        instant_arg=_factor(arguments,'INCREMENT').get('LIST_INST')
        schedule=assignments.get(instant_arg.id) if isinstance(instant_arg,ast.Name) else None
        adaptive=nonlinear.get('adaptive')
        if adaptive is not None:
            if not _call(schedule,'DEFI_LIST_INST'):
                raise ValueError('Code_Aster adaptive numerical controls need native DEFI_LIST_INST')
            automatic=_keywords(schedule)
            _match(automatic,{'METHODE':'AUTO'},'DEFI_LIST_INST')
            schedule_factor=_factor(automatic,'DEFI_LIST')
            _match(schedule_factor,{'PAS_INIT':adaptive['initial_step'],'PAS_MINI':adaptive['minimum_step'],
                         'PAS_MAXI':adaptive['maximum_step']},'DEFI_LIST')
            behavior=_factor(automatic,'ADAPTATION')
            _match(behavior,{'EVENEMENT':'TOUT_INST','MODE_CALCUL_TPLUS':'FIXE',
                             'PCENT_AUGM':adaptive['growth_percent']},'ADAPTATION')
            reference=schedule_factor.get('LIST_INST')
            schedule=assignments.get(reference.id) if isinstance(reference,ast.Name) else None
        if not _call(schedule,'DEFI_LIST_REEL'):
            raise ValueError('Code_Aster nonlinear numerical controls need literal DEFI_LIST_REEL instants')
        item=_keywords(schedule).get('VALE')
        if not isinstance(item,(ast.Tuple,ast.List)) or [scalar(ast.literal_eval(number)) for number in item.elts]!=nonlinear['instants']:
            raise ValueError('Native Code_Aster INCREMENT.LIST_INST differs from the declared instants')
    return command


def prepare(root,source):
    try:
        raw=checked_path(root,CASE_DICT_PATH).read_bytes()
        method=parse_casedict(raw.decode('utf-8')).get('numericalMethod')
    except (FileNotFoundError,UnicodeDecodeError,ValueError):
        return
    if not isinstance(method,dict) or 'codeAster' not in method:
        return
    settings=declared(method['codeAster'])
    plan=source['plan']
    if 'input' in plan.get('base',{}):
        raise ValueError('Declared Code_Aster numerical schedule requires a fresh study')
    command,=(name for name,kind in plan['inputs'].items() if kind=='comm')
    message,=(name for name,kind in plan['outputs'].items() if kind=='mess')
    operation=_native_settings(checked_path(root,command).read_bytes(),settings)
    source['numerical']={**settings,'command':operation,'message':message}
    source['files'][CASE_DICT_PATH]=record(raw)


def observed(root,settings):
    path=checked_path(root,settings['message'])
    text=path.read_text(encoding='utf-8',errors='replace')
    if not re.search(r"^\s+METHODE='MUMPS',",text,re.M) or 'librairie MUMPS' not in text:
        raise ValueError('Code_Aster message does not confirm its native MUMPS configuration')
    result={'linear_solver':'MUMPS'}
    nonlinear=settings.get('nonlinear')
    if nonlinear is not None:
        sections=re.split(r'^Instant de calcul:[ \t]+(\S+)[^\n]*\n',text,flags=re.M)[1:]
        instants,iterations,residuals=[],[],[]
        for moment,detail in zip(sections[::2],sections[1::2],strict=True):
            count=re.search(r"Nombre d'itérations de Newton\s*:\s*(\d+)",detail)
            residual=re.search(r'résidu de type <RESI_GLOB_RELA> vaut\s+(\S+)',detail)
            if count is not None and residual is not None and 'Critère(s) de convergence atteint(s)' in detail:
                instants.append(scalar(moment))
                iterations.append(int(count[1]))
                residuals.append(scalar(residual[1]))
        expected=nonlinear['instants'][1:]
        if (not instants or max(iterations)>nonlinear['max_iterations']
                or max(residuals)>nonlinear['relative_tolerance']):
            raise ValueError('Code_Aster message does not confirm the declared Newton convergence schedule')
        adaptive=nonlinear.get('adaptive')
        if adaptive is None:
            if len(instants)!=len(expected) or any(not math.isclose(a,b,rel_tol=1e-10) for a,b in zip(instants,expected)):
                raise ValueError('Code_Aster did not converge at every declared fixed instant')
        else:
            minimum,maximum=adaptive['minimum_step'],adaptive['maximum_step']
            cursor=0
            previous=0.
            sizes=[]
            for instant in instants:
                size=instant-previous
                end_of_interval=cursor<len(expected) and math.isclose(instant,expected[cursor],rel_tol=1e-10)
                if size<=0 or size>maximum*(1+1e-9) or (size<minimum*(1-1e-9) and not end_of_interval):
                    raise ValueError('Code_Aster used a load increment outside its declared adaptive range')
                cursor+=int(end_of_interval)
                previous=instant
                sizes.append(size)
            if cursor!=len(expected) or not math.isclose(instants[-1],expected[-1],rel_tol=1e-10):
                raise ValueError('Code_Aster did not complete the declared adaptive load schedule')
            result.update(minimum_time_step=min(sizes),maximum_time_step=max(sizes))
        result.update(algorithm='Newton-Raphson',converged_increments=len(instants),period=instants[-1],
                      maximum_iterations=max(iterations),worst_relative_residual=max(residuals))
    return result
