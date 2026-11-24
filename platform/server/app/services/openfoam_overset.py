"""Verify a declared overset heat-transfer interface in native OpenFOAM."""

from collections import Counter
import gzip
import hashlib
import math
import re

from .casedict import parse_casedict
from .casedict_schema import interface_pair


SOLVER = 'overLaplacianDyMFoam'
MOTION_MESH = 'dynamicOversetFvMesh'
MESH_REGIONS = 'mesh-diagnostics/cellToRegion'
_FIELD_LIMIT = 16 * 1024 * 1024
_NAME = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,79}\Z')
_LABEL = re.compile(r'(?:0|[1-9][0-9]?)\Z')
_SUMMARY = re.compile(
    r'Overset analysis\s*:\s*nCells\s*:\s*(\d+)\s*\n'
    r'\s*calculated\s*:\s*(\d+)\s*\n'
    r'\s*interpolated\s*:\s*(\d+)[^\n]*\n\s*hole\s*:\s*(\d+)',
)


class OversetInputError(ValueError):
    pass


def _input(root, relative):
    path = root/relative
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('缺少可核验的 Overset 输入：'+relative)
    return path


def declaration(name, entry):
    settings = entry['overset']
    if (entry.get('type') != 'overset' or entry.get('coupling') != 'thermal'
            or not isinstance(settings,dict) or set(settings) != {'patch','zones','interpolation'}
            or not isinstance(settings['patch'],str) or not _NAME.fullmatch(settings['patch'])
            or settings['interpolation'] != 'inverseDistance'):
        raise ValueError('Overset 热耦合需要原生 patch、连续 zones 和 inverseDistance：'+name)
    zones = interface_pair(settings['zones'])
    if (not isinstance(zones,list) or len(zones)<2 or any(not _LABEL.fullmatch(str(value)) for value in zones)
            or [int(value) for value in zones] != list(range(len(zones)))):
        raise ValueError('Overset zones 必须是从 0 开始且连续的区域编号：'+name)
    return {'name':name,'type':'overset','coupling':'thermal','patch':settings['patch'],
            'zones':list(range(len(zones))),'interpolation':'inverseDistance'}


def declared(entries):
    if not isinstance(entries,dict):
        return []
    return [declaration(name,entry) for name,entry in sorted(entries.items())
            if isinstance(entry,dict) and 'overset' in entry]


def mesh_bindings(interfaces, patches):
    by_name = {patch['name']:patch for patch in patches}
    bindings = {}
    for interface in interfaces:
        patch = by_name.get(interface['patch'])
        if patch is None or patch['type'] != 'overset' or patch['nFaces'] <= 0:
            raise ValueError('Overset 声明没有对应的原生插值边界：'+interface['patch'])
        bindings[interface['name']]={**interface,'native_faces':patch['nFaces']}
    return bindings


def retain_mesh_regions(run, run_dir, previous_files):
    original = 'constant/cellToRegion'
    path = run/original
    if run_dir+'/'+original in previous_files or not path.exists():
        return
    source = _input(run,original)
    with source.open(encoding='ascii') as stream:
        header = stream.read(8192)
    if not re.search(r'\bobject\s+cellToRegion\s*;',header):
        raise ValueError('Overset 区域检查输出缺少原生字段标识')
    target = run/MESH_REGIONS
    target.parent.mkdir(exist_ok=True)
    if target.exists():
        raise ValueError('Overset 区域检查输出已存在')
    source.replace(target)


