"""Export a portable OpenFOAM case with canonical input files and native scripts."""

import hashlib
import json
from functools import partial
from pathlib import Path
import re
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from . import attempt_workspace, case_physical_frames, mesh_import_source, mesh_refinement, openfoam_acoustic_medium, openfoam_cht, openfoam_cht_materials, openfoam_conduction_material, openfoam_deformation, openfoam_dynamic_refinement, openfoam_equations, openfoam_fluid_model, openfoam_fluid_thermophysics, openfoam_numerical_inputs, openfoam_pressure_reference, openfoam_reference_temperature, openfoam_rotation
from .case_mesh_schema import directional_refinement
from .case_numerical_method import convergence_required, declared_time_keys
from .case_boundary_inputs import active_material_names
from .casedict import CASE_DICT_PATH, parse_casedict
from .mesh_artifacts import EXTRA_NATIVE_INPUTS, IMPORTED_GMSH, IMPORTED_POLY_MESH, NATIVE_INPUT, gmsh_format, mesh_source, selection, validate_source_role
from .mesh_publication import inspect_inputs
from .openfoam_structural_feedback import selected as structural_feedback
from .request_deadline import current
from .solve_artifacts import control_values, gravity_input, motion_input, requested_motion, transport_input


RUNTIME = 'system/vibeflow-openfoam.py'
REFINEMENT = 'system/vibeflow-refinement'
RESIDUALS = 'system/vibeflow_openfoam_residuals.py'
REFERENCE = 'system/vibeflow_reference_temperature'
PRESSURE_REFERENCE = 'system/vibeflow_reference_pressure'
SCRIPTS = {'Allrun.pre':'pre', 'Allrun':'run', 'Allclean':'clean'}
README = '''# 标准 OpenFOAM 算例

解包后在 Linux 或 WSL 中加载与算例兼容的 OpenFOAM 环境，并使用 Python 3.9 或更新版本。
从 `.geo` 重新划分网格需要 Gmsh。导入的 `.msh` 算例使用 OpenFOAM 自带的 `gmshToFoam`。
单区域原生网格从 `system/polyMesh` 复制到独立工作目录，由 OpenFOAM 检查。

- `bash Allrun.pre` 准备独立算例并执行网格检查。
- `bash Allrun` 创建独立算例，从初始状态执行网格准备和串行求解。
- `bash Allclean` 清理本算例生成且内容未被修改的文件。

每次执行都会打印 `runs/` 下的工作目录。两种运行方式都保持包内的原始输入。
原生 `0` 优先使用。只有 `0.orig` 时，脚本把它的副本放到运行目录的 `0`。
更改过的成果和未登记的文件保留在原运行目录。
'''


def _records(root, run_dir, source):
    paths = selection(root,run_dir)
    # The imported mesh is an input. Generated native meshes are rebuilt.
    if source == openfoam_cht.MESH_SOURCE:
        openfoam_cht.inspect_inputs(root,run_dir)
    elif source == IMPORTED_GMSH:
        gmsh_format(root/run_dir/NATIVE_INPUT[source])
    if not any((root/run_dir/name).is_dir() and any((root/run_dir/name).iterdir()) for name in ('0','0.orig')):
        raise ValueError('导出标准算例需要 0 或 0.orig 原生初值')
    records = attempt_workspace.inventory(root,paths,deadline=current())
    if source==IMPORTED_POLY_MESH:
        required={path.relative_to(root).as_posix() for path in mesh_import_source.core_paths(root/run_dir).values()}
        required.add(run_dir+'/system/controlDict')
    else:
        required={run_dir+'/'+name for name in ('system/controlDict',NATIVE_INPUT[source],*EXTRA_NATIVE_INPUTS.get(source,()))}
    if not required <= records.keys() or (run_dir+'/'+RUNTIME) in records:
        raise ValueError('标准算例缺少原生控制文件，或与生成脚本路径冲突')
    return records


def _script(archive, name, command):
    info = ZipInfo(name)
    info.create_system = 3
    info.external_attr = 0o100755 << 16
    body = ('#!/bin/bash\nset -euo pipefail\ncd -- "$(dirname -- "$0")"\n'
            f'exec python3 -B {RUNTIME} {command}\n')
    archive.writestr(info,body)


