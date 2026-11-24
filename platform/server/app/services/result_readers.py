"""通用结果文件读取：VTK/XML/XDMF/Exodus/STL 与 ParaView .series。

只负责数据读取，不负责文件归属；路由必须先完成 case 内路径校验。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
import math
from pathlib import Path


class ResultReaderError(ValueError):
    pass


class ResultBlockUnavailable(ResultReaderError):
    pass


SUPPORTED_RESULT_EXTENSIONS = {
    ".vtk", ".vtu", ".vtp", ".vti", ".vtr", ".vts", ".vtm", ".xdmf", ".xmf",
    ".exo", ".e", ".exodus", ".stl", ".series",
}


@dataclass
class ResultData:
    dataset: object
    times: list[float]
    reader_kind: str
    source_path: str
    blocks: tuple[ResultBlock, ...] = ()


@dataclass(frozen=True)
class ResultBlock:
    key: str
    label: str


def _reader_for(path: Path, vtk):
    suffix = path.suffix.lower()
    factories = {
        ".vtk": vtk.vtkDataSetReader,
        ".vtu": vtk.vtkXMLUnstructuredGridReader,
        ".vtp": vtk.vtkXMLPolyDataReader,
        ".vti": vtk.vtkXMLImageDataReader,
        ".vtr": vtk.vtkXMLRectilinearGridReader,
        ".vts": vtk.vtkXMLStructuredGridReader,
        ".vtm": vtk.vtkXMLMultiBlockDataReader,
        ".xdmf": vtk.vtkXdmfReader,
        ".xmf": vtk.vtkXdmfReader,
        ".exo": vtk.vtkExodusIIReader,
        ".e": vtk.vtkExodusIIReader,
        ".exodus": vtk.vtkExodusIIReader,
        ".stl": vtk.vtkSTLReader,
    }
    factory = factories.get(suffix)
    if factory is None:
        raise ResultReaderError(f"不支持的结果格式: {suffix or '(无扩展名)'}")
    reader = factory()
    reader.SetFileName(str(path))
    return reader, suffix.lstrip(".")


def _copy_dataset(output, vtk):
    if output is None:
        raise ResultReaderError("读取器没有输出数据")
    if output.IsA("vtkCompositeDataSet"):
        geometry = vtk.vtkCompositeDataGeometryFilter()
        geometry.SetInputData(output)
        geometry.Update()
        output = geometry.GetOutput()
    if not output.IsA("vtkDataSet"):
        raise ResultReaderError("读取器没有输出可视化网格")
    dataset = output.NewInstance()
    dataset.DeepCopy(output)
    if dataset.GetNumberOfPoints() < 1 or dataset.GetNumberOfCells() < 1:
        raise ResultReaderError("结果文件没有可渲染的点或单元")
    return dataset


def _composite_leaves(output, vtk):
    if output is None or not output.IsA("vtkCompositeDataSet"):
        return {}
    leaves = {}
    name_key = vtk.vtkCompositeDataSet.NAME()
    iterator = output.NewIterator()
    iterator.SkipEmptyNodesOn()
    if hasattr(iterator, "VisitOnlyLeavesOn"):
        iterator.VisitOnlyLeavesOn()
    iterator.InitTraversal()
    while not iterator.IsDoneWithTraversal():
        dataset = iterator.GetCurrentDataObject()
        if dataset is not None and dataset.IsA("vtkDataSet") and dataset.GetNumberOfCells():
            index = iterator.GetCurrentFlatIndex()
            metadata = iterator.GetCurrentMetaData() if iterator.HasCurrentMetaData() else None
            name = metadata.Get(name_key) if metadata is not None and metadata.Has(name_key) else ""
            label = f"{name} · #{index}" if name else f"数据块 #{index}"
            leaves[f"{index}:{name}"] = (label, dataset)
        iterator.GoToNextItem()
    return leaves


def _series_member(path: Path, time: float | None) -> tuple[Path, list[float]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ResultReaderError(f"series 无法解析: {error}") from error
    entries = document.get("files") if isinstance(document, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ResultReaderError("series.files 必须是非空列表")
    root = path.parent.resolve()
    normalized = []
    for entry in entries:
        if isinstance(entry, str):
            name, time_value = entry, float(len(normalized))
        elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
            name = entry["name"]
            try:
                time_value = float(entry.get("time", len(normalized)))
                if not math.isfinite(time_value):
                    raise ValueError('Nonfinite series time')
            except (TypeError, ValueError) as error:
                raise ResultReaderError("series 时刻必须是数值") from error
        else:
            raise ResultReaderError("series.files 条目需要 name/time")
        member = Path(name)
        if member.is_absolute() or ".." in member.parts:
            raise ResultReaderError("series 引用了越界路径")
        resolved = (root / member).resolve()
        if root not in resolved.parents:
            raise ResultReaderError("series 引用越过所在目录")
        normalized.append((time_value, resolved))
    normalized.sort(key=lambda item: item[0])
    times = [item[0] for item in normalized]
    if len(set(times)) != len(times):
        raise ResultReaderError("series 时刻不能重复")
    selected = times[-1] if time is None else float(time)
    matches = [item for item in normalized if abs(item[0] - selected) <= 1e-9]
    if not matches:
        raise ResultReaderError(f"series 不含时刻 {selected:g}；可用: {times}")
    member_path = matches[0][1]
    if member_path.suffix.lower() == ".series":
        raise ResultReaderError("暂不支持嵌套 series")
    return member_path, times


def _load_series(path: Path, time: float | None, block: str | None) -> ResultData:
    member_path, times = _series_member(path, time)
    result = load_result_file(member_path, block=block)
    return ResultData(result.dataset, times, f"series:{result.reader_kind}", str(path), result.blocks)


def _reader_times(reader, kind, vtk):
    """Read the data pipeline's advertised steps without reading the mesh."""
    info = reader.GetOutputInformation(0)
    key = vtk.vtkStreamingDemandDrivenPipeline.TIME_STEPS()
    values = list(info.Get(key)) if info is not None and info.Has(key) else []
    if kind == "vtk" and values == [0.0]:
        # The legacy reader advertises 0 even for files without temporal data.
        return []
    if not all(math.isfinite(time) for time in values) or len(set(values)) != len(values):
        raise ResultReaderError("结果文件含无效或重复时刻")
    return sorted(values)


