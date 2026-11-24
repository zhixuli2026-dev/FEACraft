"""WP06 专用后处理执行器。

输入是 case 内的 JSON 配置和真实 OpenFOAM 场；输出按 job 版本化保存，
最近一次有效指针只在全部产物成功后更新。数值提取使用 VTK，频谱/POD 使用 NumPy。
本模块也可作为独立进程入口，不导入 FastAPI 或数据库。
"""
from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import io
import json
import math
import operator
import os
import re
import sys
import textwrap
import threading
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath

import numpy as np

if __package__:
    from .postprocess_profile import profile_at_probes, profile_paths
    from .postprocess_reference_grid import ResultReaderError, load_grid, sample
    from .postprocess_series import align, case_series_paths, load_case_series, series_for_metric
else:
    from postprocess_profile import profile_at_probes, profile_paths
    from postprocess_reference_grid import ResultReaderError, load_grid, sample
    from postprocess_series import align, case_series_paths, load_case_series, series_for_metric


class PostprocessError(ValueError):
    pass


def _safe_rel(value: str) -> str:
    text = value.replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or PureWindowsPath(text).drive or ".." in path.parts:
        raise PostprocessError(f"非法相对路径: {value}")
    return path.as_posix().rstrip("/")


def _inside_case(case_root: Path, path: Path) -> Path:
    resolved = path.resolve()
    root = case_root.resolve()
    if resolved != root and root not in resolved.parents:
        raise PostprocessError(f"路径越过 case 根目录: {path}")
    return resolved


def _id(value, label="operation id") -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise PostprocessError(f"{label} 只能是 1–80 位字母/数字/_/-")
    return value


def _number(value, label="number") -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise PostprocessError(f"{label} 不是数值: {value!r}") from error
    if not math.isfinite(result):
        raise PostprocessError(f"{label} 必须有限")
    return result


def _point(value, label="point") -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise PostprocessError(f"{label} 必须是三维坐标")
    return [_number(v, label) for v in value]


def _time_value(value) -> float:
    return float(format(_number(value, "time"), ".12g"))


def _time_label(value: float) -> str:
    return format(value, ".12g")


def _field_unit(fields: dict, name: str) -> str:
    spec = fields.get(name, {})
    unit = spec.get("unit", "unknown") if isinstance(spec, dict) else "unknown"
    return str(unit or "unknown")


def _area_unit(unit: str) -> str:
    return "m^2" if unit == "1" else f"({unit})*m^2"


def _json_values(array, index: int):
    value = array[index]
    if np.ndim(value) == 0:
        return float(value)
    return [float(v) for v in np.asarray(value).reshape(-1)]


