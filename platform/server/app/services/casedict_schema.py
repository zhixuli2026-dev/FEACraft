"""caseDict v1 结构/枚举校验：只覆盖原始 StandardCase 明确约束。"""
import math
import re

from . import case_materials

_CASE_BLOCKS = {
    "caseMeta", "intent", "physicalProblem", "geometry", "materials", "regions",
    "interfaces", "boundariesLoadsInitials", "mesh", "numericalMethod", "execution",
    "postProcessing", "report", "provenance", "errors", "extensions",
}
_STATUS = {"draft", "readyForGeometry", "readyForMesh", "readyForRun", "readyForPost", "readyForReport", "completed"}
_DISCIPLINE = {"fluid", "solid", "thermal", "electromagnetic", "acoustic", "multiphysics", "custom"}
_ANALYSIS = {"steady", "transient", "modal", "harmonic", "static", "dynamic", "optimization", "parametric", "UQ"}
_DIMENSIONS = {"0D", "1D", "2D", "2D-axisymmetric", "2D-wedge", "3D"}
_FIDELITY = {"concept", "smoke", "engineering", "validation", "production"}
_EXECUTION = {"local", "container", "remote", "HPC"}
CASE_META_ENUMS = {
    "status": _STATUS, "discipline": _DISCIPLINE, "analysisType": _ANALYSIS,
    "dimensions": _DIMENSIONS, "fidelity": _FIDELITY, "executionProfile": _EXECUTION,
}
_META_STRING_FIELDS = ("caseName", "displayName", "description", "owner", "createdBy", "createdAt", "updatedAt")
CASE_META_TEXT_FIELDS = (*_META_STRING_FIELDS, "project", "revision", "notes")
_PROBLEM_CLASS = {"externalFlow", "internalFlow", "structural", "thermal", "electromagnetic", "acoustic", "coupled"}
_TIME_BEHAVIOR = {"steady", "quasiSteady", "transient", "periodic", "frequencyDomain"}
_COORDINATE_FRAMES = {"inertial", "rotating", "moving", "deforming"}
_COUPLED_FLAGS = ("fluid", "solidMechanics", "heatTransfer", "electromagnetics", "acoustics", "particles",
                  "chemistry", "phaseChange", "porousMedia", "radiation", "meshMotion")
_GEOMETRY_ENUMS = {
    "sourceType": {"parametricCAD", "importedCAD", "importedMesh", "primitive", "scan", "external"},
    "generator": {"cadquery", "freecad", "gmsh", "salome", "external", "manual"},
    "topology": {"singleRegion", "multiRegion", "assembly", "porous", "movingParts", "periodic"},
}
_REGION_ROLES = {"fluidDomain", "solidPart", "thermalRegion", "electromagneticRegion", "acousticRegion"}
_INTERFACE_TYPES = {"contact", "coupledWall", "periodic", "cyclic", "overset", "mapped", "conformal", "nonConformal"}
_INTERFACE_COUPLINGS = {"thermal", "mechanical", "fluidStructure", "electromagnetic", "acoustic", "none"}
_STATUS_WORKFLOW = {
    "draft": "A", "readyForGeometry": "A", "readyForMesh": "B",
    "readyForRun": "C", "readyForPost": "D", "readyForReport": "E", "completed": "E",
}
_STAGES = {"elicitation", "geometry", "mesh", "run", "post", "report", "toolchain"}
_CATEGORIES = {"missingInput", "geometry", "meshQuality", "conversion", "solver", "postprocess", "report", "environment"}
_SEVERITIES = {"info", "warning", "error", "fatal"}
_ERROR_STATUSES = {"open", "investigating", "fixProposed", "fixed", "verified", "wontFix"}
_POLICIES = {"fail", "warn", "allowWithReason"}
_ANALYSIS_TIME = {
    "steady": {"steady", "quasiSteady"},
    "transient": {"transient", "periodic"},
    "harmonic": {"frequencyDomain", "periodic"},
    "modal": {"frequencyDomain"},
}
_UNIT_VALUES = {
    "length": {"m", "mm", "inch"}, "mass": {"kg"}, "time": {"s"},
    "temperature": {"K", "degC"}, "angle": {"rad", "deg"}, "force": {"N"},
    "pressure": {"Pa"}, "energy": {"J"},
}


