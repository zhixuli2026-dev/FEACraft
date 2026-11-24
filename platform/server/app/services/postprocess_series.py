"""Read sealed reference time series using explicit clock, units and datum."""
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
import csv
import hashlib
import io
import json
import math


MAX_SERIES_BYTES = 2 * 1024 * 1024
_LENGTH_SCALE = {'m': 1.0, 'ft': 0.3048}


def utc_time(value, *, assume_utc=False):
    try:
        if not isinstance(value, str) or len(value) < 16 or value[10] not in ('T', ' '):
            raise ValueError('ISO time requires hours and minutes')
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            if not assume_utc:
                raise ValueError('a UTC offset is required')
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError('时刻必须是含时分的 ISO 日期，未带偏移的时刻需明确 UTC') from error


def read_rows(data, time_column, value_column, *, assume_utc):
    if (not isinstance(time_column, str) or not time_column or not isinstance(value_column, str) or
            not value_column or time_column == value_column):
        raise ValueError('CSV 需要不同的时间列和值列')
    if len(data) > MAX_SERIES_BYTES:
        raise ValueError('参考 CSV 超过 2 MiB')
    if not data:
        raise ValueError('参考 CSV 为空')
    rows = csv.reader(io.StringIO(data.decode('utf-8-sig'), newline=''), strict=True)
    columns = [column.strip() for column in next(rows, ())]
    if not columns or len(set(columns)) != len(columns) or {time_column, value_column} - set(columns):
        raise ValueError('参考 CSV 缺少声明的列或含重复列')
    time_index = columns.index(time_column)
    value_index = columns.index(value_column)
    values = {}
    records = []
    for row in rows:
        if len(row) != len(columns):
            raise ValueError('参考 CSV 的数据行与列数不一致')
        time = utc_time(row[time_index].strip(), assume_utc=assume_utc)
        if time in values:
            raise ValueError('参考 CSV 含重复的 UTC 时刻')
        numeric = float(row[value_index])
        if not math.isfinite(numeric):
            raise ValueError('参考 CSV 需要有限数值')
        values[time] = numeric
        records.append(dict(zip(columns, row)))
    if not values:
        raise ValueError('参考 CSV 没有观测记录')
    return values, records


def case_series_paths(spec, safe_relative):
    if not isinstance(spec, dict) or not isinstance(spec.get('path'), str):
        raise ValueError('参考 CSV 需要算例内路径')
    paths = [safe_relative(spec['path'])]
    if 'receipt' in spec:
        if not isinstance(spec['receipt'], str):
            raise ValueError('观测收集回执需要算例内路径')
        paths.append(safe_relative(spec['receipt']))
    suffixes = ('.csv', '.json')
    if any(PurePosixPath(path).suffix.lower() != suffixes[index] or
           PurePosixPath(path).parts[0].casefold() == '.vibeflow'
           for index, path in enumerate(paths)):
        raise ValueError('参考数据需要算例内 CSV 和可选 JSON 收集回执')
    return paths


def read_bounded_reference(path):
    with path.open('rb') as stream:
        data = stream.read(MAX_SERIES_BYTES + 1)
    if len(data) > MAX_SERIES_BYTES:
        raise ValueError('参考数据文件超过 2 MiB')
    return data


def _unit(value):
    if not isinstance(value, str) or not value.strip() or value.casefold() == 'unknown':
        raise ValueError('表格比较需要实际单位')
    return value


def load_case_series(spec, safe_relative, open_path):
    paths = case_series_paths(spec, safe_relative)
    raw = read_bounded_reference(open_path(paths[0]))
    digest = hashlib.sha256(raw).hexdigest()
    receipt = None
    if len(paths) == 2:
        receipt = json.loads(read_bounded_reference(open_path(paths[1])))
        if (not isinstance(receipt, dict) or receipt.get('schema') != 'vibeflow.noaa-observations.v1' or
                receipt.get('path') != paths[0] or receipt.get('sha256') != digest or receipt.get('size') != len(raw)):
            raise ValueError('观测收集回执与原始 CSV 不一致')
    keys = ('timeColumn', 'valueColumn', 'timeZone', 'unit', 'datum')
    if receipt is not None and any(key in spec and spec[key] != receipt.get(key) for key in keys):
        raise ValueError('观测声明与收集回执的列、时区、单位或基准不一致')
    options = receipt if receipt is not None else spec
    unit = _unit(options.get('unit'))
    datum = options.get('datum')
    if datum is not None and (not isinstance(datum, str) or not datum.strip()):
        raise ValueError('垂直基准需要非空标识')
    time_zone = options.get('timeZone')
    if time_zone not in ('UTC', 'offset'):
        raise ValueError('表格时间需声明 UTC 或逐行 ISO 偏移')
    values, _ = read_rows(raw, options.get('timeColumn'), options.get('valueColumn'), assume_utc=time_zone == 'UTC')
    evidence = {'path': paths[0], 'sha256': digest, 'unit': unit, 'datum': datum}
    if receipt is not None:
        evidence.update({'receipt': paths[1], 'source': receipt.get('source'), 'provider': receipt.get('provider')})
    return values, evidence


def series_for_metric(metric, reference):
    if metric is None:
        raise ValueError('指标不存在')
    sampling = metric.get('sampling')
    if not isinstance(sampling, dict) or sampling.get('layout') != 'time,point':
        raise ValueError('指标缺少 time,point 时间采样轴')
    origin = utc_time(reference.get('timeOrigin'))
    points = sampling['points']
    point = reference.get('point')
    if point is None and len(points) == 1:
        index = 0
    elif isinstance(point, list) and points.count(point) == 1:
        index = points.index(point)
    else:
        raise ValueError('时序参考需要一个与指标采样完全匹配的 point')
    result = {}
    for time, row in zip(sampling['times'], metric['value'], strict=True):
        moment = origin + timedelta(seconds=float(time))
        value = float(row[index])
        if moment in result or not math.isfinite(value):
            raise ValueError('指标的 UTC 时刻重复或数值无效')
        result[moment] = value
    return result, {'metric': metric['name'], 'unit': _unit(metric['unit']), 'datum': reference.get('metricDatum'),
                    'point': points[index], 'time_origin_utc': origin.isoformat()}


def align(actual, actual_spec, observed, observed_spec):
    if actual.keys() != observed.keys():
        raise ValueError('实际指标与观测的 UTC 时刻不一致')
    if actual_spec['datum'] != observed_spec['datum']:
        raise ValueError('实际指标与观测的垂直基准不一致')
    source, target = actual_spec['unit'], observed_spec['unit']
    if source == target:
        conversion = 1.0
    elif source in _LENGTH_SCALE and target in _LENGTH_SCALE:
        conversion = _LENGTH_SCALE[source] / _LENGTH_SCALE[target]
    else:
        raise ValueError('实际指标与观测的单位不兼容')
    times = sorted(observed)
    metadata = {'samples': len(times), 'first_utc': times[0].isoformat(), 'last_utc': times[-1].isoformat(),
                'unit': target, 'datum': observed_spec['datum'], 'actual_scale': conversion,
                'actual': actual_spec, 'observed': observed_spec}
    return [actual[time] * conversion for time in times], [observed[time] for time in times], metadata