def _checked_result_path(path):
    path = Path(path)
    if not path.is_file():
        raise ResultReaderError(f"结果文件不存在: {path.name}")
    if path.suffix.lower() not in SUPPORTED_RESULT_EXTENSIONS:
        raise ResultReaderError(f"不支持的结果格式: {path.suffix.lower() or '(无扩展名)'}")
    return path


def result_file_times(path: Path) -> list[float]:
    path = _checked_result_path(path)
    if path.suffix.lower() == ".series":
        return _series_member(path, None)[1]
    import vtk

    try:
        reader, kind = _reader_for(path, vtk)
        reader.UpdateInformation()
        return _reader_times(reader, kind, vtk)
    except ResultReaderError:
        raise
    except Exception as error:
        raise ResultReaderError(f"读取 {path.name} 时刻失败: {error}") from error


def load_result_file(path: Path, time: float | None = None, *, block: str | None = None) -> ResultData:
    path = _checked_result_path(path)
    if path.suffix.lower() == ".series":
        return _load_series(path, time, block)
    import vtk

    try:
        reader, kind = _reader_for(path, vtk)
        reader.UpdateInformation()
        times = _reader_times(reader, kind, vtk)
        if isinstance(reader, vtk.vtkExodusIIReader):
            for association in (reader.NODAL, reader.ELEM_BLOCK, reader.GLOBAL):
                reader.SetAllArrayStatus(association, 1)
        if time is not None and times:
            selected = float(time)
            matches = [value for value in times if abs(value - selected) <= 1e-9]
            if not matches:
                raise ResultReaderError(f"文件不含时刻 {selected:g}；可用: {times}")
            reader.UpdateTimeStep(matches[0])
        else:
            reader.Update()
        output = reader.GetOutput()
        leaves = _composite_leaves(output, vtk)
        if block is not None:
            if not isinstance(block, str) or block not in leaves:
                raise ResultBlockUnavailable("所选数据块在当前时刻不可用")
            output = leaves[block][1]
        dataset = _copy_dataset(output, vtk)
        blocks = tuple(ResultBlock(key, label) for key, (label, _) in leaves.items())
        return ResultData(dataset, times, kind, str(path), blocks)
    except ResultReaderError:
        raise
    except Exception as error:
        raise ResultReaderError(f"读取 {path.name} 失败: {error}") from error


def _finite_range(values):
    low, high = values
    return [low, high] if math.isfinite(low) and math.isfinite(high) and low <= high else None


def _bounds(values, points):
    if not points:
        return None
    axes = [_finite_range(values[index:index + 2]) for index in (0, 2, 4)]
    return axes if all(axis is not None for axis in axes) else None


def _array_bytes(array, tuples, components, vtk):
    if array.GetDataType() == vtk.VTK_BIT:
        return (tuples * components + 7) // 8
    if array.IsA("vtkDataArray"):
        return tuples * components * array.GetDataTypeSize()
    return None