def case_meta_options() -> dict:
    return {
        "schema_version": 1,
        "enums": {name: sorted(values) for name, values in CASE_META_ENUMS.items()},
        "text_fields": CASE_META_TEXT_FIELDS,
        "units": {name: sorted(values) for name, values in _UNIT_VALUES.items()},
        "time_behaviors": sorted(_TIME_BEHAVIOR),
    }


def _error(errors, path, code, message):
    errors.append({"path": path, "code": code, "message": message})


def _require_dict(data, key, path, errors, required=False):
    value = data.get(key)
    if value is None:
        if required:
            _error(errors, path, "required", "缺少必需块")
        return None
    if not isinstance(value, dict):
        _error(errors, path, "type", "必须是字典块")
        return None
    return value


def _enum(data, key, allowed, path, errors):
    if key not in data:
        return
    value = data[key]
    if not isinstance(value, str):
        _error(errors, f"{path}.{key}", "type", "必须是单个字符串值")
    elif value not in allowed:
        _error(errors, f"{path}.{key}", "enum", f"值 {value!r} 不合法，可选 {sorted(allowed)}")


def _count(data, key, path, errors):
    if key not in data:
        return
    value = data[key]
    if isinstance(value, bool):
        _error(errors, f"{path}.{key}", "type", "必须是非负整数")
        return
    try:
        number = int(value)
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        _error(errors, f"{path}.{key}", "type", "必须是非负整数")
        return
    if not math.isfinite(numeric) or numeric != number:
        _error(errors, f"{path}.{key}", "type", "必须是非负整数")
        return
    if number < 0:
        _error(errors, f"{path}.{key}", "range", "必须是非负整数")
        return
    return number


def _bool(value, path, errors):
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    _error(errors, path, "type", "必须是 true 或 false")
    return None


def _positive_number(data, key, path, errors):
    if key not in data:
        return
    try:
        number = float(data[key])
    except (TypeError, ValueError):
        _error(errors, f"{path}.{key}", "type", "必须是正数")
        return
    if not math.isfinite(number) or number <= 0:
        _error(errors, f"{path}.{key}", "range", "必须是正数")


def finite_vector(value):
    """Parse the parenthesized form retained by the caseDict reader."""
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        value = " ".join(value)
    if not isinstance(value, str) or not value.startswith('(') or not value.endswith(')'):
        raise ValueError('Expected a parenthesized vector')
    parts = value[1:-1].split()
    if len(parts) != 3:
        raise ValueError('Expected three components')
    vector = tuple(float(part) for part in parts)
    if not all(math.isfinite(component) for component in vector):
        raise ValueError('Vector components must be finite')
    return vector


