"""Compile confirmed caseDict forces and displacements into named CCX node sets."""

import hashlib
import re

from . import case_boundary_inputs as schema
from .attempt_workspace import checked_path
from .casedict import CASE_DICT_PATH, parse_casedict
from .compose_artifacts import record


_MECHANICAL = {'force':'loadSurface','displacement':'support'}
_STATIC = re.compile(rb'^[ \t]*\*STATIC(?:,[^\r\n]*)?[ \t]*\r?$',re.I | re.M)
_KEYWORD = re.compile(rb'^[ \t]*\*(?!\*)',re.M)


def _managed_units(parsed):
    meta = parsed.get('caseMeta') or {}
    units = meta.get('units',{}) if isinstance(meta,dict) else None
    if not isinstance(units,dict) or units.get('length','m')!='m' or units.get('force','N')!='N':
        raise ValueError('Managed CalculiX loads require m and N in the native deck')


def selected(root, pressure_transfer=None):
    try:
        raw = checked_path(root,CASE_DICT_PATH).read_bytes()
        parsed = parse_casedict(raw.decode('utf-8'))
    except (FileNotFoundError,UnicodeDecodeError,ValueError) as error:
        if pressure_transfer is not None:
            raise ValueError('Pressure transfer requires readable case units') from error
        return None
    pressure_loads = []
    if pressure_transfer is not None:
        _managed_units(parsed)
        pressure_loads = [{'name':'openfoamPressure','kind':'force','node_set':pressure_transfer['node_set'],
                           'value_si':pressure_transfer['force_n']}]
    block = parsed.get(schema.BLOCK)
    boundaries = block.get('boundarySets') if isinstance(block,dict) else None
    if not isinstance(boundaries,dict) or not any(isinstance(entry,dict) and schema.physical_type(entry) in _MECHANICAL
                                                  for entry in boundaries.values()):
        return (raw,pressure_loads) if pressure_loads else None
    errors = []
    schema.validate(parsed,block,errors)
    if errors:
        raise ValueError(errors[0]['message'])
    load_cases = block.get('loadCases',{})
    active = block.get('activeLoadCase')
    if load_cases and not active:
        raise ValueError('Select activeLoadCase for the declared CalculiX loads')
    if active:
        load = load_cases[active]
        if set(schema.reference_names(load.get('activePhysics',[]))) - {'solidMechanics'}:
            raise ValueError('CalculiX mechanical loads require solid mechanics')
        if schema.active_material_names(parsed):
            problem = parsed.get('physicalProblem')
            models = problem.get('governingModels') if isinstance(problem,dict) else None
            model = models.get('solidMechanics') if isinstance(models,dict) else None
            if not isinstance(model,dict) or model.get('materialLaw')!='linearElastic':
                raise ValueError('CalculiX activeMaterials requires the declared linearElastic model')
        boundaries = {name:boundaries[name] for name in schema.reference_names(load.get('activeBoundaries',[]))}
    if not any(schema.physical_type(entry) in _MECHANICAL for entry in boundaries.values()):
        return (raw,pressure_loads) if pressure_loads else None
    _managed_units(parsed)
    loads = []
    for name,entry in sorted(boundaries.items()):
        kind = schema.physical_type(entry)
        if kind not in _MECHANICAL or entry.get('role')!=_MECHANICAL[kind] or not schema.confirmed(entry):
            raise ValueError('CalculiX requires confirmed displacement supports and force surfaces')
        profile,_ = schema.profile_data(entry,kind)
        if profile!='constant' or entry.get('unit') not in schema.UNITS[kind]:
            raise ValueError('CalculiX static loads require constant values and a declared unit')
        node_set = entry.get('patch',entry.get('geometryEntity',name))
        if not re.fullmatch('[A-Za-z_][A-Za-z0-9_-]{0,79}',node_set):
            raise ValueError('CalculiX load must reference a named native node set')
        scale = {'mm':.001,'kN':1000}.get(entry['unit'],1)
        vector = [schema.scalar(scale*value) for value in schema.boundary_value(kind,entry['value'])]
        loads.append({'name':name,'kind':kind,'node_set':node_set.upper(),'value_si':vector})
    return raw,loads+pressure_loads