def dataset_information(dataset):
    """Information for the exact live VTK source or filter output."""
    import vtk

    points = dataset.GetNumberOfPoints()
    cell_types = vtk.vtkCellTypes()
    if dataset.GetNumberOfCells():
        dataset.GetDistinctCellTypes(cell_types)
    information = {
        "dataset": dataset.GetClassName(),
        "memory_kib": dataset.GetActualMemorySize(),
        "points": points,
        "cells": dataset.GetNumberOfCells(),
        "bounds": _bounds(dataset.GetBounds(), points),
        "cell_types": [cell_types.GetClassNameFromTypeId(cell_types.GetCellType(index))
                       for index in range(cell_types.GetNumberOfTypes())],
        "arrays": [],
    }
    if dataset.IsA("vtkImageData"):
        information.update(dimensions=dataset.GetDimensions(), origin=dataset.GetOrigin(), spacing=dataset.GetSpacing())
    elif dataset.IsA("vtkRectilinearGrid") or dataset.IsA("vtkStructuredGrid"):
        information["dimensions"] = dataset.GetDimensions()
    for association, attributes in (("点", dataset.GetPointData()), ("单元", dataset.GetCellData()),
                                    ("全局", dataset.GetFieldData())):
        for index in range(attributes.GetNumberOfArrays()):
            array = attributes.GetAbstractArray(index)
            components, tuples = array.GetNumberOfComponents(), array.GetNumberOfTuples()
            size = _array_bytes(array, tuples, components, vtk)
            information["arrays"].append({
                "name": array.GetName() or "未命名",
                "association": association,
                "components": components,
                "tuples": tuples,
                "type": array.GetDataTypeAsString(),
                "bytes": size,
                "allocated_kib": array.GetActualMemorySize(),
                "range": _finite_range(array.GetFiniteRange(-1)) if size is not None and tuples else None,
            })
    return information


def paraview_information(source: Path, time: float | None):
    """Open the selected source in pvpython and report server-manager information."""
    import vtk
    from paraview.servermanager import vtkSMProxyManager
    from paraview.simple import OpenDataFile, OpenFOAMReader

    path = source
    series_times = None
    if path.suffix.lower() == ".series":
        path, series_times = _series_member(path, time)
    if path.suffix.lower() == ".foam":
        reader = OpenFOAMReader(FileName=str(path))
        reader.SkipZeroTime = 0
    else:
        reader = OpenDataFile(str(path))
    if reader is None:
        raise ResultReaderError("ParaView 没有找到适用的读取器")
    if time is not None and series_times is None:
        reader.UpdatePipeline(time=time)
    else:
        reader.UpdatePipeline()
    data = reader.GetDataInformation().DataInformation
    if data.IsNull() or not data.GetNumberOfDataSets():
        raise ResultReaderError("ParaView 读取器未返回数据")
    points = data.GetNumberOfPoints()
    result = {
        "version": ".".join(str(part) for part in (vtkSMProxyManager.GetVersionMajor(),
                                                  vtkSMProxyManager.GetVersionMinor(),
                                                  vtkSMProxyManager.GetVersionPatch())),
        "dataset": data.GetDataSetTypeAsString(),
        "composite": vtk.vtkDataObjectTypes.GetClassNameFromTypeId(data.GetCompositeDataSetType())
        if data.IsCompositeDataSet() else None,
        "datasets": data.GetNumberOfDataSets(),
        "points": points,
        "cells": data.GetNumberOfCells(),
        "memory_kib": data.GetMemorySize(),
        "bounds": _bounds(data.GetBounds(), points),
        "time_steps": len(series_times) if series_times is not None else data.GetNumberOfTimeSteps(),
        "arrays": [],
    }
    extent = data.GetExtent()
    if len(extent) == 6 and all(extent[index] <= extent[index + 1] for index in (0, 2, 4)):
        result["dimensions"] = [extent[index + 1] - extent[index] + 1 for index in (0, 2, 4)]
    for association, kind in (("点", vtk.vtkDataObject.POINT), ("单元", vtk.vtkDataObject.CELL),
                              ("全局", vtk.vtkDataObject.FIELD), ("行", vtk.vtkDataObject.ROW)):
        attributes = data.GetAttributeInformation(kind)
        for index in range(attributes.GetNumberOfArrays()):
            item = attributes.GetArrayInformation(index)
            array = vtk.vtkAbstractArray.CreateArray(item.GetDataType())
            components, tuples = item.GetNumberOfComponents(), item.GetNumberOfTuples()
            size = _array_bytes(array, tuples, components, vtk) if array is not None else None
            result["arrays"].append({
                "name": item.GetName() or "未命名",
                "association": association,
                "components": components,
                "tuples": tuples,
                "type": array.GetDataTypeAsString() if array is not None else "未知",
                "bytes": size,
                "range": _finite_range(item.GetComponentFiniteRange(-1)) if size is not None and tuples else None,
            })
    return result


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--time", type=float)
    options = parser.parse_args()
    print("VIBEFLOW_PARAVIEW_INFORMATION " + json.dumps(paraview_information(options.source, options.time), allow_nan=False))