def _validate_physical_fields(problem, geometry, errors):
    path = "physicalProblem"
    _enum(problem, "coordinateFrame", _COORDINATE_FRAMES, path, errors)
    if 'rotatingFrame' in problem:
        from .openfoam_rotation import declared
        try:
            declared({'physicalProblem': problem})
        except ValueError as error:
            _error(errors, f'{path}.rotatingFrame', 'frame', str(error))
    if 'movingFrame' in problem:
        from .case_physical_frames import moving_region
        try:
            moving_region(problem)
        except ValueError as error:
            _error(errors, f'{path}.movingFrame', 'frame', str(error))
    if 'deformingFrame' in problem:
        from .openfoam_deformation import declared
        try:
            declared({'physicalProblem': problem})
        except ValueError as error:
            _error(errors, f'{path}.deformingFrame', 'frame', str(error))
    _enum(problem, 'gravityCoordinates', {'global','geometry'}, path, errors)
    if "gravityEnabled" in problem:
        enabled = _bool(problem["gravityEnabled"], f"{path}.gravityEnabled", errors)
        if enabled and "gravityVector" not in problem:
            _error(errors, f"{path}.gravityVector", "required", "启用重力时需提供重力矢量")
    if "gravityVector" in problem:
        try:
            finite_vector(problem["gravityVector"])
        except (ValueError, OverflowError):
            _error(errors, f"{path}.gravityVector", "vector", "必须是括号包围的三个有限分量")
    if problem.get('gravityCoordinates')=='geometry' and str(problem.get('gravityEnabled')).lower()=='true' and 'gravityVector' in problem:
        from .case_physical_frames import global_gravity
        try:
            global_gravity(problem,geometry)
        except (ValueError,OverflowError) as error:
            _error(errors,f'{path}.gravityCoordinates','frame',str(error))

    for name in ("referenceLength", "referenceArea", "referenceDensity"):
        _positive_number(problem, name, path, errors)
    for name in ("referencePressure", "referenceTemperature"):
        if name in problem:
            try:
                if not math.isfinite(float(problem[name])):
                    raise ValueError('Reference is not finite')
            except (TypeError, ValueError, OverflowError):
                _error(errors, f"{path}.{name}", "finite", "必须是有限标量")
    if "referenceVelocity" in problem:
        value = problem["referenceVelocity"]
        try:
            if not math.isfinite(float(value)):
                raise ValueError('Reference velocity is not finite')
        except (TypeError, ValueError, OverflowError):
            try:
                finite_vector(value)
            except (ValueError, OverflowError):
                _error(errors, f"{path}.referenceVelocity", "finite", "必须是有限标量或三分量矢量")

    flags = _require_dict(problem, "coupledPhysics", f"{path}.coupledPhysics", errors)
    if flags is not None:
        for name in _COUPLED_FLAGS:
            if name in flags:
                _bool(flags[name], f"{path}.coupledPhysics.{name}", errors)

    models = _require_dict(problem, "governingModels", f"{path}.governingModels", errors)
    if models is not None:
        continuum_path = f'{path}.governingModels.continuum'
        continuum = _require_dict(models,'continuum',continuum_path,errors)
        if continuum is not None:
            from .openfoam_equations import conservation
            _enum(continuum,'model',{'continuum','rarefied','lumped','network','custom'},continuum_path,errors)
            try:
                conservation(continuum)
            except ValueError as error:
                _error(errors,f'{continuum_path}.conservation','conservation',str(error))
        for name in ("fluid", "solidMechanics", "thermal", "electromagnetic", "acoustic"):
            model_path = f"{path}.governingModels.{name}"
            model = _require_dict(models, name, model_path, errors)
            if model is not None and "enabled" in model:
                _bool(model["enabled"], f"{model_path}.enabled", errors)
            if name == 'acoustic' and model is not None and 'soundSpeed' in model:
                from .openfoam_acoustic_medium import declared
                try:
                    declared({'physicalProblem':problem})
                except ValueError as error:
                    _error(errors,f'{model_path}.soundSpeed','quantity',str(error))


def _validate_geometry_fields(geometry, errors):
    for name, allowed in _GEOMETRY_ENUMS.items():
        _enum(geometry, name, allowed, "geometry", errors)
    _positive_number(geometry, "scaleToMeters", "geometry", errors)
    for name in ("symmetryUsed", "periodicUsed", "watertightRequired", "manifoldRequired"):
        if name in geometry:
            _bool(geometry[name], f"geometry.{name}", errors)

    path = "geometry.coordinateSystem"
    coordinates = _require_dict(geometry, "coordinateSystem", path, errors)
    if coordinates is None:
        return
    _enum(coordinates, "handedness", {"rightHanded", "leftHanded"}, path, errors)
    _enum(coordinates, "angleUnit", _UNIT_VALUES['angle'], path, errors)
    _enum(coordinates, "lengthUnit", _UNIT_VALUES['length'], path, errors)
    axes = []
    for name in ("origin", "xAxis", "yAxis", "zAxis"):
        if name not in coordinates:
            continue
        try:
            vector = finite_vector(coordinates[name])
            if name != "origin":
                scale = max(map(abs, vector))
                if not scale:
                    raise ValueError('Axis must be nonzero')
                axes.append(tuple(value / scale for value in vector))
        except (ValueError, OverflowError):
            _error(errors, f"{path}.{name}", "vector", "必须是有效的有限三分量矢量，轴方向不能为零")
    if len(axes) == 3:
        x, y, z = axes
        determinant = (x[0] * (y[1]*z[2] - y[2]*z[1]) + x[1] * (y[2]*z[0] - y[0]*z[2])
                       + x[2] * (y[0]*z[1] - y[1]*z[0]))
        if abs(determinant) <= 1e-12:
            _error(errors, path, "degenerate_basis", "三个坐标轴需要线性无关")
        elif coordinates.get("handedness") == ("leftHanded" if determinant > 0 else "rightHanded"):
            _error(errors, f"{path}.handedness", "basis_mismatch", "左右手声明与三个坐标轴的方向不一致")


