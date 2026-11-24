"""Read neutral ECXML solids into the platform's versioned thermal IR."""

import math
import xml.etree.ElementTree as xml

from .flotherm_ir import build_ir
from .flotherm_flow_devices import ECXML_FANS, ECXML_RESISTANCES, update_mapping
from .flotherm_geometry import IDENTITY, make_box_union, make_flat_plate, make_shape, plane_normal
from .flotherm_observations import bind_probes, probe, region
from .flotherm_parts import enclosure
from .flotherm_two_resistance import MATERIAL, MATERIAL_ID, parameters


def tag(element):
    return element.tag.rsplit('}', 1)[-1]


def child(element, name):
    if element is None:
        return None
    matches = [item for item in element if tag(item) == name]
    if len(matches) > 1:
        raise ValueError(f'ECXML 字段重复：{name}')
    return matches[0] if matches else None


def text(element, name):
    item = child(element, name)
    return (item.text or '').strip() if item is not None else ''


def number(element, name, *, default=None):
    value = text(element, name)
    if not value and default is not None:
        return default
    try:
        result = float(value)
    except ValueError as error:
        raise ValueError(f'ECXML 缺少有效的 {name} 数值') from error
    if not math.isfinite(result):
        raise ValueError(f'ECXML {name} 必须是有限数值')
    return result


def vector(element, name):
    item = child(element, name)
    return [number(item, axis) for axis in ('x', 'y', 'z')]


def bounds(element):
    low = vector(element, 'location')
    size = vector(element, 'size')
    high = [start+length for start, length in zip(low, size)]
    if any(not math.isfinite(end) or end <= start for start, end in zip(low, high)):
        raise ValueError('ECXML 的三维尺寸必须为正且处于有效坐标范围内')
    return low, size, high


def active(element):
    value = text(element, 'active').lower()
    if value in ('', 'true', '1', 'yes', 'on'):
        return True
    if value in ('false', '0', 'no', 'off'):
        return False
    raise ValueError(f'ECXML 的 active 状态无效：{value}')


def material(element, name):
    thermal = child(element, 'thermalConductivity')
    isotropic = child(thermal, 'isotropic')
    orthotropic = child(thermal, 'orthotropic')
    if (isotropic is None) == (orthotropic is None):
        raise ValueError(f'材料 {name} 需要唯一的各向同性或正交各向异性导热率')
    k = [number(orthotropic, axis) for axis in 'xyz'] if orthotropic is not None else number(isotropic, 'conductivity')
    density = number(element, 'density')
    heat_capacity = number(element, 'specific_heat')
    rho_cp = density * heat_capacity
    if min(*(k if isinstance(k, list) else [k]),density,heat_capacity,rho_cp) <= 0 or not math.isfinite(rho_cp):
        raise ValueError(f'材料 {name} 的导热率、密度和比热必须为正且有限')
    return {'conductivity_w_mk': k, 'density_kg_m3': density, 'specific_heat_j_kg_k': heat_capacity,
            'rho_cp': rho_cp}


def airflow(item, kind, label):
    device = {'source_type': kind}
    is_fan = kind in ECXML_FANS
    plane = text(item, 'plane')
    direction = plane_normal(plane) if plane else None
    if direction is not None and is_fan:
        device['direction'] = list(direction)
    if is_fan:
        if child(item, 'hubSize') is not None:
            device['source_hub_size'] = number(item, 'hubSize')
        definition = child(item, 'flowDefinition')
        if child(definition, 'fixedFlowRate') is not None:
            device['fixed_flow_m3_s'] = number(definition, 'fixedFlowRate')
        curve = child(definition, 'fanCurve')
        if curve is not None:
            device['pq_curve'] = sorted([number(point, 'volumeFlow'), number(point, 'pressure')]
                                        for point in curve if tag(point) == 'fanCurvePoint')
    position, size = vector(item, 'location'), vector(item, 'size')
    if min(size) < 0:
        raise ValueError(f'{label} 的原始尺寸不能为负数')
    if kind in ('rectangular2dFan', 'grille') or 0 in size:
        if direction is None and size.count(0) == 1:
            direction = [float(length == 0) for length in size]
        if direction is None:
            raise ValueError(f'{label} 缺少可确认的平面几何')
        axis = next(i for i, value in enumerate(direction) if value)
        edges = [[size[j] if i == j else 0 for i in range(3)] for j in range(3) if j != axis]
        thickness = number(item, 'thickness') if child(item, 'thickness') is not None else None
        shape = make_flat_plate(position, edges, direction, thickness)
    else:
        position, size, _ = bounds(item)
        shape = {'kind': 'box', 'origin': position, 'dimensions': size}
    obj = {'type': 'fan' if is_fan else 'resistance', 'flow_device': device, 'shape': shape, 'power_w': 0}
    update_mapping(obj)
    return obj


