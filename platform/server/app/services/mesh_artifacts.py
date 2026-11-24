"""Pure OpenFOAM input/boundary/proof helpers, shared by Linux worker and API."""
import hashlib
import json
from pathlib import Path
import re

from .case_evidence import case_input_fingerprint, file_record, safe_run_dir
from .case_mesh_schema import element_families
from .casedict import parse_casedict
from .mesh_jacobian import CELL_COUNTS, METHOD, VTK_EVIDENCE, measure
from .mesh_quality import evaluate_mesh_quality, gate_decision, mesh_block_hash
from . import (mesh_boundary_layers, mesh_import_source, mesh_presets, mesh_refinement, mesh_study_geometry,
               openfoam_cht, openfoam_deformation, openfoam_overset, openfoam_rotation, periodic_interfaces)

QUALITY = 'B-mesh/quality.json'
BOUNDARIES = 'B-mesh/boundaries.json'
TOOLCHAIN = 'B-mesh/toolchain.json'
ARTIFACTS = 'B-mesh/artifacts.json'
MESH_FILES = mesh_import_source.CORE_FILES
IMPORTED_GMSH = 'importedGmsh'
IMPORTED_POLY_MESH = 'importedPolyMesh'
POLY_MESH_INPUT = mesh_import_source.DIRECTORY
NATIVE_INPUT = {'blockMesh':'system/blockMeshDict', 'gmsh':'system/mesh.geo', IMPORTED_GMSH:'system/mesh.msh',
                'snappyHexMesh':'system/snappyHexMeshDict','cfMesh':'system/meshDict',
                IMPORTED_POLY_MESH:POLY_MESH_INPUT+'/points',
                openfoam_cht.MESH_SOURCE:'constant/regionProperties'}
EXTRA_NATIVE_INPUTS = {'snappyHexMesh':('system/blockMeshDict',)}
NATIVE_TOOLS = {'blockMesh':('blockMesh','checkMesh'), 'gmsh':('gmsh','gmshToFoam','checkMesh'),
                IMPORTED_GMSH:('gmshToFoam','checkMesh'), 'snappyHexMesh':('blockMesh','snappyHexMesh','checkMesh'),
                IMPORTED_POLY_MESH:('checkMesh',),
                'cfMesh':('cartesianMesh','checkMesh'),openfoam_cht.MESH_SOURCE:('checkMesh',)}
NATIVE_LOGS = {'blockMesh':('blockMesh.log',), 'gmsh':('gmsh.log','gmshToFoam.log','gmsh.msh'),
               IMPORTED_GMSH:('gmshToFoam.log','gmsh.msh'), 'snappyHexMesh':('blockMesh.log','snappyHexMesh.log'),
               'cfMesh':('cartesianMesh.log',),IMPORTED_POLY_MESH:()}
_SOURCE_ROLES={
    **dict.fromkeys(('blockMesh','gmsh','snappyHexMesh','cfMesh'),frozenset({'generated'})),
    IMPORTED_GMSH:{'imported','external'},
    IMPORTED_POLY_MESH:{'imported','solverNative','external'},
    openfoam_cht.MESH_SOURCE:{'imported','solverNative','external'},
}
_NATIVE_FAMILIES = {'hexahedra':'hex','prisms':'prism','wedges':'wedge','pyramids':'pyramid',
                    'tet wedges':'tetWedge','tetrahedra':'tet','polyhedra':'poly'}


def mesh_source(root):
    source=(parse_casedict((root/'0-caseDict/caseDict').read_text(encoding='utf8')).get('mesh') or {}).get('source')
    if not isinstance(source,str) or source not in NATIVE_INPUT:
        raise ValueError('Registered mesh requires mesh.source from '+', '.join(NATIVE_INPUT))
    return source


def required_outputs(run_dir, source, *, with_jacobian=False, with_artifacts=False, with_refinement=False):
    common = {QUALITY, BOUNDARIES, TOOLCHAIN} | ({ARTIFACTS} if with_artifacts else set())
    if source == openfoam_cht.MESH_SOURCE:
        return common | {regions_log(run_dir)}
    logs=list(NATIVE_LOGS[source])
    if with_jacobian:
        logs.extend(('foamToVTK.log', VTK_EVIDENCE))
    if with_refinement:
        logs.extend(mesh_refinement.LOGS)
    return common | {run_dir+'/checkMesh.log',
        *(run_dir+'/'+name for name in logs),*(run_dir+'/constant/polyMesh/'+name for name in MESH_FILES)}