def _node_sets(lines, targets, coordinates=None):
    definitions = {}
    nodes = set()
    section = name = None
    generated = False
    for line in lines:
        if line.startswith('*'):
            section,*options = line.upper().split(',')
            if coordinates is not None and (section=='*SYSTEM' or section=='*NODE' and
                    any(option.startswith('SYSTEM=') and option!='SYSTEM=R' for option in options)):
                raise ValueError('Conformal pressure transfer requires global Cartesian CalculiX nodes')
            name = next((option[5:] for option in options if option.startswith('NSET=')),None)
            generated = 'GENERATE' in options
            continue
        if section == '*NODE':
            identifier, *values = line.split(',')
            identifier = int(identifier)
            if coordinates is not None:
                if identifier in coordinates:
                    raise ValueError('Conformal transfer found a duplicate CalculiX node')
                local = (values[:3]+['0']*3)[:3]
                coordinates[identifier] = [schema.scalar((value or '0').upper().replace('D','E')) for value in local]
            nodes.add(identifier)
            if name:
                definitions.setdefault(name,[]).append(identifier)
        elif section == '*NSET' and name:
            items = [item.upper() for item in line.split(',') if item]
            parsed = [int(item) if item.isdigit() else item for item in items]
            if generated:
                if len(parsed) not in (2,3) or any(not isinstance(item,int) for item in parsed):
                    raise ValueError('Invalid native CalculiX NSET GENERATE data')
                first,last,*stride = parsed
                step = stride[0] if stride else 1
                if first<1 or last<first or step<1:
                    raise ValueError('Invalid native CalculiX NSET range')
                definitions.setdefault(name,[]).append(range(first,last+1,step))
            else:
                definitions.setdefault(name,[]).extend(parsed)

    def resolve(name, active):
        if name in active or name not in definitions:
            raise ValueError('Missing or cyclic native CalculiX node set: '+name)
        result = set()
        for member in definitions[name]:
            if isinstance(member,int):
                result.add(member)
            elif isinstance(member,range):
                selected = {identifier for identifier in nodes if identifier in member}
                count = (member.stop-1-member.start)//member.step + 1
                if len(selected)!=count:
                    raise ValueError('Generated CalculiX node set references undefined nodes: '+name)
                result.update(selected)
            else:
                result.update(resolve(member,active|{name}))
        if not result or not result <= nodes:
            raise ValueError('CalculiX node set is empty or references undefined nodes: '+name)
        return result

    return {name:resolve(name,set()) for name in targets}


def render(original, loads):
    static = _STATIC.search(original)
    after = _KEYWORD.search(original,static.end()) if static else None
    if after is None:
        raise ValueError('Managed CalculiX loads require a complete native static step')
    cards = []
    for kind,keyword in (('displacement','*BOUNDARY'),('force','*CLOAD')):
        group = [load for load in loads if load['kind']==kind]
        if not group:
            continue
        cards.append(keyword)
        for load in group:
            if 'node_forces' in load:
                cards.extend(f"{entry['node_id']},{axis},{value:.16g}" for entry in load['node_forces']
                             for axis,value in enumerate(entry['force_n'],1))
                continue
            for axis,value in enumerate(load['value_si'],1):
                if kind=='displacement':
                    cards.append(f"{load['node_set']},{axis},{axis},{value:.16g}")
                else:
                    cards.append(f"{load['node_set']},{axis},{value/load['node_count']:.16g}")
    return original[:after.start()] + ('\n'.join(cards)+'\n').encode('ascii') + original[after.start():]


def prepare(root, source, cards, lines, declaration):
    raw,loads = declaration
    keywords = [line.upper().split(',')[0] for line in cards]
    transferred = 'pressure_transfer' in source
    native_conflicts = {'*CLOAD'}
    if not transferred or any(load['kind']=='displacement' for load in loads):
        native_conflicts.add('*BOUNDARY')
    if transferred:
        native_conflicts.update({'*DLOAD','*DSLOAD'})
    if keywords.count('*STEP')!=1 or keywords.count('*STATIC')!=1 or native_conflicts.intersection(keywords):
        raise ValueError('Managed CalculiX loads require one static step with a single source for force and displacement')
    pressure = source.get('pressure_transfer',{})
    spatial = pressure.get('distribution')=='conformalNodal'
    coordinates = {} if spatial else None
    sets = _node_sets(lines,{load['node_set'] for load in loads},coordinates)
    seen = {'force':set(),'displacement':set()}
    for load in loads:
        members = sets[load['node_set']]
        group = seen[load['kind']]
        if group & members:
            raise ValueError('Managed CalculiX node sets overlap for the same load type')
        group.update(members)
        load['node_count'] = len(members)
        if spatial and load['name']=='openfoamPressure':
            from .calculix_pressure_transfer import match_nodes

            load['node_forces'] = match_nodes(pressure,members,coordinates)
    from .calculix_artifacts import render_input
    source['mechanical_loads'] = {'loads':loads}
    native = render_input(source,checked_path(root,source['input']).read_bytes())
    source['files'][CASE_DICT_PATH] = record(raw)
    source['mechanical_loads']['native_input_sha256'] = hashlib.sha256(native).hexdigest()