def _validate_materials(materials, errors):
    entries = _require_dict(materials, "entries", "materials.entries", errors)
    if entries is None:
        return
    for name, material in entries.items():
        path = f"materials.entries.{name}"
        if not isinstance(material, dict):
            _error(errors, path, "type", "命名材料必须是字典块")
            continue
        for field, allowed in case_materials.ENUMS.items():
            _enum(material, field, allowed, path, errors)
        if "confirmed" in material:
            _bool(material["confirmed"], f"{path}.confirmed", errors)
        for field in sorted(case_materials.UNITS.keys() & material.keys()):
            try:
                case_materials.quantity_si(field, material[field])
            except ValueError as error:
                _error(errors, f"{path}.{field}", "quantity", str(error))


def porous_coefficients(value):
    if not isinstance(value, dict) or not {'d', 'f'} <= value.keys():
        raise ValueError('需要提供 Darcy 的 d [1/m²] 与 Forchheimer 的 f [1/m]')
    try:
        coefficients = {name: finite_vector(value[name]) for name in ('d', 'f')}
    except (ValueError, OverflowError) as error:
        raise ValueError('d 与 f 均须为三个有限分量') from error
    numbers = [number for vector in coefficients.values() for number in vector]
    if min(numbers) < 0 or max(numbers) == 0:
        raise ValueError('阻力分量必须非负，且至少一项为正')
    return coefficients


def linear_motion_velocity(value):
    if not isinstance(value, dict) or value.get('type') != 'linearMotion':
        raise ValueError('运动类型须为 linearMotion')
    try:
        velocity = finite_vector(value.get('velocity'))
    except (ValueError, OverflowError) as error:
        raise ValueError('velocity 必须是三个有限的 m/s 分量') from error
    if not any(velocity):
        raise ValueError('线性运动速度至少有一项非零')
    return velocity


def valid_cell_zone(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.-]*', value) is not None


def _validate_regions(regions, errors):
    for name, region in regions.items():
        path = f"regions.{name}"
        if not isinstance(region, dict):
            _error(errors, path, "type", "命名区域必须是字典块")
            continue
        _enum(region, "role", _REGION_ROLES, path, errors)
        for field in ("material", "geometryEntity", "meshRegion", "cellZone"):
            if field in region and (not isinstance(region[field], str) or not region[field].strip()):
                _error(errors, f"{path}.{field}", "type", "引用必须是非空名称")
        for field in ("moving", "porous"):
            if field in region:
                _bool(region[field], f"{path}.{field}", errors)
        for key, flag, label, validate in (('porousResistance','porous','多孔阻力',porous_coefficients),
                                           ('motion','moving','区域运动',linear_motion_velocity)):
            if key not in region:
                continue
            if str(region.get(flag)).lower() != 'true':
                _error(errors, f'{path}.{flag}', 'required', f'指定{label}时需启用 {flag}')
            if not valid_cell_zone(region.get('cellZone')):
                _error(errors, f'{path}.cellZone', 'required', f'指定{label}时需提供 OpenFOAM 网格区名称')
            try:
                validate(region[key])
            except ValueError as error:
                _error(errors, f'{path}.{key}', 'quantity', str(error))


def interface_pair(value):
    if isinstance(value, str) and value.startswith('(') and value.endswith(')'):
        return value[1:-1].split()
    if (isinstance(value, list) and len(value)>=2 and all(isinstance(item,str) for item in value)
            and value[0].startswith('(') and value[-1].endswith(')')):
        return [item for item in (value[0][1:],*value[1:-1],value[-1][:-1]) if item]
    return value


def interface_regions(entry):
    members = interface_pair(entry.get('regions'))
    if entry.get('type') in ('periodic','cyclic'):
        if isinstance(members, str):
            return [members,members]
        if isinstance(members, list) and len(members) == 1:
            return members*2
    return members