def source_input_name(run,source):
    if source==IMPORTED_POLY_MESH:
        return mesh_import_source.core_paths(run)['points'].relative_to(run).as_posix()
    return NATIVE_INPUT[source]


def artifact_paths(mesh, run_dir, source, input_name):
    native = run_dir+'/constant' if source==openfoam_cht.MESH_SOURCE else run_dir+'/constant/polyMesh'
    paths = {'meshScript':run_dir+'/'+input_name,
             'meshFile':run_dir+'/gmsh.msh' if source in ('gmsh',IMPORTED_GMSH) else native,
             'convertedMesh':native, 'qualityReport':QUALITY, 'boundaryReport':BOUNDARIES}
    declared = mesh.get('artifacts', {})
    if not isinstance(declared,dict):
        raise ValueError('mesh.artifacts 必须是路径引用字典')
    for name, relative in declared.items():
        if name not in paths or relative != paths[name]:
            raise ValueError('mesh.artifacts.'+name+' 不匹配当前网格运行目录与输出')
    return paths


def validate_source_role(mesh,source):
    role=mesh.get('meshSource')
    if 'meshSource' in mesh and (not isinstance(role,str) or role not in _SOURCE_ROLES[source]):
        raise ValueError('mesh.meshSource 与当前原生网格来源不一致：'+source)


def gmsh_format(path):
    with path.open('rb') as stream:
        header = [stream.readline(128).strip() for _ in range(3)]
    if len(header[1].split())!=3 or header[0]!=b'$MeshFormat' or header[2]!=b'$EndMeshFormat':
        raise ValueError('Missing Gmsh native mesh format header')
    version, encoding, data_size = header[1].decode('ascii').split()
    if not re.fullmatch(r'2\.\d+',version) or encoding!='0' or data_size not in {'4','8'}:
        raise ValueError('Gmsh conversion requires ASCII MSH version 2')
    return version


def _conversion(root, run_dir, native_cells):
    source = root/run_dir/'gmsh.msh'
    log = root/run_dir/'gmshToFoam.log'
    version=gmsh_format(source)
    text = log.read_text(encoding='utf-8',errors='replace')
    if not re.search(r'^End\s*$',text,re.M):
        raise ValueError('gmshToFoam did not finish cleanly')
    match = re.search(r'^Cells:[ \t]*\n[ \t]+total\s*:\s*(\d+)[ \t]*\n'
                      r'((?:[ \t]+(?:hex|prism|pyr|tet)\s*:\s*\d+[ \t]*\n)+)',text,re.M)
    vertex_count = re.findall(r'^Vertices read:\s*(\d+)\s*$',text,re.M)
    element_count = re.findall(r'^Cells to be read:\s*(\d+)\s*$',text,re.M)
    if match is None or len(vertex_count)!=1 or len(element_count)!=1:
        raise ValueError('Gmsh conversion log lacks the native counts')
    cell_types = {name:int(count) for name,count in re.findall(r'^\s*(hex|prism|pyr|tet)\s*:\s*(\d+)\s*$',match[2],re.M)}
    if native_cells != int(match[1]) or sum(cell_types.values())!=native_cells or not native_cells:
        raise ValueError('Converted Gmsh cells differ from checkMesh')
    return {'tool':'gmshToFoam', 'format':'msh', 'version':version, 'encoding':'ascii',
            'input':file_record(root,source), 'log':file_record(root,log),
            'vertices':int(vertex_count[0]), 'input_elements':int(element_count[0]),
            'volume_cells':native_cells, 'cell_types':cell_types}


