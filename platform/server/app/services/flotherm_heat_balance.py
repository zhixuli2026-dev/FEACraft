"""Evaluate the solved thermal balance of a generated homogeneous heat case."""
from collections import defaultdict
import hashlib
import math
import re

from .flotherm_foam_io import FoamFile
from .tree_metadata_native import checked, ordinary


SCHEMA = 'vibeflow.flotherm.heat-balance.v1'
MAX_INPUT = 16 * 1024 * 1024
NUMBER = r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?'


def _read_bytes(root, path, digests):
    source = checked(root, path)
    ordinary(source)
    with source.open('rb') as stream:
        data = stream.read(MAX_INPUT+1)
    if len(data) > MAX_INPUT:
        raise ValueError(f'热平衡输入超出大小限制：{path}')
    digests[path] = hashlib.sha256(data).hexdigest()
    return data


def _read(root, path, digests):
    return FoamFile(_read_bytes(root, path, digests), path)


def _items(data, pattern, label):
    match = re.fullmatch(r'(\d+)\s*\((.*)\)\s*;?', data, re.S)
    if match is None:
        raise ValueError(f'OpenFOAM {label} 列表无效')
    body = match[2]
    entries = re.findall(pattern, body)
    if len(entries) != int(match[1]) or re.sub(pattern, '', body).strip():
        raise ValueError(f'OpenFOAM {label} 数量或格式不一致')
    return entries


def _binary_end(source, offset):
    if source.text(offset) not in ('', ';'):
        raise ValueError(f'OpenFOAM 二进制列表含额外内容：{source.path}')


def _points(source):
    if source.binary:
        if source.class_name not in (b'vectorField', b'pointField'):
            raise ValueError('OpenFOAM 二进制 points 类型无效')
        values, offset = source.raw_list(source.scalar*3)
        _binary_end(source, offset)
        return values
    return [tuple(map(float, value.split())) for value in _items(
        source.text(), rf'\(({NUMBER}\s+{NUMBER}\s+{NUMBER})\)', 'points')]


def _faces(source, *, quadrilaterals=True):
    if source.binary:
        if source.class_name != b'faceCompactList':
            raise ValueError('OpenFOAM 二进制 faces 需要 faceCompactList')
        offsets, cursor = source.labels()
        labels, cursor = source.labels(cursor)
        _binary_end(source, cursor)
        if (len(offsets) < 2 or offsets[0] != 0 or offsets[-1] != len(labels)
                or any(b-a < 3 for a,b in zip(offsets, offsets[1:]))):
            raise ValueError('OpenFOAM 紧凑面偏移不匹配')
        faces = [tuple(labels[a:b]) for a,b in zip(offsets, offsets[1:])]
    else:
        faces = []
        for row in _items(source.text(), r'\d+\s*\(\s*\d+(?:\s+\d+){2,}\s*\)', 'faces'):
            count, values = row.split('(', 1)
            labels = tuple(map(int, values[:-1].split()))
            if int(count) != len(labels):
                raise ValueError('OpenFOAM 面顶点数量不符')
            faces.append(labels)
    if quadrilaterals and any(len(face) != 4 for face in faces):
        raise ValueError('OpenFOAM 紧凑面偏移不匹配或包含非四边形')
    return faces


def _labels(source, name):
    if source.binary:
        if source.class_name != b'labelList':
            raise ValueError(f'OpenFOAM 二进制 {name} 类型无效')
        values, offset = source.labels()
        _binary_end(source, offset)
        return values
    return list(map(int, _items(source.text(), r'\d+', name)))


def _snap_grid_points(points, precision):
    """Combine binary roundoff variants of the same observed coordinate plane."""
    coordinates = []
    for axis in _axes(points):
        span = axis[-1]-axis[0]
        if not math.isfinite(span):
            raise ValueError('OpenFOAM 网格坐标范围无效')
        slack = 16*precision*max(abs(axis[0]), abs(axis[-1]), span)
        group = axis[0]
        mapped = {}
        for position in axis:
            if position-group > slack:
                group = position
            mapped[position] = group
        coordinates.append(mapped)
    return [tuple(axis[value] for axis, value in zip(coordinates, point)) for point in points]


