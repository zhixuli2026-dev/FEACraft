"""Bind caseDict Joule heating to the registered Elmer current and heat solvers."""

import re

from . import elmer_electrical_interface
from .attempt_workspace import checked_path
from .case_boundary_inputs import scalar
from .casedict import CASE_DICT_PATH, parse_casedict
from .compose_artifacts import record


_CURRENT = '"statcurrentsolve" "statcurrentsolver"'
_HEAT = '"heatsolve" "heatsolver"'


def _native_sections(text, *, model='Joule heating'):
    sections = {}
    label = None
    for index, raw in enumerate(text.splitlines()):
        line = raw.split('!', 1)[0].strip().lower()
        if not line:
            continue
        if label is None:
            label, values = line, {}
        elif line == 'end':
            if label in sections:
                raise ValueError(model+' requires unique native SIF sections')
            sections[label] = index, values
            label = None
        elif '=' in line:
            key, value = (' '.join(part.split()) for part in line.split('=', 1))
            if key in values:
                raise ValueError(model+' requires unique native SIF settings')
            values[key] = value
    return sections


def _by_kind(sections, kind):
    return {int(label.split()[-1]):section for label,section in sections.items()
            if re.fullmatch(kind+r' [0-9]+', label)}


def _native_plan(sections, *, electrical_contact=False):
    simulation = sections.get('simulation',(None,{}))[1]
    if simulation.get('simulation type','').strip('"') not in {'steady','steady state'}:
        raise ValueError('Managed Elmer Joule heating requires a steady native SIF')
    solvers = _by_kind(sections, 'solver')
    bodies = _by_kind(sections, 'body')
    equations = _by_kind(sections, 'equation')
    body_count = len(bodies)>=2 if electrical_contact else len(bodies)==1
    if len(solvers)!=2 or not body_count or len(equations)!=1:
        raise ValueError('Joule heating needs one equation with current and heat solvers and a matching set of native bodies')
    procedures = {values.get('procedure','').removeprefix('file '):index for index,(_,values) in solvers.items()}
    if set(procedures)!={_CURRENT,_HEAT}:
        raise ValueError('Joule heating needs the stock Elmer static current and heat equations')
    current_id, heat_id = procedures[_CURRENT], procedures[_HEAT]
    equation_id,(_,equation) = next(iter(equations.items()))
    active = [(key,value.removeprefix('integer ').split()) for key,value in equation.items() if key.startswith('active solvers')]
    if (active != [('active solvers(2)',[str(current_id),str(heat_id)])]
            or any(body.get('equation','').removeprefix('integer ')!=str(equation_id) for _,body in bodies.values())):
        raise ValueError('Native Elmer equation must run current before heat on each body')
    for index,variable in ((current_id,'potential'),(heat_id,'temperature')):
        settings = solvers[index][1]
        if (settings.get('variable','').strip('"')!=variable
                or settings.get('exec solver','always').strip('"')!='always'
                or settings.get('variable dofs','1').removeprefix('integer ')!='1'):
            raise ValueError('Joule heating requires the native scalar Potential and Temperature solvers on each iteration')
    forces = _by_kind(sections,'body force')
    additions = {}
    generated_force = next(index for index in range(1,len(forces)+2) if index not in forces)
    current_end,current = solvers[current_id]
    flags = [(current_end,current,'calculate joule heating','Calculate Joule Heating')]
    materials = _by_kind(sections,'material')
    binding = []
    for body_id,(body_end,body) in sorted(bodies.items()):
        force_ref = body.get('body force')
        if force_ref is None:
            force_id = generated_force
            additions[body_end] = f'  Body Force = {force_id}\n'
        else:
            force_ref = force_ref.removeprefix('integer ')
            if re.fullmatch('[1-9][0-9]{0,8}',force_ref) is None or int(force_ref) not in forces:
                raise ValueError('Elmer body force reference is not present in the native SIF')
            force_id = int(force_ref)
        row = {'body':body_id,'body_force':force_id}
        if electrical_contact:
            material_ref = body.get('material','').removeprefix('integer ')
            if re.fullmatch('[1-9][0-9]{0,8}',material_ref) is None or int(material_ref) not in materials:
                raise ValueError('Elmer electrical interface body requires a native material')
            material = materials[int(material_ref)][1]
            conductivity = scalar(material.get('electric conductivity','').removeprefix('real '))
            if conductivity <= 0:
                raise ValueError('Elmer electrical interface requires positive scalar conductivity')
            row.update(material=int(material_ref),conductivity_s_m=conductivity)
        binding.append(row)
    force_ids = {row['body_force'] for row in binding}
    for force_id in sorted(force_ids & forces.keys()):
        end,settings = forces[force_id]
        flags.append((end,settings,'joule heat','Joule Heat'))
    for end,values,key,label in flags:
        native = values.get(key)
        if native is None:
            additions[end] = f'  {label} = Logical True\n'
        elif native.removeprefix('logical ') != 'true':
            raise ValueError('Native Elmer setting conflicts with requested '+label)
    new_force = [f'\nBody Force {generated_force}\n','  Joule Heat = Logical True\n','End\n'] if generated_force in force_ids else []
    selection = {'mode':'jouleHeating','current_solver':current_id,'heat_solver':heat_id}
    selection.update({'bodies':binding} if electrical_contact else binding[0])
    return selection,additions,new_force


def prepare(root, source, text):
    try:
        raw = checked_path(root,CASE_DICT_PATH).read_bytes()
        parsed = parse_casedict(raw.decode('utf-8'))
    except (FileNotFoundError,UnicodeDecodeError,ValueError):
        return
    interfaces = elmer_electrical_interface.declared(parsed)
    problem = parsed.get('physicalProblem')
    models = problem.get('governingModels') if isinstance(problem,dict) else None
    electrical = models.get('electromagnetic') if isinstance(models,dict) else None
    if not isinstance(electrical,dict) or electrical.get('coupling')!='jouleHeating':
        if interfaces:
            raise ValueError('Managed Elmer electrical interfaces require the steady Joule heating model')
        return
    flags = problem.get('coupledPhysics')
    if not isinstance(flags,dict) or any(str(flags.get(flag)).lower()!='true' for flag in ('electromagnetics','heatTransfer')):
        raise ValueError('Elmer Joule heating requires heatTransfer and electromagnetics enabled')
    if electrical.get('enabled','true') not in (True,'true','yes'):
        raise ValueError('Elmer Joule heating requires the electromagnetic model enabled')
    sections = _native_sections(text)
    selection,_,_ = _native_plan(sections,electrical_contact=bool(interfaces))
    if interfaces:
        selection['electrical_interfaces'] = elmer_electrical_interface.bind(
            root,source['mesh'],parsed,interfaces,_by_kind(sections,'body'),_by_kind(sections,'boundary condition'))
    source['files'][CASE_DICT_PATH] = record(raw)
    source['coupling'] = selection


def render(original, selection):
    text = original.decode('utf-8-sig')
    observed,additions,tail = _native_plan(_native_sections(text),electrical_contact='electrical_interfaces' in selection)
    if observed != {name:selection[name] for name in observed}:
        raise ValueError('Elmer Joule heating bindings changed before launch')
    lines = [additions.get(index,'')+line+'\n' for index,line in enumerate(text.splitlines())]
    return ''.join([*lines,*tail]).encode('utf-8')
