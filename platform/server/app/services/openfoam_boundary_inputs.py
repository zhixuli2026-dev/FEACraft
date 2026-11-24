"""Render declared field inputs for a single-region OpenFOAM case."""

import gzip
import math
import re

from . import case_boundary_inputs as schema
from .casedict import parse_casedict
from .casedict_schema import finite_vector, valid_cell_zone


FIELDS = {
    'U': ('volVectorField', '0 1 -1 0 0 0 0'), 'p': ('volScalarField', '0 2 -2 0 0 0 0'),
    'T': ('volScalarField', '0 0 0 1 0 0 0'), 'pa': ('volScalarField', '1 -1 -2 0 0 0 0'),
}
SOLVER_FIELDS = {
    'icoFoam': {'U','p'}, 'pimpleFoam': {'U','p'},
    'laplacianFoam': {'T'}, 'acousticFoam': {'pa'},
    'buoyantBoussinesqSimpleFoam': {'U','T'}, 'buoyantBoussinesqPimpleFoam': {'U','T'},
}
_FIELD_NAMES = {'velocity': 'U', 'pressure': 'p', 'temperature': 'T'}
_BOUNDARY_FIELDS = {**_FIELD_NAMES,'heatFlux':'T','convection':'T','impedance':'pa'}
_PHYSICAL_TYPES = {**{field:kind for kind,field in _FIELD_NAMES.items()},'pa':'pressure'}
_NATIVE_PATCH_TYPES = {'empty', 'symmetry', 'symmetryPlane', 'wedge', 'cyclic'}


def _bound_active_materials(case, solver):
    from . import openfoam_cht, openfoam_cht_materials, openfoam_conduction_material, openfoam_fluid_thermophysics

    if solver == 'icoFoam':
        from .solve_artifacts import declared_viscosity
        return declared_viscosity(case) is not None
    if solver == openfoam_conduction_material.SOLVER:
        return openfoam_conduction_material.selection(case) is not None
    if solver in openfoam_fluid_thermophysics.SOLVERS.values():
        return openfoam_fluid_thermophysics.selection(case) is not None
    if solver == openfoam_cht.SOLVER:
        return openfoam_cht_materials.selection(case)
    return False


def active_declarations(parsed, solver=None):
    block = parsed.get(schema.BLOCK, {})
    if not isinstance(block, dict):
        return {}, {}
    errors = []
    schema.validate(parsed, block, errors)
    if errors:
        issue = errors[0]
        raise ValueError(issue['path'] + ' ' + issue['message'])
    selected = block.get('activeLoadCase')
    loads = block.get('loadCases', {})
    if loads and not selected:
        raise ValueError('选择 activeLoadCase 后再提交求解')
    boundaries = block.get('boundarySets', {})
    if selected:
        load = loads[selected]
        physics = {'acoustics'} if solver == 'acousticFoam' else {'fluid','heatTransfer'}
        if set(schema.reference_names(load.get('activePhysics', []))) - physics:
            raise ValueError('当前 OpenFOAM 载荷仅支持当前求解器的原生物理和边界切换')
        if schema.active_material_names(parsed) and not _bound_active_materials(parsed,solver):
            raise ValueError('当前 OpenFOAM 载荷的材料尚未绑定到原生控制模型')
        names = schema.reference_names(load.get('activeBoundaries', []))
        boundaries = {name: boundaries[name] for name in names}
    return boundaries, block.get('initialConditions', {})


def _add(left, right, factor=1.):
    value = (tuple(a + factor*b for a, b in zip(left, right)) if isinstance(left, tuple)
             else left + factor*right)
    for component in value if isinstance(value, tuple) else (value,):
        schema.scalar(component)
    return value


def _literal(value):
    if isinstance(value, tuple):
        return '(' + ' '.join(repr(component) for component in value) + ')'
    return repr(value)


def _quantity_transform(kind, unit, parsed, *, difference=False, field=None):
    if kind == 'velocity':
        return lambda value: tuple(component / 1000 for component in value) if unit == 'mm/s' else value
    if kind == 'temperature':
        return lambda value: _temperature(value, unit, difference)
    if field == 'pa':
        if unit not in (None,'Pa','kPa'):
            raise ValueError('声压 pa 需要 Pa 或 kPa')
        return lambda value: schema.scalar(value * (1000 if unit == 'kPa' else 1))
    if unit in (None, 'm2/s2'):
        return lambda value: value
    problem = parsed.get('physicalProblem', {})
    density = schema.scalar(problem.get('referenceDensity') if isinstance(problem, dict) else None)
    if density <= 0:
        raise ValueError('将 Pa 转换为 OpenFOAM 运动学压力需要正的 referenceDensity')
    return lambda value: schema.scalar(value / density * (1000 if unit == 'kPa' else 1))


