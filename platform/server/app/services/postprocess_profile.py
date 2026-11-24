"""Align sealed experimental spatial measurements with the same native probe locations."""

import csv
import hashlib
import io
import json
import math
from pathlib import PurePosixPath

if __package__:
    from .postprocess_series import read_bounded_reference
else:
    from postprocess_series import read_bounded_reference


def profile_paths(spec, safe_relative):
    if not isinstance(spec, dict) or set(spec) - {'path', 'sourcePath', 'receipt', 'coordinateUnit', 'time'}:
        raise ValueError('实验剖面需要 CSV 路径、坐标单位和求解时刻')
    if 'receipt' in spec and 'sourcePath' not in spec:
        raise ValueError('实验剖面采集回执需要原始观测文件')
    keys = ('path', *(key for key in ('sourcePath', 'receipt') if key in spec))
    if any(not isinstance(spec.get(key), str) for key in keys):
        raise ValueError('实验剖面只接受算例内文件路径')
    paths = [safe_relative(spec[key]) for key in keys]
    if (PurePosixPath(paths[0]).suffix.casefold() != '.csv'
            or 'receipt' in spec and PurePosixPath(paths[-1]).suffix.casefold() != '.json'
            or any(not PurePosixPath(path).parts or PurePosixPath(path).parts[0].casefold() == '.vibeflow'
                   for path in paths)
            or len(set(path.casefold() for path in paths)) != len(paths)):
        raise ValueError('实验剖面需要独立的算例内 CSV 和可选原始观测文件')
    return paths


def profile_at_probes(spec, metric, safe_relative, open_path):
    paths = profile_paths(spec, safe_relative)
    if spec.get('coordinateUnit') != 'm':
        raise ValueError('实验剖面坐标须以 m 声明')
    if not isinstance(metric, dict):
        raise ValueError('实验剖面缺少求解采样指标')
    sampled = metric.get('sampling') or {}
    time = float(spec['time']) if 'time' in spec else None
    times = sampled.get('times') or []
    if sampled.get('layout') != 'time,point' or time is None or not math.isfinite(time) or times.count(time) != 1:
        raise ValueError('实验剖面需要唯一的求解采样时刻')
    points = [tuple(point) for point in sampled['points']]
    if not points or len(set(points)) != len(points):
        raise ValueError('实验剖面需要唯一的求解采样坐标')

    raw = read_bounded_reference(open_path(paths[0]))
    reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig'), newline=''), strict=True)
    if not reader.fieldnames or set(reader.fieldnames) != {'x', 'y', 'z', 'value'} or len(reader.fieldnames) != 4:
        raise ValueError('实验剖面 CSV 需要 x、y、z 和 value 四列')
    coordinates = {}
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError('实验剖面 CSV 的数据行与列数不一致')
        point = tuple(float(row[axis]) for axis in ('x', 'y', 'z'))
        value = float(row['value'])
        if point in coordinates or not all(math.isfinite(item) for item in (*point, value)):
            raise ValueError('实验剖面含重复坐标或非有限数值')
        coordinates[point] = value
    if coordinates.keys() != set(points):
        raise ValueError('实验剖面的坐标与求解采样点不一致')
    alignment = {'kind': 'spatialProfile', 'samples': len(points), 'time': time, 'coordinate_unit': 'm',
                 'reference': {'path': paths[0], 'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)}}
    if 'sourcePath' in spec:
        source = read_bounded_reference(open_path(paths[1]))
        if not source:
            raise ValueError('实验剖面原始观测文件为空')
        alignment['source'] = {'path': paths[1], 'sha256': hashlib.sha256(source).hexdigest(), 'size': len(source)}
    if 'receipt' in spec:
        body = read_bounded_reference(open_path(paths[-1]))
        receipt = json.loads(body)
        expected = {'schema': 'vibeflow.wind-tunnel-profile.v1', 'path': paths[0], 'sourcePath': paths[1],
                    'sha256': alignment['reference']['sha256'], 'size': len(raw),
                    'source_sha256': alignment['source']['sha256'], 'source_size': alignment['source']['size'],
                    'coordinateUnit': 'm', 'unit': metric.get('unit'), 'records': len(points)}
        if not isinstance(receipt, dict) or any(receipt.get(key) != value for key,value in expected.items()):
            raise ValueError('实验剖面采集回执与封存文件或单位不一致')
        alignment['receipt'] = {'path': paths[-1], 'sha256': hashlib.sha256(body).hexdigest(), 'size': len(body)}
    return metric['value'][times.index(time)], [coordinates[point] for point in points], alignment
