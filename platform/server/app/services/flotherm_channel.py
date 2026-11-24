"""Check the latest OpenFOAM channel fluxes and optionally native wall heat flux."""
import json
import math
import re

from .flotherm_heat_balance import NUMBER, _faces, _labels, _points, _read, _read_bytes
from .flotherm_surface_results import mesh_patches, port_velocities, scalar_field
from .tree_metadata_native import checked, ordinary


SCHEMA = 'vibeflow.flotherm.channel-flow.v1'
_DIMENSIONS = {(0,3,-1,0,0,0,0): 'm3/s', (1,0,-1,0,0,0,0): 'kg/s'}
_PATH_WORD = re.compile(r'[A-Za-z_][A-Za-z0-9_.-]{0,127}')
_PORTS = (('ambient_xMin','ambient_xMax'), ('inlet','outlet'))


def _latest(root, region_dir):
    times = [path.name for path in root.iterdir()
             if re.fullmatch(NUMBER,path.name) and 0 < float(path.name) < math.inf and path.is_dir()]
    if not times:
        raise ValueError('未找到已求解的流道时刻')
    selected = max(times, key=float)
    if sum(float(value) == float(selected) for value in times) != 1:
        raise ValueError('最新流道时刻的目录重名')
    if any(checked(root,name+'/'+region_dir+'polyMesh').exists() for name in (*times,'0')):
        raise ValueError('流道回查需要固定的 constant/polyMesh 网格拓扑')
    ordinary(checked(root,selected))
    return selected


def _ports(patches, inlet, outlet):
    if inlet is None and outlet is None:
        candidates = [pair for pair in _PORTS if all(name in patches for name in pair)]
        if len(candidates) != 1:
            raise ValueError('请指定唯一的一对入口和出口边界')
        return candidates[0]
    if inlet == outlet or inlet not in patches or outlet not in patches:
        raise ValueError('入口和出口必须是两个存在的网格边界')
    return inlet, outlet


def _flow_rows(boundary):
    rows = {}
    for name, values in boundary.items():
        outward = math.fsum(value for value in values if value > 0)
        inward = math.fsum(-value for value in values if value < 0)
        rows[name] = {'name':name,'faces':len(values),
                      'inward':inward,'outward':outward,'net_outward':outward-inward}
    return rows


def _report_path(root, region, function_name, moment, filename='wallHeatFlux.dat'):
    choices = (f'postProcessing/{region}/{function_name}', f'postProcessing/{function_name}/{region}') if region else (
               f'postProcessing/{function_name}',)
    datasets = []
    for relative in choices:
        folder = checked(root,relative)
        if not folder.is_dir():
            continue
        ordinary(folder)
        for start in folder.iterdir():
            if start.is_dir() and re.fullmatch(NUMBER,start.name) and 0 <= float(start.name) <= float(moment):
                path = checked(root,f'{relative}/{start.name}/{filename}')
                if path.is_file():
                    datasets.append((float(start.name),path.relative_to(root).as_posix()))
    if not datasets:
        raise ValueError(f'未找到原生 {function_name}/{filename} 报告')
    latest = max(start for start, _ in datasets)
    current = [path for start,path in datasets if start == latest]
    if len(current) != 1:
        raise ValueError(f'原生 {function_name}/{filename} 报告目录不唯一')
    return current[0]


def _wall_rows(data, moment):
    result = {}
    for line in data.decode('utf8').splitlines():
        row = line.split()
        if not row or row[0].startswith('#'):
            continue
        if len(row) != 5:
            raise ValueError('原生 wallHeatFlux 报告列数无效')
        time = float(row[0])
        values = tuple(map(float,row[2:]))
        if not math.isfinite(time) or not all(map(math.isfinite,values)):
            raise ValueError('原生 wallHeatFlux 报告含无效数值')
        if time == float(moment):
            if row[1] in result:
                raise ValueError(f'原生 wallHeatFlux 包含重复时刻和边界：{row[1]}')
            result[row[1]] = values
    if not result:
        raise ValueError('原生 wallHeatFlux 缺少最新求解时刻')
    return result


def _area(points, face):
    if len(set(face)) != len(face) or any(label >= len(points) for label in face):
        raise ValueError('OpenFOAM 壁面顶点编号无效')
    first = points[face[0]]
    relative = [tuple(b-a for a,b in zip(first,points[label])) for label in face[1:]]
    cross = [(a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])
             for a,b in zip(relative,relative[1:])]
    area = math.hypot(*(math.fsum(axis) for axis in zip(*cross)))/2
    if not math.isfinite(area) or area <= 0:
        raise ValueError('OpenFOAM 壁面面积退化或无效')
    return area