def _temperature(value, unit, difference):
    kelvin = schema.scalar(value + 273.15 if unit == 'degC' and not difference else value)
    if not difference and kelvin <= 0:
        raise ValueError('OpenFOAM 温度需高于绝对零度')
    return kelvin


def _fixed_entries(entry, kind, parsed, end_time, field):
    convert = _quantity_transform(kind, entry.get('unit'), parsed, field=field)
    profile, data = schema.profile_data(entry, kind)
    if profile == 'table':
        points = [(time, convert(value)) for time, value in data['values']]
    else:
        value = convert(schema.boundary_value(kind, entry.get('value')))
        if profile == 'constant':
            return f'type fixedValue; value uniform {_literal(value)};'
        convert_difference = _quantity_transform(kind, entry.get('unit'), parsed, difference=True, field=field)
        if profile == 'harmonic':
            amplitude = convert_difference(data['amplitude'])
            if kind == 'temperature':
                _temperature(value-abs(amplitude), 'K', False)
            offset = schema.scalar(-data['phase'] / (2 * math.pi) / data['frequency'])
            return ('type uniformFixedValue; uniformValue { type sine; '
                    f"frequency {data['frequency']!r}; scale {_literal(amplitude)}; "
                    f'level {_literal(value)}; t0 {offset!r}; }}')
        if profile == 'function':
            final = _add(value, convert_difference(data['slope']), end_time)
            if kind == 'temperature':
                _temperature(final, 'K', False)
            points = [(0., value), (end_time, final)]
        else:
            start, end, target = data['start'], data['end'], convert(data['target'])
            if profile == 'ramp':
                points = [(start, value), (end, target)]
            else:
                points = [(start, value), (start+data['rise'], target), (end-data['fall'], target), (end, value)]
                if points[1][0] == points[2][0]:
                    points.pop(2)
    rows = ' '.join(f'({time!r} {_literal(value)})' for time, value in points)
    return 'type uniformFixedValue; uniformValue { type table; outOfBounds clamp; values ('+rows+'); }'


def _native_condition(entry, field, kind, patch, parsed, cfg):
    role = entry.get('role')
    native_type = patch['type']
    if role == 'symmetry':
        if native_type not in {'symmetry', 'symmetryPlane'} or kind or 'condition' in entry:
            raise ValueError('对称声明必须与原生对称 patch 一致')
        return f'type {native_type};'
    if native_type not in {'patch', 'wall'} or native_type == 'wall' and role not in (None, 'wall', 'heatSource'):
        raise ValueError('物理边界角色与原生 OpenFOAM patch 不一致')
    if kind == 'heatFlux':
        from .openfoam_heat_flux import boundary
        return boundary(entry,parsed,cfg['solver'])
    if kind == 'convection':
        from .openfoam_heat_flux import convection
        return convection(entry,parsed,cfg)
    if kind == 'impedance':
        if cfg['solver'] != 'acousticFoam' or native_type != 'patch':
            raise ValueError('声阻抗需要 acousticFoam 的原生出口 patch')
        speed = schema.scalar(schema.acoustic_impedance(entry) / cfg['acoustic_density_kg_m3'])
        if speed <= 0:
            raise ValueError('声阻抗传播速度必须为正')
        return f'type acousticWaveTransmissive; advectiveSpeed {speed!r}; value uniform 0;'
    condition = entry.get('condition', 'fixedValue')
    if condition == 'fixedValue' and kind in _FIELD_NAMES:
        return _fixed_entries(entry, kind, parsed, cfg['end_time'], field)
    if kind or condition not in {'zeroGradient', 'noSlip'} or (condition == 'noSlip' and field != 'U'):
        raise ValueError('需要受支持的物理值或显式 zeroGradient/noSlip 条件')
    return f'type {condition};'


def _field_dictionary(field, internal, patches):
    kind, dimensions = FIELDS[field]
    return field_dictionary(field, kind, dimensions, internal, patches)


def field_dictionary(field, kind, dimensions, internal, patches):
    patch_blocks = '\n'.join(f'    {name} {{ {body} }}' for name, body in patches.items())
    return (f'FoamFile {{ version 2.0; format ascii; class {kind}; object {field}; }}\n'
            f'dimensions [{dimensions}];\ninternalField {internal};\n'
            f'boundaryField\n{{\n{patch_blocks}\n}}\n').encode('ascii')