def artifact_index(root, run_dir, source, execution_id, regions, log_path, *, with_study_geometry=True):
    mesh = parse_casedict((root/'0-caseDict/caseDict').read_text(encoding='utf-8'))['mesh']
    paths = artifact_paths(mesh,run_dir,source,source_input_name(root/run_dir,source))
    directories = [paths['convertedMesh']] if not regions else [f'{run_dir}/constant/{name}/polyMesh' for name in sorted(regions)]
    members = [file_record(root,file) for directory in directories for file in (root/directory).rglob('*')
               if file.is_file()]
    members.sort(key=lambda record:record['path'])
    available = {record['path'] for record in members}
    if any(directory+'/'+name not in available for directory in directories for name in MESH_FILES):
        raise ValueError('Native mesh artifact index is missing core mesh files')
    counts = CELL_COUNTS.findall((root/log_path).read_text(encoding='utf-8',errors='replace'))
    if len(counts)!=len(directories) or any(int(count)<1 for count in counts):
        raise ValueError('Native mesh artifact index has no matching cell counts')
    input_file=file_record(root,root/paths['meshScript'])
    conversion=_conversion(root,run_dir,int(counts[0])) if source in ('gmsh',IMPORTED_GMSH) else None
    if source==IMPORTED_GMSH and any(input_file[field]!=conversion['input'][field] for field in ('size','sha256')):
        raise ValueError('Published Gmsh input differs from the imported mesh file')
    result={'schema':'vibeflow.mesh-artifacts.v1', 'run_dir':run_dir, 'execution_id':execution_id,
            'mesh_source':source, 'case_dict_artifacts':paths, 'declared':mesh.get('artifacts',{}),
            'mesh_source_input':input_file,
            'volume_cells':sum(int(count) for count in counts), 'native_mesh':members,
            'quality_report':file_record(root,root/QUALITY), 'boundary_report':file_record(root,root/BOUNDARIES),
            'conversion':conversion}
    if with_study_geometry and source=='blockMesh' and 'refinement' not in mesh:
        geometry_sha256=mesh_study_geometry.geometry_digest(root/paths['meshScript'])
        if geometry_sha256 is not None:
            result['mesh_study_geometry_sha256']=geometry_sha256
    if source==IMPORTED_POLY_MESH:
        originals=run_dir+'/'+POLY_MESH_INPUT
        imported=[file_record(root,file) for file in sorted((root/originals).rglob('*')) if file.is_file()]
        compressed={path.relative_to(root).as_posix():name for name,path in mesh_import_source.core_paths(root/run_dir).items()
                    if path.suffix=='.gz'}
        expected={}
        for record in imported:
            path=record['path']
            if path in compressed:
                expected[paths['convertedMesh']+'/'+compressed[path]]=mesh_import_source.expanded_record(root/path)
            else:
                expected[paths['convertedMesh']+path[len(originals):]]={key:record[key] for key in ('size','sha256')}
        actual={record['path']:{key:record[key] for key in ('size','sha256')} for record in members}
        if actual!=expected:
            raise ValueError('Published native mesh differs from its imported input files')
        result['mesh_source_files']=imported
    return result


def selection(root, run_dir):
    run_dir = safe_run_dir(run_dir)
    paths = ['0-caseDict/caseDict', run_dir+'/system']
    constant = root/run_dir/'constant'
    if constant.exists():
        paths.extend(item.relative_to(root).as_posix() for item in sorted(constant.iterdir())
                     if item.name != 'polyMesh')
    paths.extend(run_dir+'/'+name for name in ('0', '0.orig') if (root/run_dir/name).exists())
    return paths


def input_snapshot(root, run_dir):
    paths = selection(root, run_dir)
    records = []
    for relative in paths[1:]:
        target = root/relative
        members = [target] if target.is_file() else sorted(path for path in target.rglob('*') if path.is_file())
        records.extend(file_record(root, path) for path in members)
    return {'case_input_sha256':case_input_fingerprint((root/paths[0]).read_text(encoding='utf-8')),
        'run_dir':safe_run_dir(run_dir), 'selection':paths[1:],
        'files':{record['path']:{key:record[key] for key in ('size','sha256')} for record in records}}


def boundaries(path, *, include_mapping=False):
    text = re.sub(r'/\*.*?\*/|//[^\n]*', '', path.read_text(encoding='utf-8'), flags=re.S)
    header = re.search(r'FoamFile\s*\{[^}]*\}', text)
    if not header:
        raise ValueError('Boundary has no FoamFile header')
    body = text[header.end():].strip()
    match = re.fullmatch(r'(\d+)\s*\((.*)\)\s*', body, re.S)
    if not match:
        raise ValueError('Boundary list is malformed')
    items, end = [], 0
    for patch in re.finditer(r'("[^"]+"|[\w.-]+)\s*\{([^{}]*)\}', match[2]):
        if match[2][end:patch.start()].strip():
            raise ValueError('Unexpected boundary list content')
        values = parse_casedict(patch[2])
        item = {'name':patch[1].strip('"'), 'type':values.get('type'),
                'nFaces':int(values['nFaces']), 'startFace':int(values['startFace'])}
        if not isinstance(item['type'],str) or item['nFaces'] < 0 or item['startFace'] < 0:
            raise ValueError('Invalid mesh patch')
        if include_mapping and all(isinstance(values.get(key),str) for key in ('sampleRegion','samplePatch')):
            item['sample']=[values['sampleRegion'],values['samplePatch']]
        if include_mapping and isinstance(values.get('neighbourPatch'),str):
            item['neighbour']=values['neighbourPatch']
        items.append(item)
        end = patch.end()
    if (match[2][end:].strip() or len(items)!=int(match[1]) or not items or
            len({item['name'] for item in items})!=len(items)):
        raise ValueError('Boundary count or names are inconsistent')
    return items


