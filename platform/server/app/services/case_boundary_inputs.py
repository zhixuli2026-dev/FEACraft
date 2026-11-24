"""Typed boundary, initial field and load declarations in caseDict."""

import math
import re
import uuid

from .casedict_schema import _bool, _enum, _error, _require_dict, finite_vector, interface_pair, valid_cell_zone


BLOCK = 'boundariesLoadsInitials'
ROLES = {'inlet', 'outlet', 'wall', 'support', 'loadSurface', 'symmetry', 'farField', 'electrode', 'heatSource'}
UNITS = {
    'velocity': {'m/s', 'mm/s'}, 'pressure': {'Pa', 'kPa', 'm2/s2'}, 'displacement': {'m', 'mm'},
    'force': {'N', 'kN'}, 'heatFlux': {'W/m2'}, 'temperature': {'K', 'degC'}, 'voltage': {'V'},
    'current': {'A', 'mA'}, 'impedance': {'Ohm', 'Pa*s/m'}, 'convection': {'W/m2/K', 'W/(m2*K)'},
}
VECTORS = {'velocity', 'displacement', 'force'}
ALIASES = {'velocityInlet': 'velocity', 'pressureOutlet': 'pressure'}
PROFILES = {'constant', 'table', 'function', 'harmonic', 'ramp', 'pulse'}
INITIALIZATIONS = {'uniform', 'mapped', 'restart', 'analytical', 'fromMeasurement'}
ACTIVE_PHYSICS = {'fluid', 'solidMechanics', 'heatTransfer', 'electromagnetics', 'acoustics', 'particles',
                  'chemistry', 'phaseChange', 'porousMedia', 'radiation', 'meshMotion'}


def scalar(value):
    if not isinstance(value, (int, float, str)) or isinstance(value, bool):
        raise ValueError('需要有限标量')
    try:
        number = float(value)
    except (ValueError, OverflowError) as error:
        raise ValueError('需要有限标量') from error
    if not math.isfinite(number):
        raise ValueError('需要有限标量')
    return number


def boundary_value(kind, value):
    if kind in VECTORS:
        try:
            return finite_vector(value)
        except (ValueError, OverflowError) as error:
            raise ValueError('需要三个有限分量') from error
    return scalar(value)


def confirmed(entry):
    return entry.get('confirmed') is True or str(entry.get('confirmed')).lower() in {'true', 'yes'}


def linear_initial(field, entry):
    if (not isinstance(entry, dict) or set(entry)-{'model','axis','value','slope','origin','unit'}
            or entry.get('model') != 'linear' or entry.get('axis') not in ('x','y','z')):
        raise ValueError('解析初值需要 linear 模型和 x/y/z 轴')
    quantity = finite_vector if field in {'U','displacement'} else scalar
    return entry['axis'], scalar(entry.get('origin',0)), quantity(entry.get('value')), quantity(entry.get('slope'))


def measurement_source(source):
    if not isinstance(source,dict) or set(source)-{'path','coordinateUnit','maxDistance'}:
        raise ValueError('测量初值需要 CSV 文件、坐标单位和最大距离')
    path = source.get('path')
    if not isinstance(path,str) or re.fullmatch(r'system/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.csv',path) is None:
        raise ValueError('测量初值 CSV 应位于当前运行目录的 system 下')
    unit = source.get('coordinateUnit','m')
    if unit not in ('m','mm'):
        raise ValueError('测量坐标单位需要 m 或 mm')
    distance = scalar(source.get('maxDistance'))
    if distance <= 0:
        raise ValueError('测量初值 maxDistance 必须大于零，单位 m')
    return path,unit,distance


def measurement_columns(field, entry):
    if not isinstance(entry,dict) or set(entry)-{'columns','unit'}:
        raise ValueError('测量初值需要字段列名与单位')
    columns = reference_names(entry.get('columns'))
    if len(columns) != (3 if field in {'U','displacement'} else 1):
        raise ValueError('列数量与测量字段的分量不一致')
    return columns


def physical_type(entry):
    kind = entry.get('physicalType')
    return ALIASES.get(kind, kind) if isinstance(kind, str) else None