def _write_member(archive, name, path, record, deadline):
    info = ZipInfo(name)
    info.create_system = 3
    info.file_size = record['size']
    info.external_attr = (0o100755 if record['executable'] else 0o100644) << 16
    digest = hashlib.sha256()
    size = 0
    with archive.open(info,'w') as target, path.open('rb') as source:
        while chunk := source.read(1024*1024):
            if deadline is not None:
                deadline.check()
            size += len(chunk)
            if size > record['size']:
                raise ValueError('标准算例输入在导出期间变化，请重新下载')
            digest.update(chunk)
            target.write(chunk)
    if size!=record['size'] or digest.hexdigest()!=record['sha256']:
        raise ValueError('标准算例输入在导出期间变化，请重新下载')


def _write(root, run_dir, download):
    source = mesh_source(root)
    files = _records(root,run_dir,source)
    document=(root/CASE_DICT_PATH).read_text(encoding='utf-8')
    parsed=parse_casedict(document)
    if structural_feedback(parsed) is not None:
        raise ValueError('结构位移反馈需要托管工作流封存 CalculiX 结果与匹配壁面')
    mesh=parsed['mesh']
    validate_source_role(mesh,source)
    refinement={}
    if 'refinement' in mesh:
        if source!='blockMesh' or any(path.startswith(f'{run_dir}/{REFINEMENT}/') or path==f'{run_dir}/{REFINEMENT}' for path in files):
            raise ValueError('标准算例方向加密需要 blockMesh 及独立的生成控制文件路径')
        refinement=mesh_refinement.dictionaries(directional_refinement(mesh['refinement']))
    method=parsed.get('numericalMethod') or {}
    controls = control_values((root/run_dir/'system/controlDict').read_text(encoding='utf-8'),extra=declared_time_keys(method))
    solver = controls.get('application', '')
    if not re.fullmatch('[A-Za-z][A-Za-z0-9_]*Foam',solver):
        raise ValueError('标准算例需要可执行的原生 OpenFOAM application')
    openfoam_equations.configuration(parsed,solver)
    openfoam_numerical_inputs.validate_time(method,controls)
    openfoam_numerical_inputs.validate_pressure_and_momentum(root,run_dir,solver,method)
    numerical=openfoam_numerical_inputs.native_inputs(root,run_dir,solver,document)
    medium=openfoam_acoustic_medium.declared(parsed)
    if medium is not None and solver!='acousticFoam':
        raise ValueError('声明的压力波声学介质需要 acousticFoam')
    conduction=openfoam_conduction_material.prepare(root,run_dir,solver,openfoam_conduction_material.declared(parsed))
    flow=openfoam_fluid_model.prepare(root,run_dir,solver,openfoam_fluid_model.declared(parsed))
    transport=transport_input(root,run_dir) if solver == 'icoFoam' and active_material_names(parsed) else {}
    thermophysics=openfoam_fluid_thermophysics.prepare(root,run_dir,solver,openfoam_fluid_thermophysics.declared(parsed))
    dynamic_refinement=openfoam_dynamic_refinement.prepare(root,run_dir,solver,openfoam_dynamic_refinement.declared(mesh))
    rotation=openfoam_rotation.declared(parsed)
    mrf=openfoam_rotation.prepare(root,run_dir,solver,rotation)
    moving=case_physical_frames.moving_region(parsed.get('physicalProblem'))
    motion=motion_input(root,run_dir,solver) if moving is not None else {}
    deformation=openfoam_deformation.declared(parsed)
    if deformation is not None and requested_motion(document) is not None:
        raise ValueError('同一算例只能声明 Laplacian 网格变形或刚体线性运动')
    deforming,displacement=openfoam_deformation.prepare(root,run_dir,solver,deformation)
    cht_materials={}
    if openfoam_cht_materials.selection(parsed):
        if solver != openfoam_cht.SOLVER or source != openfoam_cht.MESH_SOURCE:
            raise ValueError('区域材料共轭传热需要既有多区域网格和 chtMultiRegionFoam')
        materials=openfoam_cht_materials.declared(parsed,openfoam_cht.inspect_inputs(root,run_dir))
        cht_materials=openfoam_cht_materials.prepare(root,run_dir,materials)
    generated={**gravity_input(root,run_dir),**numerical,**openfoam_acoustic_medium.prepare(root,run_dir,medium),
               **conduction,**flow,**transport,**thermophysics,**dynamic_refinement,
               **mrf,**motion,**deforming,**displacement,**cht_materials}
    overlay=openfoam_reference_temperature.prepare(root,run_dir,solver,openfoam_reference_temperature.declared(parsed))
    reference=next(iter(overlay),None)
    gauge=openfoam_pressure_reference.declared(parsed)
    pressure_overlay=openfoam_pressure_reference.prepare(root,run_dir,solver,gauge)
    pressure=next(iter(pressure_overlay),None)
    if gauge is not None:
        initial='0' if (root/run_dir/'0').exists() else '0.orig'
        openfoam_pressure_reference.pressure_field(root,run_dir,initial=initial,required=True)
    for source_path,member in ((reference,REFERENCE),(pressure,PRESSURE_REFERENCE)):
        reserved=run_dir+'/'+member
        if source_path and any(path==reserved or path.startswith(reserved+'/') for path in files):
            raise ValueError('标准算例的派生参考量文件与现有输入冲突：'+member)
    convergence=openfoam_numerical_inputs.requested_linear_solvers(document) if convergence_required(method) else None
    if convergence and run_dir+'/'+RESIDUALS in files:
        raise ValueError('标准算例原生残差检查路径与现有文件冲突')
    runtime = Path(__file__).with_name('openfoam_package_runtime.py').read_bytes()
    download.reserve_body(sum(record['size'] for record in files.values())+len(runtime)+65536
                          +sum(len(body) for body in [*generated.values(),*overlay.values(),*pressure_overlay.values()])
                          +sum(256+2*len(path.encode('utf-8')) for path in files))
    with ZipFile(download,'w',compression=ZIP_STORED) as archive:
        if not any(path.startswith(run_dir+'/constant/') for path in files):
            archive.writestr('constant/','')
        deadline = current()
        for path,record in files.items():
            relative = path if path == CASE_DICT_PATH else path[len(run_dir)+1:]
            _write_member(archive,relative,root/path,record,deadline)
        for name,command in SCRIPTS.items():
            _script(archive,name,command)
        archive.writestr(RUNTIME,runtime)
        for name,body in refinement.items():
            archive.writestr(f'{REFINEMENT}/{name}',body)
        for path,body in generated.items():
            archive.writestr(path[len(run_dir)+1:],body)
        if reference:
            archive.writestr(REFERENCE,overlay[reference])
        if pressure:
            archive.writestr(PRESSURE_REFERENCE,pressure_overlay[pressure])
        if convergence:
            archive.writestr(RESIDUALS,Path(__file__).with_name('native_openfoam_residuals.py').read_bytes())
        archive.writestr('README.md',README)
        archive.writestr('vibeflow-case.json',json.dumps({
            'schema':'vibeflow.openfoam-case-package.v1','run_dir':run_dir,
            'mesh_source':source,'solver':solver,
            **({'mesh_dimension':int(mesh['dimension'])} if 'dimension' in mesh else {}),
            **({'directional_refinement':True} if refinement else {}),
            **({'linear_convergence':convergence} if convergence else {}),
            **({'rotation_zone':rotation['cell_zone']} if rotation else {}),
            **({'reference_pressure_file':pressure[len(run_dir)+1:]} if pressure else {}),
            **({'reference_temperature_file':reference[len(run_dir)+1:]} if reference else {})},ensure_ascii=False,indent=2)+'\n')
    if _records(root,run_dir,source) != files:
        raise ValueError('标准算例输入在导出期间变化，请重新下载')
    result = download.tell()
    download.seek(0)
    return result


def build(prefix, run_dir, download):
    return inspect_inputs(prefix,partial(_write,download=download),run_dir)