def _mesh(root, digests):
    prefix = 'constant/polyMesh/'
    point_source = _read(root, prefix+'points', digests)
    points = _points(point_source)
    faces = _faces(_read(root, prefix+'faces', digests))
    owner = _labels(_read(root, prefix+'owner', digests), 'owner')
    neighbour = _labels(_read(root, prefix+'neighbour', digests), 'neighbour')
    boundary = _items(_read(root, prefix+'boundary', digests).text(), r'\bwalls\s*\{([^{}]*)\}', 'boundary')
    if (len(boundary) != 1 or len(owner) != len(faces) or not points or not faces or not neighbour
            or max(owner) >= len(faces)
            or any(index >= len(points) for face in faces for index in face)
            or any(not math.isfinite(axis) for point in points for axis in point)):
        raise ValueError('热平衡只适用于完整的正交六面体单固体网格')
    start = re.search(r'\bstartFace\s+(\d+)\s*;', boundary[0])
    count = re.search(r'\bnFaces\s+(\d+)\s*;', boundary[0])
    if (start is None or count is None or int(start[1]) != len(neighbour)
            or int(count[1]) <= 0 or int(start[1])+int(count[1]) != len(faces)):
        raise ValueError('walls 边界面与网格面编号不一致')
    precision = 2**-23 if point_source.binary and point_source.scalar == 'f' else 2**-52
    if point_source.binary:
        points = _snap_grid_points(points, precision)
    return points, faces, owner, neighbour, precision


def _binary_temperatures(source, cells):
    if source.class_name != b'volScalarField':
        raise ValueError('OpenFOAM 二进制 T 类型无效')
    dimensions = source.match(rb'dimensions\s+\[\s*0\s+0\s+0\s+1\s+0\s+0\s+0\s*\]\s*;')
    if dimensions is None:
        raise ValueError('求解场 T 的量纲必须是开尔文')
    uniform = source.match(rb'internalField\s+uniform\s+('+NUMBER.encode()+rb')\s*;', dimensions.end())
    if uniform:
        return [float(uniform[1])]*cells, source.text(uniform.end())
    marker = source.match(rb'internalField\s+nonuniform\s+List<scalar>', dimensions.end())
    if marker is None:
        raise ValueError('无法读取二进制内部温度场')
    values, offset = source.raw_list(source.scalar, marker.end())
    end = source.match(rb';', offset)
    if end is None:
        raise ValueError('二进制内部温度场缺少终止符')
    return [value for (value,) in values], source.text(end.end())


def _ascii_temperatures(value, cells):
    if not re.search(r'\bdimensions\s+\[\s*0\s+0\s+0\s+1\s+0\s+0\s+0\s*\]\s*;', value):
        raise ValueError('求解场 T 的量纲必须是开尔文')
    uniform = re.search(rf'\binternalField\s+uniform\s+({NUMBER})\s*;', value)
    if uniform:
        temperatures = [float(uniform[1])]*cells
    else:
        match = re.search(r'\binternalField\s+nonuniform\s+List<scalar>\s+(\d+\s*\([^)]*\))\s*;', value)
        if match is None:
            raise ValueError('无法读取求解后的内部温度场')
        temperatures = list(map(float, _items(match[1], NUMBER, '温度场')))
    return temperatures, value


def _temperature(source, cells):
    if source.binary:
        temperatures, value = _binary_temperatures(source, cells)
    else:
        temperatures, value = _ascii_temperatures(source.text(), cells)
    walls = re.search(r'\bboundaryField\s*\{\s*walls\s*\{([^{}]*)\}\s*\}', value)
    fixed = re.search(rf'\bvalue\s+uniform\s+({NUMBER})\s*;', walls[1]) if walls else None
    if fixed is None or not re.search(r'\btype\s+fixedValue\s*;', walls[1]):
        raise ValueError('热平衡需要单一固定温度 walls 边界')
    wall_temperature = float(fixed[1])
    if len(temperatures) != cells or any(not math.isfinite(t) or t <= 0 for t in (*temperatures, wall_temperature)):
        raise ValueError('求解温度数量与网格不一致或温度值无效')
    return temperatures, wall_temperature