def regions_log(run_dir):
    return f'{run_dir}/checkMesh-regions.log'


def imported_boundaries(root, run_dir, regions):
    return [dict(patch, region=name) for name in regions
            for patch in boundaries(root/run_dir/'constant'/name/'polyMesh/boundary',include_mapping=True)]


def geometric_dimension(log, expected=None):
    dimensions=re.findall(r'^[ \t]*Mesh has ([123]) geometric \(non-empty/wedge\) directions',log,re.M)
    if len(dimensions)!=1:
        raise ValueError('checkMesh did not prove a unique geometric dimension')
    measured=int(dimensions[0])
    if expected is not None and measured!=int(expected):
        raise ValueError(f'mesh.dimension 声明为 {expected}，原生网格实测为 {measured}')
    return measured


def family_evidence(log, declaration):
    allowed=element_families(declaration)
    heading='Overall number of cells of each type:'
    if log.count(heading)!=1:
        raise ValueError('checkMesh did not provide a unique native element inventory')
    body=log.split(heading,1)[1]
    pattern=r'^[ \t]+('+r'|'.join(map(re.escape,_NATIVE_FAMILIES))+r')[ \t]*:[ \t]*(\d+)[ \t]*$'
    rows=re.findall(pattern,body,re.M)
    sizes=CELL_COUNTS.findall(log)
    if (len(sizes)!=1 or len(rows)!=len(_NATIVE_FAMILIES)
            or len({name for name,_ in rows})!=len(rows)):
        raise ValueError('checkMesh element inventory is incomplete')
    counts={_NATIVE_FAMILIES[name]:int(count) for name,count in rows if int(count)}
    if sum(counts.values())!=int(sizes[0]) or not counts:
        raise ValueError('Native element counts do not match checkMesh volume cells')
    dimension=geometric_dimension(log)
    equivalent={1:{'hex':'line'},2:{'hex':'quad','prism':'tri'},3:{}}[dimension]
    unexpected={kind for kind in counts if kind not in allowed and equivalent.get(kind) not in allowed}
    if unexpected:
        raise ValueError('检查到声明之外的原生单元族：'+', '.join(sorted(unexpected)))
    return {'declared':sorted(allowed),'native_counts':counts,'geometric_dimension':dimension}


def imported_quality(root, run_dir, text, mesh, targets, *, check_families, check_dimension):
    regions = openfoam_cht.inspect_inputs(root, run_dir)
    log_file = root/regions_log(run_dir)
    log_record = file_record(root, log_file)
    segments = re.split(r'(?m)^Mesh stats ([A-Za-z_][A-Za-z0-9_.-]*)[ \t]*$',
                        log_file.read_text(encoding='utf8',errors='replace'))
    logs = dict(zip(segments[1::2], segments[2::2]))
    if len(segments) != 1+2*len(regions) or set(logs) != set(regions):
        raise ValueError('checkMesh region inventory differs from the declared OpenFOAM regions')
    records, metrics, violations = {}, {}, []
    for name, binding in regions.items():
        directory = root/run_dir/'constant'/name/'polyMesh'
        if any(not (directory/file).is_file() for file in MESH_FILES):
            raise ValueError('Missing required imported OpenFOAM mesh files for region '+name)
        log = logs[name]
        counts = CELL_COUNTS.findall(log)
        if len(counts) != 1 or int(counts[0]) < 1:
            raise ValueError('checkMesh did not prove a positive cell count for region '+name)
        measure = evaluate_mesh_quality(log,targets,registered=True)
        records[name] = {**binding, 'cell_count':int(counts[0]), 'quality':measure}
        if check_dimension and 'dimension' in mesh:
            records[name]['geometric_dimension']=geometric_dimension(log,mesh['dimension'])
        if check_families and 'elementFamilies' in mesh:
            records[name]['element_families']=family_evidence(log,mesh['elementFamilies'])
        for key,value in measure['metrics'].items():
            combine = min if key in {'jacobian','minimum_volume'} else max
            metrics[key] = combine(metrics.get(key,value),value)
        violations.extend({'region':name,**violation} for violation in measure['violations'])

    proof = {'schema':'vibeflow.mesh-quality.v1', 'metrics':metrics, 'violations':violations,
             'status':'fail' if violations else 'pass', 'policy':targets.get('policy','fail'),
             'mesh_ok':all(item['quality']['mesh_ok'] for item in records.values()), 'regions':records,
             'case_dict_mesh_hash':mesh_block_hash(mesh), 'quality_targets':targets,
             'run_dir':run_dir, 'case_input_sha256':case_input_fingerprint(text),
             'log_path':log_record['path'], 'log_sha256':log_record['sha256']}
    declared = parse_casedict(text).get('interfaces')
    if declared:
        bindings = openfoam_cht.interface_bindings(declared,regions,imported_boundaries(root,run_dir,regions))
        if bindings:
            proof['interface_bindings'] = bindings
    proof['gate'] = gate_decision(proof,targets)
    return proof


