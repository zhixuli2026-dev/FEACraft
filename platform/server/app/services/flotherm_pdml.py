"""Typed V2310 records and solid geometry with explicit thermal references."""

from dataclasses import dataclass, field
import math
import struct

from .flotherm_flow_devices import update_mapping
from .flotherm_observations import probe, region
from .flotherm_geometry import make_flat_plate, make_shape
from .flotherm_parts import pdml_heatsink
from .flotherm_two_resistance import MATERIAL as CASE_MATERIAL, MATERIAL_ID, parameters


MATERIAL = 0x201B000
SOLID = 0x2025000
ASSEMBLY = 0x202E000
SOURCE = 0x202C000
PCB = 0x2035000
COMPACT = 0x2057000
HEATSINK = 0x2034000
ENCLOSURE = 0x2031000
FLOW_RECORDS = {0x2032000: ('fan', 'axial3dFan'), 0x2033000: ('fan', 'fixedFlow'),
                0x202A000: ('resistance', 'flowResistance'), 0x205D000: ('resistance', 'perforatedPlate')}
MONITOR = 0x2027000
REGION = 0x2029000
THERMAL_ATTRIBUTE = 0x2022000
IDENTITY = ((1., 0., 0.), (0., 1., 0.), (0., 0., 1.))
ZERO = (0., 0., 0.)
AXES = 0x100300A
SIZES = 0x1003009
ORIGIN = 0x100300B
ACTIVE = 0x1003004
FACE = 0x1003003
OBJECT_KEY = 0x1001002
MATERIAL_REFERENCE = 0x1001005
THERMAL_REFERENCE = 0x1001008
POWER = 0x2022004
GEOMETRY_RECORDS = {ASSEMBLY, SOLID, SOURCE, 0x2028000, 0x2073100, 0x2073200, 0x202A000,
    0x2034000, 0x2035000, 0x2038000, 0x2030000, 0x2032000, 0x2037000, 0x2084000,
    0x2087000, 0x2033000, 0x2057000, 0x205D000, 0x2026000, 0x2027000, 0x2029000,
    0x2074000, 0x2077000, 0x2080000, 0x2081000, 0x2050000, 0x2031000, 0x202F000}
PRIMITIVES = {SOLID:'box', SOURCE:'box', PCB:'box', COMPACT:'box', 0x2028000:'prism',0x2073100:'tet',
              0x2073200:'invertedTet',0x2030000:'cylinder'}


@dataclass
class Record:
    offset: int
    kind: int
    name: str
    parent: int | None
    methods: dict = field(default_factory=dict)

    def values(self, method, kind=None):
        return [value for group in self.methods.get(method, ()) for tag, value in group if kind is None or tag == kind]

    def scalar(self, method, default=None, kind=None):
        values = self.values(method, kind)
        if not values:
            return default
        if len(values) != 1:
            raise ValueError(f'PDML 字段 0x{method:x} 不唯一（偏移 {self.offset}）')
        return values[0]


def _decode_string(value):
    try:
        return value.decode('utf-8')
    except UnicodeDecodeError:
        try:
            return value.decode('gb18030')
        except UnicodeDecodeError:
            return value.decode('latin-1')


def _tokens(data, start, boundary):
    widths = {1: 4, 3: 4, 6: 8, 7: 4, 10: 4, 12: 4}
    cursor = start
    count = 0
    while cursor < boundary:
        offset, code = cursor, data[cursor]
        cursor += 1
        count += 1
        if count > 500000:
            raise ValueError('PDML 类型记录数量超过 500,000')
        if code == 8:
            if cursor+4 > boundary:
                raise ValueError(f'PDML 字符串长度截断（偏移 {offset}）')
            size = int.from_bytes(data[cursor:cursor+4], 'big')
            cursor += 4
        else:
            size = widths.get(code)
            if size is None:
                raise ValueError(f'PDML 未知类型 0x{code:02x}（偏移 {offset}）')
        end = cursor+size
        if end > boundary:
            raise ValueError(f'PDML 字段超出记录边界（偏移 {offset}）')
        payload = data[cursor:end]
        if code == 8:
            value = _decode_string(payload)
        elif code == 6:
            value = struct.unpack('>d', payload)[0]
            if not math.isfinite(value):
                raise ValueError(f'PDML 包含非有限数值（偏移 {offset}）')
        else:
            value = int.from_bytes(payload, 'big')
        yield offset, code, value
        cursor = end