def _validate_zone(root, digests, zone, cells):
    source = _read(root, 'constant/polyMesh/cellZones', digests)
    labels = []
    if source.binary:
        prefix = rb'1\s*\(\s*'+re.escape(zone.encode())+rb'\s*\{\s*type\s+cellZone\s*;\s*cellLabels\s+List<label>'
        marker = source.match(prefix)
        if marker is not None:
            labels, offset = source.labels(marker.end())
            if not re.fullmatch(r';\s*\}\s*\)\s*;?', source.text(offset)):
                raise ValueError('OpenFOAM 二进制 cellZones 存在额外数据')
    else:
        contents = _items(source.text(), rf'\b{re.escape(zone)}\s*\{{([^{{}}]*)\}}', 'cellZones')
        match = re.search(r'\bcellLabels\s+List<label>\s+(\d+\s*\([^)]*\))\s*;', contents[0]) if len(contents) == 1 else None
        if match is not None:
            labels = list(map(int, _items(match[1], r'\d+', 'cellLabels')))
    if sorted(labels) != list(range(cells)):
        raise ValueError('热源选区未覆盖已求解的全部网格单元')


def _axes(points):
    return tuple(sorted(set(values)) for values in zip(*points))


def _bounds(points):
    axes = _axes(points)
    if len(set(points)) != 8 or any(len(axis) != 2 for axis in axes):
        raise ValueError('热平衡需要正交六面体网格单元')
    return axes


def _flux(points, faces, owner, neighbour, temperatures, wall_temperature, conductivity):
    cells = [set() for _ in temperatures]
    if any(cell >= len(cells) for cell in (*owner, *neighbour)):
        raise ValueError('网格单元编号超出温度场范围')
    for index, vertices in enumerate(faces):
        cells[owner[index]].update(vertices)
        if index < len(neighbour):
            cells[neighbour[index]].update(vertices)
    boundaries = [_bounds([points[id] for id in vertices]) for vertices in cells]
    domain = _axes(points)
    area, watts = defaultdict(list), defaultdict(list)
    for index in range(len(neighbour), len(faces)):
        cell = owner[index]
        planes = _axes([points[id] for id in faces[index]])
        normals = [i for i, axis in enumerate(planes) if len(axis) == 1]
        tangents = [axis for axis in planes if len(axis) != 1]
        if len(set(faces[index])) != 4 or len(normals) != 1 or any(len(axis) != 2 for axis in tangents):
            raise ValueError('walls 下包含非正交矩形面')
        normal = normals[0]
        location = planes[normal][0]
        low, high = boundaries[cell][normal]
        if location != min(domain[normal]) and location != max(domain[normal]):
            raise ValueError('walls 包含内部表面')
        if location not in (low, high):
            raise ValueError('walls 面与所属网格单元不匹配')
        size = math.prod(high-low for low, high in tangents)
        name = 'xyz'[normal]+('_min' if location == low else '_max')
        area[name].append(size)
        watts[name].append(conductivity * (temperatures[cell]-wall_temperature) * size / ((high-low)/2))
    if len(watts) != 6:
        raise ValueError('walls 未覆盖完整计算域外表面')
    result = []
    for name, values in sorted(watts.items()):
        normal = 'xyz'.index(name[0])
        expected = math.prod(axis[-1]-axis[0] for i, axis in enumerate(domain) if i != normal)
        observed = math.fsum(area[name])
        if not math.isclose(observed, expected, rel_tol=1e-8):
            raise ValueError('walls 的面积与外表面几何不一致')
        power = math.fsum(values)
        if not math.isfinite(power):
            raise ValueError('壁面热通量数值超出有效范围')
        result.append({'side': name, 'area_m2': observed, 'outward_power_w': power})
    volumes = [math.prod(high-low for low, high in bounds) for bounds in boundaries]
    volume = math.fsum(volumes)
    if not math.isfinite(volume) or volume <= 0:
        raise ValueError('网格总体积超出有效范围')
    average = math.fsum(t*(v/volume) for t, v in zip(temperatures, volumes))
    return result, average