def quality_targets(root):
    mesh=parse_casedict((root/'0-caseDict/caseDict').read_text(encoding='utf-8')).get('mesh') or {}
    return mesh_presets.effective_targets(mesh)


def _cell_zone_counts(log):
    header = re.search(r'^\s*CellZone[ \t]+Cells[ \t]+Points[ \t]+Volume[ \t]+BoundingBox[ \t]*$', log, re.M)
    counts = {}
    if header is None:
        return counts
    for line in log[header.end():].lstrip('\r\n').splitlines():
        row = re.match(r'\s*(\S+)\s+(\d+)\s+\d+\s+\S+\s+\(', line)
        if not row:
            break
        if row[1] in counts:
            raise ValueError('Duplicate cell zone in checkMesh evidence: '+row[1])
        counts[row[1]] = int(row[2])
    return counts


def _region_bindings(root, log, regions):
    if not isinstance(regions, dict):
        return {}
    bindings = {name:{field:region[field] for field in ('geometryEntity','cellZone') if field in region}
                for name,region in regions.items() if isinstance(region, dict) and ('geometryEntity' in region or 'cellZone' in region)}
    if not bindings:
        return bindings
    counts = _cell_zone_counts(log)
    geometry_names = set()
    if any('geometryEntity' in binding for binding in bindings.values()):
        validation = json.loads((root/'A-geo/validation.json').read_text(encoding='utf-8'))
        geometry_names = {item['name'] for item in validation['objects'] if item.get('valid')}
    for name, binding in bindings.items():
        if 'geometryEntity' in binding and binding['geometryEntity'] not in geometry_names:
            raise ValueError(f"Region {name} refers to an absent or invalid geometry entity: {binding['geometryEntity']}")
        if 'cellZone' in binding:
            count = counts.get(binding['cellZone'], 0)
            if count < 1:
                raise ValueError(f"Region {name} has no cells in the checked OpenFOAM zone: {binding['cellZone']}")
            binding['cellCount'] = count
    return bindings