def convection_values(entry):
    coefficient = scalar(entry.get('value'))
    unit = entry.get('unit')
    if coefficient <= 0 or not isinstance(unit, str) or unit not in UNITS['convection']:
        raise ValueError('对流换热系数必须为正，单位为 W/m2/K')
    ambient = entry.get('ambientTemperature')
    if not isinstance(ambient, dict) or set(ambient) != {'value', 'unit'} or ambient['unit'] not in ('K','degC'):
        raise ValueError('对流换热需要环境温度，单位为 K 或 degC')
    kelvin = scalar(ambient['value']) + (273.15 if ambient['unit'] == 'degC' else 0)
    if not math.isfinite(kelvin) or kelvin <= 0:
        raise ValueError('环境绝对温度必须大于零')
    if (entry.get('role') not in ('wall', 'heatSource') or entry.get('timeProfile', 'constant') != 'constant'
            or entry.get('condition', 'externalWallHeatFluxTemperature') != 'externalWallHeatFluxTemperature'):
        raise ValueError('对流换热需要壁面角色和恒定换热系数')
    return coefficient, kelvin


def acoustic_impedance(entry):
    impedance = scalar(entry.get('value'))
    if (impedance <= 0 or entry.get('unit') != 'Pa*s/m' or entry.get('role') not in ('outlet','farField')
            or entry.get('timeProfile','constant') != 'constant'
            or entry.get('condition','acousticWaveTransmissive') != 'acousticWaveTransmissive'):
        raise ValueError('声阻抗需要正的恒定 Pa*s/m 值和 outlet 或 farField 边界')
    return impedance


def electrical_impedance(entry):
    resistance=scalar(entry.get('value'))
    if (resistance<=0 or entry.get('unit')!='Ohm' or entry.get('role')!='electrode'
            or entry.get('timeProfile','constant')!='constant'
            or entry.get('condition','externalConductivity')!='externalConductivity'):
        raise ValueError('电极阻抗需要正的恒定电阻值，单位 Ohm')
    potential=entry.get('externalPotential')
    if not isinstance(potential,dict) or set(potential)!={'value','unit'} or potential['unit']!='V':
        raise ValueError('电极阻抗需要 externalPotential，单位 V')
    return resistance,scalar(potential['value'])


def profile_table(value, kind):
    # Reassemble nested parentheses locally to keep historical case fingerprints stable.
    text = ' '.join(value) if isinstance(value, list) and all(isinstance(item, str) for item in value) else value
    if not isinstance(text, str) or not text.startswith('(') or not text.endswith(')'):
        raise ValueError('需要形如 ((0 value) (1 value)) 的时间表')
    rows = []
    cursor = 0
    body = text[1:-1]
    for row in re.finditer(r'\(\s*([^\s()]+)\s+(\([^()]*\)|[^\s()]+)\s*\)', body):
        if body[cursor:row.start()].strip():
            raise ValueError('时间表含有无法读取的行')
        time = scalar(row[1])
        if time < 0 or (rows and time <= rows[-1][0]):
            raise ValueError('时间表秒数须非负且严格递增')
        rows.append((time, boundary_value(kind, row[2])))
        cursor = row.end()
    if body[cursor:].strip() or not 2 <= len(rows) <= 4096:
        raise ValueError('时间表需要 2 到 4096 行')
    return rows


def profile_data(entry, kind):
    profile = entry.get('timeProfile', 'constant')
    if not isinstance(profile, str) or profile not in PROFILES:
        raise ValueError('未知时间 profile')
    data = entry.get('profileData', {})
    if not isinstance(data, dict):
        raise ValueError('profileData 必须是字典')
    if profile == 'constant':
        if data:
            raise ValueError('constant 无需 profileData')
        return profile, {}
    if profile == 'table':
        if set(data) != {'values'}:
            raise ValueError('table 需要 profileData.values')
        return profile, {'values': profile_table(data['values'], kind)}
    if profile == 'function':
        if not {'type', 'slope'} <= data.keys() or data['type'] != 'linear' or set(data) - {'type', 'slope'}:
            raise ValueError('function 需要 type linear 与每秒变化量 slope')
        return profile, {'slope':boundary_value(kind, data['slope'])}
    if profile == 'harmonic':
        if not {'frequency', 'amplitude'} <= data.keys() or set(data) - {'frequency', 'amplitude', 'phase'}:
            raise ValueError('harmonic 需要 frequency [Hz]、amplitude 及可选 phase [rad]')
        frequency = scalar(data['frequency'])
        if frequency <= 0:
            raise ValueError('harmonic 频率必须为正')
        return profile, {'frequency':frequency, 'amplitude':boundary_value(kind, data['amplitude']),
                         'phase':scalar(data.get('phase', 0))}
    required = {'start', 'end', 'target'} | ({'rise', 'fall'} if profile == 'pulse' else set())
    if set(data) != required:
        raise ValueError(f'{profile} 需要 {sorted(required)}')
    start, end = scalar(data['start']), scalar(data['end'])
    if start < 0 or end <= start:
        raise ValueError('profile 结束时间需晚于非负开始时间')
    result = {'start':start, 'end':end, 'target':boundary_value(kind, data['target'])}
    if profile == 'pulse':
        rise, fall = scalar(data['rise']), scalar(data['fall'])
        if rise <= 0 or fall <= 0 or not start < start+rise <= end-fall < end:
            raise ValueError('pulse 需要正的上升和下降时长，且两段不得交叉')
        result.update(rise=rise, fall=fall)
    return profile, result


