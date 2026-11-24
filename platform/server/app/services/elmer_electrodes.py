"""Compile confirmed DC electrodes into a private Elmer SIF."""

import math
import re

from . import case_boundary_inputs as schema
from .attempt_workspace import checked_path
from .casedict import CASE_DICT_PATH, parse_casedict
from .compose_artifacts import record
from .elmer_electrostatic import PROCEDURE as ELECTROSTATIC


PROCEDURE = ('statcurrentsolve', 'statcurrentsolver')
VECTORIZED = ('statcurrentsolvevec', 'statcurrentsolver')
PROCEDURES = {PROCEDURE,VECTORIZED}
KINDS = {'voltage': 'Potential', 'current': 'Current Density', 'impedance': 'External Conductivity'}


def _electrode_kind(entry):
    if not isinstance(entry,dict):
        return None
    kind=schema.physical_type(entry)
    if kind=='impedance' and entry.get('unit')!='Ohm':
        return None
    return kind if kind in KINDS else None


def declarations(root, *, material_bound=False):
    try:
        raw = checked_path(root, CASE_DICT_PATH).read_bytes()
        parsed = parse_casedict(raw.decode('utf-8'))
    except (FileNotFoundError, UnicodeDecodeError, ValueError):
        return None
    block = parsed.get(schema.BLOCK)
    entries = block.get('boundarySets') if isinstance(block, dict) else None
    if not isinstance(entries, dict) or not any(_electrode_kind(entry) for entry in entries.values()):
        return None
    errors = []
    schema.validate(parsed, block, errors)
    if errors:
        raise ValueError(errors[0]['message'])
    loads = block.get('loadCases', {})
    active = block.get('activeLoadCase')
    if loads and not active:
        raise ValueError('Select activeLoadCase for the declared Elmer electrodes')
    if active:
        load = loads[active]
        if (not material_bound and schema.active_material_names(parsed)) or (
                set(schema.reference_names(load.get('activePhysics', []))) - {'electromagnetics'}):
            raise ValueError('Elmer electrodes use the native electrical material and physics')
        entries = {name: entries[name] for name in schema.reference_names(load.get('activeBoundaries', []))}
    if not any(_electrode_kind(entry) for entry in entries.values()):
        return None
    electrodes = []
    for name, entry in sorted(entries.items()):
        kind = _electrode_kind(entry)
        if kind is None or entry.get('role') != 'electrode' or not schema.confirmed(entry):
            raise ValueError('Managed Elmer electrodes require confirmed voltage, current or resistive impedance')
        profile, _ = schema.profile_data(entry, kind)
        unit = entry.get('unit')
        tag = entry.get('patch', '')
        if profile != 'constant' or unit not in schema.UNITS[kind] or not re.fullmatch('[1-9][0-9]{0,8}', tag):
            raise ValueError('An Elmer electrode needs a constant SI quantity and numeric native boundary tag')
        electrode={'name':name,'kind':kind,'tag':int(tag)}
        if kind=='impedance':
            value,external=schema.electrical_impedance(entry)
            electrode['external_potential_v']=external
        else:
            condition='fixedValue' if kind=='voltage' else 'fixedGradient'
            if entry.get('condition',condition)!=condition:
                raise ValueError('Elmer electrode condition disagrees with the specified quantity')
            value=schema.scalar(entry['value'])*(.001 if unit=='mA' else 1.)
        electrode['value_si']=value
        electrodes.append(electrode)
    if len({row['tag'] for row in electrodes}) != len(electrodes):
        raise ValueError('Managed Elmer electrodes must reference distinct boundary tags')
    return raw, electrodes


def _cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def _area(points):
    edges = [tuple(b-a for a, b in zip(start, end))
             for start, end in zip(points, points[1:]+points[:1])]
    turns = [_cross(a, b) for a, b in zip(edges, edges[1:]+edges[:1])]
    normal = turns[0]
    length = math.hypot(*normal)
    if not length or not math.isfinite(length):
        raise ValueError('Elmer electrode contains a degenerate face')
    unit = [value/length for value in normal]
    for turn in turns:
        dot = sum(a*b for a, b in zip(unit, turn))
        size = math.hypot(*turn)
        if not math.isfinite(size) or dot <= 0 or abs(size-dot) > 1e-10*size:
            raise ValueError('Elmer electrode requires planar convex faces')
    if len(points) == 3:
        return length/2
    diagonal = tuple(b-a for a, b in zip(points[0], points[2]))
    return (length + math.hypot(*_cross(diagonal, edges[2])))/2


def _areas(root, mesh, tags, scale):
    faces = {tag: [] for tag in tags}
    seen_faces = set()
    with checked_path(root, f'{mesh}/mesh.boundary').open(encoding='ascii') as stream:
        for line in stream:
            fields = line.split()
            if not fields:
                continue
            _, tag, parent1, parent2, kind, *nodes = map(int, fields)
            if tag not in tags:
                continue
            face = frozenset(nodes)
            if (kind not in (303, 404) or len(nodes) != kind % 100 or len(face) != len(nodes)
                    or min(parent1, parent2) != 0 or max(parent1, parent2) <= 0 or face in seen_faces):
                raise ValueError('Elmer electrode requires unique external triangle or quadrilateral faces')
            faces[tag].append(nodes)
            seen_faces.add(face)
    if any(not group for group in faces.values()):
        raise ValueError('Elmer electrode boundary tag has no native faces')
    needed = {node for group in faces.values() for face in group for node in face}
    points = {}
    with checked_path(root, f'{mesh}/mesh.nodes').open(encoding='ascii') as stream:
        for line in stream:
            fields = line.split()
            if not fields or int(fields[0]) not in needed:
                continue
            node, _, *xyz = fields
            if int(node) in points or len(xyz) != 3:
                raise ValueError('Elmer electrode coordinates are malformed')
            points[int(node)] = tuple(schema.scalar(scale*schema.scalar(value)) for value in xyz)
    if needed != points.keys():
        raise ValueError('Elmer electrode references absent mesh nodes')
    return {tag: math.fsum(_area([points[node] for node in face]) for face in group)
            for tag, group in faces.items()}


