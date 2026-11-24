"""Build native OpenFOAM volume refinement from FloTHERM regions and objects."""

import math

from .flotherm_geometry import cross, dot, unit_axes


def refinement_level(value):
    if type(value) is not int or not 0 <= value <= 8:
        raise ValueError('refinement 必须为 0–8 整数')
    return value


def region_levels(settings, regions):
    values = settings.get('refinement', {})
    if not isinstance(values, dict):
        raise ValueError('refinement 需要按原始网格区域 ID 索引的对象')
    known = {row['id'] for row in regions if row['type']=='mesh-region'}
    if set(values)-known:
        raise ValueError(f'refinement 引用未知网格区域: {sorted(set(values)-known)}')
    return {name:refinement_level(level) for name,level in values.items()}


def _tuple(values):
    return '('+' '.join(format(value,'.16g') for value in values)+')'


def _region_geometry(row):
    bounds = row['bounds_m']
    if 'edge_vectors_m' not in row:
        return {'boxes':[[bounds[:3],bounds[3:]]]}
    origin, edges = row['origin_m'], row['edge_vectors_m']
    axes = unit_axes(edges)
    low = [origin[i]+math.fsum(min(0,axis[i]) for axis in edges) for i in range(3)]
    high = [origin[i]+math.fsum(max(0,axis[i]) for axis in edges) for i in range(3)]
    if any(not math.isclose(a,b,rel_tol=1e-9,abs_tol=1e-12) for a,b in zip(bounds,low+high)):
        raise ValueError('网格区域包围盒与原始有向边不一致')
    if all(sum(abs(value)>1e-12 for value in axis)==1 for axis in axes):
        return {'boxes':[[low,high]]}
    if dot(cross(axes[0],axes[1]),axes[2]) < 0:
        origin = [a+b for a,b in zip(origin,edges[1])]
        edges = [edges[0],[-value for value in edges[1]],edges[2]]
    return {'oriented_box':{'origin_m':origin,'edges_m':edges}}


def refinement_plan(ir, domain, solids, devices, surface_mesh):
    targets, blockers = [], []
    regions = ir.get('regions', ())
    levels = region_levels(ir['settings'],regions)
    for row in regions:
        level = levels.get(row['id'], 0)
        if not level:
            continue
        bounds = row['bounds_m']
        if any(a<c and not math.isclose(a,c,abs_tol=1e-12) or b>d and not math.isclose(b,d,abs_tol=1e-12)
               for a,b,c,d in zip(bounds,bounds[3:],domain,domain[3:])):
            blockers.append({'object':row['id'],'reason':'请求细化的网格区域超出当前计算域'})
            continue
        targets.append({'id':row['id'],'level':level,'source':'settings.refinement',
                        **_region_geometry(row)})
    zones = {row['id']:(name,row) for name,row in solids.items()}
    merge_members = {member['id']:member for row in solids.values()
                     for member in row.get('merged_boxes',{}).get('members', ())}
    devices_by_id = {row['id']:row for row in devices}
    for obj in ir['objects']:
        level = refinement_level(obj.get('refinement',0))
        if not level:
            continue
        oid = obj['id']
        if oid in merge_members:
            geometry = {'boxes':[merge_members[oid]['bounds_m']]}
        elif oid in zones:
            name, row = zones[oid]
            geometry = {'surface':name} if surface_mesh else {'boxes':row.get('mesh_boxes',[row['bounds_m']])}
            geometry['material_zone'] = name
        elif oid in devices_by_id:
            geometry = {'boxes':[devices_by_id[oid]['bounds_m']]}
        else:
            blockers.append({'object':oid,'reason':'逐对象细化需要已解析的实体或流体设备'})
            continue
        targets.append({'id':oid,'level':level,'source':'objectOverrides.refinement',**geometry})
    for index,row in enumerate(targets):
        row['name'] = f'refinement{index}'
    return targets, blockers


def refinement_entries(targets):
    definitions, entries = [], []
    for row in targets:
        name = row['name']
        if 'surface' in row:
            names = [row['surface']]
        elif 'oriented_box' in row:
            geometry = row['oriented_box']
            e1,_,e3 = unit_axes(geometry['edges_m'])
            span = [math.hypot(*axis) for axis in geometry['edges_m']]
            definitions.append(f'{name} {{ type searchableRotatedBox; span {_tuple(span)}; '
                               f'origin {_tuple(geometry["origin_m"])}; e1 {_tuple(e1)}; e3 {_tuple(e3)}; }}')
            names = [name]
        else:
            names = []
            for index,(low,high) in enumerate(row['boxes']):
                box = f'{name}_{index}'
                definitions.append(f'{box} {{ type searchableBox; min {_tuple(low)}; max {_tuple(high)}; }}')
                names.append(box)
        entries.extend(f'{key} {{ mode inside; levels ((1 {row["level"]})); }}' for key in names)
    return definitions, entries


def refinement_check_dict(targets):
    actions = []
    for row in targets:
        set_name = row['name']
        if 'material_zone' in row:
            selection = f'zoneToCell; zone {row["material_zone"]};'
        elif 'oriented_box' in row:
            box = row['oriented_box']
            vectors = ' '.join(f'{axis} {_tuple(vector)};' for axis,vector in zip('ijk',box['edges_m']))
            selection = f'rotatedBoxToCell; origin {_tuple(box["origin_m"])}; {vectors}'
        else:
            low,high = row['boxes'][0]
            selection = f'boxToCell; box {_tuple(low)} {_tuple(high)};'
        actions.extend((
            f'{{ name {set_name}; type cellSet; action new; source {selection} }}',
            f'{{ name {set_name}; type cellSet; action subset; source fieldToCell; field cellLevel; '
            f'min {row["level"]}; max 8; }}',
        ))
    return 'actions (\n'+'\n'.join(actions)+'\n);'