def _validate_interfaces(interfaces, errors):
    for name, interface in interfaces.items():
        path = f'interfaces.{name}'
        if not isinstance(interface, dict):
            _error(errors, path, 'type', '命名接口必须是字典块')
            continue
        _enum(interface, 'type', _INTERFACE_TYPES, path, errors)
        _enum(interface, 'coupling', _INTERFACE_COUPLINGS, path, errors)
        for field, pair in (('regions', interface_regions(interface)), ('patches', interface_pair(interface.get('patches')))):
            if field in interface and (not isinstance(pair,list) or len(pair)!=2
                                       or any(not isinstance(item,str) or not item.strip() for item in pair)):
                _error(errors, f'{path}.{field}', 'reference_pair', '接口两侧需各指定一个名称')
        if 'contactLaw' in interface:
            from .calculix_contact import declaration
            try:
                declaration(name,interface)
            except ValueError as error:
                _error(errors,f'{path}.contactLaw','value',str(error))
        if 'overset' in interface:
            from .openfoam_overset import declaration
            try:
                declaration(name,interface)
            except ValueError as error:
                _error(errors,f'{path}.overset','value',str(error))
        if 'elmerElectrical' in interface:
            from .elmer_electrical_interface import declaration
            try:
                declaration(name,interface)
            except ValueError as error:
                _error(errors,f'{path}.elmerElectrical','value',str(error))
        if 'structuralDisplacement' in interface:
            from .openfoam_structural_feedback import declaration
            try:
                declaration(name,interface)
            except ValueError as error:
                _error(errors,f'{path}.structuralDisplacement','value',str(error))


def _nonempty(value) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return any(_nonempty(item) if isinstance(item, (str, dict, list)) else item is not None
                   for item in value.values())
    if isinstance(value, list):
        return any(_nonempty(item) if isinstance(item, (str, dict, list)) else item is not None
                   for item in value)
    return False


def _validate_error_entries(block: dict, errors: list[dict]):
    entries = _require_dict(block, "entries", "errors.entries", errors)
    if entries is None:
        return None
    for entry_id, record in entries.items():
        path = f"errors.entries.{entry_id}"
        if not isinstance(record, dict):
            _error(errors, path, "type", "错误条目必须是字典")
            continue
        for key in ("title", "stage", "category", "severity", "status", "blocking"):
            if key not in record:
                _error(errors, f"{path}.{key}", "required", "缺少错误条目必需字段")
        if not isinstance(record.get("title"), str) or not record["title"].strip():
            _error(errors, f"{path}.title", "required", "title 不能为空")
        if "message" in record and not isinstance(record["message"], str):
            _error(errors, f"{path}.message", "type", "message 必须是字符串")
        _enum(record, "stage", _STAGES, path, errors)
        _enum(record, "category", _CATEGORIES, path, errors)
        _enum(record, "severity", _SEVERITIES, path, errors)
        _enum(record, "status", _ERROR_STATUSES, path, errors)
        _bool(record.get("blocking"), f"{path}.blocking", errors)
        if "relatedPaths" in record and not isinstance(record["relatedPaths"], (str, list)):
            _error(errors, f"{path}.relatedPaths", "type", "relatedPaths 必须是字符串或列表")
        status = record.get("status")
        if status == "fixProposed" and not _nonempty(record.get("fixOptions")):
            _error(errors, f"{path}.fixOptions", "required", "fixProposed 必须提供 fixOptions")
        if status in ("fixed", "wontFix") and not _nonempty(record.get("resolution")):
            _error(errors, f"{path}.resolution", "required", f"{status} 必须提供 resolution")
        if status == "verified" and (
                not _nonempty(record.get("resolution")) or not _nonempty(record.get("verification"))):
            _error(errors, path, "required", "verified 必须同时提供 resolution 和 verification")
    return entries


def _error_counts(entries: dict) -> dict:
    counts = {"total": len(entries), "open": 0, "blocking": 0, "resolved": 0}
    for record in entries.values():
        status = record.get("status", "open")
        terminal = status in ("fixed", "verified", "wontFix")
        if terminal:
            counts["resolved"] += 1
        else:
            if record.get("severity", "warning") != "info":
                counts["open"] += 1
            if str(record.get("blocking")).lower() == "true":
                counts["blocking"] += 1
    return counts