def heatsink(item, label):
    boxes, materials, powers, parts = [], set(), [], []
    inactive = 0
    inherited = text(item,'material')
    for part in child(item,'parts') or ():
        kind = tag(part)
        if kind in ('name','active'):
            continue
        if not active(part):
            inactive += 1
            continue
        if kind not in ('solid3dBlock','sourceBlock'):
            raise ValueError(f'散热器 {label} 包含未识别的零件类型 {kind}')
        low,_,high = bounds(part)
        boxes.append([low,high])
        materials.add(text(part,'material') or inherited)
        power = number(part,'powerDissipation',default=0.0)
        if power<0:
            raise ValueError(f'散热器 {label} 的零件功率不得为负')
        powers.append(power)
        parts.append({'name':text(part,'name') or f'part {len(parts)+1}','bounds_m':[low,high],'power_w':power})
    if not boxes or len(materials)!=1:
        raise ValueError(f'散热器 {label} 需要活动的 parts 及唯一明确的等效材料')
    return (make_box_union((0,0,0),IDENTITY,boxes), materials.pop(), math.fsum(powers), inactive,
            {'heatsink':{'family':'explicitParts','part_count':len(parts),'source_parts':parts}})


def neutral_model(data, source_name):
    try:
        root = xml.fromstring(data)
    except (xml.ParseError, LookupError) as error:
        raise ValueError('ECXML 格式无效') from error
    if tag(root) != 'neutralXML':
        raise ValueError('ECXML 根元素必须为 neutralXML')
    solution = child(root, 'solutionDomain')
    if solution is None:
        raise ValueError('ECXML 缺少显式 solutionDomain')
    low, _, high = bounds(solution)
    temperature = number(child(solution, 'ambientConditions'), 'temperature')
    if temperature <= 0:
        raise ValueError('ECXML 环境温度必须是正的开尔文温度')
    definitions = {}
    for item in child(root, 'materials') or ():
        if tag(item) != 'material':
            continue
        name = text(item, 'name')
        if not name or name in definitions:
            raise ValueError('ECXML 材料名称为空或重复')
        definitions[name] = item
    geometry = child(root, 'geometry')
    if geometry is None:
        raise ValueError('ECXML 缺少 geometry')

    objects, assemblies, materials, monitors = [], [], {}, []
    domain_name = text(solution, 'name') or 'solutionDomain'
    regions = [region('ecxmlDomain', 'domain', domain_name, domain_name, low, high,
                      {'format':'ECXML', 'file':source_name, 'element':'solutionDomain'})]
    disabled = 0

    def walk(container, ancestry, indices=()):
        nonlocal disabled
        if len(ancestry) > 128:
            raise ValueError('ECXML 装配嵌套超过 128 层')
        for index, item in enumerate(container):
            kind = tag(item)
            if kind in ('name', 'active'):
                continue
            if not active(item):
                disabled += 1
                continue
            if kind in ('geometry', 'assembly'):
                label = text(item, 'name') or f'{kind} {index+1}'
                # ECXML solid locations are global. Keep hierarchy for provenance.
                if kind == 'assembly' and any(child(item, field) is not None for field in ('transform', 'rotation', 'location')):
                    raise ValueError(f'装配 {label} 包含显式坐标变换，当前导入无法核验其全局位置')
                path = (*ancestry, label)
                if kind == 'assembly':
                    assemblies.append({'path': list(path)})
                walk(item, path, (*indices, index))
                continue
            label = text(item, 'name') or f'{kind} {index+1}'
            display = '/'.join((*ancestry, label))
            if kind == 'monitorPoint':
                monitors.append(probe(f'ecxmlProbe{len(monitors)+1}', label, display, vector(item, 'location'),
                                      {'format':'ECXML', 'file':source_name, 'element':display,
                                       'indices':[*indices, index]}))
                continue
            if kind == 'region':
                start, _, end = bounds(item)
                regions.append(region(f'ecxmlRegion{len(regions)}', 'mesh-region', label, display, start, end,
                                      {'format':'ECXML', 'file':source_name, 'element':display,
                                       'indices':[*indices, index]}))
                continue
            if kind in ECXML_FANS or kind in ECXML_RESISTANCES:
                obj = airflow(item, kind, display)
                origin, dimensions = obj['shape']['origin'], obj['shape']['dimensions']
                if any(a < c or a+b > d for a, b, c, d in zip(origin, dimensions, low, high)):
                    raise ValueError(f'对象 {display} 超出计算域')
                objects.append({**obj, 'id': f'ecxmlFlow{len(objects)+1}', 'name': label, 'path': display,
                                'source': {'format': 'ECXML', 'file': source_name, 'element': display,
                                           'indices': [*indices, index]}})
                continue
            if kind not in ('solid3dBlock','sourceBlock','solidCylinder','solid2dBlock','printedCircuitBoard',
                            'twoResistorModel','heatsink','enclosure'):
                raise ValueError(f'对象 {display} 的类型 {kind} 暂无法导入三维热模型')
            device = None
            details, material_name, parts_power = {}, text(item,'material'), 0
            if kind == 'heatsink':
                shape, material_name, parts_power, skipped, details = heatsink(item,display)
                disabled += skipped
                position = shape['origin']
                end = [a+b for a,b in zip(position,shape['dimensions'])]
            elif kind in ('solid2dBlock','twoResistorModel'):
                position, size = vector(item,'location'),vector(item,'size')
                if any(length<0 for length in size):
                    raise ValueError(f'{display} 的原始尺寸不能为负数')
                direction = plane_normal(text(item,'plane'))
                if kind == 'twoResistorModel':
                    device = parameters(number(item,'thetaJC'),number(item,'thetaJB'),direction)
                if kind == 'solid2dBlock' or size[next(i for i,value in enumerate(direction) if value)] == 0:
                    edges = [[size[j] if i==j else 0 for i in range(3)] for j in range(3) if not direction[j]]
                    thickness = number(item,'thickness') if child(item,'thickness') is not None else None
                    shape = make_flat_plate(position,edges,direction,thickness)
                    end = [point+span for point,span in zip(position,shape['dimensions'])]
                else:
                    position, size, end = bounds(item)
                    shape = {'kind':'box','origin':position,'dimensions':size}
            else:
                position, size, end = bounds(item)
                shape = {'kind':'box','origin':position,'dimensions':size}
                if kind == 'enclosure':
                    shape, shell = enclosure(position,size,number(item,'wallThickness'))
                    details = {'enclosure':{**shell,'thickness_source':'ECXML.wallThickness'}}
            if any(a < c or b > d for a, b, c, d in zip(position, end, low, high)):
                raise ValueError(f'对象 {display} 超出计算域')
            if device:
                if MATERIAL_ID in definitions:
                    raise ValueError('ECXML 材料名称与 2R 等效壳体保留名称冲突')
                name = MATERIAL_ID
                materials.setdefault(name,dict(MATERIAL))
            else:
                name = material_name
                if name not in definitions:
                    raise ValueError(f'对象 {display} 缺少可确认的材料：{name or "未设置"}')
                if name not in materials:
                    materials[name] = material(definitions[name], name)
            power = number(item, 'powerDissipation', default=0.0)
            if power < 0:
                raise ValueError(f'对象 {display} 的总功率不得为负')
            if power and parts_power:
                raise ValueError(f'散热器 {display} 同时声明整体和零件功率，请明确总瓦数')
            power += parts_power
            if kind == 'solidCylinder':
                normal = {'yz':0,'xz':1,'xy':2}.get(text(item,'plane').removeprefix('+').removeprefix('-'))
                if normal is None:
                    raise ValueError(f'圆柱 {display} 缺少有效的 plane')
                radial = [axis for axis in range(3) if axis != normal]
                base = [point+width/2 if i in radial else point for i,(point,width) in enumerate(zip(position,size))]
                axes = [[size[j] if i==j else 0 for i in range(3)] for j in (*radial,normal)]
                shape = make_shape('cylinder',base,axes)
            objects.append({'id': f'ecxmlSolid{len(objects)+1}', 'path': display, 'name': label,
                            'source': {'format': 'ECXML', 'file': source_name, 'element': display,
                                       'indices': [*indices, index]},
                            'type':kind if kind in ('printedCircuitBoard','twoResistorModel','heatsink','enclosure') else shape['kind'],
                            'mapping':'equivalent' if kind in ('solid2dBlock','printedCircuitBoard','twoResistorModel','heatsink','enclosure') else 'converted',
                            'material':name, 'power_w':power, 'shape':shape,
                            **details,
                            **({'two_resistance':device} if device else {}),
                            **({'thin_plate':{'source_size_m':size,'source_type':kind}} if kind == 'solid2dBlock' else {}),
                            **({'pcb':{'model':'supplied-effective-material', 'normal':plane_normal(text(item,'plane'))}}
                               if kind == 'printedCircuitBoard' else {})})

    walk(geometry, ())
    if not objects:
        raise ValueError('ECXML 缺少可导入的活动三维实体')
    model = build_ir(source_name, data, objects, materials, monitors=bind_probes(monitors, objects))
    model['source']['format'] = 'ECXML'
    model['source']['inactive_entries'] = disabled
    model['assembly']['groups'] = assemblies
    model['regions'] = regions
    model['source_conditions'] = {'domain': low+high, 'ambient_temperature_k': temperature}
    return model
