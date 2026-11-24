"""Apply an archived CalculiX displacement field to a conformal OpenFOAM wall."""

import gzip
import math
import re
from uuid import UUID

from . import openfoam_deformation as deformation
from .case_boundary_inputs import reference_names, scalar
from .casedict_schema import valid_cell_zone
from .conformal_points import matched_nodes
from .flotherm_foam_io import FoamFile
from .flotherm_heat_balance import _faces, _points
from .openfoam_boundary_inputs import _existing
from .tree_metadata_native import checked, name as safe_name


MAX_NATIVE_BYTES = 16 * 1024 * 1024


def declaration(name, interface):
    settings = interface['structuralDisplacement']
    required = {'publicationId','inputPath','sourceTime','movingPatch','fixedPatch','freePatches'}
    if (interface.get('type') not in {'coupledWall','conformal'} or interface.get('coupling')!='fluidStructure'
            or not isinstance(settings,dict) or not required<=settings.keys()<=(required|{'matchTolerance'})):
        raise ValueError('结构位移需要流固耦合接口、原生发布、时刻和全部边界：'+name)
    try:
        published = str(UUID(str(settings['publicationId'])))
        path = safe_name(settings['inputPath'])
        if not path.lower().endswith('.inp') or len(path.encode('utf8'))>125:
            raise ValueError('Invalid CalculiX input path')
    except (AttributeError,TypeError,ValueError) as error:
        raise ValueError('结构位移需要 CalculiX 发布 ID 和原始 INP 路径') from error
    moving, fixed = settings['movingPatch'], settings['fixedPatch']
    free = reference_names(settings['freePatches'])
    names = [moving,fixed,*free]
    if any(not valid_cell_zone(patch) for patch in names) or len(set(names))!=len(names):
        raise ValueError('结构位移需要互不重复的网格边界名称')
    time = scalar(settings['sourceTime'])
    tolerance = scalar(settings.get('matchTolerance',1e-7))
    if time<=0 or tolerance<=0:
        raise ValueError('结构位移时刻与匹配容差须为正数')
    return {'interface':name, 'publication_id':published, 'input_path':path, 'frd_path':path[:-4]+'.frd',
            'source_time':time, 'moving_patch':moving, 'fixed_patch':fixed, 'free_patches':sorted(free),
            'match_tolerance_m':tolerance}


def selected(case):
    interfaces = case.get('interfaces')
    if not isinstance(interfaces,dict):
        return None
    plans = [declaration(name,item) for name,item in interfaces.items()
             if isinstance(item,dict) and 'structuralDisplacement' in item]
    if len(plans)>1:
        raise ValueError('一个原生求解只允许一组结构位移边界')
    return plans[0] if plans else None


def read_archive(prefix, publication, settings):
    from .file_read import ArchivedPublication, FileReadConflict

    try:
        with ArchivedPublication(prefix,publication) as archive:
            descriptor = archive.manifest['descriptor']
            if descriptor.get('handler')!='calculix.v1' or descriptor.get('input')!=settings['input_path']:
                raise ValueError('结构反馈发布与所选 CalculiX 原件不一致')
            member = archive.member(settings['frd_path'])
            if not 0<member.size<=MAX_NATIVE_BYTES:
                raise ValueError('结构位移需要完整且大小可核验的 FRD 文件')
            return b''.join(member.chunks())
    except FileReadConflict as error:
        raise ValueError('所选 CalculiX 归档已变化') from error


def _read(root, relative):
    path = checked(root, relative)
    opener = gzip.open if path.suffix=='.gz' else open
    with opener(path,'rb') as source:
        raw = source.read(MAX_NATIVE_BYTES+1)
    if len(raw)>MAX_NATIVE_BYTES:
        raise ValueError('结构位移输入文件超过可核验大小：'+relative)
    return raw


def _mesh_file(root, relative):
    name = relative if (root/relative).is_file() else relative+'.gz'
    return FoamFile(_read(root,name),name)


def frd_displacements(raw, time):
    try:
        text = raw.decode('ascii')
    except UnicodeDecodeError as error:
        raise ValueError('结构位移需要包含 DISP 结果的 ASCII CalculiX FRD') from error
    positions, values, records = {}, None, None
    dataset = None
    for line in text.splitlines():
        if line.startswith('    2C'):
            if positions:
                raise ValueError('CalculiX FRD 包含多个网格节点表')
            records = positions
        elif line.startswith('  100CL'):
            columns = line.split()
            if len(columns)<4:
                raise ValueError('CalculiX FRD 时刻记录无效')
            dataset = scalar(columns[2])
        elif line.startswith(' -4'):
            if line.split()[1:2]==['DISP'] and dataset==time:
                if values is not None:
                    raise ValueError('CalculiX FRD 在所选时刻包含多组位移')
                values = {}
                records = values
        elif line.startswith(' -3'):
            records = None
        elif records is not None and line.startswith(' -1'):
            node = int(line[3:13])
            if node in records:
                raise ValueError('CalculiX 位移结果包含重复节点')
            records[node] = [scalar(line[index:index+12]) for index in (13,25,37)]
    if not positions or values is None or positions.keys()!=values.keys():
        raise ValueError('CalculiX FRD 在所选时刻缺少完整的原生 DISP 节点结果')
    return positions,values