def read_records(data):
    header_end = data.find(b'\n', 0, 4096)
    signature = b'\xff\xfb\x00\x00\x09\x06\x00\x0a'
    suffix = b'\n#END\n'
    if (header_end < 0 or not data.startswith(b'#FFFB V2310 ') or
            data[header_end+1:header_end+9] != signature or not data.endswith(suffix)):
        raise ValueError('PDML 需要完整的 V2310 记录头、二进制标识和结束标记')
    stream = _tokens(data, header_end+9, len(data)-len(suffix))
    records, parents = [], []
    method = None
    ended = False
    for offset, code, value in stream:
        if code == 7 and method is None:
            name, flags, begin = (next(stream, None) for _ in range(3))
            if not name or not flags or not begin or (name[1], flags[1], begin[1:]) != (8, 3, (1, 1)):
                raise ValueError(f'PDML 实体记录头无效（偏移 {offset}）')
            if len(parents) >= 128:
                raise ValueError('PDML 装配嵌套超过 128 层')
            record = Record(offset, value, name[2], parents[-1].offset if parents else None)
            records.append(record)
            parents.append(record)
        elif code == 10 and parents and method is None:
            method = []
            parents[-1].methods.setdefault(value, []).append(method)
        elif code == 1:
            if value == 3 and method is not None:
                method = None
            elif value == 2 and parents and method is None:
                parents.pop()
            elif value == 0 and not parents and method is None and next(stream, None) is None:
                ended = True
                break
            else:
                raise ValueError(f'PDML 实体或方法未正确闭合（偏移 {offset}）')
        elif code in (3, 6, 8, 12) and method is not None:
            method.append((code, value))
        else:
            raise ValueError(f'PDML 字段不在有效记录中（偏移 {offset}）')
    if not ended or not records:
        raise ValueError('PDML 缺少实体或终止控制记录')
    return records


def _dot(first, second):
    return sum(a*b for a, b in zip(first, second))


def _transform(columns, vector):
    return tuple(_dot(row, vector) for row in zip(*columns))


def _near(first, second):
    return math.isclose(first, second, rel_tol=1e-8, abs_tol=1e-10)


def _position(record, parent_position, parent_axes):
    local = record.values(ORIGIN, 6) or ZERO
    if len(local) != 3:
        raise ValueError(f'PDML 对象 {record.name} 缺少完整坐标')
    translated = _transform(parent_axes, local)
    return tuple(a+b for a, b in zip(parent_position, translated))


def _columns(record, parent_axes):
    values = record.values(AXES, 6)
    if len(values) != 9:
        raise ValueError(f'PDML 对象 {record.name} 缺少三条明确的边向量')
    columns = tuple(_transform(parent_axes, values[i:i+3]) for i in (0, 3, 6))
    magnitudes = [math.hypot(*col) for col in columns]
    if any(not math.isfinite(value) or value <= 0 for value in magnitudes):
        raise ValueError(f'PDML 对象 {record.name} 包含退化或非有限边向量')
    axes = tuple(tuple(value/length for value in col) for col, length in zip(columns, magnitudes))
    if any(not _near(_dot(axes[i], axes[j]), 0) for i, j in ((0, 1), (0, 2), (1, 2))):
        raise ValueError(f'PDML 对象 {record.name} 的坐标轴不正交')
    return columns, axes, magnitudes


def _check_dimensions(record, magnitudes):
    sizes = record.values(SIZES, 6)
    if len(sizes) != 3 or any(size <= 0 or not _near(size, length) for size, length in zip(sizes, magnitudes)):
        raise ValueError(f'PDML 对象 {record.name} 的尺寸和边向量不一致')


