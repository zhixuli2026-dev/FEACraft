"""Prepare a coordinated metadata and physical time-behavior edit."""

from .casedict import UNCHANGED_DICT_ENTRY, LiteralDictString, parse_casedict, set_block
from .casedict_schema import CASE_META_ENUMS, CASE_META_TEXT_FIELDS, interface_regions, validate_case_dict_data

UNSET_TIME_BEHAVIOR = object()
_FIELDS = {*CASE_META_ENUMS, *CASE_META_TEXT_FIELDS, "tags", "units"}
_DISCIPLINE_PROBLEMS = {
    "fluid": {"externalFlow", "internalFlow"}, "solid": {"structural"},
    "thermal": {"thermal"}, "electromagnetic": {"electromagnetic"}, "acoustic": {"acoustic"},
}
_MESH_DIMENSIONS = {
    "0D": set(), "1D": {"1"}, "2D": {"2"}, "2D-axisymmetric": {"2", "3"}, "2D-wedge": {"2", "3"}, "3D": {"3"},
}


def _patch_fields(current, selected):
    values = dict.fromkeys(current, UNCHANGED_DICT_ENTRY)
    for key, value in selected.items():
        if value is None:
            values.pop(key, None)
        else:
            values[key] = value
    return values


def _meta_values(current, changes):
    result = {}
    for key, value in changes.items():
        if key not in _FIELDS:
            raise ValueError(f"caseMeta.{key}: 请通过字典编辑器修改自定义字段")
        if value is None:
            result[key] = None
        elif key == "units":
            if not isinstance(value, dict) or any(unit is not None and not isinstance(unit, str) for unit in value.values()):
                raise ValueError("caseMeta.units: 必须提供单位字典")
            units = current.get(key, {})
            result[key] = _patch_fields(units if isinstance(units, dict) else {}, value)
        elif key == "tags":
            if not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
                raise ValueError("caseMeta.tags: 必须提供字符串列表")
            result[key] = [LiteralDictString(tag) for tag in value]
        elif isinstance(value, str):
            result[key] = LiteralDictString(value) if key in CASE_META_TEXT_FIELDS else value
        else:
            raise ValueError(f"caseMeta.{key}: 必须提供字符串")
    return result


def prepare(text, changes, time_behavior=UNSET_TIME_BEHAVIOR):
    parsed = parse_casedict(text)
    if changes:
        current = parsed.get("caseMeta", {})
        if not isinstance(current, dict):
            raise ValueError("caseMeta: 必须是字典")
        text = set_block(text, "caseMeta", _patch_fields(current, _meta_values(current, changes)))
    if time_behavior is not UNSET_TIME_BEHAVIOR:
        problem = parsed.get("physicalProblem", {})
        if not isinstance(problem, dict):
            raise ValueError("physicalProblem: 必须是字典")
        text = set_block(text, "physicalProblem", _patch_fields(problem, {"timeBehavior": time_behavior}))
    return text


def _geometry_warnings(parsed, problem, mesh):
    geometry = parsed.get("geometry")
    if not isinstance(geometry, dict):
        return

    topology = geometry.get("topology")
    interfaces = parsed.get("interfaces")
    has_periodic_interface = isinstance(interfaces, dict) and any(
        isinstance(entry, dict) and entry.get("type") in ("periodic", "cyclic")
        for entry in interfaces.values()
    )
    loads = parsed.get("boundariesLoadsInitials")
    boundaries = loads.get("boundarySets") if isinstance(loads, dict) else None
    has_symmetry_boundary = isinstance(boundaries, dict) and any(
        isinstance(entry, dict) and entry.get("role") == "symmetry" for entry in boundaries.values()
    )
    physics = problem.get("coupledPhysics") if isinstance(problem, dict) else None
    mesh_source = mesh.get("meshSource") if isinstance(mesh, dict) else None
    checks = (
        ("periodicUsed", "periodic_geometry_mismatch", "几何关闭了周期配置，但定义了周期拓扑或周期接口。",
         str(geometry.get("periodicUsed")).lower() == "false" and (topology == "periodic" or has_periodic_interface)),
        ("symmetryUsed", "symmetry_geometry_mismatch", "几何关闭了对称配置，但已声明对称边界。",
         str(geometry.get("symmetryUsed")).lower() == "false" and has_symmetry_boundary),
        ("sourceType", "geometry_mesh_source_mismatch", "几何源为导入网格，网格来源声明为生成。请核对是否需要重划网格。",
         geometry.get("sourceType") == "importedMesh" and mesh_source == "generated"),
        ("topology", "geometry_mesh_motion_mismatch", "几何声明含运动部件，耦合物理配置关闭了网格运动。",
         topology == "movingParts" and isinstance(physics, dict) and str(physics.get("meshMotion")).lower() == "false"),
        ("topology", "geometry_porous_physics_mismatch", "几何声明为多孔拓扑，耦合物理配置关闭了多孔介质。",
         topology == "porous" and isinstance(physics, dict) and str(physics.get("porousMedia")).lower() == "false"),
    )
    for field, code, message, active in checks:
        if active:
            yield {"severity": "warning", "path": f"geometry.{field}", "code": code, "message": message}