def patch_geometry(root, run_dir, settings):
    from .mesh_artifacts import boundaries

    prefix = run_dir+'/constant/polyMesh/'
    patches = {item['name']:item for item in boundaries(checked(root,prefix+'boundary'))}
    moving, fixed = settings['moving_patch'], settings['fixed_patch']
    if (set(patches)!={moving,fixed,*settings['free_patches']}
            or patches[moving]['type']!='wall' or patches[fixed]['type'] not in {'wall','patch'}):
        raise ValueError('结构反馈边界须覆盖原生网格，移动边界须为 wall')
    points = _points(_mesh_file(root,prefix+'points'))
    faces = _faces(_mesh_file(root,prefix+'faces'),quadrilaterals=False)

    def point_ids(patch):
        start, count = patches[patch]['startFace'], patches[patch]['nFaces']
        if count<1 or start+count>len(faces):
            raise ValueError('结构反馈边界没有有效的原生网格面')
        # OpenFOAM PrimitivePatch::calcMeshData numbers points by first encounter.
        return list(dict.fromkeys(point for face in faces[start:start+count] for point in face))

    moving_ids, fixed_ids = point_ids(moving), point_ids(fixed)
    if set(moving_ids)&set(fixed_ids) or max((*moving_ids,*fixed_ids))>=len(points):
        raise ValueError('结构反馈的移动和固定点不可相交，且须属于原生网格')
    return points,moving_ids,fixed_ids


def _vectors(raw, settings, geometry):
    points,nodes,_ = geometry
    coordinates,displacements = frd_displacements(raw,settings['source_time'])
    matched = matched_nodes([points[node] for node in nodes],coordinates,settings['match_tolerance_m'])
    return [displacements[node] for node in matched]


def displacement_dictionary(settings, vectors):
    body = '\n'.join('('+' '.join(f'{value:.16g}' for value in offset)+')' for offset in vectors)
    fixed,moving = settings['fixed_patch'],settings['moving_patch']
    return ('FoamFile { version 2.0; format ascii; class pointVectorField; object pointDisplacement; }\n'
            'dimensions [0 1 0 0 0 0 0];\ninternalField uniform (0 0 0);\nboundaryField\n{\n'
            f' {fixed} {{ type fixedValue; value uniform (0 0 0); }}\n'
            f' {moving} {{ type fixedValue; value nonuniform List<vector> {len(vectors)}\n(\n{body}\n); }}\n'
            + ''.join(f' {patch} {{ type zeroGradient; }}\n' for patch in settings['free_patches'])+'}\n').encode('ascii')


def prepared_inputs(root, run_dir, settings, *, sealed=False):
    velocity = _existing(root,run_dir+'/0/U')
    bound = (velocity or {}).get('boundaryField',{}).get(settings['moving_patch'],{})
    if bound.get('type')!='movingWallVelocity':
        raise ValueError('结构反馈移动边界的原生 U 需要 movingWallVelocity')
    if not sealed:
        for relative in (deformation.DYNAMIC,deformation.DISPLACEMENT):
            if any((root/run_dir/(relative+suffix)).exists() for suffix in ('','.gz')):
                raise ValueError('结构反馈需要独立生成动态网格控制和点位移文件')
    return patch_geometry(root,run_dir,settings)


def generated(run_dir, settings, geometry, data):
    return {run_dir+'/'+deformation.DYNAMIC:deformation.dynamic_dictionary(None),
            run_dir+'/'+deformation.DISPLACEMENT:displacement_dictionary(settings,_vectors(data,settings,geometry))}


def verify(root, run_dir, settings):
    geometry = prepared_inputs(root,run_dir,settings,sealed=True)
    data = _read(root,settings['frd_path'])
    for path, expected in generated(run_dir,settings,geometry,data).items():
        if _read(root,path)!=expected:
            raise ValueError('封存的结构位移字段与 CalculiX 归档不一致')


def observed(root, run_dir, settings, label, log, precision):
    if ('Selecting motion solver: '+deformation.MOTION_SOLVER not in log
            or not re.search(r'Solving for cellDisplacement[xyz]',log)):
        raise ValueError('原生日志未证明结构位移驱动网格变形')
    geometry = patch_geometry(root,run_dir,settings)
    points,moving,fixed = geometry
    offsets = _vectors(_read(root,settings['frd_path']),settings,geometry)
    final = _points(_mesh_file(root,run_dir+'/'+label+'/polyMesh/points'))
    if len(points)!=len(final):
        raise ValueError('结构位移过程中原生网格点数量变化')
    bound = 10.**(2-precision)*max(1.,max(abs(axis) for point in final for axis in point))
    errors = [math.dist(final[node],[value+component for value,component in zip(points[node],delta)])
              for node,delta in zip(moving,offsets,strict=True)]
    errors.extend(math.dist(final[node],points[node]) for node in fixed)
    if not all(math.isfinite(error) and error<=bound for error in errors):
        raise ValueError('OpenFOAM 实际点位移与 CalculiX 节点结果不一致')
    return {**settings,'matched_points':len(moving),'fixed_points':len(fixed),
            'maximum_displacement_m':max(math.hypot(*delta) for delta in offsets),
            'maximum_coordinate_error_m':max(errors),'foam_time_s':float(label)}