def _normalize(value):
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    words = ' '.join(value) if isinstance(value, list) else value
    tokens = re.findall(r'[^\s()\[\]]+|[()\[\]]', words)
    result = []
    for token in tokens:
        try:
            result.append(schema.scalar(token))
        except ValueError:
            result.append(token)
    return result


def _existing(root, relative):
    path = root/relative
    compressed = path.with_name(path.name+'.gz')
    if path.is_file() and compressed.is_file():
        raise ValueError('OpenFOAM 初值同时存在压缩和普通版本：'+relative)
    if path.is_file() or compressed.is_file():
        stream = path.open('rt', encoding='utf-8') if path.is_file() else gzip.open(compressed, 'rt', encoding='utf-8')
        with stream:
            text = stream.read(512 * 1024 + 1)
        if len(text) > 512 * 1024:
            raise ValueError('OpenFOAM 初值超出可核验的字典大小：'+relative)
        return parse_casedict(text)
    return None


def _native_scalar(root, run_dir, name, dimensions):
    native = _existing(root, run_dir+'/constant/transportProperties') or {}
    raw = native.get(name)
    if isinstance(raw, list):
        match = re.fullmatch(r'\[([^]]+)\]\s*(\S+)', ' '.join(raw))
        if not match or [schema.scalar(part) for part in match[1].split()] != dimensions:
            raise ValueError('transportProperties.'+name+' 的量纲与当前求解器不符')
        raw = match[2]
    value = schema.scalar(raw)
    if value <= 0:
        raise ValueError('transportProperties.'+name+' 必须为正')
    return value


def native_configuration(root, run_dir, parsed, solver, *, thermal_diffusivity=None):
    properties = {'laplacianFoam':('convection','DT',[0,2,-1,0,0,0,0],'convection_diffusivity'),
                  'acousticFoam':('impedance','rho',[1,-3,0,0,0,0,0],'acoustic_density_kg_m3')}
    if solver not in properties:
        return {}
    kind, name, dimensions, key = properties[solver]
    block = parsed.get(schema.BLOCK)
    boundaries = block.get('boundarySets') if isinstance(block, dict) else None
    if not isinstance(boundaries, dict) or not any(
            isinstance(entry, dict) and schema.physical_type(entry) == kind for entry in boundaries.values()):
        return {}
    active, _ = active_declarations(parsed, solver)
    if not any(schema.physical_type(entry) == kind for entry in active.values()):
        return {}
    if kind=='convection' and thermal_diffusivity is not None:
        return {key:thermal_diffusivity}
    return {key:_native_scalar(root, run_dir, name, dimensions)}


def _field_conditions(cfg, parsed, declarations, fields, patches_by_name):
    conditions = {field:{} for field in fields}
    for name, entry in declarations.items():
        kind = schema.physical_type(entry)
        expected_field = 'pa' if cfg['solver'] == 'acousticFoam' and kind == 'pressure' else _BOUNDARY_FIELDS.get(kind)
        field = entry.get('field', expected_field)
        patch_name = entry.get('patch', entry.get('geometryEntity', name))
        if (field not in fields or (kind and expected_field != field) or 'region' in entry
                or patch_name not in patches_by_name or patch_name in conditions[field]):
            raise ValueError('边界需要唯一对应当前网格 patch 与已声明初值字段：'+name)
        if not schema.confirmed(entry):
            raise ValueError('请确认求解边界：'+name)
        if cfg['solver']=='buoyantBoussinesqSimpleFoam' and entry.get('timeProfile','constant')!='constant':
            raise ValueError('稳态求解只接受 constant 边界 profile')
        conditions[field][patch_name] = _native_condition(entry, field, kind, patches_by_name[patch_name],parsed,cfg)
    return conditions


def _complete_conditions(field, conditions, patches_by_name):
    for name, patch in patches_by_name.items():
        if not valid_cell_zone(name):
            raise ValueError('无法安全生成不规则名称的原生边界：'+name)
        if name in conditions:
            continue
        if patch['type'] not in _NATIVE_PATCH_TYPES:
            raise ValueError('生成初值需要明确给出每个物理边界：'+field+'/'+name)
        conditions[name] = 'type '+patch['type']+';'


def _required_fields(root, run_dir, cfg, provided):
    supported = SOLVER_FIELDS[cfg['solver']]
    buoyancy_fields = {'p_rgh'} if cfg['solver'].startswith('buoyantBoussinesq') else set()
    for field in supported | buoyancy_fields:
        path = run_dir+'/0/'+field
        if field not in provided and not any((root/(path+suffix)).is_file() for suffix in ('','.gz')):
            raise ValueError('缺少求解器必需的初值场：'+path)