def _validate_boundaries(boundaries, errors):
    for name, entry in boundaries.items():
        path = f'{BLOCK}.boundarySets.{name}'
        if not valid_cell_zone(name) or not isinstance(entry, dict):
            _error(errors, path, 'type', '边界需要有效名称和字典块')
            continue
        _enum(entry, 'role', ROLES, path, errors)
        _enum(entry, 'physicalType', UNITS.keys() | ALIASES.keys(), path, errors)
        if 'confirmed' in entry:
            flag = entry['confirmed']
            if isinstance(flag, str) and flag.lower() in {'yes', 'no'}:
                flag = flag.lower() == 'yes'
            _bool(flag, f'{path}.confirmed', errors)
        for reference in ('geometryEntity', 'patch', 'region', 'field', 'material'):
            if reference in entry and (not isinstance(entry[reference], str) or not entry[reference].strip()):
                _error(errors, f'{path}.{reference}', 'type', '需要非空名称')
        _enum(entry, 'condition', {'fixedValue', 'fixedGradient', 'zeroGradient', 'noSlip',
                                   'externalWallHeatFluxTemperature', 'acousticWaveTransmissive',
                                   'externalConductivity'}, path, errors)
        kind = physical_type(entry)
        if kind not in UNITS:
            if {'value', 'unit', 'timeProfile', 'profileData'} & entry.keys() and 'physicalType' not in entry:
                _error(errors, f'{path}.physicalType', 'required', '物理边界值需要 physicalType')
            continue
        if 'value' in entry:
            try:
                boundary_value(kind, entry['value'])
            except ValueError as error:
                _error(errors, f'{path}.value', 'quantity', str(error))
        elif confirmed(entry) and entry.get('timeProfile') != 'table':
            _error(errors, f'{path}.value', 'required', '已确认的物理边界需要 value')
        unit = entry.get('unit')
        if 'unit' in entry and (not isinstance(unit, str) or unit not in UNITS[kind]):
            _error(errors, f'{path}.unit', 'unit', f'{kind} 可用单位 {sorted(UNITS[kind])}')
        if kind == 'convection' and confirmed(entry):
            try:
                convection_values(entry)
            except ValueError as error:
                _error(errors, path, 'convection', str(error))
        if kind == 'impedance' and unit in ('Pa*s/m','Ohm') and confirmed(entry):
            try:
                validate_impedance=acoustic_impedance if unit=='Pa*s/m' else electrical_impedance
                validate_impedance(entry)
            except ValueError as error:
                _error(errors, path, 'impedance', str(error))
        try:
            profile_data(entry, kind)
        except ValueError as error:
            _error(errors, f'{path}.timeProfile', 'profile', str(error))


def reference_names(value):
    items = interface_pair(value)
    items = [items] if isinstance(items, str) and items else items
    if not isinstance(items, list) or any(not valid_cell_zone(item) for item in items):
        raise ValueError('需要由有效名称组成的列表')
    if len(items) != len(set(items)):
        raise ValueError('不得重复引用名称')
    return items


def active_material_names(case):
    block = case.get(BLOCK)
    active = block.get('activeLoadCase') if isinstance(block, dict) else None
    if not active:
        return []
    loads = block.get('loadCases')
    load = loads.get(active) if isinstance(loads, dict) and isinstance(active, str) else None
    if not isinstance(load, dict):
        raise ValueError('活跃载荷需要引用当前算例的命名载荷')
    return reference_names(load.get('activeMaterials', []))


def material_for_active_load(case, default):
    names = active_material_names(case)
    if len(names) > 1:
        raise ValueError('当前控制模型只能从活跃载荷选择一个材料')
    return names[0] if names else default