def configuration(root, run_dir, solver, entries, *, motion=None):
    interfaces = declared(entries)
    if not interfaces:
        if motion is not None:
            raise ValueError('移动 Overset 需要声明插值接口及区域编号')
        return None
    if solver != SOLVER:
        raise ValueError('当前 Overset 热耦合需要 overLaplacianDyMFoam')
    if len({tuple(interface['zones']) for interface in interfaces}) != 1:
        raise ValueError('同一个 Overset 网格须采用同一组区域编号')
    mesh = parse_casedict(_input(root,run_dir+'/constant/dynamicMeshDict').read_text(encoding='utf-8'))
    mesh_type = MOTION_MESH if motion else 'staticOversetFvMesh'
    if mesh.get('dynamicFvMesh') != mesh_type:
        raise ValueError('Overset 网格类型与声明的区域运动不一致，需要 '+mesh_type)
    schemes = parse_casedict(_input(root,run_dir+'/system/fvSchemes').read_text(encoding='utf-8'))
    interpolation = schemes.get('oversetInterpolation')
    if not isinstance(interpolation,dict) or interpolation.get('method') != 'inverseDistance':
        raise ValueError('原生 fvSchemes 的 Overset 插值方法与算例声明不一致')
    field = _input(root,run_dir+'/0/zoneID')
    if field.stat().st_size > _FIELD_LIMIT:
        raise ValueError('Overset zoneID 超过可核验大小')
    raw = field.read_bytes()
    text = re.sub(r'/\*.*?\*/|//[^\n]*',' ',raw.decode('ascii'),flags=re.S)
    header = re.search(r'\bFoamFile\s*\{([^{}]+)\}',text)
    metadata = parse_casedict(header[1]) if header else {}
    labels = re.search(r'\binternalField\s+nonuniform\s+List<scalar>\s+(\d+)\s*\(([^()]*)\)\s*;',text)
    if (metadata.get('class') != 'volScalarField' or metadata.get('object') != 'zoneID'
            or metadata.get('format') != 'ascii' or labels is None
            or not re.search(r'\bdimensions\s*\[\s*0(?:\s+0){6}\s*\]\s*;',text)):
        raise ValueError('Overset zoneID 需要无量纲的 ASCII 单元编号场')
    values = labels[2].split()
    if len(values) != int(labels[1]) or any(not _LABEL.fullmatch(value) for value in values):
        raise ValueError('Overset zoneID 的单元数或编号不正确')
    counts = dict(sorted(Counter(values).items(),key=lambda item:int(item[0])))
    if list(map(int,counts)) != interfaces[0]['zones']:
        raise ValueError('Overset zoneID 与算例声明的区域不一致')
    result = {'interfaces':interfaces,'zone_cells':counts,'zone_id_sha256':hashlib.sha256(raw).hexdigest()}
    if motion:
        zone, velocity = motion
        result['motion'] = {'cell_zone':zone,'velocity_m_s':list(velocity)}
    return result


def _coordinates(root, relative):
    from .flotherm_foam_io import FoamFile
    from .flotherm_heat_balance import _points

    name = relative if (root/relative).exists() else relative+'.gz'
    path = _input(root,name)
    with (gzip.open if path.suffix == '.gz' else open)(path,'rb') as stream:
        data = stream.read(_FIELD_LIMIT+1)
    if len(data) > _FIELD_LIMIT:
        raise ValueError('Overset 移动网格 points 超过可核验大小')
    source = FoamFile(data,name)
    if source.class_name not in {b'pointField',b'vectorField'} or source.object_name != b'points':
        raise ValueError('Overset 网格缺少原生 points 向量场')
    points = _points(source)
    if any(not math.isfinite(value) for point in points for value in point):
        raise ValueError('Overset 网格坐标必须为有限数值')
    return points


def observed_motion(settings, log, root, label, *, write_precision):
    zone = settings['cell_zone']
    if f'Applying motion to cellZone: {zone}\n' not in log or 'linearMotion' not in log:
        raise ValueError('原生日志未证明声明的 Overset 线性运动区域')
    before = _coordinates(root,'constant/polyMesh/points')
    after = _coordinates(root,label+'/polyMesh/points')
    shift = [speed*float(label) for speed in settings['velocity_m_s']]
    if len(before) != len(after) or not before:
        raise ValueError('Overset 刚体运动的原始和移动点数不一致')
    scale = max(1.,max(abs(axis) for point in before for axis in point),math.hypot(*shift))
    slack = scale*max(2e-7,10.**(1-write_precision))
    if not math.isfinite(slack) or math.hypot(*shift) <= 2*slack:
        raise ValueError('Overset 目标位移小于网格 points 写出精度')
    moved, error = 0, 0.
    for start,end in zip(before,after):
        if start == end:
            continue
        delta = [b-a for a,b in zip(start,end)]
        discrepancy = math.dist(delta,shift)
        if not math.isfinite(discrepancy) or discrepancy > slack:
            raise ValueError('Overset 网格 points 与声明的刚体位移不一致')
        moved += 1
        error = max(error,discrepancy)
    if not 0 < moved < len(before):
        raise ValueError('Overset 需要原生证据区分静止网格与运动区域')
    return {**settings,'time_s':float(label),'translated_points':moved,'stationary_points':len(before)-moved,
            'expected_displacement_m':shift,'maximum_error_m':error}


def observed(settings, log):
    result = _SUMMARY.findall(log)
    declared = settings['zone_cells']
    zones = dict(re.findall(r'\bzone:(\d+)\s+nCells:(\d+)',log))
    if not result or {key:int(value) for key,value in zones.items()} != declared:
        raise ValueError('原生求解日志缺少匹配的 Overset 区域证据')
    count,calculated,interpolated,holes = map(int,result[-1])
    if count != sum(declared.values()) or count != calculated+interpolated+holes or not interpolated:
        raise ValueError('原生 Overset 单元分类或插值数不匹配')
    return {**settings,'total_cells':count,'calculated_cells':calculated,
            'interpolated_cells':interpolated,'hole_cells':holes}
