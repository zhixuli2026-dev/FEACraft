"""Map resolved FloTHERM airflow devices to OpenFOAM fluid-zone sources."""

from itertools import combinations
import math
from pathlib import Path

from .flotherm_geometry import extrude_plate


ECXML_FANS = {'axial3dFan', 'rectangular2dFan'}
ECXML_RESISTANCES = {'grille', 'flowResistance', 'perforatedPlate'}
FIXED_MODEL = 'fixedVolumetricFan'
FIXED_LIBRARY = 'libvibeflowFlothermFixedFlow.so'
FIXED_SOURCE_DIR = 'constant/flotherm-fixed-flow-src'
FIXED_FILES = ('fixedVolumetricFan.C', 'fixedVolumetricFan.H', 'Make/files', 'Make/options')
DEVICE_FIELDS = {'source_type', 'direction', 'source_hub_size', 'pq_curve', 'fixed_flow_m3_s',
                 'coefficients', 'source_parameters', 'overridden_fields'}


def _nonnegative(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _vector(values):
    return '(' + ' '.join(str(value) for value in values) + ')'


def validate_device(obj):
    if 'flow_device' not in obj:
        return
    device = obj['flow_device']
    if (not isinstance(device, dict) or device.keys()-DEVICE_FIELDS or obj.get('type') not in ('fan', 'resistance')
            or not isinstance(device.get('source_type'), str) or not device['source_type']
            or 'material' in obj or obj.get('power_w', 0) != 0):
        raise ValueError('流体设备需要原始类型，且不接受固体材料或热源功率')
    normal = device.get('direction')
    if normal is not None and (
            not isinstance(normal, list) or len(normal) != 3
            or any(type(v) not in (int, float) or v not in (-1, 0, 1) for v in normal)
            or sum(abs(v) for v in normal) != 1):
        raise ValueError('流体设备方向必须为带符号的主轴单位向量')
    if 'source_hub_size' in device and not _nonnegative(device['source_hub_size']):
        raise ValueError('风扇的原始 hubSize 必须是有限非负值')
    curve, rate = device.get('pq_curve'), device.get('fixed_flow_m3_s')
    if curve is not None:
        if (not isinstance(curve, list) or len(curve) < 2
                or any(not isinstance(point, list) or len(point) != 2
                       or not all(map(_nonnegative, point)) for point in curve)
                or any(left[0] >= right[0] for left, right in zip(curve, curve[1:]))):
            raise ValueError('风扇 P-Q 曲线需要至少两个有限非负流量/压升点，且流量严格递增')
    if rate is not None and (not _nonnegative(rate) or rate == 0):
        raise ValueError('固定体积流量必须为正有限的 m³/s')
    if curve is not None and rate is not None:
        raise ValueError('单个风扇只能使用 P-Q 曲线或指定体积流量')
    coeffs = device.get('coefficients')
    if coeffs is not None and (
            not isinstance(coeffs, dict) or set(coeffs) != {'d', 'f'}
            or any(not isinstance(values, list) or len(values) != 3
                   or not all(map(_nonnegative, values)) for values in coeffs.values())
            or not any(any(values) for values in coeffs.values())):
        raise ValueError('DarcyForchheimer 需要全局主轴 d [1/m²]、f [1/m] 三分量，至少一项为正')
    if obj['type'] == 'fan' and coeffs is not None or obj['type'] == 'resistance' and (curve is not None or rate is not None):
        raise ValueError('流体设备参数与设备类型不一致')


def ready(obj):
    device = obj['flow_device']
    if obj['type'] == 'fan':
        return bool(device.get('direction') and (device.get('pq_curve') or device.get('fixed_flow_m3_s')))
    return device.get('coefficients') is not None


def update_mapping(obj):
    if ready(obj):
        obj['mapping'] = 'equivalent'
        obj.pop('reason', None)
        obj.pop('missing_fields', None)
        return
    device = obj['flow_device']
    if obj['type'] == 'fan':
        fields = [field for field, present in (('direction', device.get('direction')),
                   ('pqCurve 或 fixedFlowRate', device.get('pq_curve') or device.get('fixed_flow_m3_s'))) if not present]
    else:
        fields = ['coefficients.d', 'coefficients.f']
    obj.update(mapping='missing-info', reason='缺少可核验的流体设备物理参数', missing_fields=fields)


def apply_device_settings(objects, settings):
    for key, kind, fields in (
            ('fan', 'fan', {'direction': 'direction', 'pqCurve': 'pq_curve', 'fixedFlowRate': 'fixed_flow_m3_s'}),
            ('resistances', 'resistance', {'coefficients': 'coefficients'})):
        values = settings.get(key, {})
        if not isinstance(values, dict):
            raise ValueError(f'{key} 须为按对象 ID 索引的对象')
        for oid, override in values.items():
            obj = objects.get(oid)
            if not obj or obj.get('type') != kind or 'flow_device' not in obj:
                raise ValueError(f'{key} 引用了未知或类型不符的流体设备 {oid}')
            if (not isinstance(override, dict) or not override or set(override) - fields.keys()
                    or any(value is None for value in override.values())):
                raise ValueError(f'{key}.{oid} 含不支持或空的设备参数')
            device = obj['flow_device']
            if 'pqCurve' in override and 'fixedFlowRate' in override:
                raise ValueError(f'风扇 {oid} 的两个控制模式互斥')
            device.setdefault('source_parameters', {name: device[name] for name in fields.values() if name in device})
            if 'pqCurve' in override:
                device.pop('fixed_flow_m3_s', None)
            if 'fixedFlowRate' in override:
                device.pop('pq_curve', None)
            for name, value in override.items():
                device[fields[name]] = value
            device['overridden_fields'] = sorted(set(device.get('overridden_fields', ())) | set(override))
            validate_device(obj)
            update_mapping(obj)


def prepare_devices(ir, domain, solids, base_size, surface_mesh):
    devices, blockers = [], []
    options = ir['settings'].get('openfoam', {})
    spans = [hi-lo for lo, hi in zip(domain, domain[3:])]
    for index, obj in enumerate(ir['objects']):
        if 'flow_device' not in obj:
            continue
        model = obj['flow_device']
        shape = obj['shape']
        reason = None
        thickness_source = None
        if not ready(obj) or obj['mapping'] not in ('converted', 'equivalent'):
            setting = ('fan（方向及 P-Q 曲线或定流量）' if obj['type'] == 'fan'
                       else 'resistances（DarcyForchheimer d、f）')
            reason = f'缺少可核验的物理参数，请配置 {setting}'
        elif surface_mesh:
            reason = '流体设备的精确面选择目前需要 Cartesian 网格'
        elif shape['kind'] == 'flat-plate':
            choices = ((obj.get('plate_thickness_override_m'), 'objectOverrides.plateThickness'),
                       (shape.get('thickness_m'), 'source.thickness'),
                       (options.get('thinPlateThickness'), 'openfoam.thinPlateThickness'))
            thickness, thickness_source = next(((value, source) for value, source in choices if value is not None),
                                               (None, None))
            if thickness is None and obj['type'] == 'resistance':
                reason = '薄阻力件需要物理厚度才能由体积阻力得到压降'
            else:
                if thickness is None:
                    thickness, thickness_source = max(.2*base_size, max(spans)*1e-7), 'mesh-scale-equivalent'
                shape = extrude_plate(shape, thickness)
        if not reason and shape['kind'] != 'box':
            reason = '流体设备需要轴对齐的矩形选择体积'
        if reason:
            blockers.append({'object': obj['id'], 'reason': reason})
            continue
        low = shape['origin']
        high = [a+b for a, b in zip(low, shape['dimensions'])]
        bounds = [low, high]
        if any(a < c or b > d or b-a <= span*1e-12 for a, b, c, d, span in zip(low, high, domain, domain[3:], spans)):
            blockers.append({'object': obj['id'], 'reason': '流体设备必须具有位于流体域内的正体积'})
            continue
        zone = f'flowDevice{index}'
        row = {**model, 'id': obj['id'], 'bounds_m': bounds, 'cell_zone': zone, 'source': zone+'Source'}
        if thickness_source:
            row['thickness_source'] = thickness_source
        if obj['type'] == 'fan':
            direction = model['direction']
            axis = next(i for i, value in enumerate(direction) if value)
            if obj['shape']['kind'] == 'flat-plate' and abs(obj['shape']['normal'][axis]) < 1-1e-8:
                blockers.append({'object': obj['id'], 'reason': '二维风扇方向必须垂直于原始风扇平面'})
                continue
            face = bounds[direction[axis] < 0][axis]
            if not domain[axis] < face < domain[axis+3]:
                blockers.append({'object': obj['id'], 'reason': '风扇入口面必须位于流体域内部'})
                continue
            area = math.prod(high[i]-low[i] for i in range(3) if i != axis)
            if not math.isfinite(area) or area <= 0:
                blockers.append({'object': obj['id'], 'reason': '风扇入口面积必须是有限正值'})
                continue
            row.update(axis=axis, thickness_m=high[axis]-low[axis], area_m2=area,
                       face_zone=zone+'Faces', upstream_plane_m=face)
            if 'pq_curve' in model:
                row.update(model='fanMomentumSource', interpolation='linear', out_of_bounds='clamp')
            else:
                row['model'] = FIXED_MODEL
        else:
            row['model'] = 'DarcyForchheimer'
        devices.append(row)
    for row, other in combinations([*devices, *solids.values()], 2):
        if 'cell_zone' not in row:
            continue
        a, b = row['bounds_m'], other['bounds_m']
        if all(min(x, y) > max(u, v) for u, x, v, y in zip(*a, *b)):
            blockers.append({'object': row['id'], 'reason': f'流体设备与 {other["id"]} 体积重叠'})
        elif row.get('face_zone') and 'cell_zone' not in other:
            axis, face = row['axis'], row['upstream_plane_m']
            if (b[0][axis] <= face <= b[1][axis]
                    and all(min(a[1][i], b[1][i]) > max(a[0][i], b[0][i]) for i in range(3) if i != axis)):
                blockers.append({'object': row['id'], 'reason': f'风扇入口与固体 {other["id"]} 接触'})
    return devices, blockers


def topo_set(devices):
    actions = []
    for row in devices:
        zone = row['cell_zone']
        low, high = row['bounds_m']
        actions.extend((
            f'{{ name {zone}Set; type cellSet; action new; source boxToCell; box {_vector(low)} {_vector(high)}; }}',
            f'{{ name {zone}; type cellZoneSet; action new; source setToCellZone; set {zone}Set; }}',
        ))
        if 'face_zone' in row:
            axis, face = row['axis'], row['upstream_plane_m']
            start, end = list(low), list(high)
            tolerance = min(high[i]-low[i] for i in range(3))*1e-9
            start[axis], end[axis] = face-tolerance, face+tolerance
            actions.extend((
                f'{{ name {zone}FaceSet; type faceSet; action new; source boxToFace; box {_vector(start)} {_vector(end)}; }}',
                f'{{ name {row["face_zone"]}; type faceZoneSet; action new; source setsToFaceZone; '
                f'faceSet {zone}FaceSet; cellSet {zone}Set; flip true; }}',
            ))
    return 'actions (\n'+'\n'.join(actions)+'\n);'


def source_entries(devices):
    entries = []
    for row in devices:
        model = row['model']
        zone = f'selectionMode cellZone; cellZone {row["cell_zone"]};'
        if model in ('fanMomentumSource', FIXED_MODEL):
            physics = (f'faceZone {row["face_zone"]}; flowDir {_vector(row["direction"])}; '
                       f'thickness {row["thickness_m"]}; ')
        if model == 'fanMomentumSource':
            curve = _vector(_vector(point) for point in row['pq_curve'])
            physics += f'fanCurve {{ type table; values {curve}; outOfBounds clamp; interpolationScheme linear; }}'
        elif model == FIXED_MODEL:
            physics += f'volumeFlow {row["fixed_flow_m3_s"]}; area {row["area_m2"]};'
        else:
            model = 'explicitPorositySource'
            coeffs = row['coefficients']
            physics = (f'type DarcyForchheimer; d {_vector(coeffs["d"])}; f {_vector(coeffs["f"])}; '
                       'coordinateSystem { origin (0 0 0); e1 (1 0 0); e2 (0 1 0); }')
            entries.append(f'{row["source"]} {{ type {model}; {model}Coeffs {{ {zone} {physics} }} }}')
            continue
        entries.append(f'{row["source"]} {{ type {model}; {zone} {physics} }}')
    return '\n'.join(entries)


def write_fixed_sources(output):
    source = Path(__file__).with_name('flotherm_fixed_flow_native')
    for name in FIXED_FILES:
        destination = output/FIXED_SOURCE_DIR/name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((source/name).read_bytes())