def _walls(root, digests, moment, region, function, field, mesh, time_dir, patches, face_count, cell_count):
    report = _report_path(root,region,function,moment)
    native = _wall_rows(_read_bytes(root,report,digests),moment)
    if any(name not in patches or not patches[name].count for name in native):
        raise ValueError('原生 wallHeatFlux 包含未知或空边界')
    dimensions, values = scalar_field(_read(root,time_dir+field,digests),class_name=b'volScalarField',
                                      object_name=field.encode(),patches=patches,internal_count=cell_count)
    if dimensions != (1,0,-3,0,0,0,0):
        raise ValueError('壁面热流密度场的量纲必须为 W/m²')
    points = _points(_read(root,mesh+'points',digests))
    faces = _faces(_read(root,mesh+'faces',digests),quadrilaterals=False)
    if len(faces) != face_count or any(not math.isfinite(axis) for point in points for axis in point):
        raise ValueError('壁面热流所用的网格坐标或面数无效')
    rows, reasons = [], []
    for name, (expected_min,expected_max,expected_power) in native.items():
        patch = patches[name]
        areas = [_area(points,faces[index]) for index in range(patch.first,patch.first+patch.count)]
        observed_power = math.fsum(area*value for area,value in zip(areas,values[name]))
        if not math.isfinite(observed_power):
            raise ValueError('重算壁面功率不是有限值')
        observed = min(values[name]), max(values[name]), observed_power
        if not all(math.isclose(a,b,rel_tol=1e-5,abs_tol=1e-6)
                   for a,b in zip(observed,(expected_min,expected_max,expected_power))):
            reasons.append({'code':'wall_heat_flux_mismatch','patch':name})
        rows.append({'name':name,'area_m2':math.fsum(areas),'native_integral_w':expected_power,
                     'integral_from_field_w':observed_power,'minimum_w_m2':observed[0],'maximum_w_m2':observed[1]})
    return {'native_report':report,'field':time_dir+field,'patches':rows}, reasons


def _latest_scalar(root, digests, region, function, moment, *, filename='surfaceFieldValue.dat'):
    dataset = _report_path(root, region, function, moment, filename)
    matches = []
    for line in _read_bytes(root,dataset,digests).decode('utf8').splitlines():
        row = line.split()
        if row and not row[0].startswith('#'):
            if len(row) != 2 or not all(math.isfinite(float(value)) for value in row):
                raise ValueError(f'{function} 原生统计格式无效')
            if float(row[0]) == float(moment):
                matches.append(float(row[1]))
    if len(matches) != 1:
        raise ValueError(f'{function} 缺少唯一的最新时刻记录')
    return matches[0]


def _sensible_heat(root, digests, report, manifest, relative_tolerance, kinetic=None):
    channel = manifest['channel']
    wall = report['wall_heat_flux']['patches']
    if report['unit'] != 'kg/s' or sorted(row['name'] for row in wall) != sorted(channel['interfaces']):
        raise ValueError('流道显热核验需要质量通量和完整的流固界面数据')
    reference, cp = channel['reference_temperature_k'], channel['air_cp_j_kg_k']
    power = math.fsum(solid['power_w'] for solid in manifest['solid_regions'].values())
    through_wall = math.fsum(row['integral_from_field_w'] for row in wall)
    ports = {}
    for label in ('inlet','outlet'):
        mass = _latest_scalar(root,digests,report['region'],label+'Flow',report['time'])
        temperature = _latest_scalar(root,digests,report['region'],label+'Temperature',report['time'])
        if temperature <= 0 or not math.isclose(mass,report[label]['net_outward'],rel_tol=1e-5,abs_tol=1e-10):
            raise ValueError(f'{label} 的原生温度或质量流量与原始结果不一致')
        ports[label] = {'temperature_k':temperature, 'outward_sensible_w':mass*cp*(temperature-reference)}
    net = math.fsum(port['outward_sensible_w'] for port in ports.values())
    net_transport = net
    tolerance = max(1e-5,abs(power)*relative_tolerance)
    result = {'source_power_w':power, 'heat_into_fluid_w':through_wall, 'port_net_sensible_w':net,
              'reference_temperature_k':reference, 'tolerance_w':tolerance, 'ports':ports,
              'source_to_wall_residual_w':power-through_wall, 'wall_to_ports_residual_w':through_wall-net,
              'terms':['solid_source','fluid_interface_heat','port_sensible_advection']}
    mismatch = 'wall_to_ports_sensible_mismatch'
    if kinetic is not None:
        for label,value in kinetic.items():
            ports[label]['outward_kinetic_w'] = value
        net_kinetic = math.fsum(kinetic.values())
        net_transport = net+net_kinetic
        result.update(port_net_kinetic_w=net_kinetic, port_net_total_w=net_transport,
                      wall_to_ports_residual_w=through_wall-net_transport)
        result['terms'].append('port_kinetic_advection')
        mismatch = 'wall_to_ports_total_mismatch'
    if not all(math.isfinite(value) for value in (power,through_wall,net_transport,tolerance)):
        raise ValueError('流道显热结果不是有限值')
    reasons = [{'code':code} for code,value in (('source_to_wall_heat_mismatch',power-through_wall),
               (mismatch,through_wall-net_transport)) if abs(value) > tolerance]
    return result, reasons