def _series(root, digests, descriptor, moment, filename, count):
    relative = 'postProcessing/'+descriptor['object_name']
    output = checked(root, relative)
    ordinary(output)
    starts = [path.name for path in output.iterdir()
              if path.is_dir() and re.fullmatch(NUMBER, path.name) and 0 <= float(path.name) <= float(moment)]
    if not starts:
        raise ValueError(f'未找到监视器结果目录：{descriptor["object_name"]}')
    start = max(starts, key=float)
    path = f'{relative}/{start}/{filename}'
    samples = []
    for line in _read_bytes(root, path, digests).decode('utf8').splitlines():
        row = line.strip().split()
        if not row or row[0].startswith('#'):
            continue
        if len(row) != count+1:
            raise ValueError(f'监视器结果列数不一致：{path}')
        time, *temperatures = map(float, row)
        if not math.isfinite(time) or any(not math.isfinite(t) or t <= 0 for t in temperatures):
            raise ValueError(f'监视器结果包含无效温度：{path}')
        if time == float(moment):
            samples.append(temperatures)
    if len(samples) != 1:
        raise ValueError(f'监视器缺少唯一的最新求解时刻：{path}')
    return {'temperature_k': samples[0], 'result_path': path}


def _monitors(root, digests, descriptors, moment, outward):
    results = []
    for descriptor in descriptors:
        method = descriptor['method']
        if method == 'solved-temperature-wall-gradient':
            result = {'outward_power_w': outward}
        elif method == 'volFieldValue':
            result = _series(root, digests, descriptor, moment, 'volFieldValue.dat', 1)
        else:
            result = _series(root, digests, descriptor, moment, 'T', len(descriptor['locations']))
        results.append({**descriptor, **result})
    return results


def evaluate(root, model, monitors):
    objects = model['objects']
    if model['source'].get('format') not in {'ECXML', 'PDML'} or len(objects) != 1:
        raise ValueError('当前热平衡要求导入的单一均质固体')
    source = objects[0]
    material = model['materials'][source['material']]
    k = material['conductivity_w_mk']
    if type(k) not in (int, float):
        raise ValueError('当前热平衡要求各向同性材料')
    times = [path.name for path in root.iterdir()
             if path.is_dir() and re.fullmatch(NUMBER, path.name) and 0 < float(path.name) < math.inf]
    if not times:
        raise ValueError('未找到已求解的温度时刻')
    # Use the latest positive time, never fall back over a partial latest write.
    moment = max(times, key=float)
    if sum(float(name) == float(moment) for name in times) != 1:
        raise ValueError('最新求解时刻的目录重名')
    digests = {}
    points, faces, owner, neighbour, precision = _mesh(root, digests)
    cells = max(owner)+1
    axes = _axes(points)
    actual_domain = [axis[0] for axis in axes] + [axis[-1] for axis in axes]
    expected_domain = model.get('settings', {}).get('domain', model['source_conditions']['domain'])
    if not all(math.isclose(a,b,rel_tol=max(1e-8,4*precision),abs_tol=1e-10)
               for a,b in zip(actual_domain, expected_domain)):
        raise ValueError('实际网格计算域与转换条件不一致')
    _validate_zone(root, digests, source['id']+'Cells', cells)
    temperatures, wall_temperature = _temperature(_read(root, moment+'/T', digests), cells)
    if wall_temperature != model['source_conditions']['ambient_temperature_k']:
        raise ValueError('求解温度场的壁温与导入条件不一致')
    walls, average = _flux(points, faces, owner, neighbour, temperatures, wall_temperature, k)
    supplied = source.get('power_w', 0)
    outward = math.fsum(row['outward_power_w'] for row in walls)
    residual = supplied - outward
    tolerance = max(.01, abs(supplied)*.001)
    results = _monitors(root, digests, monitors, moment, outward)
    reasons = []
    if abs(residual) > tolerance:
        reasons.append({'code': 'wall_power_mismatch', 'message': '壁面净散热与总输入功率之差超过容差'})
    for row in results:
        if row['method'] == 'volFieldValue':
            row['recomputed_temperature_k'] = average
            if abs(row['temperature_k'][0]-average) > .001:
                reasons.append({'code': 'region_temperature_mismatch', 'monitor': row['monitor'],
                                'message': '原生区域平均温度与当前温度场回算不一致'})
    return {'ok': not reasons, 'schema': SCHEMA, 'source': model['source'],
            'time': moment, 'cells': len(temperatures), 'conductivity_w_mk': k,
            'wall_temperature_k': wall_temperature, 'input_power_w': supplied,
            'outward_power_w': outward, 'residual_w': residual, 'tolerance_w': tolerance,
            'walls': walls, 'monitors': results, 'reasons': reasons, 'result_sha256': digests}