def _csv_bytes(fieldnames: list[str], rows: list[dict]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _boolean(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in ("true", "yes", "on", "1"):
        return True
    if normalized in ("false", "no", "off", "0"):
        return False
    raise PostprocessError(f"非法布尔值: {value!r}")


def _list_value(value):
    if isinstance(value, str):
        if value.startswith("(") and value.endswith(")"):
            return value[1:-1]
        return value
    if not isinstance(value, list):
        return value
    items = list(value)
    if items and isinstance(items[0], str) and items[0].startswith("("):
        items[0] = items[0][1:]
    if items and isinstance(items[-1], str) and items[-1].endswith(")"):
        items[-1] = items[-1][:-1]
    return [item for item in items if item != ""]


def _points_value(value):
    value = _list_value(value)
    if not isinstance(value, list) or not value:
        raise PostprocessError("points 必须是非空的三维坐标列表")
    if all(isinstance(item, (list, tuple)) for item in value):
        return value
    if len(value) % 3:
        raise PostprocessError("points 扁平坐标数量必须是 3 的倍数")
    return [value[index:index + 3] for index in range(0, len(value), 3)]


def _pressure_normalization(problem, *, moment=False):
    if not isinstance(problem,dict):
        raise PostprocessError('压力力系数需要 physicalProblem 中的参考量')
    pressure=_number(problem.get('referencePressure',0),'参考压力 Pa')
    density=_number(problem.get('referenceDensity'),'参考密度 kg/m³')
    area=_number(problem.get('referenceArea'),'参考面积 m²')
    velocity=problem.get('referenceVelocity')
    if isinstance(velocity,list) or isinstance(velocity,str) and velocity.startswith('('):
        components=_list_value(velocity)
        if isinstance(components,str):
            components=components.split()
        speed=math.hypot(*_point(components,'参考速度 m/s'))
    else:
        speed=abs(_number(velocity,'参考速度 m/s'))
    dynamic_pressure=_number(.5*density*speed*speed,'参考动压 Pa')
    scale=_number(dynamic_pressure*area,'参考力尺度 N')
    if min(density,area,speed,scale)<=0:
        raise PostprocessError('压力力系数需要正的参考密度、面积和速度幅值')
    result = {'source':'physicalProblem','reference_pressure_pa':pressure,'reference_density_kg_m3':density,
              'reference_area_m2':area,'reference_speed_m_s':speed,'dynamic_pressure_pa':dynamic_pressure,
              'reference_force_n':scale}
    if moment:
        length = _number(problem.get('referenceLength'), '参考长度 m')
        torque = _number(scale * length, '参考力矩尺度 N*m')
        if length <= 0 or torque <= 0:
            raise PostprocessError('压力力矩系数需要正的参考长度和力矩尺度')
        result.update(reference_length_m=length, reference_moment_n_m=torque)
    return result


def config_from_case_dict(parsed: dict, run_dir_override: str | None = None) -> dict:
    """把 caseDict.postProcessing 规范化为执行器快照；caseDict 仍是唯一参数源。"""
    post = parsed.get("postProcessing") or {}
    if not isinstance(post, dict):
        raise PostprocessError("postProcessing 必须是字典")
    extraction = post.get("dataExtraction") or {}
    derived = post.get("derivedQuantities") or {}
    artifact_config = post.get("artifacts") or {}
    objectives = post.get("objectives") or {}
    if (not isinstance(extraction, dict) or not isinstance(derived, dict) or
            not isinstance(artifact_config, dict) or not isinstance(objectives, dict)):
        raise PostprocessError("objectives/dataExtraction/derivedQuantities/artifacts 必须是字典")

    def operations(name: str) -> list[dict]:
        raw = extraction.get(name) or {}
        items = []
        if isinstance(raw, dict):
            for op_id, spec in raw.items():
                if not isinstance(spec, dict):
                    raise PostprocessError(f"{name}.{op_id} 必须是字典")
                items.append({**spec, "id": op_id})
        elif isinstance(raw, list):
            items = raw
        else:
            raise PostprocessError(f"{name} 必须是操作字典")
        result = []
        for spec in items:
            if not isinstance(spec, dict):
                raise PostprocessError(f"{name} 包含非字典操作")
            item = dict(spec)
            for key in ("fields", "start", "end", "times"):
                if key in item:
                    item[key] = _list_value(item[key])
            for key in ("fields", "times"):
                if key in item and not isinstance(item[key], list):
                    item[key] = [item[key]]
            if "points" in item:
                item["points"] = _points_value(item["points"])
            pressure_moment = name == 'surfaceIntegrals' and item.get('mode') == 'pressureMoment'
            if pressure_moment:
                center = _list_value(item.get('momentCenter'))
                if isinstance(center, str):
                    center = center.split()
                item['momentCenter'] = _point(center, '压力力矩原点 m')
            if 'nodalLoads' in item:
                if name!='surfaceIntegrals' or item.get('mode')!='pressureForce':
                    raise PostprocessError('nodalLoads 只适用于 pressureForce 边界积分')
                item['nodalLoads']=_boolean(item['nodalLoads'])
            if 'normalization' in item:
                if (name!='surfaceIntegrals' or item.get('mode') not in ('pressureForce','pressureMoment')
                        or item['normalization']!='physicalProblem'):
                    raise PostprocessError('normalization physicalProblem 适用于压力合力与压力力矩边界积分')
                scale=_pressure_normalization(parsed.get('physicalProblem'),moment=pressure_moment)
                for field,key in (('referencePressure','reference_pressure_pa'),('referenceDensity','reference_density_kg_m3')):
                    if field in item and _number(item.pop(field),field)!=scale[key]:
                        raise PostprocessError(f'压力载荷 {field} 与 physicalProblem 不一致')
                item['normalization']=scale
            result.append(item)
        return result

    def derived_specs(name: str) -> list[dict]:
        raw = derived.get(name) or {}
        if not isinstance(raw, dict):
            raise PostprocessError(f"derivedQuantities.{name} 必须是字典")
        result = []
        for item_id, spec in raw.items():
            if not isinstance(spec, dict):
                raise PostprocessError(f"derivedQuantities.{name}.{item_id} 必须是字典")
            result.append({**spec, "id": item_id})
        return result

    references = {}
    raw_references = derived.get("referenceValues") or {}
    if not isinstance(raw_references, dict):
        raise PostprocessError("referenceValues 必须是字典")
    for key, value in raw_references.items():
        if not isinstance(value, dict):
            raise PostprocessError(f"referenceValues.{key} 必须是字典")
        reference = dict(value)
        reference.setdefault("id", key)
        metric = reference.pop("metric", key)
        if metric in references:
            raise PostprocessError(f"重复参考指标: {metric}")
        if "value" in reference:
            reference["value"] = _list_value(reference["value"])
        references[metric] = reference

    targets = []
    raw_targets = objectives.get("comparisonTargets") or {}
    if not isinstance(raw_targets, dict):
        raise PostprocessError("objectives.comparisonTargets 必须是字典")
    for target_id, spec in raw_targets.items():
        if not isinstance(spec, dict):
            raise PostprocessError(f"comparisonTargets.{target_id} 必须是字典")
        targets.append({**spec, "id": target_id})

    plots = []
    raw_plots = artifact_config.get("plots") or {}
    if not isinstance(raw_plots, dict):
        raise PostprocessError("artifacts.plots 必须是字典")
    for plot_id, spec in raw_plots.items():
        if not isinstance(spec, dict):
            raise PostprocessError(f"artifacts.plots.{plot_id} 必须是字典")
        plot = {**spec, "id": plot_id}
        formats = _list_value(plot.get("formats", ["pdf", "svg", "png"]))
        plot["formats"] = formats if isinstance(formats, list) else [formats]
        plots.append(plot)

    fields = post.get("fields") or {}
    if not isinstance(fields, dict):
        raise PostprocessError("postProcessing.fields 必须是字典")
    return {
        "schema": "vibeflow.postprocess.v1",
        "run_dir": run_dir_override or post.get("runDir") or "C-run/case-1",
        "fields": fields,
        "pointProbes": operations("pointProbes"),
        "lineSamples": operations("lineSamples"),
        "surfaceIntegrals": operations("surfaceIntegrals"),
        "volumeAverages": operations("volumeAverages"),
        "frequencySpectra": operations("frequencySpectra"),
        "modalQuantities": operations("modalQuantities"),
        "referenceFields": derived_specs("referenceFields"),
        "formulas": derived_specs("formulas"),
        "uncertainty": derived_specs("uncertainty"),
        "referenceValues": references,
        "comparisonTargets": targets,
        "plots": plots,
    }


def postprocess_fingerprint(config: dict) -> str:
    """只指纹化后处理语义，不包含 caseDict revision 或 _source 元数据。"""
    payload = {key: value for key, value in config.items() if key != "_source"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def reference_input_paths(config):
    paths = {_safe_rel(str(spec.get('path', ''))) for spec in config.get('referenceFields') or []}
    for reference in (config.get('referenceValues') or {}).values():
        if 'profile' in reference:
            paths.update(profile_paths(reference['profile'], _safe_rel))
        for key in ('series', 'candidateSeries'):
            if key in reference:
                paths.update(case_series_paths(reference[key], _safe_rel))
    return sorted(paths)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class FoamReader:
    def __init__(self, run_dir: Path):
        import vtk
        self.vtk = vtk
        self.run_dir = run_dir
        if not (run_dir / "system" / "controlDict").is_file():
            raise PostprocessError(f"缺 system/controlDict: {run_dir.name}")
        foam = run_dir / f"{run_dir.name}.foam"
        if not foam.exists():
            candidates = sorted(run_dir.glob("*.foam"))
            if candidates:
                foam = candidates[0]
            else:
                foam.touch()
        self.foam = foam
        self.reader = vtk.vtkOpenFOAMReader()
        self.reader.SetFileName(str(foam))
        self.reader.CreateCellToPointOn()
        self.reader.UpdateInformation()
        self.reader.EnableAllCellArrays()
        values = self.reader.GetTimeValues()
        self.times = [_time_value(values.GetValue(i)) for i in range(values.GetNumberOfValues())]
        if not self.times:
            raise PostprocessError("OpenFOAM 结果没有可读取的时刻")
        self._grids = {}
        self._patch_reader = None

    def select_times(self, spec: dict, default_all: bool) -> list[float]:
        if "times" in spec:
            requested = [_time_value(t) for t in spec["times"]]
        elif "time" in spec:
            requested = [_time_value(spec["time"])]
        else:
            requested = self.times if default_all else [self.times[-1]]
        for value in requested:
            if not any(math.isclose(value, actual, rel_tol=0, abs_tol=1e-9) for actual in self.times):
                raise PostprocessError(f"时刻 {value:g} 不存在；可用: {self.times}")
        return requested

    def grid(self, time_value: float, *, cache=True):
        key = _time_value(time_value)
        if key in self._grids:
            return self._grids[key]
        self.reader.UpdateTimeStep(key)
        output = self.reader.GetOutput()
        block = output.GetBlock(0) if hasattr(output, "GetNumberOfBlocks") else output
        if block is None or not block.IsA("vtkUnstructuredGrid") or block.GetNumberOfCells() == 0:
            raise PostprocessError(f"时刻 {key:g} 没有可用的 internalMesh")
        grid = self.vtk.vtkUnstructuredGrid()
        grid.DeepCopy(block)
        if cache:
            self._grids[key] = grid
        return grid

    def patch(self, name, time_value):
        if self._patch_reader is None:
            self._patch_reader = self.vtk.vtkOpenFOAMReader()
            self._patch_reader.SetFileName(str(self.foam))
            self._patch_reader.CreateCellToPointOff()
            self._patch_reader.UpdateInformation()
            self._patch_reader.EnableAllCellArrays()
        reader = self._patch_reader
        arrays = [reader.GetPatchArrayName(index) for index in range(reader.GetNumberOfPatchArrays())]
        selection = next((value for value in arrays if value.lstrip('/') == f'patch/{name}'), None)
        if selection is None:
            raise PostprocessError(f"OpenFOAM 边界不存在: {name}")
        reader.DisableAllPatchArrays()
        reader.SetPatchArrayStatus(selection, 1)
        reader.UpdateTimeStep(time_value)
        output = reader.GetOutput()
        for index in range(output.GetNumberOfBlocks()):
            group = output.GetBlock(index)
            if output.GetMetaData(index).Get(output.NAME()) != 'boundary' or group is None:
                continue
            for child in range(group.GetNumberOfBlocks()):
                if group.GetMetaData(child).Get(group.NAME()) != name:
                    continue
                patch = group.GetBlock(child)
                if patch is not None and patch.IsA('vtkPolyData') and patch.GetNumberOfCells():
                    return patch
        raise PostprocessError(f"时刻 {time_value:g} 的边界 {name} 没有可用的面单元")


def _probe(grid, points: list[list[float]], fields: list[str], vtk) -> dict[str, np.ndarray]:
    vtk_points = vtk.vtkPoints()
    for point in points:
        vtk_points.InsertNextPoint(*point)
    poly = vtk.vtkPolyData()
    poly.SetPoints(vtk_points)
    probe = vtk.vtkProbeFilter()
    probe.SetInputData(poly)
    probe.SetSourceData(grid)
    probe.Update()
    output = probe.GetOutput()
    mask = output.GetPointData().GetArray("vtkValidPointMask")
    if mask is not None:
        from vtk.util.numpy_support import vtk_to_numpy
        valid = vtk_to_numpy(mask)
        if not np.all(valid == 1):
            bad = [i for i, value in enumerate(valid) if value != 1]
            raise PostprocessError(f"探针在计算域外: indices={bad}")
    from vtk.util.numpy_support import vtk_to_numpy
    result = {}
    for field in fields:
        array = output.GetPointData().GetArray(field)
        if array is None:
            raise PostprocessError(f"点数据场不存在: {field}")
        result[field] = vtk_to_numpy(array).copy()
    return result


def extract_point_probes(reader: FoamReader, specs: list, fields: dict):
    extractions, csv_rows = [], []
    for spec in specs:
        op_id = _id(spec.get("id"))
        points = [_point(p, f"{op_id}.points") for p in spec.get("points", [])]
        if not 1 <= len(points) <= 256:
            raise PostprocessError(f"{op_id}: points 数量须为 1–256")
        names = spec.get("fields") or []
        if not names or len(names) > 32:
            raise PostprocessError(f"{op_id}: fields 须为 1–32 项")
        times = reader.select_times(spec, default_all=True)
        values_by_field = {name: [] for name in names}
        components = {}
        for time_value in times:
            arrays = _probe(reader.grid(time_value), points, names, reader.vtk)
            for name in names:
                array = arrays[name]
                components[name] = 1 if array.ndim == 1 else array.shape[1]
                values_by_field[name].append([_json_values(array, i) for i in range(len(points))])
        series = {
            name: {"unit": _field_unit(fields, name), "components": components[name],
                   "values": values_by_field[name]}
            for name in names
        }
        extractions.append({"id": op_id, "points": points, "times": times,
                            "layout": "time,probe[,component]", "series": series})
        for ti, time_value in enumerate(times):
            for pi, point in enumerate(points):
                for name in names:
                    value = values_by_field[name][ti][pi]
                    components_values = [value] if isinstance(value, float) else value
                    for ci, component in enumerate(components_values):
                        csv_rows.append({"operation": op_id, "time": _time_label(time_value),
                                         "probe_index": pi, "x": point[0], "y": point[1], "z": point[2],
                                         "field": name, "component": ci, "value": component})
    artifacts = {"probes.csv": _csv_bytes(
        ["operation", "time", "probe_index", "x", "y", "z", "field", "component", "value"], csv_rows)}
    return extractions, artifacts


def extract_line_samples(reader: FoamReader, specs: list, fields: dict):
    extractions, artifacts = [], {}
    for spec in specs:
        op_id = _id(spec.get("id"))
        start = _point(spec.get("start"), f"{op_id}.start")
        end = _point(spec.get("end"), f"{op_id}.end")
        samples = int(spec.get("samples", 100))
        if not 2 <= samples <= 1001:
            raise PostprocessError(f"{op_id}: samples 须为 2–1001")
        if start == end:
            raise PostprocessError(f"{op_id}: start/end 不能重合")
        names = spec.get("fields") or []
        if not names:
            raise PostprocessError(f"{op_id}: fields 不能为空")
        points = [[start[axis] + (end[axis] - start[axis]) * i / (samples - 1)
                   for axis in range(3)] for i in range(samples)]
        distances = [math.dist(start, point) for point in points]
        times = reader.select_times(spec, default_all=False)
        values_by_field = {name: [[] for _ in range(samples)] for name in names}
        components = {}
        for time_value in times:
            arrays = _probe(reader.grid(time_value), points, names, reader.vtk)
            for name in names:
                array = arrays[name]
                components[name] = 1 if array.ndim == 1 else array.shape[1]
                for i in range(samples):
                    values_by_field[name][i].append(_json_values(array, i))
        series = {name: {"unit": _field_unit(fields, name), "components": components[name],
                         "values": values_by_field[name]} for name in names}
        extractions.append({"id": op_id, "times": times, "points": points, "distances": distances,
                            "layout": "sample,time[,component]", "series": series})
        rows = []
        for i, point in enumerate(points):
            for ti, time_value in enumerate(times):
                for name in names:
                    value = values_by_field[name][i][ti]
                    components_values = [value] if isinstance(value, float) else value
                    for ci, component in enumerate(components_values):
                        rows.append({"time": _time_label(time_value), "sample_index": i,
                                     "distance": distances[i], "x": point[0], "y": point[1], "z": point[2],
                                     "field": name, "component": ci, "value": component})
        artifacts[f"{op_id}.csv"] = _csv_bytes(
            ["time", "sample_index", "distance", "x", "y", "z", "field", "component", "value"], rows)
    return extractions, artifacts


def _cell_values(grid, field):
    from vtk.util.numpy_support import vtk_to_numpy

    values = grid.GetCellData().GetArray(field)
    if values is None:
        raise PostprocessError(f"缺少单元场: {field}")
    data = vtk_to_numpy(values).astype(float).reshape(grid.GetNumberOfCells(), -1)
    if not np.all(np.isfinite(data)):
        raise PostprocessError(f"单元场包含非有限值: {field}")
    return data


def _cell_values_and_weights(reader: FoamReader, grid, field: str, surface: bool):
    vtk = reader.vtk
    source = grid
    if surface and not grid.IsA('vtkPolyData'):
        boundary = vtk.vtkDataSetSurfaceFilter()
        boundary.SetInputData(grid)
        boundary.Update()
        source = boundary.GetOutput()
    sizes = vtk.vtkCellSizeFilter()
    sizes.SetInputData(source)
    if surface:
        sizes.ComputeAreaOn()
        sizes.ComputeVolumeOff()
    else:
        sizes.ComputeVolumeOn()
        sizes.ComputeAreaOff()
    sizes.ComputeLengthOff()
    sizes.ComputeVertexCountOff()
    sizes.Update()
    output = sizes.GetOutput()
    measure_name = "Area" if surface else "Volume"
    from vtk.util.numpy_support import vtk_to_numpy
    measure = output.GetCellData().GetArray(measure_name)
    if measure is None:
        raise PostprocessError(f"积分缺少 {measure_name} 权重: {field}")
    weights = vtk_to_numpy(measure).astype(float)
    data = _cell_values(output, field)
    if len(weights) != len(data) or not np.all(np.isfinite(weights)) or np.any(weights < 0) or np.sum(weights) <= 0:
        raise PostprocessError(f"无效 {'面积' if surface else '体积'}权重: {field}")
    return data, weights


def _patch_geometry(patch):
    from vtk.util.numpy_support import vtk_to_numpy

    polygons = patch.GetPolys()
    offsets = vtk_to_numpy(polygons.GetOffsetsArray())
    sizes = np.diff(offsets)
    if polygons.GetNumberOfCells() != patch.GetNumberOfCells() or np.any(sizes < 3):
        raise PostprocessError("法向通量需要多边形边界面")
    points = vtk_to_numpy(patch.GetPoints().GetData()).astype(float)
    connectivity = vtk_to_numpy(polygons.GetConnectivityArray())
    return points, connectivity, offsets, sizes


def _patch_area_vectors(patch, *, centers=False):
    """Use OpenFOAM's face winding; outward normals must retain their sign."""
    points, connectivity, offsets, sizes = _patch_geometry(patch)
    faces = points[connectivity]
    starts = offsets[:-1]
    local = faces - np.repeat(faces[starts], sizes, axis=0)
    following = np.roll(local, -1, axis=0)
    following[offsets[1:] - 1] = 0
    vectors = np.add.reduceat(np.cross(local, following), starts, axis=0) * .5
    if not np.all(np.isfinite(vectors)) or np.any(np.linalg.norm(vectors, axis=1) == 0):
        raise PostprocessError("法向通量的边界面法向无效")
    if not centers:
        return vectors

    # Use triangle centroids and areas about the mean face point, as in OpenFOAM.
    means = np.add.reduceat(local, starts, axis=0) / sizes[:, None]
    vertices = local - np.repeat(means, sizes, axis=0)
    following = np.roll(vertices, -1, axis=0)
    following[offsets[1:] - 1] = vertices[starts]
    areas = np.linalg.norm(np.cross(vertices, following), axis=1)
    total = np.add.reduceat(areas, starts)
    coordinates = faces[starts] + means + np.add.reduceat((vertices + following) * areas[:, None], starts, axis=0) / (3 * total[:, None])
    if not np.all(np.isfinite(coordinates)) or np.any(total <= 0):
        raise PostprocessError('压力力矩的面中心无效')
    return vectors, coordinates


def _pressure_nodal_loads(mesh, traction, expected):
    points, connectivity, offsets, sizes = _patch_geometry(mesh)
    starts, ends = offsets[:-1], offsets[1:]-1
    vertices = points[connectivity]
    local = vertices - np.repeat(vertices[starts], sizes, axis=0)
    means = np.add.reduceat(local, starts, axis=0)/sizes[:,None]
    centered = local - np.repeat(means, sizes, axis=0)
    following = np.roll(centered,-1,axis=0)
    following[ends] = centered[starts]
    # Integrate the linear nodal weights on each triangle. At the face mean
    # the weights are equal, so its third is shared by all incident vertices.
    thirds = np.cross(centered,following)*np.repeat(traction,sizes)[:,None]/6
    previous = np.roll(thirds,1,axis=0)
    previous[starts] = thirds[ends]
    mean_share = np.add.reduceat(thirds,starts,axis=0)/sizes[:,None]
    forces = np.zeros_like(points)
    np.add.at(forces,connectivity,thirds+previous+np.repeat(mean_share,sizes,axis=0))
    bound = 1e-12 * max(1, np.sum(np.abs(thirds)))
    if not np.all(np.isfinite(forces)) or np.any(np.abs(forces.sum(axis=0)-expected)>bound):
        raise PostprocessError('边界节点载荷未守恒压力合力')
    used = np.unique(connectivity)
    return {'positions_m':points[used].tolist(), 'forces_n':forces[used].tolist()}


def _pressure_load(spec, data, mesh, unit, *, moment=False):
    if data.shape[1] != 1:
        raise PostprocessError('压力载荷需要标量压力场')
    scale=spec.get('normalization')
    pressure = scale['reference_pressure_pa'] if scale else _number(spec.get('referencePressure', 0), '参考压力 Pa')
    settings = {'reference_pressure_Pa': pressure}
    if unit == 'm^2/s^2':
        density = scale['reference_density_kg_m3'] if scale else _number(spec.get('referenceDensity'), '参考密度 kg/m^3')
        if density <= 0:
            raise PostprocessError('运动学压力需要正的参考密度')
        settings['reference_density_kg_m3'] = density
    elif unit == 'Pa' and 'referenceDensity' not in spec:
        density = 1
    else:
        raise PostprocessError('压力载荷需要 Pa，或 m^2/s^2 与参考密度')
    traction = density*data[:,0] - pressure
    if moment:
        area, centers = _patch_area_vectors(mesh, centers=True)
        center = _point(spec.get('momentCenter'), '压力力矩原点 m')
        settings['moment_center_m'] = center
        contributions = np.cross(centers - center, traction[:, None] * area)
        coefficient, reference = 'moment_coefficient', 'reference_moment_n_m'
    else:
        contributions = traction[:, None] * _patch_area_vectors(mesh)
        coefficient, reference = 'force_coefficient', 'reference_force_n'
    load = np.sum(contributions, axis=0)
    if not np.all(np.isfinite(load)):
        raise PostprocessError('压力载荷需要有限的分量')
    if not moment and _boolean(spec.get('nodalLoads',False)):
        settings['nodal_loads'] = _pressure_nodal_loads(mesh,traction,load)
    if scale:
        settings['normalization']=scale
        settings[coefficient]=[_number(value/scale[reference], '压力载荷系数') for value in load]
    return load, settings


def extract_integrals(reader: FoamReader, surface_specs: list, volume_specs: list, fields: dict):
    extractions = {"surfaceIntegrals": [], "volumeAverages": []}
    metrics = []
    for operation, specs, is_surface in (("surfaceIntegrals", surface_specs, True),
                                         ("volumeAverages", volume_specs, False)):
        for spec in specs:
            op_id = _id(spec.get("id"))
            field = spec.get("field")
            if not isinstance(field, str) or not field:
                raise PostprocessError(f"{op_id}: field 不能为空")
            patch = spec.get('patch') if is_surface else None
            mode = spec.get('mode', 'areaIntegrate') if is_surface else 'areaIntegrate'
            if patch is not None and (not isinstance(patch, str) or not patch or '/' in patch or '\\' in patch):
                raise PostprocessError(f"{op_id}: patch 必须是准确的 OpenFOAM 边界名称")
            if mode not in ('areaIntegrate', 'normalFlux', 'pressureForce', 'pressureMoment') or (mode != 'areaIntegrate' and patch is None):
                raise PostprocessError(f"{op_id}: 法向通量或压力载荷需要指定 patch")
            for time_value in reader.select_times(spec, default_all=False):
                mesh = reader.patch(patch, time_value) if patch is not None else reader.grid(time_value)
                data, weights = _cell_values_and_weights(reader, mesh, field, is_surface)
                measure = float(np.sum(weights))
                field_unit = _field_unit(fields, field)
                unit = _area_unit(field_unit) if is_surface else field_unit
                load_settings = {}
                if mode == 'normalFlux':
                    if data.shape[1] != 3:
                        raise PostprocessError(f"{op_id}: normalFlux 需要三分量向量单元场: {field}")
                    integral = np.array([np.sum(data * _patch_area_vectors(mesh))])
                elif mode in ('pressureForce', 'pressureMoment'):
                    moment = mode == 'pressureMoment'
                    integral, load_settings = _pressure_load(spec, data, mesh, field_unit, moment=moment)
                    unit = 'N*m' if moment else 'N'
                else:
                    integral = np.sum(data * weights.reshape(-1, 1), axis=0)
                values = integral if is_surface else integral / measure
                record = {"id": op_id, "field": field, "time": time_value,
                          "area" if is_surface else "volume": measure,
                          "integral": float(integral[0]) if len(integral) == 1 else integral.tolist(),
                          "unit": unit, **load_settings}
                if patch is not None:
                    record.update(patch=patch, mode=mode, face_count=mesh.GetNumberOfCells())
                if not is_surface:
                    record["average"] = float(values[0]) if len(values) == 1 else values.tolist()
                extractions[operation].append(record)
                for ci, value in enumerate(values):
                    suffix = "" if len(values) == 1 else f"[{ci}]"
                    metric = {"name": f"{op_id}.{field}{suffix}@{_time_label(time_value)}",
                                    "operation": operation, "field": field, "component": ci,
                                    "time": time_value, "value": float(value), "unit": unit}
                    if patch is not None:
                        metric.update(patch=patch, mode=mode)
                    metrics.append(metric)
                    for key in ('force_coefficient', 'moment_coefficient'):
                        if key in load_settings:
                            metrics.append({**metric,'name':f'{op_id}.{field}Coefficient{suffix}@{_time_label(time_value)}',
                                            'value':load_settings[key][ci],'unit':'1'})
    return extractions, metrics


def _source_series(point_probes: list, source: str, field: str, component: int):
    extraction = next((item for item in point_probes if item["id"] == source), None)
    if extraction is None or field not in extraction.get("series", {}):
        raise PostprocessError(f"频谱/POD 来源不存在: {source}.{field}")
    series = extraction["series"][field]
    values = np.asarray(series["values"], dtype=float)
    if values.ndim == 2:
        values = values.reshape(values.shape[0], values.shape[1], 1)
    if not 0 <= component < values.shape[2]:
        raise PostprocessError(f"{source}.{field}: component {component} 越界")
    return extraction, series, values[:, :, component]


def extract_frequency_spectra(specs: list, point_probes: list):
    extractions, metrics, artifacts = [], [], {}
    for spec in specs:
        op_id = _id(spec.get("id"))
        field = spec.get("field")
        component = int(spec.get("component", 0))
        source, series, values = _source_series(point_probes, spec.get("source"), field, component)
        times = np.asarray(source["times"], dtype=float)
        if len(times) < 4 or values.shape[0] != len(times):
            raise PostprocessError(f"{op_id}: 至少需要 4 个等间隔时刻")
        intervals = np.diff(times)
        dt = float(np.mean(intervals))
        if dt <= 0 or not np.allclose(intervals, dt, rtol=1e-8, atol=1e-12):
            raise PostprocessError(f"{op_id}: 频谱要求等间隔递增时刻")
        detrend = _boolean(spec.get("detrend", True))
        centered = values - np.mean(values, axis=0) if detrend else values
        transformed = np.fft.rfft(centered, axis=0)
        frequencies = np.fft.rfftfreq(len(times), dt)
        amplitudes = 2.0 / len(times) * np.abs(transformed)
        amplitudes[0, :] *= 0.5
        if len(times) % 2 == 0:
            amplitudes[-1, :] *= 0.5
        if len(frequencies) < 2:
            raise PostprocessError(f"{op_id}: 没有非零频率")
        mean_amplitudes = np.mean(amplitudes, axis=1)
        dominant = 1 + int(np.argmax(mean_amplitudes[1:]))
        record = {"id": op_id, "source": source["id"], "field": field, "component": component,
                  "method": "rFFT", "detrend": detrend, "sample_interval": dt,
                  "frequencies_hz": frequencies.tolist(), "amplitudes": amplitudes.tolist(),
                  "dominant_frequency_hz": float(frequencies[dominant]),
                  "dominant_amplitude": float(mean_amplitudes[dominant]), "unit": series["unit"]}
        extractions.append(record)
        metrics.extend([
            {"name": f"{op_id}.dominant_frequency_hz", "operation": "frequencySpectra",
             "value": record["dominant_frequency_hz"], "unit": "Hz"},
            {"name": f"{op_id}.dominant_amplitude", "operation": "frequencySpectra",
             "value": record["dominant_amplitude"], "unit": series["unit"]},
        ])
        rows = [{"frequency_hz": frequencies[fi], "probe_index": pi, "amplitude": amplitudes[fi, pi]}
                for fi in range(len(frequencies)) for pi in range(values.shape[1])]
        artifacts[f"{op_id}.csv"] = _csv_bytes(["frequency_hz", "probe_index", "amplitude"], rows)
    return extractions, metrics, artifacts


def _mesh_arrays(grid):
    from vtk.util.numpy_support import vtk_to_numpy

    yield vtk_to_numpy(grid.GetPoints().GetData())
    yield vtk_to_numpy(grid.GetCellTypes())
    for cells in (grid.GetCells(), grid.GetPolyhedronFaces(), grid.GetPolyhedronFaceLocations()):
        if cells is not None:
            yield vtk_to_numpy(cells.GetOffsetsArray())
            yield vtk_to_numpy(cells.GetConnectivityArray())


def _mesh_pod(reader, fields, spec, op_id):
    from vtk.util.numpy_support import numpy_to_vtk

    field = spec.get('field')
    if not isinstance(field, str) or not field:
        raise PostprocessError(f'{op_id}: cellField 需要指定场名称')
    times = reader.select_times(spec, default_all=True)
    if len(times) < 2 or len(times) > 256 or np.any(np.diff(times) <= 0):
        raise PostprocessError(f'{op_id}: 全场 POD 需要 2–256 个递增的不同输出时刻')
    mesh = reader.grid(times[0], cache=False)
    first, volumes = _cell_values_and_weights(reader, mesh, field, False)
    component = None
    if 'component' in spec:
        selection = _number(spec['component'], f'{op_id}.component')
        if not selection.is_integer() or not 0 <= selection < first.shape[1]:
            raise PostprocessError(f'{op_id}: component 越界')
        component = int(selection)
    selected = first if component is None else first[:, component:component+1]
    if np.any(volumes <= 0) or len(times) * selected.size > 8_000_000:
        raise PostprocessError(f'{op_id}: 全场 POD 需要正单元体积，最多处理 800 万个快照分量值')
    geometry = tuple(_mesh_arrays(mesh))
    snapshots = np.empty((len(times), *selected.shape), dtype=float)
    snapshots[0] = selected
    for index, time_value in enumerate(times[1:], 1):
        other = reader.grid(time_value, cache=False)
        arrays = tuple(_mesh_arrays(other))
        if len(arrays) != len(geometry) or any(not np.array_equal(a,b) for a,b in zip(geometry,arrays)):
            raise PostprocessError(f'{op_id}: 时刻 {time_value:g} 的网格与首个快照不一致')
        values = _cell_values(other, field)
        if values.shape != first.shape:
            raise PostprocessError(f'{op_id}: 时刻 {time_value:g} 的场分量与首个快照不一致')
        snapshots[index] = values if component is None else values[:, component:component+1]

    detrend = _boolean(spec.get('detrend', True))
    mean = snapshots.mean(axis=0)
    if detrend:
        snapshots -= mean
    cell_weights = np.sqrt(volumes)[:,None]
    snapshots *= cell_weights
    matrix = snapshots.reshape(len(times), -1)
    eigenvalues, temporal = np.linalg.eigh(matrix @ matrix.T)
    eigenvalues = eigenvalues[::-1]
    power = float(eigenvalues[0])
    available = int(np.count_nonzero(eigenvalues > power * np.finfo(float).eps * max(matrix.shape)))
    if power <= 0 or available == 0:
        raise PostprocessError(f'{op_id}: 零能量矩阵无法提取模态')
    rank_value = _number(spec.get('rank', min(4, available)), f'{op_id}.rank')
    if not rank_value.is_integer() or not 1 <= rank_value <= available:
        raise PostprocessError(f'{op_id}: rank 须为 1–{available}')
    rank = int(rank_value)
    singular = np.sqrt(eigenvalues[:rank])
    temporal = temporal[:,::-1][:,:rank].T.copy()
    spatial = (temporal @ matrix) / singular[:,None]
    for index, mode in enumerate(spatial):
        if mode[np.argmax(np.abs(mode))] < 0:
            spatial[index] *= -1
            temporal[index] *= -1
    vtk = reader.vtk
    output = vtk.vtkUnstructuredGrid()
    output.CopyStructure(mesh)
    names = ['PODMean', *[f'PODMode_{index+1}' for index in range(rank)]]
    for index, name in enumerate(names):
        values = mean if index == 0 else spatial[index-1].reshape(mean.shape) / cell_weights
        array = numpy_to_vtk(values, deep=True)
        array.SetName(name)
        output.GetCellData().AddArray(array)
    writer = vtk.vtkXMLUnstructuredGridWriter()
    writer.SetInputData(output)
    writer.SetDataModeToBinary()
    writer.WriteToOutputStringOn()
    if writer.Write() != 1:
        raise PostprocessError(f'{op_id}: 无法写入全场模态网格')
    artifact = f'{op_id}.vtu'
    record = {'id':op_id, 'source':'internalMesh', 'field':field, 'method':'POD-snapshots',
        'weighting':'cellVolume', 'component':component, 'cell_count':mesh.GetNumberOfCells(),
        'times':times, 'rank':rank, 'detrend':detrend, 'singular_values':singular.tolist(),
        'energy_fractions':(eigenvalues[:rank] / np.clip(eigenvalues, 0, None).sum()).tolist(),
        'temporal_modes':temporal.tolist(), 'unit':_field_unit(fields,field),
        'spatial_artifact':artifact, 'spatial_arrays':names}
    return record, writer.GetOutputString().encode('utf8')


def extract_modal_quantities(specs: list, point_probes: list, *, reader=None, fields=None):
    extractions, metrics, artifacts = [], [], {}
    for spec in specs:
        op_id = _id(spec.get("id"))
        source_type = spec.get('sourceType','pointProbes')
        if source_type == 'cellField':
            record, artifact = _mesh_pod(reader, fields, spec, op_id)
            artifacts[record['spatial_artifact']] = artifact
        elif source_type == 'pointProbes':
            record = _probe_pod(spec, point_probes, op_id)
        else:
            raise PostprocessError(f'{op_id}: POD sourceType 必须为 pointProbes 或 cellField')
        extractions.append(record)
        for index, fraction in enumerate(record['energy_fractions']):
            metrics.append({"name": f"{op_id}.energy_fraction[{index}]", "operation": "modalQuantities",
                            "value": float(fraction), "unit": "1"})
        artifacts[f"{op_id}.json"] = _json_bytes(record)
    return extractions, metrics, artifacts


def _probe_pod(spec, point_probes, op_id):
    field = spec.get("field")
    source, series, values = _source_series(
        point_probes, spec.get("source"), field, int(spec.get("component", 0)))
    detrend = _boolean(spec.get("detrend", True))
    mean = np.mean(values, axis=0)
    matrix = values - mean if detrend else values
    u, singular, vt = np.linalg.svd(matrix, full_matrices=False)
    energy = singular ** 2
    if not len(energy) or float(np.sum(energy)) <= 0:
        raise PostprocessError(f"{op_id}: 零能量矩阵无法提取模态")
    rank = int(spec.get("rank", min(4, len(singular))))
    if not 1 <= rank <= len(singular):
        raise PostprocessError(f"{op_id}: rank 须为 1–{len(singular)}")
    fractions = energy / np.sum(energy)
    return {"id": op_id, "source": source["id"], "field": field, "method": "POD-SVD",
            "detrend": detrend, "rank": rank, "mean": mean.tolist(),
            "singular_values": singular[:rank].tolist(),
            "energy_fractions": fractions[:rank].tolist(),
            "temporal_modes": u[:, :rank].T.tolist(), "spatial_modes": vt[:rank, :].tolist(),
            "unit": series["unit"]}


_CHART_LOCK = threading.RLock()
_CHART_STYLES = (
    ("#0072B2", "o", "-"), ("#D55E00", "s", "--"), ("#009E73", "^", "-."),
    ("#CC79A7", "D", ":"), ("#E69F00", "v", "--"), ("#000000", "P", "-."),
)


def render_charts(specs: list, point_probes: list, line_samples: list, provenance: dict):
    # Publication verification can run concurrently in API worker threads.
    with _CHART_LOCK:
        import matplotlib
        matplotlib.use('Agg')
        with matplotlib.rc_context(rc=matplotlib.rcParamsDefault):
            return _render_charts(specs, point_probes, line_samples, provenance)


def _render_charts(specs: list, point_probes: list, line_samples: list, provenance: dict):
    import matplotlib
    matplotlib.use("Agg")
    matplotlib.rcParams["svg.fonttype"] = "none"
    matplotlib.rcParams["svg.hashsalt"] = "vibeflow-postprocess-v1"
    import matplotlib.pyplot as plt

    charts, artifacts = [], {}
    for spec in specs:
        chart_id = _id(spec.get("id"), "plot id")
        dpi, formats = _chart_formats(spec, chart_id)
        if "series" in spec or "layout" in spec:
            chart, files = _comparison_chart(spec, chart_id, point_probes, line_samples, provenance, dpi, formats)
            charts.append(chart)
            artifacts.update(files)
            continue
        source_id = spec.get("source")
        field = spec.get("field")
        x, y, detail, default_x_label = _chart_values(spec, chart_id, point_probes, line_samples)
        x_unit, y_unit = detail.pop("x_unit"), detail.pop("y_unit")
        title = str(spec.get("title", f"{source_id}.{field}"))
        x_label = str(spec.get("xLabel", default_x_label))
        y_label = str(spec.get("yLabel", field))
        color = "#0072B2"  # Okabe-Ito 色盲友好蓝；首版统一单序列配色
        footer = (f"case={provenance['case_id']} job={provenance['job_id']} run={provenance['run_dir']}\n"
                  f"caseDict={provenance.get('sha256', '')} source={source_id}.{field}")
        figure, axis = plt.subplots(figsize=(6, 4))
        try:
            axis.plot(x, y, color=color, linewidth=1.8, marker="o", markersize=3)
            axis.set_title(title)
            axis.set_xlabel(f"{x_label} [{x_unit}]")
            axis.set_ylabel(f"{y_label} [{y_unit}]")
            axis.grid(True, color="#d9d9d9", linewidth=0.6, alpha=0.8)
            figure.text(0.08, 0.025, footer, fontsize=6, color="#555555")
            figure.subplots_adjust(left=0.14, right=0.96, top=0.86, bottom=0.28)
            files = _chart_files(figure, chart_id, dpi, formats, title, footer)
        finally:
            plt.close(figure)
        artifacts.update(files)
        charts.append({"id": chart_id, **detail, "title": title, "x_unit": x_unit, "y_unit": y_unit,
                       "backend": "Agg", "dpi": dpi, "color": color, "artifacts": list(files),
                       "provenance": {"case_dict_sha256": provenance.get("sha256", ""),
                                      "case_dict_revision": provenance.get("dict_revision")}})
    return charts, artifacts


def _chart_formats(spec, chart_id):
    dpi = int(spec.get("dpi", 300))
    if not 300 <= dpi <= 600:
        raise PostprocessError(f"{chart_id}: dpi 须为 300–600")
    formats = [str(value).lower() for value in spec.get("formats", ["pdf", "svg", "png"])]
    if not formats or len(set(formats)) != len(formats) or any(value not in ("pdf", "svg", "png") for value in formats):
        raise PostprocessError(f"{chart_id}: formats 只能包含不重复的 pdf/svg/png")
    return dpi, formats


def _chart_values(spec, chart_id, point_probes, line_samples):
    source_id, field = spec.get("source"), spec.get("field")
    component = int(spec.get("component", 0))
    source = next((item for item in point_probes if item["id"] == source_id), None)
    source_kind = "pointProbes"
    if source is None:
        source = next((item for item in line_samples if item["id"] == source_id), None)
        source_kind = "lineSamples"
    if source is None or not isinstance(field, str) or field not in source.get("series", {}):
        raise PostprocessError(f"图表来源不存在: {source_id}.{field}")
    series = source["series"][field]
    values = np.asarray(series["values"], dtype=float)
    if values.ndim == 2:
        values = values.reshape(values.shape[0], values.shape[1], 1)
    detail = {"source": source_id, "source_kind": source_kind, "field": field, "component": component}
    if source_kind == "pointProbes":
        probe_index = int(spec.get("probe", 0))
        if not 0 <= probe_index < values.shape[1] or not 0 <= component < values.shape[2]:
            raise PostprocessError(f"{chart_id}: probe/component 越界")
        x = np.asarray(source["times"], dtype=float)
        y = values[:, probe_index, component]
        canonical_unit, axis = "s", "Time"
        detail["probe_index"] = probe_index
    else:
        times = source["times"]
        requested_time = _time_value(spec.get("time", times[-1]))
        matches = [index for index, value in enumerate(times)
                   if math.isclose(value, requested_time, rel_tol=0, abs_tol=1e-9)]
        if not matches or not 0 <= component < values.shape[2]:
            raise PostprocessError(f"{chart_id}: time/component 越界")
        x = np.asarray(source["distances"], dtype=float)
        y = values[:, matches[0], component]
        canonical_unit, axis = "m", "Distance"
        detail["time"] = requested_time
    x_unit = str(spec.get("xUnit", canonical_unit))
    y_unit = series["unit"]
    if not x_unit or x_unit == "unknown" or not y_unit or y_unit == "unknown":
        raise PostprocessError(f"{chart_id}: 出版图必须声明 x/y 单位")
    if len(x) != len(y) or len(x) < 2 or not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
        raise PostprocessError(f"{chart_id}: 没有有效绘图序列")
    detail.update(x_unit=x_unit, y_unit=y_unit)
    return x, y, detail, axis


def _chart_files(figure, chart_id, dpi, formats, title, footer):
    files = {}
    for file_format in formats:
        if file_format == "pdf":
            metadata = {"Title": title, "Author": "VibeFlow", "Subject": footer,
                        "CreationDate": None, "ModDate": None}
        elif file_format == "svg":
            metadata = {"Title": title, "Creator": "VibeFlow", "Description": footer, "Date": None}
        else:
            metadata = {"Title": title, "Description": footer, "Software": "VibeFlow Agg"}
        with io.BytesIO() as stream:
            figure.savefig(stream, format=file_format, dpi=dpi, facecolor="white", metadata=metadata)
            files[f"{chart_id}.{file_format}"] = stream.getvalue()
    return files


def _chart_band(spec, y, detail, label):
    band = spec.get("band")
    if not isinstance(band, dict) or set(band) != {"unit", "source", "halfWidth"}:
        raise PostprocessError(f"{label}: band 必须声明 unit、source 和 halfWidth")
    source = band["source"]
    if not isinstance(source, str) or not source.strip() or band["unit"] != detail["y_unit"]:
        raise PostprocessError(f"{label}: 误差带必须声明来源，且单位与纵轴一致")
    width = _list_value(band["halfWidth"])
    if isinstance(width, list):
        if len(width) != len(y):
            raise PostprocessError(f"{label}: 误差带样本数与数据不一致")
        width = np.asarray([_number(value, "band.halfWidth") for value in width])
    else:
        width = np.full(len(y), _number(width, "band.halfWidth"))
    if np.any(width < 0):
        raise PostprocessError(f"{label}: 误差带半宽不能为负数")
    with np.errstate(over="ignore"):
        lower, upper = y - width, y + width
    if not np.all(np.isfinite(lower)) or not np.all(np.isfinite(upper)):
        raise PostprocessError(f"{label}: 误差带边界必须有限")
    detail["band"] = {"source": source.strip(), "unit": band["unit"]}
    return lower, upper


def _comparison_chart(spec, chart_id, point_probes, line_samples, provenance, dpi, formats):
    import matplotlib.pyplot as plt

    layout = spec.get("layout", "overlay")
    configured = spec.get("series")
    if layout not in ("overlay", "panels") or not isinstance(configured, dict) or not 1 <= len(configured) <= len(_CHART_STYLES):
        raise PostprocessError(f"{chart_id}: layout 须为 overlay 或 panels，并声明 1–{len(_CHART_STYLES)} 条 series")
    if any(key in spec for key in ("source", "field", "time", "probe", "component")):
        raise PostprocessError(f"{chart_id}: source/field/time/probe/component 应写在各 series 内")
    columns = spec.get("columns", 1)
    if str(columns) not in ("1", "2") or (layout == "overlay" and "columns" in spec):
        raise PostprocessError(f"{chart_id}: panels 的 columns 必须为 1 或 2")
    columns = int(columns)
    rows = math.ceil(len(configured) / columns) if layout == "panels" else 1
    if rows > 4:
        raise PostprocessError(f"{chart_id}: 分面最多 4 行，请使用两列")

    series, labels, csv_rows = [], set(), []
    for key, (color, marker, style) in zip(sorted(configured), _CHART_STYLES):
        identifier = _id(key, "plot series id")
        entry = configured[key]
        if not isinstance(entry, dict):
            raise PostprocessError(f"{chart_id}.{identifier}: series 必须是字典")
        if any(not re.fullmatch(r"[0-9]+", str(entry[key])) for key in ("component", "probe") if key in entry):
            raise PostprocessError(f"{chart_id}.{identifier}: component 和 probe 必须是非负整数")
        label = entry.get("label", identifier)
        if not isinstance(label, str) or not label.strip() or len(label) > 72 or label in labels:
            raise PostprocessError(f"{chart_id}.{identifier}: 曲线名称应唯一且不超过 72 个字符")
        labels.add(label)
        x, y, detail, x_label = _chart_values(entry, f"{chart_id}.{identifier}", point_probes, line_samples)
        wrong_selector = "time" if detail["source_kind"] == "pointProbes" else "probe"
        if wrong_selector in entry:
            raise PostprocessError(f"{chart_id}.{identifier}: 此类数据源不支持 {wrong_selector} 选择")
        actual_x_unit = "s" if detail["source_kind"] == "pointProbes" else "m"
        if detail["x_unit"] != actual_x_unit or spec.get("xUnit", actual_x_unit) != actual_x_unit:
            raise PostprocessError(f"{chart_id}.{identifier}: 横轴单位必须使用源数据的 {actual_x_unit}")
        limits = _chart_band(entry, y, detail, f"{chart_id}.{identifier}") if "band" in entry else None
        detail.update(id=identifier, label=label, color=color, marker=marker, line_style=style, samples=len(y))
        series.append((detail, x, y, limits, entry.get("xLabel", spec.get("xLabel", x_label)),
                       entry.get("yLabel", spec.get("yLabel", detail["field"]))))
        for index, (x_value, y_value) in enumerate(zip(x, y)):
            csv_rows.append({"series": identifier, "source_kind": detail["source_kind"], "source": detail["source"],
                "field": detail["field"], "component": detail["component"], "probe_index": detail.get("probe_index"),
                "time": detail.get("time"), "x": float(x_value), "x_unit": detail["x_unit"], "y": float(y_value),
                "y_unit": detail["y_unit"], "lower": float(limits[0][index]) if limits is not None else None,
                "upper": float(limits[1][index]) if limits is not None else None,
                "band_source": detail.get("band", {}).get("source")})
    units = {(detail["x_unit"], detail["y_unit"]) for detail, *_ in series}
    if layout == "overlay" and len(units) != 1:
        raise PostprocessError(f"{chart_id}: 叠加曲线的横纵轴单位必须一致，请使用 panels")

    title = str(spec.get("title", chart_id))
    footer = "\n".join(textwrap.fill(line,width=100,break_on_hyphens=False) for line in (
        f"case={provenance['case_id']} job={provenance['job_id']} run={provenance['run_dir']}",
        f"caseDict={provenance.get('sha256', '')}", f"data={chart_id}.plot.csv"))
    legend_columns = 1 if any(len(label) > 28 for label in labels) else 2
    figure, axes = plt.subplots(rows, columns, squeeze=False, figsize=(6.8, 4 if layout == "overlay" else 2*rows+1.7))
    try:
        height = figure.get_figheight()
        footer_top = .025 + (footer.count("\n") + 1) * 8 / (72 * height)
        figure.suptitle(title, fontsize=13)
        handles = []
        for index, (detail, x, y, limits, x_label, y_label) in enumerate(series):
            axis = axes.flat[index if layout == "panels" else 0]
            if limits is not None:
                axis.fill_between(x, *limits, color=detail["color"], alpha=.18)
            line, = axis.plot(x, y, color=detail["color"], marker=detail["marker"], linestyle=detail["line_style"],
                              lw=1.6, markersize=3, markevery=max(1, len(x)//12), label=detail["label"], zorder=3)
            handles.append(line)
            axis.grid(True, color="#d9d9d9", linewidth=.6)
            if layout == "panels":
                axis.set_title(detail["label"], fontsize=10)
                axis.set_xlabel(f"{x_label} [{detail['x_unit']}]", fontsize=9)
                axis.set_ylabel(f"{y_label} [{detail['y_unit']}]", fontsize=9)
        if layout == "overlay":
            x_unit, y_unit = next(iter(units))
            _, _, _, _, x_label, y_label = series[0]
            fields = {detail["field"] for detail, *_ in series}
            axis.set_xlabel(f"{spec.get('xLabel', x_label)} [{x_unit}]")
            axis.set_ylabel(f"{spec.get('yLabel', y_label if len(fields) == 1 else 'Value')} [{y_unit}]")
            legend_bottom = footer_top + .015
            figure.legend(handles=handles, loc="lower center", bbox_to_anchor=(.5, legend_bottom), ncol=legend_columns,
                          frameon=False, fontsize=8)
            figure.subplots_adjust(left=.15, right=.96, top=.87,
                                   bottom=legend_bottom + .15 + .043*(math.ceil(len(series)/legend_columns)-1))
        else:
            for axis in list(axes.flat)[len(series):]:
                axis.set_visible(False)
            figure.subplots_adjust(left=.13, right=.95, top=.85, bottom=footer_top + .6/height, hspace=.78, wspace=.44)
        figure.text(.07, .025, footer, fontsize=6, color="#555555")
        files = _chart_files(figure, chart_id, dpi, formats, title, footer)
    finally:
        plt.close(figure)
    csv_name = f"{chart_id}.plot.csv"
    files[csv_name] = _csv_bytes(list(csv_rows[0]), csv_rows)
    record = {"id": chart_id, "title": title, "backend": "Agg", "dpi": dpi, "layout": layout,
              "series": [detail for detail, *_ in series], "artifacts": list(files), "data": csv_name,
              "provenance": {"case_dict_sha256": provenance.get("sha256", ""),
                             "case_dict_revision": provenance.get("dict_revision")}}
    return record, files


def _parse_reference_field(text: str) -> np.ndarray:
    number = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"

    def values(payload: str) -> list[float]:
        return [float(value) for value in re.findall(number, payload)]

    uniform = re.search(r"internalField\s+uniform\s+([^;]+);", text, re.DOTALL)
    if uniform:
        data = values(uniform.group(1))
        if not data:
            raise PostprocessError("参考场 uniform 值为空")
        return np.asarray([data], dtype=float)
    scalar = re.search(r"internalField\s+nonuniform\s+List<scalar>\s+(\d+)\s*\((.*?)\)\s*;",
                       text, re.DOTALL)
    if scalar:
        data = values(scalar.group(2))
        if len(data) != int(scalar.group(1)):
            raise PostprocessError("参考场 List<scalar> 数量不匹配")
        return np.asarray(data, dtype=float).reshape(-1, 1)
    vector = re.search(r"internalField\s+nonuniform\s+List<vector>\s+(\d+)\s*\((.*?)\)\s*;",
                       text, re.DOTALL)
    if vector:
        rows = [values(payload) for payload in re.findall(r"\(([^()]*)\)", vector.group(2))]
        if len(rows) != int(vector.group(1)) or not rows or any(len(row) != 3 for row in rows):
            raise PostprocessError("参考场 List<vector> 数量或分量不匹配")
        return np.asarray(rows, dtype=float)
    raise PostprocessError("仅支持 internalField uniform 或 nonuniform List<scalar/vector> 参考场")


def evaluate_reference_fields(specs: list, reader: FoamReader, fields: dict, case_root: Path):
    comparisons, input_paths = [], set()
    for spec in specs:
        item_id = _id(spec.get("id"), "reference field id")
        field = spec.get("field")
        path_text = _safe_rel(str(spec.get("path")))
        reference_path = _inside_case(case_root, case_root / path_text)
        kind = spec.get("kind")
        source = str(spec.get("source", "")).strip()
        mode = spec.get("mode", "index")
        unit = _field_unit(fields, field)
        reference = {"type": "field", "path": path_text, "kind": kind, "source": source,
                     "unit": unit, "l2_max": spec.get("l2_max", 0)}
        reference_grid = None
        for time_value in reader.select_times(spec, default_all=False):
            name = f"{item_id}.{field}@{_time_label(time_value)}"
            comparison = {"metric": name, "reference": reference}
            try:
                if kind not in ("verification", "validation", "regression") or not source:
                    raise PostprocessError("参考场需要 kind 和非空 source")
                target = reader.grid(time_value)
                data, weights = _cell_values_and_weights(reader, target, field, False)
                if mode == "cellCenters":
                    reference_field = spec.get("referenceField", field)
                    association = spec.get("referenceAssociation")
                    if unit == "unknown" or spec.get("referenceUnit") != unit:
                        raise PostprocessError("VTU 参考场单位必须与目标场声明单位一致")
                    if not isinstance(reference_field, str) or not reference_field.strip():
                        raise PostprocessError("VTU referenceField 必须是有效场名")
                    if reference_grid is None:
                        reference_grid = load_grid(reference_path, reference_field, association)
                    mapping = {"method": mode, "field": reference_field, "association": association,
                               "source_cells": reference_grid.GetNumberOfCells()}
                    comparison.update(reference={**reference, "mapping": mapping}, reference_id=item_id)
                    expected = sample(reader.vtk, reference_grid, target, reference_field)
                elif mode == "index":
                    expected = _parse_reference_field(reference_path.read_text(encoding="utf-8"))
                    if expected.shape[0] == 1 and data.shape[0] > 1:
                        expected = np.broadcast_to(expected, (data.shape[0], expected.shape[1]))
                else:
                    raise PostprocessError("参考场 mode 必须为 index 或 cellCenters")
                input_paths.add(reference_path)
                if expected.shape != data.shape:
                    raise PostprocessError(f"参考场形状不匹配: {expected.shape} vs {data.shape}")
                if not np.all(np.isfinite(expected)) or not np.all(np.isfinite(data)):
                    raise PostprocessError("参考场和实际场必须为有限数值")
                tolerance = _number(spec.get("l2_max", 0), "l2_max")
                if tolerance < 0:
                    raise PostprocessError("l2_max 必须 >=0")
                errors = data - expected
                volumes = weights[:, None]
                numerator = float(np.sqrt(np.sum(volumes * errors ** 2)))
                denominator = float(np.sqrt(np.sum(volumes * expected ** 2)))
                if not math.isfinite(numerator) or not math.isfinite(denominator):
                    raise PostprocessError("参考场 L2 超出有限数值范围")
                relative = numerator / denominator if denominator > 0 else (0.0 if numerator == 0 else None)
                comparison.update({"l2_absolute": numerator, "l2_relative": relative,
                                   "max_absolute_error": float(np.max(np.abs(errors))),
                                   "cells": len(weights), "status": "pass" if relative is not None and relative <= tolerance else "fail"})
                if comparison["status"] == "fail":
                    comparison["blocker"] = f"参考场 L2 超过容差 {tolerance:g}"
            except (OSError, PostprocessError, ResultReaderError, UnicodeError) as error:
                comparison.update({"status": "invalid_reference", "blocker": str(error)})
            comparisons.append(comparison)
    return comparisons, input_paths


def _array_formula_call(name, values):
    if name in ("min", "max"):
        if len(values) < 2:
            raise PostprocessError("数组 min/max 需要至少两个逐元素输入")
        compare = np.minimum if name == "min" else np.maximum
        result = values[0]
        for value in values[1:]:
            result = compare(result, value)
        return result
    if name == "log" and len(values) == 2:
        return np.log(values[0]) / np.log(values[1])
    if len(values) != 1:
        raise PostprocessError(f"数组 {name} 输入数量不正确")
    functions = {"sqrt": np.sqrt, "abs": np.abs, "log": np.log, "exp": np.exp}
    return functions[name](values[0])


def evaluate_formula(expression: str, inputs: dict, *, arrays=False, sensitivities=False):
    """小型 AST 解释器；绝不 eval/exec，也不允许属性、下标或任意调用。"""
    if not isinstance(expression, str) or not expression.strip() or len(expression) > 500:
        raise PostprocessError("公式须为 1–500 字符")
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise PostprocessError(f"公式语法错误: {error.msg}") from error
    if len(list(ast.walk(tree))) > 100:
        raise PostprocessError("公式节点超过 100")
    binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod}
    unary = {ast.UAdd: operator.pos, ast.USub: operator.neg}
    functions = {"sqrt": math.sqrt, "abs": abs, "min": min, "max": max,
                 "log": math.log, "exp": math.exp}
    values = {}

    def visit(node):
        value = read_value(node)
        if sensitivities:
            values[node] = value
        return value

    def read_value(node):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise PostprocessError("公式只允许数值常量")
            return _number(node.value, "公式常量")
        if isinstance(node, ast.Name):
            if node.id not in inputs:
                raise PostprocessError(f"公式输入未定义: {node.id}")
            value = inputs[node.id]
            return value if arrays and isinstance(value, np.ndarray) else _number(value, f"公式输入 {node.id}")
        if isinstance(node, ast.BinOp) and type(node.op) in binary:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and np.any(np.abs(right) > 12):
                raise PostprocessError("公式指数绝对值不得超过 12")
            return binary[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in unary:
            return unary[type(node.op)](visit(node.operand))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions:
            if node.keywords or not 1 <= len(node.args) <= 16:
                raise PostprocessError("公式函数不接受关键字参数，参数数须为 1–16")
            arguments = [visit(arg) for arg in node.args]
            if arrays:
                return _array_formula_call(node.func.id, arguments)
            return functions[node.func.id](*arguments)
        raise PostprocessError(f"公式含禁止语法: {type(node).__name__}")

    try:
        if not arrays:
            value = _number(visit(tree.body), "公式结果")
            if sensitivities:
                return _formula_gradient(tree.body, values, inputs)
            return value
        with np.errstate(divide="raise", invalid="raise", over="raise"):
            result = np.asarray(visit(tree.body), dtype=float)
        if not np.all(np.isfinite(result)):
            raise PostprocessError("公式结果必须有限")
    except (ArithmeticError, ValueError, TypeError) as error:
        raise PostprocessError(f"公式计算失败: {error}") from error
    return result


def _formula_sample(binding, samples):
    if set(binding) - {"source", "field", "component", "unit"} or not isinstance(binding.get("field"), str):
        raise PostprocessError("采样公式输入需要 source、field，并可指定 component、unit")
    source = _id(binding.get("source"), "公式采样来源")
    matches = [record for record in samples if record["id"] == source]
    if len(matches) != 1:
        raise PostprocessError(f"公式采样来源不存在或不唯一: {source}")
    sample = matches[0]
    series = sample["series"].get(binding["field"])
    if series is None:
        raise PostprocessError(f"公式采样字段不存在: {source}.{binding['field']}")
    unit = series["unit"]
    if unit in ("", "unknown") or ("unit" in binding and binding["unit"] != unit):
        raise PostprocessError(f"公式采样单位不一致或未声明: {source}.{binding['field']}")
    raw_component = binding.get("component", 0)
    component = _number(raw_component, "采样分量")
    if isinstance(raw_component, bool) or int(component) != component or not 0 <= component < series["components"]:
        raise PostprocessError(f"公式采样分量超出范围: {source}.{binding['field']}")
    values = np.asarray(series["values"], dtype=float)
    if values.ndim == 3:
        values = values[..., int(component)]
    if sample["layout"] == "sample,time[,component]":
        values = values.T
    sampling = {"layout": "time,point", "times": sample["times"], "points": sample["points"]}
    return values, sampling


def evaluate_formulas(specs: list, metrics: list, *, samples=()):
    available = {metric["name"]: metric for metric in metrics}
    output = []
    for spec in specs:
        formula_id = _id(spec.get("id"), "formula id")
        aliases = spec.get("inputs") or {}
        if not isinstance(aliases, dict):
            raise PostprocessError(f"{formula_id}.inputs 必须是字典")
        values, sampling = {}, None
        for alias, binding in aliases.items():
            if isinstance(binding, dict):
                value, axes = _formula_sample(binding, samples)
            else:
                previous = available.get(binding) if isinstance(binding, str) else None
                if previous is None:
                    raise PostprocessError(f"{formula_id}: 输入指标不存在 {alias} -> {binding}")
                value, axes = previous["value"], previous.get("sampling")
            if axes is not None:
                if sampling is not None and axes != sampling:
                    raise PostprocessError(f"{formula_id}: 输入数组的时刻或采样坐标不一致")
                sampling = axes
                value = np.asarray(value, dtype=float)
                if (value.shape != (len(axes["times"]), len(axes["points"])) or
                        not value.size or not np.all(np.isfinite(value))):
                    raise PostprocessError(f"{formula_id}: 采样数组的形状或数值无效")
            values[alias] = value
        value = evaluate_formula(spec.get("expression"), values, arrays=sampling is not None)
        metric = {"name": f"{formula_id}.value", "operation": "formulas", "value": value,
                  "unit": str(spec.get("unit") or "unknown"),
                  "expression": spec.get("expression"), "inputs": aliases}
        if sampling is not None:
            shape = (len(sampling["times"]), len(sampling["points"]))
            metric["value"] = np.broadcast_to(value, shape).tolist()
            metric["sampling"] = sampling
        output.append(metric)
        available[metric["name"]] = metric
    return output


def _formula_gradient(root, values, inputs):
    """Reuse scalar AST values and propagate analytical first derivatives."""
    zero = np.zeros(len(inputs))
    seeds = dict(zip(inputs, np.eye(len(inputs))))

    def variable(node):
        return any(isinstance(child, ast.Name) and child.id in seeds for child in ast.walk(node))

    def undefined(operation):
        raise PostprocessError(f"公式在 {operation} 处无可确认的局部灵敏度")

    def binary(node):
        left, right = values[node.left], values[node.right]
        partial_left, partial_right = derivative(node.left), derivative(node.right)
        operation = type(node.op)
        if operation is ast.Add:
            return partial_left + partial_right
        if operation is ast.Sub:
            return partial_left - partial_right
        if operation is ast.Mult:
            return right * partial_left + left * partial_right
        if operation is ast.Div:
            return (partial_left - values[node] * partial_right) / right
        if operation is ast.Mod:
            quotient, remainder = divmod(left, right)
            if remainder == 0 and variable(node):
                undefined("%")
            return partial_left - quotient * partial_right

        if left < 0 and variable(node.right):
            undefined("幂指数")
        if left == 0 and variable(node.left) and (right <= 0 or not float(right).is_integer() or variable(node.right)):
            undefined("幂底数")
        if left == 0:
            if right == 0 and variable(node.right):
                undefined("幂指数")
            return partial_left if right == 1 else zero
        out = right * (left ** (right - 1)) * partial_left if right and np.any(partial_left) else zero
        if left > 0 and np.any(partial_right):
            out = out + values[node] * math.log(left) * partial_right
        return out

    def function(node):
        name = node.func.id
        if name in ("min", "max"):
            active = [arg for arg in node.args if values[arg] == values[node]]
            gradient = derivative(active[0])
            if any(not np.array_equal(gradient, derivative(arg)) for arg in active[1:]):
                undefined(name)
            return gradient
        arg = node.args[0]
        value = values[arg]
        gradient = derivative(arg)
        if name == "abs":
            if value == 0 and np.any(gradient):
                undefined(name)
            return np.sign(value) * gradient
        if name == "sqrt":
            if value == 0:
                if variable(arg):
                    undefined(name)
                return zero
            return gradient / (2 * values[node])
        if name == "exp":
            return values[node] * gradient
        gradient = gradient / value
        if len(node.args) == 1:
            return gradient
        base = node.args[1]
        base_value = values[base]
        return (gradient - values[node] * (derivative(base) / base_value)) / math.log(base_value)

    def derivative(node):
        if isinstance(node, ast.Name):
            return seeds[node.id]
        if isinstance(node, ast.Constant):
            return zero
        if isinstance(node, ast.UnaryOp):
            gradient = derivative(node.operand)
            return -gradient if isinstance(node.op, ast.USub) else gradient
        if isinstance(node, ast.BinOp):
            return binary(node)
        return function(node)

    gradient = derivative(root)
    if not np.all(np.isfinite(gradient)):
        raise PostprocessError("局部灵敏度必须有限")
    return gradient


def _propagation_covariance(names, standards, declaration, default_source):
    product = np.outer(standards, standards)
    if declaration is None:
        matrix = np.diag(np.diag(product))
        return matrix, np.eye(len(names)), default_source, "independent"
    if not isinstance(declaration, dict) or set(declaration) != {"source", "rows"}:
        raise PostprocessError("covariance 需要来源和按公式输入命名的 rows")
    source = declaration["source"]
    rows = declaration["rows"]
    if not isinstance(source, str) or not source.strip():
        raise PostprocessError("covariance 必须声明来源")
    if (not isinstance(rows, dict) or set(rows) != set(names) or
            any(not isinstance(row, dict) or set(row) != set(names) for row in rows.values())):
        raise PostprocessError("covariance 的行和列必须与公式输入一致")
    matrix = np.array([[_number(rows[left][right], "covariance") for right in names] for left in names])
    if not np.array_equal(matrix, matrix.T):
        raise PostprocessError("covariance 必须对称")
    if not np.allclose(np.diag(matrix), np.diag(product), rtol=1e-12, atol=0):
        raise PostprocessError("covariance 对角必须等于相应标准不确定度的平方")
    nonzero = product != 0
    if np.any(matrix[~nonzero]):
        raise PostprocessError("零方差输入不能有非零协方差")
    normalized = np.divide(matrix, product, out=np.zeros_like(matrix), where=nonzero)
    tolerance = 16 * np.finfo(float).eps * len(names)
    if np.any(np.abs(normalized) > 1 + tolerance) or np.linalg.eigvalsh(normalized)[0] < -tolerance:
        raise PostprocessError("covariance 必须为正半定矩阵")
    return matrix, normalized, source, "declared"


def _propagated_uncertainty(spec, metric, metrics, coverage):
    bindings = metric.get("inputs")
    required = spec.get("inputs")
    if (metric.get("operation") != "formulas" or metric.get("sampling") is not None or
            not isinstance(bindings, dict) or not bindings or not isinstance(required, dict) or
            set(required) != set(bindings) or metric.get("unit") in (None, "", "unknown")):
        raise PostprocessError("传播需要有单位的标量公式和全部公式输入的不确定度")
    if not isinstance(spec.get("source"), str) or not spec["source"].strip():
        raise PostprocessError("传播必须声明不确定度来源")
    if "covariance" in spec and spec["covariance"] is None:
        raise PostprocessError("显式 covariance 需要来源和按公式输入命名的 rows")
    inputs, standards, report = {}, [], {}
    for alias, name in bindings.items():
        operand = metrics.get(name) if isinstance(name, str) else None
        stated = required[alias]
        if (operand is None or operand.get("sampling") is not None or not isinstance(stated, dict) or
                set(stated) != {"unit", "standard"}):
            raise PostprocessError(f"{alias}: 传播需要标量输入、单位和标准不确定度")
        unit = operand.get("unit")
        if unit in (None, "", "unknown") or stated["unit"] != unit:
            raise PostprocessError(f"{alias}: 不确定度输入单位与实际指标不一致")
        standard = _number(stated["standard"], f"{alias} 标准不确定度")
        if standard < 0:
            raise PostprocessError(f"{alias}: 标准不确定度须 >=0")
        inputs[alias] = _number(operand["value"], f"{alias} 指标")
        standards.append(standard)
        report[alias] = {"metric": name, "unit": unit, "standard": standard}
    names = list(inputs)
    try:
        with np.errstate(divide="raise", invalid="raise", over="raise"):
            matrix, normalized, covariance_source, dependence = _propagation_covariance(
                names, standards, spec.get("covariance"), spec["source"])
            gradients = evaluate_formula(metric["expression"], inputs, sensitivities=True)
            weighted = gradients * standards
            variance = _number(weighted @ normalized @ weighted, "合成方差")
            if variance < 0 and abs(variance) > 16 * np.finfo(float).eps * len(names) * (weighted @ weighted):
                raise PostprocessError("合成方差必须非负")
            standard = math.sqrt(max(0.0, variance))
            expanded = _number(standard * coverage, "扩展不确定度")
    except (ArithmeticError, np.linalg.LinAlgError) as error:
        raise PostprocessError("相关不确定度计算范围无效") from error
    for alias, gradient in zip(names, gradients):
        report[alias]["sensitivity"] = float(gradient)
    return {"status": "pass", "method": "first_order_covariance", "unit": metric["unit"],
            "standard": standard, "coverage": coverage, "expanded": expanded, "inputs": report,
            "covariance": {"source": covariance_source, "dependence": dependence,
                           "order": names, "rows": matrix.tolist()}}


def evaluate_uncertainty(specs: list, metrics: list):
    by_name = {metric["name"]: metric for metric in metrics}
    records = []
    for spec in specs:
        item_id = _id(spec.get("id"), "uncertainty id")
        metric_name = spec.get("metric")
        propagated = "inputs" in spec or "covariance" in spec
        record = {"id": item_id, "metric": metric_name, "source": str(spec.get("source", "")),
                  "method": "first_order_covariance" if propagated else "expanded=standard*coverage"}
        metric = by_name.get(metric_name)
        try:
            if metric is None:
                raise PostprocessError("不确定度关联指标不存在")
            if not record["source"].strip():
                raise PostprocessError("不确定度必须声明来源")
            coverage = _number(spec.get("coverage", 2), "coverage factor")
            if propagated:
                if coverage <= 0:
                    raise PostprocessError("coverage 须 >0")
                record.update(_propagated_uncertainty(spec, metric, by_name, coverage))
            else:
                standard = _number(spec.get("standard"), "standard uncertainty")
                if standard < 0 or coverage <= 0:
                    raise PostprocessError("standard 须 >=0，coverage 须 >0")
                record.update({"status": "pass", "standard": standard, "coverage": coverage,
                               "expanded": standard * coverage, "unit": metric["unit"]})
        except PostprocessError as error:
            record.update({"status": "invalid", "blocker": str(error)})
        records.append(record)
    return records


def merge_artifacts(*sources: dict[str, bytes]) -> dict[str, bytes]:
    merged = {}
    for source in sources:
        for name, data in source.items():
            if name == "artifact-manifest.json" or name in merged:
                raise PostprocessError(f"后处理产物路径冲突: {name}")
            merged[name] = data
    return merged


def validate_metric_names(metrics: list):
    names = [metric["name"] for metric in metrics]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise PostprocessError(f"指标名称重复: {', '.join(duplicates)}")


def compare_numeric(actual, reference):
    actual_array = np.asarray(actual, dtype=float)
    reference_array = np.asarray(reference, dtype=float)
    if reference_array.size == 1 and actual_array.size > 1:
        reference_array = np.full(actual_array.shape, float(reference_array.reshape(-1)[0]))
    if actual_array.shape != reference_array.shape:
        raise PostprocessError(f"参考值形状不匹配: {actual_array.shape} vs {reference_array.shape}")
    if not np.all(np.isfinite(actual_array)) or not np.all(np.isfinite(reference_array)):
        raise PostprocessError("指标和参考值必须为有限数值")
    error = actual_array - reference_array
    denominator = float(np.sqrt(np.sum(reference_array ** 2)))
    numerator = float(np.sqrt(np.sum(error ** 2)))
    relative = numerator / denominator if denominator > 0 else (0.0 if numerator == 0 else None)
    return {"l2_absolute": numerator, "l2_relative": relative,
            "max_absolute_error": float(np.max(np.abs(error)))}


def _series_comparison(case_root, metric, reference):
    if not isinstance(reference.get('source'), str) or not reference['source'].strip():
        raise PostprocessError('时序参考需要来源')
    if 'series' not in reference or 'value' in reference or 'previousRun' in reference:
        raise PostprocessError('时序参考需要独立的 series 配置')
    kind = reference.get('kind')
    use_candidate = 'candidateSeries' in reference
    if kind not in ('verification', 'validation', 'regression'):
        raise PostprocessError('kind 须为 verification/validation/regression')
    if use_candidate and kind != 'regression':
        raise PostprocessError('两个外部序列的对齐只能作为 regression 证据')

    def open_path(path):
        return _inside_case(case_root, case_root / path)

    observed, observed_meta = load_case_series(reference['series'], _safe_rel, open_path)
    if 'unit' in reference and reference['unit'] != observed_meta['unit']:
        raise PostprocessError('公开的参考单位与 CSV 声明不一致')
    if use_candidate:
        actual, actual_meta = load_case_series(reference['candidateSeries'], _safe_rel, open_path)
    else:
        settings = ({**reference, 'point': _point(_list_value(reference['point']))}
                    if 'point' in reference else reference)
        actual, actual_meta = series_for_metric(metric, settings)
    actual_values, observed_values, alignment = align(actual, actual_meta, observed, observed_meta)
    return _aligned_comparison(actual_values, observed_values, alignment, reference)


def _profile_comparison(case_root, metric, reference):
    if (reference.get('kind') != 'validation' or not isinstance(reference.get('source'), str)
            or not reference['source'].strip() or {'value', 'previousRun', 'series', 'candidateSeries'} & reference.keys()):
        raise PostprocessError('实验剖面需要独立的 validation 参考与来源')
    if not metric or reference.get('unit') != metric.get('unit') or metric.get('unit') in (None, '', 'unknown'):
        raise PostprocessError('实验剖面的指标缺失或单位不一致')

    def open_path(path):
        return _inside_case(case_root, case_root / path)

    actual, observed, alignment = profile_at_probes(reference['profile'], metric, _safe_rel, open_path)
    return _aligned_comparison(actual, observed, alignment, reference)


def _aligned_comparison(actual_values, observed_values, alignment, reference):
    result = compare_numeric(actual_values, observed_values)
    tolerance = _number(reference.get('l2_max', 0), 'l2_max')
    if tolerance < 0:
        raise PostprocessError('l2_max 必须非负')
    status = 'pass' if result['l2_relative'] is not None and result['l2_relative'] <= tolerance else 'fail'
    result.update({'status': status, 'alignment': alignment,
                   'rmse': result['l2_absolute'] / math.sqrt(alignment['samples'])})
    if status == 'fail':
        result['blocker'] = f'L2 超过容差 {tolerance:g}'
    return result


def build_comparisons(metrics: list, references: dict, *, case_root=None):
    by_name = {metric["name"]: metric for metric in metrics}
    comparisons = []
    for name, reference in references.items():
        metric = by_name.get(name)
        comparison = {"metric": name, "reference": reference,
                      "reference_id": reference.get("id") if isinstance(reference, dict) else None}
        if isinstance(reference, dict) and ('profile' in reference or 'series' in reference or 'candidateSeries' in reference):
            compare = _profile_comparison if 'profile' in reference else _series_comparison
            try:
                comparison.update(compare(case_root, metric, reference))
            except (OSError, ValueError, TypeError, OverflowError, csv.Error) as error:
                comparison.update({'status': 'invalid_reference', 'blocker': str(error)})
        elif metric is None:
            comparison.update({"status": "missing_metric", "blocker": "指标不存在"})
        elif not isinstance(reference, dict) or "value" not in reference:
            comparison.update({"status": "invalid_reference", "blocker": "缺 reference value"})
        elif not str(reference.get("source", "")).strip():
            comparison.update({"status": "missing_provenance", "blocker": "缺参考数据来源"})
        elif reference.get("kind") not in ("verification", "validation", "regression"):
            comparison.update({"status": "invalid_reference", "blocker": "kind 须为 verification/validation/regression"})
        elif reference.get("unit") != metric.get("unit"):
            comparison.update({"status": "unit_mismatch", "blocker": "参考值与指标单位不一致"})
        elif "previousRun" in reference and reference.get("sampling") != metric.get("sampling"):
            comparison.update({"status": "sampling_mismatch", "blocker": "前次指标的时刻或采样坐标不一致"})
        else:
            try:
                comparison.update(compare_numeric(metric["value"], reference["value"]))
                tolerance = _number(reference.get("l2_max", 0), "l2_max")
                comparison["status"] = ("pass" if comparison["l2_relative"] is not None and
                                        comparison["l2_relative"] <= tolerance else "fail")
                if comparison["status"] == "fail":
                    comparison["blocker"] = f"L2 超过容差 {tolerance:g}"
            except (PostprocessError, TypeError, ValueError) as error:
                comparison.update({"status": "invalid_reference", "blocker": str(error)})
        comparisons.append(comparison)
    return comparisons


def apply_comparison_targets(comparisons: list, specs: list):
    by_metric = {comparison["metric"]: comparison for comparison in comparisons}
    expected_kinds = {"experiment": "validation", "benchmark": "verification",
                      "previousRun": "regression", "requirement": "regression"}
    targets = []
    for spec in specs:
        target_id = _id(spec.get("id"), "comparison target id")
        metric_name = spec.get("metric")
        reference_id = spec.get("reference")
        target_type = spec.get("type")
        target = {"id": target_id, "metric": metric_name, "reference_id": reference_id,
                  "type": target_type, "evidence_kind": expected_kinds.get(target_type)}
        comparison = by_metric.get(metric_name)
        if target_type not in expected_kinds:
            target.update({"status": "invalid_target", "blocker": "未知 comparisonTarget 类型"})
        elif comparison is None:
            target.update({"status": "missing_metric", "blocker": "目标指标或参考比较不存在"})
        elif reference_id and comparison.get("reference_id") != reference_id:
            target.update({"status": "reference_mismatch", "blocker": "目标 reference ID 不匹配"})
        elif (comparison["reference"].get("kind") if isinstance(comparison["reference"], dict) else None) != target["evidence_kind"]:
            target.update({"status": "kind_mismatch",
                           "blocker": f"{target_type} 需要 {target['evidence_kind']} 参考证据"})
        else:
            target["status"] = comparison["status"]
            for key in ("l2_absolute", "l2_relative", "max_absolute_error"):
                if key in comparison:
                    target[key] = comparison[key]
            if target["status"] != "pass":
                target["blocker"] = comparison.get("blocker", "目标比较未通过")
        targets.append(target)
    return targets


def build_readiness(fields: dict, used_fields: set[str], comparisons: list, extraction_count: int,
                    metrics: list | None = None, uncertainties: list | None = None,
                    comparison_targets: list | None = None):
    blockers = [f"场 {name} 未声明单位" for name in sorted(used_fields)
                if _field_unit(fields, name) == "unknown"]
    blockers.extend(f"指标 {metric['name']} 未声明单位" for metric in metrics or []
                    if metric.get("unit") in (None, "", "unknown"))
    blockers.extend(c.get("blocker", f"参考比较 {c['metric']} 未通过")
                    for c in comparisons if c["status"] != "pass")
    blockers.extend(item.get("blocker", f"不确定度 {item['id']} 无效")
                    for item in uncertainties or [] if item["status"] != "pass")
    blockers.extend(item.get("blocker", f"比较目标 {item['id']} 未通过")
                    for item in comparison_targets or [] if item["status"] != "pass")
    if not extraction_count:
        blockers.append("没有后处理提取结果")
    verification = [c for c in comparisons if isinstance(c["reference"], dict) and
                    c["reference"].get("kind") == "verification"]
    validation = [c for c in comparisons if isinstance(c["reference"], dict) and
                  c["reference"].get("kind") == "validation"]
    can_verify = bool(verification) and all(c["status"] == "pass" for c in verification)
    can_validate = bool(validation) and all(c["status"] == "pass" for c in validation)
    return {"report_ready": not blockers, "blockers": blockers,
            "can_claim_verification": can_verify, "can_claim_validation": can_validate,
            "evidence_level": "validation" if can_validate else "verification" if can_verify else "regression"}


def _input_record(case_root: Path, path: Path):
    data = path.read_bytes()
    return {"path": path.relative_to(case_root).as_posix(), "size": len(data), "sha256": _sha256(data)}


def evaluate(case_root: Path, case_id: int, job_id: int, config: dict, *, source=None, generated_at=None):
    """Compute deterministic metrics and artifacts without publishing any files."""
    run_dir = _safe_rel(config["run_dir"])
    run_abs = _inside_case(case_root, case_root / run_dir)
    source = config.get("_source", {}) if source is None else source
    fields = config.get("fields") or {}
    reader = FoamReader(run_abs)
    references = config.get("referenceValues") or {}
    if not isinstance(references, dict):
        raise PostprocessError("referenceValues 必须是对象")

    point_probes, probe_artifacts = extract_point_probes(reader, config.get("pointProbes") or [], fields)
    line_samples, line_artifacts = extract_line_samples(reader, config.get("lineSamples") or [], fields)
    integrals, integral_metrics = extract_integrals(
        reader, config.get("surfaceIntegrals") or [], config.get("volumeAverages") or [], fields)
    spectra, spectrum_metrics, spectrum_artifacts = extract_frequency_spectra(
        config.get("frequencySpectra") or [], point_probes)
    modes, mode_metrics, mode_artifacts = extract_modal_quantities(
        config.get("modalQuantities") or [], point_probes, reader=reader, fields=fields)
    charts, chart_artifacts = render_charts(
        config.get("plots") or [], point_probes, line_samples,
        {**source, "case_id": case_id, "job_id": job_id, "run_dir": run_dir})
    metrics = integral_metrics + spectrum_metrics + mode_metrics
    metrics.extend(evaluate_formulas(config.get("formulas") or [], metrics, samples=(*point_probes, *line_samples)))
    uncertainties = evaluate_uncertainty(config.get("uncertainty") or [], metrics)
    validate_metric_names(metrics)
    reference_comparisons, reference_input_paths = evaluate_reference_fields(
        config.get("referenceFields") or [], reader, fields, case_root)
    comparisons = build_comparisons(metrics, references, case_root=case_root) + reference_comparisons
    comparison_targets = apply_comparison_targets(comparisons, config.get("comparisonTargets") or [])
    used_fields = set()
    for specs in (config.get("pointProbes") or [], config.get("lineSamples") or []):
        for spec in specs:
            used_fields.update(spec.get("fields") or [])
    for specs in (config.get("surfaceIntegrals") or [], config.get("volumeAverages") or [],
                  config.get("frequencySpectra") or [], config.get("modalQuantities") or []):
        for spec in specs:
            if spec.get("field"):
                used_fields.add(spec["field"])
    extraction_count = (len(point_probes) + len(line_samples) + len(integrals["surfaceIntegrals"]) +
                        len(integrals["volumeAverages"]) + len(spectra) + len(modes))
    readiness = build_readiness(fields, used_fields, comparisons, extraction_count,
                                metrics, uncertainties, comparison_targets)
    metrics_doc = {
        "schema": "vibeflow.postprocess.metrics.v1", "case_id": case_id, "job_id": job_id,
        "run_dir": run_dir, "times": reader.times, "source": source,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "extractions": {"pointProbes": point_probes, "lineSamples": line_samples,
                        **integrals, "frequencySpectra": spectra, "modalQuantities": modes,
                        "charts": charts, "uncertainty": uncertainties,
                        "comparisonTargets": comparison_targets},
        "metrics": metrics, "comparisons": comparisons, "readiness": readiness,
    }
    artifacts = merge_artifacts(
        {"metrics.json": _json_bytes(metrics_doc), "run-config.json": _json_bytes(config)},
        probe_artifacts, line_artifacts, spectrum_artifacts, mode_artifacts, chart_artifacts,
    )
    return metrics_doc, artifacts


def execute(case_root: Path, case_id: int, job_id: int, config_path: str, run_dir_override: str | None = None):
    server_root = str(Path(__file__).resolve().parents[2])
    if server_root not in sys.path:
        sys.path.insert(0, server_root)
    from app.services.case_evidence import case_input_fingerprint, file_record, run_inventory
    case_root = case_root.resolve()
    config_path = _safe_rel(config_path)
    config_file = _inside_case(case_root, case_root / config_path)
    if not config_file.is_file():
        raise PostprocessError(f"配置不存在: {config_path}")
    config_bytes = config_file.read_bytes()
    try:
        config = json.loads(config_bytes)
    except json.JSONDecodeError as error:
        raise PostprocessError(f"配置不是合法 JSON: {error}") from error
    if not isinstance(config, dict) or config.get("schema") != "vibeflow.postprocess.v1":
        raise PostprocessError("schema 须为 vibeflow.postprocess.v1")
    run_dir = _safe_rel(run_dir_override or config.get("run_dir") or "C-run/case-1")
    run_abs = _inside_case(case_root, case_root / run_dir)
    source = config.get("_source") or {}
    if not isinstance(source, dict):
        raise PostprocessError("_source 必须是对象")
    fields = config.get("fields") or {}
    if not isinstance(fields, dict):
        raise PostprocessError("fields 必须是对象")
    # Hash before VTK reads, then recheck before publication. Hashing after the
    # extraction could otherwise attest new bytes while metrics used old bytes.
    run_paths = run_inventory(case_root, run_dir)
    input_paths = set(run_paths) | {config_file}
    for relative in reference_input_paths(config):
        path = _inside_case(case_root, case_root / relative)
        if not path.is_file():
            raise PostprocessError(f"参考文件不存在: {relative}")
        input_paths.add(path)
    from app.services.postprocess_reference_inputs import bound_metrics_paths
    input_paths.update(_inside_case(case_root, case_root / path) for path in bound_metrics_paths(config))
    input_records = [file_record(case_root, path) for path in sorted(input_paths)]
    case_dict_path = case_root / "0-caseDict" / "caseDict"
    current_input = case_input_fingerprint(case_dict_path.read_text(encoding="utf-8"))
    if source.get("case_input_sha256") and source["case_input_sha256"] != current_input:
        raise PostprocessError("排队期间工程输入已变化，请重新提交后处理")
    source["case_input_sha256"] = current_input
    metrics_doc, artifacts = evaluate(case_root, case_id, job_id, config, source=source)
    readiness = metrics_doc["readiness"]
    result_dir = f"D-post/data/postprocess-{job_id}"
    manifest = {
        "schema": "vibeflow.postprocess.artifacts.v1", "case_id": case_id, "job_id": job_id,
        "created_at": datetime.now(timezone.utc).isoformat(), "run_dir": run_dir,
        "config_path": config_path, "config_sha256": _sha256(config_bytes), "source": source,
        "vtk_version": __import__("vtk").vtkVersion.GetVTKVersion(), "readiness": readiness,
        "inputs": [], "artifacts": [
            {"path": f"{result_dir}/{name}", "size": len(data), "sha256": _sha256(data)}
            for name, data in sorted(artifacts.items())
        ],
    }
    if run_inventory(case_root, run_dir) != run_paths:
        raise PostprocessError("后处理期间求解输入或结果目录已变化，未发布证据")
    if case_input_fingerprint(case_dict_path.read_text(encoding="utf-8")) != current_input:
        raise PostprocessError("后处理期间工程参数已变化，未发布证据")
    for record in input_records:
        path = case_root / record["path"]
        if not path.is_file() or file_record(case_root, path)["sha256"] != record["sha256"]:
            raise PostprocessError(f"后处理期间输入已变化，未发布证据: {record['path']}")
    manifest["inputs"] = input_records
    manifest["run_inventory"] = [path.relative_to(case_root).as_posix() for path in run_paths]
    artifacts["artifact-manifest.json"] = _json_bytes(manifest)

    output_dir = case_root / result_dir
    output_dir.mkdir(parents=True, exist_ok=False)
    for name, data in artifacts.items():
        (output_dir / name).write_bytes(data)
    latest = {"job_id": job_id, "result_dir": result_dir,
              "metrics_path": f"{result_dir}/metrics.json",
              "manifest_path": f"{result_dir}/artifact-manifest.json"}
    latest_path = case_root / "D-post" / "data" / "latest-postprocess.json"
    temporary = latest_path.with_name(f".latest-postprocess-{job_id}.tmp")
    temporary.write_bytes(_json_bytes(latest))
    os.replace(temporary, latest_path)
    return metrics_doc


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--case-id", type=int, required=True)
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--config", default="D-post/postprocess.json")
    parser.add_argument("--run-dir", default="")
    args = parser.parse_args()
    try:
        result = execute(Path(args.case).resolve(), args.case_id, args.job_id,
                         args.config, args.run_dir or None)
    except Exception as error:
        print(f"POSTPROCESS_ERROR: {error}", file=sys.stderr, flush=True)
        raise SystemExit(1) from error
    print(f"POSTPROCESS_OK job={args.job_id} metrics={len(result['metrics'])} "
          f"comparisons={len(result['comparisons'])} report_ready={result['readiness']['report_ready']}", flush=True)


if __name__ == "__main__":
    main()