def evaluate(root, *, region=None, inlet=None, outlet=None, relative_tolerance=.001,
             wall_heat_flux=None, wall_field='wallHeatFlux', energy_balance=False, energy_tolerance=.02,
             include_kinetic=False):
    digests = {}
    if include_kinetic and not energy_balance:
        raise ValueError('--include-kinetic 需要 --energy-balance')
    if energy_balance:
        manifest = json.loads(_read_bytes(root,'case-manifest.json',digests))
        channel = manifest.get('channel', {})
        if (manifest.get('application') != 'chtMultiRegionSimpleFoam' or not channel.get('flow_monitors') or
                manifest.get('required_overrides') or not channel.get('interfaces')):
            raise ValueError('显热对账需要通过 preflight 且启用监控的已生成流道算例')
        expected = manifest['fluid_region'], channel['inlet'], channel['outlet'], 'channelWallHeat', 'wallHeatFlux'
        chosen = region, inlet, outlet, wall_heat_flux, wall_field
        if any(actual is not None and actual != native for actual,native in zip(chosen,expected)):
            raise ValueError('显热对账需要使用生成算例的流体区、进出口和流固壁面监控')
        region, inlet, outlet, wall_heat_flux, wall_field = expected
        if not math.isfinite(energy_tolerance) or not 0 < energy_tolerance < 1:
            raise ValueError('显热相对容差必须大于 0 且小于 1')
    for value in (region,wall_heat_flux,wall_field):
        if value is not None and not _PATH_WORD.fullmatch(value):
            raise ValueError('区域、函数和场名称必须为 OpenFOAM 名称')
    if not math.isfinite(relative_tolerance) or not 0 < relative_tolerance < 1:
        raise ValueError('相对流量容差必须大于 0 且小于 1')
    region_dir = region+'/' if region else ''
    moment = _latest(root,region_dir)
    time_dir = moment+'/'+region_dir
    mesh = 'constant/'+region_dir+'polyMesh/'
    owners = _labels(_read(root,mesh+'owner',digests),'owner')
    neighbours = _labels(_read(root,mesh+'neighbour',digests),'neighbour')
    if not owners or len(neighbours) > len(owners):
        raise ValueError('流道网格面拓扑不完整')
    patches = mesh_patches(_read(root,mesh+'boundary',digests),
                           internal_faces=len(neighbours),total_faces=len(owners))
    inlet, outlet = _ports(patches,inlet,outlet)
    dimensions, values = scalar_field(_read(root,time_dir+'phi',digests),class_name=b'surfaceScalarField',
                                      object_name=b'phi',patches=patches,internal_count=len(neighbours))
    unit = _DIMENSIONS.get(dimensions)
    if unit is None:
        raise ValueError('phi 量纲必须为体积流量或质量流量')
    rows = _flow_rows(values)
    incoming, outgoing = rows[inlet]['net_outward'], rows[outlet]['net_outward']
    tolerance = max(1e-12,relative_tolerance*max(abs(incoming),abs(outgoing)))
    total = math.fsum(row['net_outward'] for row in rows.values())
    other = math.fsum(row['inward']+row['outward'] for name,row in rows.items() if name not in (inlet,outlet))
    reasons = []
    if incoming >= 0 or outgoing <= 0:
        reasons.append({'code':'unexpected_flow_direction','inlet':inlet,'outlet':outlet})
    if abs(total) > tolerance:
        reasons.append({'code':'flow_conservation_mismatch'})
    if other > tolerance:
        reasons.append({'code':'additional_boundary_flow'})
    report = {'schema':SCHEMA,'region':region,'time':moment,'unit':unit,'inlet':rows[inlet],
              'outlet':rows[outlet],'patches':list(rows.values()),'net_outward':total,
              'other_boundary_throughput':other,'tolerance':tolerance,'result_sha256':digests}
    if wall_heat_flux:
        checked_flux, mismatches = _walls(root,digests,moment,region,wall_heat_flux,wall_field,mesh,time_dir,
                                         patches,len(owners),max(max(owners),max(neighbours,default=-1))+1)
        report['wall_heat_flux'] = checked_flux
        reasons.extend(mismatches)
    if energy_balance:
        kinetic = None
        if include_kinetic:
            velocities = port_velocities(_read(root,time_dir+'U',digests),patches,(inlet,outlet))
            kinetic = {label:math.fsum(flux*sum(component*component for component in vector)*.5
                                      for flux,vector in zip(values[port],velocities[port]))
                       for label,port in (('inlet',inlet),('outlet',outlet))}
        report['energy_balance'], mismatches = _sensible_heat(root,digests,report,manifest,energy_tolerance,kinetic)
        reasons.extend(mismatches)
    return {**report,'ok':not reasons,'reasons':reasons}