def _validate_initial(initial, errors):
    path = f'{BLOCK}.initialConditions'
    _enum(initial, 'initialization', INITIALIZATIONS, path, errors)
    mode = initial.get('initialization')
    fields = initial.get('fields')
    if isinstance(fields, dict):
        for name, entry in fields.items():
            value = entry.get('value') if isinstance(entry, dict) else entry
            if not valid_cell_zone(name):
                _error(errors, f'{path}.fields.{name}', 'name', '初始字段名称无效')
                continue
            if mode == 'uniform':
                try:
                    if name in {'U', 'displacement'} or (name not in {'p', 'T', 'alpha', 'voltage'} and (
                            isinstance(value, list) or isinstance(value, str) and value.startswith('('))):
                        finite_vector(value)
                    else:
                        scalar(value)
                except (ValueError, OverflowError) as error:
                    _error(errors, f'{path}.fields.{name}', 'quantity', str(error))
            if mode == 'analytical':
                try:
                    linear_initial(name, entry)
                except (ValueError, OverflowError) as error:
                    _error(errors, f'{path}.fields.{name}', 'quantity', str(error))
            if mode == 'fromMeasurement':
                try:
                    measurement_columns(name,entry)
                except ValueError as error:
                    _error(errors, f'{path}.fields.{name}', 'value', str(error))
    elif fields is not None:
        try:
            reference_names(fields)
        except ValueError as error:
            _error(errors, f'{path}.fields', 'reference', str(error))
        if mode == 'uniform':
            _error(errors, f'{path}.fields', 'type', 'uniform 初值需要字段及值的字典')
    if mode == 'analytical' and (not isinstance(fields, dict) or not fields):
        _error(errors, f'{path}.fields', 'type', 'analytical 初值需要有配置的字段字典')
    if mode == 'fromMeasurement':
        if not isinstance(fields, dict) or not fields:
            _error(errors, f'{path}.fields', 'type', 'fromMeasurement 初值需要有配置的字段字典')
        try:
            measurement_source(initial.get('source'))
        except ValueError as error:
            _error(errors, f'{path}.source', 'reference', str(error))
    if mode in ('mapped', 'restart'):
        source = _require_dict(initial, 'source', f'{path}.source', errors, required=True)
        if source is None:
            return
        try:
            time = scalar(source.get('time'))
            attempt = source.get('attemptId')
            if time < 0 or not isinstance(attempt, str) or str(uuid.UUID(attempt)) != attempt:
                raise ValueError('需要有效 attemptId 及非负时间')
        except ValueError:
            _error(errors, f'{path}.source', 'reference', '需要有效 attemptId 与非负 time [s]')


def validate(parsed, block, errors):
    boundaries = _require_dict(block, 'boundarySets', f'{BLOCK}.boundarySets', errors)
    initial = _require_dict(block, 'initialConditions', f'{BLOCK}.initialConditions', errors)
    loads = _require_dict(block, 'loadCases', f'{BLOCK}.loadCases', errors)
    if boundaries is not None:
        _validate_boundaries(boundaries, errors)
    if initial is not None:
        _validate_initial(initial, errors)
    materials = parsed.get('materials')
    materials = materials.get('entries') if isinstance(materials, dict) else None
    refs = {'activeBoundaries':set(boundaries or {}),
            'activeMaterials':set(materials) if isinstance(materials, dict) else set(), 'activePhysics':ACTIVE_PHYSICS}
    for name, entry in (loads or {}).items():
        path = f'{BLOCK}.loadCases.{name}'
        if not valid_cell_zone(name) or not isinstance(entry, dict):
            _error(errors, path, 'type', '载荷需要有效名称和字典块')
            continue
        if 'description' in entry and not isinstance(entry['description'], str):
            _error(errors, f'{path}.description', 'type', '描述必须是字符串')
        for field, known in refs.items():
            if field not in entry:
                continue
            try:
                missing = set(reference_names(entry[field])) - known
                if missing:
                    raise ValueError('引用未定义的选项 ' + '、'.join(sorted(missing)))
            except ValueError as error:
                _error(errors, f'{path}.{field}', 'reference', str(error))
    active = block.get('activeLoadCase')
    if 'activeLoadCase' in block and (not isinstance(active, str) or active not in (loads or {})):
        _error(errors, f'{BLOCK}.activeLoadCase', 'reference', '需要引用当前算例的命名载荷')