def _native_settings(text):
    """Read only settings that affect electrode binding in a validated SIF."""
    section = None
    simulation, assigned, indices = {}, set(), set()
    for raw in text.splitlines():
        line = raw.split('!', 1)[0].strip().lower()
        if not line:
            continue
        if section is None:
            section = line
            if section.startswith('boundary condition '):
                index = int(section.split()[-1])
                if not index or index in indices:
                    raise ValueError('Elmer boundary condition identifiers must be unique and positive')
                indices.add(index)
                targets = None
            continue
        if line == 'end':
            if section.startswith('boundary condition '):
                if targets is None or assigned & targets:
                    raise ValueError('Native Elmer boundary sections must have disjoint explicit targets')
                assigned.update(targets)
            section = None
            continue
        if '=' not in line:
            continue
        key, value = (' '.join(part.split()) for part in line.split('=', 1))
        if section == 'simulation' and (key.startswith('coordinate ') or key == 'simulation type'):
            if key in simulation:
                raise ValueError('Elmer electrode coordinate settings must be unique')
            simulation[key] = value
        if section.startswith('boundary condition ') and key.startswith('target '):
            selector = re.fullmatch(r'target boundaries\((\d+)\)', key)
            ids = value.removeprefix('integer ').split()
            if (targets is not None or selector is None or int(selector[1]) != len(ids) or not ids
                    or any(not re.fullmatch('[1-9][0-9]{0,8}', item) for item in ids)):
                raise ValueError('Native Elmer boundary sections need explicit numeric targets')
            targets = set(map(int, ids))
    if (simulation.get('coordinate system', '').strip('"') != 'cartesian 3d'
            or simulation.get('simulation type', '').strip('"') not in {'steady', 'steady state'}
            or simulation.keys() - {'coordinate system', 'coordinate scaling', 'simulation type'}):
        raise ValueError('Managed Elmer electrodes require steady Cartesian 3D coordinates and uniform scaling')
    scale = schema.scalar(simulation.get('coordinate scaling', '1').removeprefix('real '))
    if scale <= 0:
        raise ValueError('Elmer mesh coordinate scaling must be positive')
    return scale, assigned, indices


def render(original, electrodes):
    sections = []
    for electrode in electrodes:
        key = KINDS[electrode['kind']]
        section=(f"Boundary Condition {electrode['index']}\n"
                 f"  Target Boundaries(1) = {electrode['tag']}\n"
                 f"  {key} = Real {electrode['native_value']:.17g}\n")
        if electrode['kind']=='impedance':
            section+=f"  External Potential = Real {electrode['external_potential_v']:.17g}\n"
        sections.append(section+'End\n')
    return original.rstrip() + b'\n\n' + '\n'.join(sections).encode('ascii')


def prepare(root, source, text, procedures):
    selected = declarations(root,material_bound='electrostatic' in source)
    if selected is None:
        return
    raw, electrodes = selected
    if ELECTROSTATIC in procedures and (procedures & PROCEDURES or any(row['kind']!='voltage' for row in electrodes)):
        raise ValueError('Elmer electrostatic electrodes require voltage boundaries on a dedicated electrostatic solver')
    if any(row['kind']=='impedance' for row in electrodes):
        if procedures & PROCEDURES != {VECTORIZED}:
            raise ValueError('Elmer resistive impedance requires StatCurrentSolveVec / StatCurrentSolver')
        if re.search(r'^\s*check\s+keywords\s+fatal\s*(?:!.*)?$',text,re.I|re.M):
            raise ValueError('Elmer 26.2 resistive impedance requires CHECK KEYWORDS Warn for External Conductivity')
    scale, assigned, indices = _native_settings(text)
    tags = {row['tag'] for row in electrodes}
    if assigned & tags:
        raise ValueError('An Elmer electrode tag already has a native SIF boundary condition')
    areas = _areas(root, source['mesh'], tags, scale)
    available = (index for index in range(1, len(indices)+len(electrodes)+1) if index not in indices)
    for row in electrodes:
        area = schema.scalar(areas[row['tag']])
        if area <= 0:
            raise ValueError('Elmer electrode area must be positive')
        native=row['value_si']
        if row['kind']=='current':
            native/=area
        elif row['kind']=='impedance':
            native=1./area/native
            if native<=0:
                raise ValueError('Elmer electrode conductance per area must be positive')
        row.update(index=next(available), area_m2=area, native_value=schema.scalar(native))
    source['files'][CASE_DICT_PATH] = record(raw)
    source['electrodes'] = {'values': electrodes}