def quality(root, run_dir, *, legacy=False, check_families=True, check_refinement=True, check_boundary_layers=True,
            check_dimension=True, check_preset=True):
    text = (root/'0-caseDict/caseDict').read_text(encoding='utf-8')
    case = parse_casedict(text)
    mesh = case.get('mesh') or {}
    preset=mesh_presets.selected(mesh) if check_preset and not legacy else None
    targets=mesh_presets.effective_targets(mesh,include_preset=preset is not None)
    layers=mesh_boundary_layers.declaration(mesh) if not legacy and check_boundary_layers else None
    if layers and mesh.get('source')!='blockMesh':
        raise ValueError('当前托管边界层检查需要 blockMesh 原生网格')
    if mesh.get('source') == openfoam_cht.MESH_SOURCE:
        if openfoam_rotation.selection(case):
            raise ValueError('声明的 MRF 旋转区域需要单区域 OpenFOAM 网格')
        if openfoam_deformation.selection(case):
            raise ValueError('声明的网格变形需要单区域 OpenFOAM 网格')
        report=imported_quality(root,run_dir,text,mesh,targets,check_families=check_families,check_dimension=check_dimension)
        if preset is not None:
            report['quality_preset']=preset
        return report
    log_path = run_dir+'/checkMesh.log'
    raw = (root/log_path).read_bytes()
    log=raw.decode('utf-8','replace')
    jacobian=None
    evidence=None
    if not legacy and 'minJacobian' in targets:
        proof=root/run_dir/VTK_EVIDENCE
        if proof.is_file():
            counts=CELL_COUNTS.findall(log)
            if len(counts)!=1:
                raise ValueError('Expected one native cell count in checkMesh')
            with proof.open('rb') as stream:
                jacobian=measure(stream,int(counts[0]))
            evidence={'method':METHOD,'volume_cells':int(counts[0]),'source':file_record(root,proof)}
    report = evaluate_mesh_quality(log, targets, measured_jacobian=jacobian,
        registered=not legacy, include_volume=not legacy)
    report.update(case_dict_mesh_hash=mesh_block_hash(mesh), quality_targets=targets,
        run_dir=run_dir, case_input_sha256=case_input_fingerprint(text),
        log_path=log_path, log_sha256=hashlib.sha256(raw).hexdigest())
    if not legacy:
        report['gate']=gate_decision(report,targets)
        if preset is not None:
            report['quality_preset']=preset
        if check_dimension and 'dimension' in mesh:
            report['geometric_dimension']=geometric_dimension(log,mesh['dimension'])
        if check_families and 'elementFamilies' in mesh:
            report['element_families']=family_evidence(log,mesh['elementFamilies'])
        if check_refinement and 'refinement' in mesh:
            report['refinement']=mesh_refinement.evidence(root,run_dir,mesh['refinement'],log)
        if layers:
            counts=CELL_COUNTS.findall(log)
            if len(counts)!=1 or not report['mesh_ok']:
                raise ValueError('边界层测量需要真实且通过完整检查的网格')
            directory=root/run_dir/'constant/polyMesh'
            report['boundary_layers']=mesh_boundary_layers.measure(directory,boundaries(directory/'boundary'),layers,int(counts[0]))
        bindings = _region_bindings(root, log, case.get('regions'))
        if bindings:
            report['region_bindings'] = bindings
        rotation = openfoam_rotation.declared(case)
        if rotation is not None:
            zone = rotation['cell_zone']
            count = _cell_zone_counts(log).get(zone, 0)
            if count < 1:
                raise ValueError('原生 checkMesh 未找到非空 MRF 单元区域：'+zone)
            patches = boundaries(root/run_dir/'constant/polyMesh/boundary')
            missing = set(rotation['non_rotating_patches']) - {patch['name'] for patch in patches}
            if missing:
                raise ValueError('MRF 静止边界不存在：'+', '.join(sorted(missing)))
            report['rotation_zone'] = {'cell_zone': zone, 'cell_count': count}
        deformation = openfoam_deformation.declared(case)
        if deformation is not None:
            patches = boundaries(root/run_dir/'constant/polyMesh/boundary')
            openfoam_deformation.verify_patches(deformation,{patch['name']:patch['type'] for patch in patches})
            report['deformation_patches'] = {name: deformation[name] for name in ('moving_patch','fixed_patch','free_patches')}
        periodic = periodic_interfaces.declarations(case.get('interfaces'))
        overset = openfoam_overset.declared(case.get('interfaces'))
        if periodic or overset:
            patches = boundaries(root/run_dir/'constant/polyMesh/boundary',include_mapping=True)
            report['interface_bindings'] = periodic_interfaces.verify(periodic,case.get('regions'),patches)
            report['interface_bindings'].update(openfoam_overset.mesh_bindings(overset,patches))
            region_labels = root/run_dir/openfoam_overset.MESH_REGIONS
            if overset and region_labels.is_file():
                report['overset_region_labels'] = file_record(root,region_labels)
    if evidence is not None:
        report['jacobian_evidence']=evidence
    return report


def managed(path, run_dir):
    return path in {QUALITY, BOUNDARIES, TOOLCHAIN, ARTIFACTS, *(run_dir+'/'+name for name in
        ['checkMesh.log','foamToVTK.log',VTK_EVIDENCE,openfoam_overset.MESH_REGIONS,*mesh_refinement.LOGS,
         *(item for names in NATIVE_LOGS.values() for item in names)])} or path.startswith(run_dir+'/constant/polyMesh/')
