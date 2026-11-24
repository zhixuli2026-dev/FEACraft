"""Compile one declared isotropic dielectric into a native Elmer electrostatic SIF."""

import math
import re

from .attempt_workspace import checked_path
from . import case_boundary_inputs as schema
from .case_boundary_inputs import confirmed, scalar
from .case_materials import quantity_si
from .casedict import CASE_DICT_PATH, parse_casedict
from .compose_artifacts import record
from .elmer_joule import _by_kind, _native_sections


PROCEDURE=('statelecsolve','statelecsolver')
VACUUM_PERMITTIVITY=8.8541878128e-12


def _native_id(value):
    literal=(value or '').removeprefix('integer ')
    return int(literal) if re.fullmatch('[1-9][0-9]{0,8}',literal) else None


def _plan(text, relative):
    sections=_native_sections(text,model='Electrostatics')
    simulation=sections.get('simulation',(None,{}))[1]
    if (simulation.get('simulation type','').strip('"') not in {'steady','steady state'}
            or simulation.get('coordinate system','').strip('"')!='cartesian 3d'):
        raise ValueError('Managed electrostatics requires a steady Cartesian 3D SIF')
    bodies=_by_kind(sections,'body')
    equations=_by_kind(sections,'equation')
    solvers=_by_kind(sections,'solver')
    if len(bodies)!=1 or len(equations)!=1 or len(solvers)!=1:
        raise ValueError('Managed electrostatics needs one dielectric body, equation and solver')
    body_id,(_,body)=next(iter(bodies.items()))
    equation_id,(_,equation)=next(iter(equations.items()))
    solver_id,(solver_end,solver)=next(iter(solvers.items()))
    expected_procedure=' '.join('"'+part+'"' for part in PROCEDURE)
    if (' '.join(solver.get('procedure','').removeprefix('file ').split())!=expected_procedure
            or solver.get('variable','potential').strip('"')!='potential'
            or solver.get('exec solver','always').strip('"')!='always'
            or _native_id(solver.get('variable dofs','1'))!=1
            or _native_id(body.get('equation'))!=equation_id
            or _native_id(equation.get('active solvers(1)'))!=solver_id):
        raise ValueError('Managed electrostatics needs an active scalar StatElecSolve Potential equation')
    materials=_by_kind(sections,'material')
    material_id=_native_id(body.get('material'))
    if material_id not in materials:
        raise ValueError('Managed electrostatics needs a native material on the dielectric body')
    material_end,material=materials[material_id]
    keys={'relative permittivity','permittivity'} & material.keys()
    additions={}

    def add(index,statement):
        additions.setdefault(index,[]).append('  '+statement+'\n')

    if keys:
        if len(keys)!=1 or not math.isclose(scalar(material[next(iter(keys))].removeprefix('real ')),
                                             relative,rel_tol=1e-12,abs_tol=0):
            raise ValueError('Native Relative Permittivity conflicts with the declared dielectric')
    else:
        add(material_end,f'Relative Permittivity = Real {relative:.17g}')

    for label in ('Calculate Electric Field','Calculate Electric Energy'):
        value=solver.get(label.lower())
        if value is None:
            add(solver_end,label+' = Logical True')
        elif value.removeprefix('logical ')!='true':
            raise ValueError('Native electrostatic output conflicts with '+label)
    constant_end,constants=sections.get('constants',(None,{}))
    vacuum=VACUUM_PERMITTIVITY
    tail=''
    key='permittivity of vacuum'
    if key in constants:
        vacuum=scalar(constants[key].removeprefix('real '))
        if vacuum<=0:
            raise ValueError('Native Permittivity Of Vacuum must be positive')
    elif constant_end is None:
        tail=f'\nConstants\n  Permittivity Of Vacuum = Real {vacuum:.17g}\nEnd\n'
    else:
        add(constant_end,f'Permittivity Of Vacuum = Real {vacuum:.17g}')
    return ({'formulation':'electrostatic','body':body_id,'solver':solver_id,'native_material':material_id,
             'relative_permittivity':relative,'vacuum_permittivity_f_m':vacuum},additions,tail)


def prepare(root,source,text):
    try:
        raw=checked_path(root,CASE_DICT_PATH).read_bytes()
        parsed=parse_casedict(raw.decode('utf-8'))
    except (FileNotFoundError,UnicodeDecodeError,ValueError):
        return
    problem=parsed.get('physicalProblem')
    models=problem.get('governingModels') if isinstance(problem,dict) else None
    model=models.get('electromagnetic') if isinstance(models,dict) else None
    if not isinstance(model,dict) or model.get('formulation')!='electrostatic':
        return
    active_materials=schema.active_material_names(parsed)
    if 'material' not in model and not active_materials:
        return
    if (str(model.get('enabled','true')).lower() not in {'true','yes'}
            or model.get('coupling','none')!='none'):
        raise ValueError('Managed electrostatics needs enabled electromagnetics with no current coupling')
    if active_materials:
        block=parsed[schema.BLOCK]
        errors=[]
        schema.validate(parsed,block,errors)
        if errors:
            raise ValueError(errors[0]['message'])
        active_load=block['loadCases'][block['activeLoadCase']]
        if set(schema.reference_names(active_load.get('activePhysics',[])))-{'electromagnetics'}:
            raise ValueError('The active electrostatic load requires native electromagnetics')
    name=schema.material_for_active_load(parsed,model.get('material'))
    materials=parsed.get('materials')
    entries=materials.get('entries') if isinstance(materials,dict) else None
    selected=entries.get(name) if isinstance(entries,dict) and isinstance(name,str) else None
    if not isinstance(selected,dict) or not confirmed(selected):
        raise ValueError('Managed electrostatics needs a confirmed named dielectric')
    relative=quantity_si('relativePermittivity',selected.get('relativePermittivity'))
    if relative<=0:
        raise ValueError('Managed electrostatics needs positive isotropic relative permittivity')
    plan,_,_=_plan(text,relative)
    source['files'][CASE_DICT_PATH]=record(raw)
    source['electrostatic']={**plan,'material':name}
    if active_materials:
        source['electrostatic']['load_case']=parsed[schema.BLOCK]['activeLoadCase']


def render(original,selection):
    text=original.decode('utf-8-sig')
    plan,additions,tail=_plan(text,selection['relative_permittivity'])
    if plan!={key:selection[key] for key in plan}:
        raise ValueError('Elmer electrostatic binding changed before launch')
    return (''.join(''.join(additions.get(index,[]))+line+'\n' for index,line in enumerate(text.splitlines()))+tail).encode('utf-8')