def _shape(record, position, columns, axes, magnitudes):
    kind = PRIMITIVES.get(record.kind, 'box')
    if kind == 'cylinder':
        normal = (record.scalar(0x2030001,0x3029003,kind=12)&0xff)-1
        radius = record.scalar(0x2030004,0.,kind=6)
        length = record.scalar(0x2030005,0.,kind=6)
        if normal not in range(3) or not all(math.isfinite(v) and v>0 for v in (radius,length)):
            raise ValueError(f'PDML 圆柱 {record.name} 缺少正半径、长度或有效轴向')
        radial = [i for i in range(3) if i != normal]
        basis = [[value*scale for value in axes[j]] for j,scale in zip((*radial,normal),(2*radius,2*radius,length))]
        return make_shape(kind,position,basis)
    _check_dimensions(record, magnitudes)
    if kind != 'box':
        return make_shape(kind,position,columns)
    indices = [max(range(3), key=lambda j: abs(axis[j])) for axis in axes]
    if len(set(indices)) != 3 or any(not _near(abs(axis[j]), 1) for axis, j in zip(axes, indices)):
        return make_shape('oriented-box',position,columns)
    origin, dimensions = list(position), [0., 0., 0.]
    for col, j in zip(columns, indices):
        origin[j] += min(col[j], 0.)
        dimensions[j] = abs(col[j])
    return {'kind': 'box', 'origin': origin, 'dimensions': dimensions}


def _material(record):
    directional = [record.scalar(code, 0., kind=6) for code in (0x201B007, 0x201B008, 0x201B009)]
    k = directional if any(directional) else record.scalar(0x201B004, 0., kind=6)
    density = record.scalar(0x201B001, 0., kind=6)
    heat_capacity = record.scalar(0x201B002, 0., kind=6)
    rho_cp = density*heat_capacity
    if min(*(k if isinstance(k, list) else [k]),density,heat_capacity,rho_cp) <= 0 or not math.isfinite(rho_cp):
        raise ValueError(f'PDML 材料 {record.name} 缺少有效的导热率、密度或比热')
    return {'conductivity_w_mk': k, 'density_kg_m3': density, 'specific_heat_j_kg_k': heat_capacity,
            'rho_cp': rho_cp, 'record_offset': record.offset}


def _flattened_plate(record, face, position, columns, directions, magnitudes):
    flat_axis = (record.scalar(0x1003002,0x3013002)&0xff)-2
    if (record.kind not in (SOLID,SOURCE,COMPACT,*FLOW_RECORDS) or face not in (2,3,4) or flat_axis not in range(3)
            or record.kind == COMPACT and flat_axis!=2):
        raise ValueError(f'PDML 对象 {record.name} 的压平模式尚无可验证映射')
    _check_dimensions(record,magnitudes)
    shift = (face-2)/2
    plane = [x+shift*edge for x,edge in zip(position,columns[flat_axis])]
    return make_flat_plate(plane,[column for i,column in enumerate(columns) if i!=flat_axis],directions[flat_axis])


