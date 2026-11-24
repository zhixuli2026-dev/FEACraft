"""Generate and verify a constant-rate OpenFOAM multiple reference frame."""

import gzip
import math
import re

from .case_boundary_inputs import scalar
from .casedict import parse_casedict, tokenize
from .casedict_schema import finite_vector, valid_cell_zone


PATH = 'constant/MRFProperties'
SOLVER = 'pimpleFoam'
RATES = {'rad/s': 1.0, 'rpm': math.tau / 60}
NATIVE_KEYS = {'cellZone', 'active', 'origin', 'axis', 'omega', 'nonRotatingPatches'}


def selection(case):
    problem = case.get('physicalProblem')
    return isinstance(problem, dict) and 'rotatingFrame' in problem


def _axis(value):
    vector = finite_vector(value)
    scale = max(map(abs, vector))
    if not scale:
        raise ValueError('rotatingFrame.axis 必须是非零矢量')
    scaled = tuple(component / scale for component in vector)
    length = math.hypot(*scaled)
    return [component / length for component in scaled]


def _patches(value):
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        value = ' '.join(value)
    if not isinstance(value, str) or not value.startswith('(') or not value.endswith(')'):
        raise ValueError('nonRotatingPatches 必须是括号包围的边界名称列表')
    names = value[1:-1].split()
    if any(not valid_cell_zone(name) for name in names) or len(set(names)) != len(names):
        raise ValueError('nonRotatingPatches 需要不重复的有效边界名称')
    return sorted(names)


def declared(case):
    if not selection(case):
        return None
    problem = case['physicalProblem']
    frame = problem['rotatingFrame']
    if problem.get('coordinateFrame') != 'rotating' or not isinstance(frame, dict):
        raise ValueError('rotatingFrame 需要 coordinateFrame rotating 和有效的控制块')
    zone = frame.get('cellZone')
    if not valid_cell_zone(zone) or zone == 'FoamFile':
        raise ValueError('rotatingFrame.cellZone 需要有效的原生单元区域名称')
    rate = frame.get('angularVelocity')
    if not isinstance(rate, dict) or not isinstance(rate.get('unit'), str) or rate['unit'] not in RATES:
        raise ValueError('rotatingFrame.angularVelocity 单位应为 rad/s 或 rpm')
    omega = scalar(rate.get('value')) * RATES[rate['unit']]
    if not omega:
        raise ValueError('rotatingFrame.angularVelocity 必须是非零角速度')
    return {'cell_zone': zone, 'origin_m': list(finite_vector(frame.get('origin'))),
            'axis': _axis(frame.get('axis')), 'omega_rad_s': omega,
            'non_rotating_patches': _patches(frame.get('nonRotatingPatches', '()'))}


def requested(document):
    return declared(parse_casedict(document))


def dictionary(frame):
    zone = frame['cell_zone']
    origin = ' '.join(map(repr, frame['origin_m']))
    axis = ' '.join(map(repr, frame['axis']))
    patches = ' '.join(frame['non_rotating_patches'])
    return ('FoamFile { version 2.0; format ascii; class dictionary; object MRFProperties; }\n'
            f'{zone}\n{{\n    cellZone {zone};\n    active yes;\n    origin ({origin});\n'
            f'    axis ({axis});\n    omega {frame["omega_rad_s"]!r};\n'
            f'    nonRotatingPatches ({patches});\n}}\n').encode('ascii')


def _native(path, frame):
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rt', encoding='utf-8') as stream:
        text = stream.read(512 * 1024 + 1)
    if len(text) > 512 * 1024:
        raise ValueError('MRFProperties 超出可校验的字典大小')
    data = parse_casedict(text)
    zone = frame['cell_zone']
    native = data.get(zone)
    header = data.get('FoamFile')
    tokens = list(tokenize(text))
    blocks = [value.strip('"') for (_, value), next_token in zip(tokens, tokens[1:]) if next_token == ('brace', '{')]
    if (set(data) != {'FoamFile', zone} or not isinstance(native, dict) or set(native) - NATIVE_KEYS
            or blocks != ['FoamFile', zone] or not isinstance(header, dict)
            or header.get('class') != 'dictionary' or header.get('object') != 'MRFProperties'):
        raise ValueError('MRFProperties 需要一个与声明同名的原生旋转区域')
    # Native includes, expressions and duplicate controls cannot be bound to a literal declaration.
    controls = [value.strip('"') for previous, (kind, value) in zip(tokens, tokens[1:])
                if previous in {('brace', '{'), ('semi', ';')} and kind in {'word', 'string'}
                and value.strip('"') in NATIVE_KEYS]
    if len(controls) != len(set(controls)):
        raise ValueError('MRFProperties 包含重复的旋转区域控制项')
    omega = native.get('omega')
    if isinstance(omega, list) and len(omega) == 2 and omega[0] == 'constant':
        omega = omega[1]
    matched = (native.get('cellZone') == zone and str(native.get('active', 'true')).lower() in {'true', 'yes', 'on', '1'}
               and list(finite_vector(native.get('origin'))) == frame['origin_m']
               and all(math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-14)
                       for a, b in zip(_axis(native.get('axis')), frame['axis'], strict=True))
               and math.isclose(scalar(omega), frame['omega_rad_s'], rel_tol=1e-12)
               and _patches(native.get('nonRotatingPatches', '()')) == frame['non_rotating_patches'])
    if not matched:
        raise ValueError('MRFProperties 与声明的旋转区域、原点、轴、角速度或静止边界不一致')


def prepare(root, run_dir, solver, frame, *, require_native=False):
    if frame is None:
        return {}
    if solver != SOLVER:
        raise ValueError('声明的 MRF 目前使用串行 pimpleFoam')
    relative = run_dir + '/' + PATH
    present = [root / name for name in (relative, relative + '.gz') if (root / name).is_file()]
    if len(present) > 1:
        raise ValueError('MRFProperties 同时提供了普通和 gzip 版本')
    if present:
        _native(present[0], frame)
        return {}
    if require_native:
        raise ValueError('封存的旋转区域缺少 MRFProperties')
    return {relative: dictionary(frame)}


def observed(frame, log):
    zones = re.findall(r'^[ \t]*creating MRF zone:[ \t]+(\S+)[ \t]*$', log, flags=re.M | re.I)
    if zones != [frame['cell_zone']]:
        raise ValueError('原生日志未证明声明的 MRF 旋转区域')
    return {'method': 'MRF', 'cell_zone': zones[0]}