def source_fields(cfg, initial):
    supported = SOLVER_FIELDS.get(cfg['solver'])
    if supported is None:
        raise ValueError('当前求解器尚无算例边界和初值映射')
    if initial.get('initialization') in {'analytical','fromMeasurement'}:
        fields = initial.get('fields',{})
    else:
        fields = supported if 'fields' not in initial else schema.reference_names(initial['fields'])
    if not fields or set(fields) - supported:
        raise ValueError('初值源选择包含当前求解器不支持的字段')
    return sorted(fields)


def analytical_specs(cfg, initial, parsed):
    formulas = {}
    for field in source_fields(cfg,initial):
        entry = initial['fields'][field]
        axis,origin,value,slope = schema.linear_initial(field,entry)
        kind = _PHYSICAL_TYPES[field]
        unit = entry.get('unit')
        if unit is not None and (not isinstance(unit,str) or unit not in schema.UNITS[kind]):
            raise ValueError('解析初值单位与字段不匹配：'+field)
        convert = _quantity_transform(kind,unit,parsed,field=field)
        difference = _quantity_transform(kind,unit,parsed,difference=True,field=field)
        formulas[field] = {'model':'linear','axis':axis,'origin_m':origin,
                           'value':convert(value),'slope_per_m':difference(slope)}
    return formulas


def source_conditions(root, run_dir, cfg, parsed, declarations, initial, patches):
    fields = source_fields(cfg, initial)
    patches_by_name = {patch['name']:patch for patch in patches}
    conditions = _field_conditions(cfg, parsed, declarations, fields, patches_by_name)
    for field in fields:
        relative = run_dir+'/0/'+field
        if any((root/(relative+suffix)).is_file() for suffix in ('','.gz')):
            raise ValueError('发布源初值与已有 OpenFOAM 文件冲突：'+relative)
        _complete_conditions(field, conditions[field], patches_by_name)
    _required_fields(root, run_dir, cfg, fields)
    return conditions


def render_source_fields(run_dir, fields, conditions):
    return {run_dir+'/0/'+field:_field_dictionary(field, internal, conditions[field])
            for field, internal in fields.items()}


def field_inputs(root, run_dir, cfg, parsed, declarations, initial, patches):
    supported = SOLVER_FIELDS.get(cfg['solver'])
    if supported is None:
        raise ValueError('当前求解器尚无算例边界和初值映射')
    if initial.get('initialization') != 'uniform' or not isinstance(initial.get('fields'), dict):
        raise ValueError('缺少可执行的 OpenFOAM 初值声明')
    fields = initial['fields']
    if not fields or set(fields) - supported:
        raise ValueError('当前 OpenFOAM 求解器的均匀初值字段可为 '+', '.join(sorted(supported)))
    patches_by_name = {patch['name']:patch for patch in patches}
    conditions = _field_conditions(cfg, parsed, declarations, fields, patches_by_name)
    generated = {}
    for field, entry in fields.items():
        value = entry.get('value') if isinstance(entry, dict) else entry
        kind = _PHYSICAL_TYPES[field]
        unit = entry.get('unit') if isinstance(entry, dict) else None
        if unit is not None and (not isinstance(unit, str) or unit not in schema.UNITS[kind]):
            raise ValueError('初值单位与字段不匹配：'+field)
        initial_value = _quantity_transform(kind, unit, parsed, field=field)(schema.boundary_value(kind, value))
        relative = run_dir+'/0/'+field
        current = _existing(root, relative)
        if current is not None:
            header = current.get('FoamFile', {})
            body = current.get('boundaryField', {})
            if (not isinstance(header, dict) or header.get('class') != FIELDS[field][0] or header.get('object') != field
                    or _normalize(current.get('dimensions', '')) != _normalize('['+FIELDS[field][1]+']')
                    or _normalize(current.get('internalField', '')) != _normalize('uniform '+_literal(initial_value))
                    or not isinstance(body, dict)
                    or any(_normalize(body.get(name, {})) != _normalize(parse_casedict(text))
                           for name, text in conditions[field].items())):
                raise ValueError('原生 OpenFOAM 初值或边界与算例声明冲突：'+relative)
            continue
        _complete_conditions(field, conditions[field], patches_by_name)
        generated[relative] = _field_dictionary(field, 'uniform '+_literal(initial_value), conditions[field])
    _required_fields(root, run_dir, cfg, fields)
    return generated