def _pcb_material(record, references, axes):
    roles = {}
    for values in record.methods.get(MATERIAL_REFERENCE,()):
        if len(values) != 2 or values[0][0] != 8 or values[1][0] != 12:
            continue
        identity, role = values[0][1],values[1][1]
        if role not in (0x301000e,0x301000f):
            continue
        candidate = references.get(identity)
        if role in roles or candidate is None or candidate.kind != MATERIAL:
            raise ValueError(f'PDML PCB {record.name} 的基材或铜材料引用不唯一或不可解析')
        roles[role] = candidate
    percent = record.scalar(0x2035005,-1.,kind=6)
    if set(roles) != {0x301000e,0x301000f} or record.scalar(0x2035004) != 0x302d002 or not 0 <= percent <= 100:
        raise ValueError(f'PDML PCB {record.name} 需要已确认的体积分数模型、基材、铜材料和 0–100% 的铜含量')
    substrate, copper = roles[0x301000e],roles[0x301000f]
    a,b = _material(substrate),_material(copper)
    ka,kb = a['conductivity_w_mk'],b['conductivity_w_mk']
    if isinstance(ka,list) or isinstance(kb,list):
        raise ValueError(f'PDML PCB {record.name} 的层合等效模型需要各向同性基材和铜材料')
    fraction = percent/100
    density = (1-fraction)*a['density_kg_m3']+fraction*b['density_kg_m3']
    rho_cp = (1-fraction)*a['rho_cp']+fraction*b['rho_cp']
    parallel = (1-fraction)*ka+fraction*kb
    normal = 1/((1-fraction)/ka+fraction/kb)
    return {'name':record.name, 'conductivity_w_mk':[parallel,parallel,normal],
            'conductivity_axes':[list(axis) for axis in axes], 'density_kg_m3':density,
            'specific_heat_j_kg_k':rho_cp/density, 'rho_cp':rho_cp, 'record_offset':record.offset,
            'laminate':{'copper_volume_fraction':fraction, 'substrate':{'name':substrate.name,**a},
                        'copper':{'name':copper.name,**b}}}


def _reference(record, references, method, kind):
    linked = record.scalar(method, kind=8)
    if linked is None:
        return None
    resolved = references.get(linked)
    if resolved is None or resolved.kind != kind:
        raise ValueError(f'PDML 对象 {record.name} 的引用 0x{method:x} 无法核验')
    return resolved


def _power(record):
    if record is None:
        return 0.0
    values = [item for group in record.methods.get(POWER, ()) for item in group]
    if len(values) != 2 or values[0][0] != 6 or values[0][1] < 0 or values[1][1] != 0x3044001:
        raise ValueError(f'PDML 热属性 {record.name} 缺少明确的恒定总瓦数')
    return values[0][1]