def error_counts(parsed: dict) -> dict:
    block = parsed.get("errors", {})
    entries = block.get("entries", {}) if isinstance(block, dict) else {}
    if isinstance(entries, dict) and entries:
        return _error_counts(entries)
    summary = block.get("summary", {}) if isinstance(block, dict) else {}
    return {
        "total": int(summary.get("total", 0) or 0),
        "open": int(summary.get("open", 0) or 0),
        "blocking": int(summary.get("blocking", 0) or 0),
        "resolved": int(summary.get("resolved", 0) or 0),
    }


def validate_case_dict_data(parsed: dict) -> list[dict]:
    errors: list[dict] = []
    state = parsed.get("workflowState")
    if not isinstance(state, str):
        _error(errors, "workflowState", "required", "缺少 workflowState")
    elif state.upper() not in ("A", "B", "C", "D", "E", "V") and state.lower() != "dev":
        _error(errors, "workflowState", "enum", f"非法状态 {state!r}")
    meta = _require_dict(parsed, "caseMeta", "caseMeta", errors, required=True)
    for block in _CASE_BLOCKS - {"caseMeta"}:
        _require_dict(parsed, block, block, errors)
    if meta is not None:
        for key, allowed in CASE_META_ENUMS.items():
            _enum(meta, key, allowed, "caseMeta", errors)
        status = meta.get("status")
        if (isinstance(status, str) and status in _STATUS_WORKFLOW
                and isinstance(state, str) and state.upper() not in {"V", "DEV"}):
            expected = _STATUS_WORKFLOW[status]
            if state.upper() != expected:
                _error(errors, "caseMeta.status", "workflow_mismatch",
                       f"{status} 对应 workflowState {expected}，实际为 {state}")
        units = _require_dict(meta, "units", "caseMeta.units", errors)
        for key in _META_STRING_FIELDS:
            if key in meta and not isinstance(meta[key], str):
                _error(errors, f"caseMeta.{key}", "type", "必须是字符串")
        if "tags" in meta and (not isinstance(meta["tags"], list) or
                              any(not isinstance(tag, str) for tag in meta["tags"])):
            _error(errors, "caseMeta.tags", "type", "必须是字符串列表")
        if units is not None:
            for key in units:
                if key not in _UNIT_VALUES:
                    _error(errors, f"caseMeta.units.{key}", "unsupported", "未知单位量纲")
            for key, allowed in _UNIT_VALUES.items():
                _enum(units, key, allowed, "caseMeta.units", errors)
    problem = parsed.get("physicalProblem")
    if isinstance(problem, dict):
        _enum(problem, "problemClass", _PROBLEM_CLASS, "physicalProblem", errors)
        _enum(problem, "timeBehavior", _TIME_BEHAVIOR, "physicalProblem", errors)
        _validate_physical_fields(problem, parsed.get('geometry'), errors)
        analysis = meta.get("analysisType") if meta is not None else None
        behavior = problem.get("timeBehavior")
        if (isinstance(analysis, str) and analysis in _ANALYSIS_TIME
                and isinstance(behavior, str) and behavior and behavior not in _ANALYSIS_TIME[analysis]):
            _error(errors, "physicalProblem.timeBehavior", "analysis_mismatch",
                   f"{analysis} 不接受 timeBehavior={behavior}")
    geometry = parsed.get("geometry")
    if isinstance(geometry, dict):
        _validate_geometry_fields(geometry, errors)
    if isinstance(problem,dict) and 'referenceTemperatureRole' in problem:
        _enum(problem,'referenceTemperatureRole',{'buoyancy'},'physicalProblem',errors)
        from .openfoam_reference_temperature import declared
        try:
            declared(parsed)
        except ValueError as error:
            _error(errors,'physicalProblem.referenceTemperature','value',str(error))
    if isinstance(problem,dict) and ('referencePressureRole' in problem or 'referencePressureCell' in problem):
        _enum(problem,'referencePressureRole',{'gauge'},'physicalProblem',errors)
        from .openfoam_pressure_reference import declared
        try:
            if declared(parsed) is None and 'referencePressureCell' in problem:
                raise ValueError('referencePressureCell 需要 referencePressureRole gauge')
        except ValueError as error:
            _error(errors,'physicalProblem.referencePressure','value',str(error))
    materials = parsed.get("materials")
    if isinstance(materials, dict):
        _validate_materials(materials, errors)
    regions = parsed.get("regions")
    if isinstance(regions, dict):
        _validate_regions(regions, errors)
    interfaces = parsed.get('interfaces')
    if isinstance(interfaces, dict):
        _validate_interfaces(interfaces, errors)
    boundaries = parsed.get('boundariesLoadsInitials')
    if isinstance(boundaries, dict):
        from . import case_boundary_inputs
        case_boundary_inputs.validate(parsed, boundaries, errors)
    mesh = parsed.get("mesh")
    if isinstance(mesh, dict):
        from . import case_mesh_schema
        case_mesh_schema.validate(mesh, errors)
        targets = _require_dict(mesh, "qualityTargets", "mesh.qualityTargets", errors)
        if targets is not None:
            for key in ("maxNonOrtho", "maxSkewness", "maxAspectRatio", "minJacobian", "minVolume"):
                _positive_number(targets, key, "mesh.qualityTargets", errors)
            _enum(targets, "policy", _POLICIES, "mesh.qualityTargets", errors)
            if "reason" in targets and not isinstance(targets["reason"], str):
                _error(errors, "mesh.qualityTargets.reason", "type", "reason 必须是字符串")
    method = parsed.get('numericalMethod')
    if isinstance(method, dict):
        from . import case_numerical_method
        case_numerical_method.validate(method, errors)
    from . import case_execution_settings
    case_execution_settings.validate(parsed, errors)
    extensions = parsed.get("extensions")
    if isinstance(extensions, dict):
        for key in extensions:
            if key not in {"schemaVersion", "entries"}:
                _error(errors, f"extensions.{key}", "unsupported", "v1 只支持 schemaVersion/entries")
        if "schemaVersion" in extensions and str(extensions["schemaVersion"]) != "1":
            _error(errors, "extensions.schemaVersion", "enum", "只支持 extensions v1")
        records = _require_dict(extensions, "entries", "extensions.entries", errors)
        if records is not None:
            for namespace, record in records.items():
                path = f"extensions.entries.{namespace}"
                if not isinstance(namespace, str) or not re.fullmatch(r"[a-z][a-z0-9._-]{0,63}", namespace):
                    _error(errors, path, "namespace", "扩展命名空间格式非法")
                if not isinstance(record, dict):
                    _error(errors, path, "type", "扩展条目必须是字典")
                    continue
                if "version" not in record or not isinstance(record["version"], str) or not record["version"].strip():
                    _error(errors, f"{path}.version", "required", "扩展版本不能为空")
                if "enabled" not in record:
                    _error(errors, f"{path}.enabled", "required", "必须显式 enabled")
                else:
                    _bool(record["enabled"], f"{path}.enabled", errors)
                if "settings" in record and not isinstance(record["settings"], dict):
                    _error(errors, f"{path}.settings", "type", "settings 必须是字典")
    errors_block = parsed.get("errors")
    if isinstance(errors_block, dict):
        summary = _require_dict(errors_block, "summary", "errors.summary", errors)
        summary_counts = {}
        if summary is not None:
            for key in ("total", "open", "blocking", "resolved"):
                count = _count(summary, key, "errors.summary", errors)
                if count is not None:
                    summary_counts[key] = count
        entries = _validate_error_entries(errors_block, errors)
        if entries is not None and summary is not None:
            if all(isinstance(record, dict) for record in entries.values()):
                counts = _error_counts(entries)
                for key, count in summary_counts.items():
                    if count != counts[key]:
                        _error(errors, f"errors.summary.{key}", "summary_mismatch",
                               f"与 entries 计算值 {counts[key]} 不一致")
            last_id = summary.get("lastErrorId")
            if last_id is not None and (not isinstance(last_id, str) or last_id not in entries):
                _error(errors, "errors.summary.lastErrorId", "reference", "必须引用 entries 中存在的错误 ID")
        elif summary is not None and "lastErrorId" in summary:
            _error(errors, "errors.summary.lastErrorId", "reference", "没有 entries 可供引用")
    if isinstance(parsed.get('report'),dict):
        from .report_templates import metadata
        try:metadata(parsed)
        except ValueError as error:_error(errors,'report','type',str(error))
    return errors


def validate_case_dict(text: str) -> list[dict]:
    from .casedict import parse_casedict
    return validate_case_dict_data(parse_casedict(text))