def _region_warnings(parsed):
    regions = parsed.get("regions")
    if not isinstance(regions, dict):
        return
    materials = parsed.get("materials")
    entries = materials.get("entries") if isinstance(materials, dict) else None
    material_roles = {"fluidDomain": {"fluid", "porous"}, "solidPart": {"solid", "conductor", "dielectric", "porous"}}
    for name, region in regions.items():
        if not isinstance(region, dict) or not isinstance(region.get("material"), str):
            continue
        reference = region["material"]
        material = entries.get(reference) if isinstance(entries, dict) else None
        path = f"regions.{name}.material"
        if not isinstance(material, dict):
            yield {"severity": "warning", "path": path, "code": "region_material_missing",
                   "message": f"当前算例尚未定义材料 {reference}。请检查区域引用。"}
            continue
        role = region.get("role")
        allowed = material_roles.get(role) if isinstance(role, str) else None
        if allowed and isinstance(material.get("role"), str) and material["role"] not in allowed:
            yield {"severity": "warning", "path": path, "code": "region_material_role_mismatch",
                   "message": f"区域角色为 {role}，引用材料 {reference} 的用途为 {material['role']}。"}


def _interface_warnings(parsed):
    interfaces = parsed.get('interfaces')
    if not isinstance(interfaces,dict):
        return
    regions = parsed.get('regions')
    known = set(regions) if isinstance(regions,dict) else set()
    for name, entry in interfaces.items():
        members = interface_regions(entry) if isinstance(entry,dict) else None
        if isinstance(members,list) and len(members)==2 and all(isinstance(region,str) for region in members):
            missing = sorted(set(members)-known)
            if missing:
                yield {'severity':'warning', 'path':f'interfaces.{name}.regions', 'code':'interface_region_missing',
                       'message':f"当前未定义接口区域 {'、'.join(missing)}。"}


def _boundary_warnings(parsed):
    from .case_boundary_inputs import BLOCK, confirmed

    block = parsed.get(BLOCK)
    boundaries = block.get('boundarySets') if isinstance(block, dict) else None
    if isinstance(boundaries, dict):
        for name, entry in boundaries.items():
            if isinstance(entry, dict) and not confirmed(entry):
                yield {'severity':'warning', 'path':f'{BLOCK}.boundarySets.{name}.confirmed',
                       'code':'boundary_unconfirmed', 'message':'激活此边界并求解前需要确认其物理条件。'}


def lint(text):
    parsed = parse_casedict(text)
    issues = [{"severity": "error", **issue} for issue in validate_case_dict_data(parsed)]
    meta = parsed.get("caseMeta")
    if not isinstance(meta, dict):
        return issues

    discipline = meta.get("discipline")
    problem = parsed.get("physicalProblem")
    problem_class = problem.get("problemClass") if isinstance(problem, dict) else None
    allowed_problems = _DISCIPLINE_PROBLEMS.get(discipline) if isinstance(discipline, str) else None
    if allowed_problems and isinstance(problem_class, str) and problem_class not in allowed_problems | {"coupled"}:
        issues.append({"severity": "warning", "path": "physicalProblem.problemClass", "code": "discipline_problem_mismatch",
                       "message": f"学科设为 {discipline}，问题类型设为 {problem_class}。请核对学科与物理问题。"})

    dimensions = meta.get("dimensions")
    mesh = parsed.get("mesh")
    mesh_dimension = mesh.get("dimension") if isinstance(mesh, dict) else None
    allowed_dimensions = _MESH_DIMENSIONS.get(dimensions) if isinstance(dimensions, str) else None
    if allowed_dimensions is not None and isinstance(mesh_dimension, str) and mesh_dimension not in allowed_dimensions:
        issues.append({"severity": "warning", "path": "mesh.dimension", "code": "case_mesh_dimension_mismatch",
                       "message": f"算例维度设为 {dimensions}，网格维度为 {mesh_dimension}。请核对降维模型或网格。"})
    issues.extend(_geometry_warnings(parsed, problem, mesh))
    issues.extend(_region_warnings(parsed))
    issues.extend(_interface_warnings(parsed))
    issues.extend(_boundary_warnings(parsed))

    execution = parsed.get("execution")
    if (meta.get("executionProfile") == "local" and isinstance(execution, dict) and execution.get("runMode") == "remote"):
        issues.append({"severity": "warning", "path": "execution.runMode", "code": "remote_mode_local_profile",
                       "message": "算例偏好本地运行，但 execution.runMode 指定远程。请核对本次执行配置。"})
    return issues