def solid_records(data, source_name, *, use_binary_material=True):
    records = read_records(data)
    references = {}
    for record in records:
        identity = record.scalar(OBJECT_KEY, kind=8)
        if identity:
            if identity in references:
                raise ValueError(f'PDML 对象引用标识重复（偏移 {record.offset}）')
            references[identity] = record

    root = ZERO, IDENTITY, (), True
    state = {}
    objects, assemblies, materials, monitors, regions = [], [], {}, [], []
    for record in records:
        position, axes, path, enabled = state.get(record.parent, root)
        state[record.offset] = position, axes, path, enabled
        if record.kind in (MATERIAL, THERMAL_ATTRIBUTE):
            continue
        if record.kind not in GEOMETRY_RECORDS and not any(
                code in record.methods for code in (SIZES, AXES, ORIGIN)):
            continue
        activity = record.scalar(ACTIVE, 0x306A001)
        if not enabled or activity != 0x306A001:
            state[record.offset] = position, axes, path, False
            continue
        label = record.name or f'object@{record.offset}'
        instance = (*path, label)
        observation_source = {'format':'PDML', 'file':source_name, 'offset':record.offset}
        if record.kind == MONITOR:
            if len(record.values(ORIGIN, 6)) != 3:
                raise ValueError(f'PDML 探针 {"/".join(instance)} 缺少明确位置（偏移 {record.offset}）')
            position = _position(record, position, axes)
            monitors.append(probe(f'pdmlProbe{record.offset}', label, '/'.join(instance), position, observation_source))
            continue
        if record.kind not in PRIMITIVES and record.kind not in (ASSEMBLY, REGION, HEATSINK, ENCLOSURE, *FLOW_RECORDS):
            raise ValueError(f'PDML 活动对象 {record.name} 类型 0x{record.kind:x} 暂无可验证的三维热模型映射')
        face = record.scalar(FACE,0x3015001)&0xff
        if face != 1 and record.kind in (ASSEMBLY,REGION):
            raise ValueError(f'PDML 装配或区域 {label} 包含压平几何')
        position = _position(record, position, axes)
        columns, transform, magnitudes = _columns(record, axes)
        if record.kind == ASSEMBLY:
            state[record.offset] = position, transform, instance, True
            assemblies.append({'path': list(instance), 'origin': list(position), 'axes': transform, 'offset': record.offset})
            continue
        if record.kind == REGION:
            _check_dimensions(record, magnitudes)
            low = [position[i]+sum(min(0, col[i]) for col in columns) for i in range(3)]
            high = [position[i]+sum(max(0, col[i]) for col in columns) for i in range(3)]
            observation = region(f'pdmlRegion{record.offset}', 'mesh-region', label, '/'.join(instance),
                                 low, high, observation_source)
            observation.update(origin_m=list(position), edge_vectors_m=[list(col) for col in columns])
            regions.append(observation)
            continue
        heatsink = None
        if record.kind == HEATSINK:
            if face != 1:
                raise ValueError(f'PDML 散热器 {label} 的压平模式尚无可验证的映射')
            shape, heatsink = pdml_heatsink(record,position,transform)
        else:
            shape = (_shape(record,position,columns,transform,magnitudes) if face == 1 else
                     _flattened_plate(record,face,position,columns,transform,magnitudes))
        obj = {'id': f'pdmlSolid{record.offset}', 'name': label, 'path': '/'.join(instance), 'record_offset': record.offset,
               'type':shape['kind'], 'mapping':'converted', 'shape':shape, 'source':observation_source}
        if heatsink:
            obj.update(type='heatsink', mapping='equivalent', heatsink=heatsink)
        if record.kind == ENCLOSURE:
            obj.update(type='enclosure', mapping='missing-info',
                       reason='外壳需要来自 ECXML 或用户配置的壁厚',
                       missing_fields=['wallThickness'])
        if face != 1:
            obj.update(mapping='equivalent',thin_plate={'source_size_m':magnitudes,'source_type':'flattened-record',
                                                        'face_mode':face})
        if record.kind in FLOW_RECORDS:
            kind, source_type = FLOW_RECORDS[record.kind]
            obj.update(id=f'pdmlFlow{record.offset}', type=kind,
                       flow_device={'source_type': source_type}, power_w=0)
            update_mapping(obj)
            objects.append(obj)
            continue
        if record.kind == PCB:
            obj.update(type='printedCircuitBoard',mapping='equivalent',
                       pcb={'model':'volume-fraction-laminate' if use_binary_material else 'supplied-effective-material',
                            'normal':list(transform[2])})
        if record.kind == COMPACT:
            resistances = record.values(0x2057014,kind=6)
            power = record.scalar(0x205701f,kind=6)
            if (record.scalar(0x2057003)!=0x3082001 or record.scalar(0x205700d)!=0x3080004 or
                    len(resistances)!=2 or power is None or not math.isfinite(power) or power<0):
                raise ValueError(f'PDML 紧凑器件 {label} 缺少确切的 2R 模式、两个热阻或总瓦数')
            obj.update(type='twoResistorModel',mapping='equivalent',power_w=power,
                       two_resistance=parameters(*resistances,transform[2]))
            if use_binary_material:
                materials.setdefault(MATERIAL_ID,dict(CASE_MATERIAL))
                obj['material'] = MATERIAL_ID
            objects.append(obj)
            continue
        if use_binary_material:
            if record.kind == PCB:
                key = f'pcbMaterial{record.offset}'
                materials[key] = _pcb_material(record,references,transform)
            else:
                record_material = _reference(record, references, MATERIAL_REFERENCE, MATERIAL)
                if record_material is None:
                    raise ValueError(f'PDML 对象 {label} 缺少可确认的材料引用')
                key = f'material{record_material.offset}'
                if key not in materials:
                    materials[key] = {'name': record_material.name, **_material(record_material)}
            obj.update(material=key, power_w=_power(_reference(record, references, THERMAL_REFERENCE, THERMAL_ATTRIBUTE)))
        objects.append(obj)
    if not objects:
        raise ValueError('PDML 缺少可导入的活动三维实体')
    return objects, materials, assemblies, monitors, regions
