"""Validate, sample and exchange scalar color maps."""
from __future__ import annotations

import json
import math
import re
from xml.etree import ElementTree as ET

import numpy as np


class ColormapError(ValueError):
    pass


SCHEMA = "vibeflow.colormap.v1"
ADVANCED_SCHEMA = "vibeflow.colormap.v2"
COLOR_SPACES = ("RGB", "HSV", "Wrapped", "Lab", "Diverging", "Lab/CIEDE2000", "Step")
MAX_COLORMAP_BYTES = 256 * 1024


def _color(value) -> str:
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        return value.upper()
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            channels = [float(channel) for channel in value]
        except (TypeError, ValueError) as error:
            raise ColormapError("颜色通道必须是 0–1 数值") from error
        if all(math.isfinite(channel) and 0 <= channel <= 1 for channel in channels):
            return "#" + "".join(f"{round(channel * 255):02X}" for channel in channels)
    raise ColormapError(f"非法颜色: {value!r}；应为 #RRGGBB 或 [r,g,b]")


def normalize_control_points(value) -> list[dict]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ColormapError(f"控制点不是合法 JSON: {error}") from error
    if isinstance(value, dict):
        if value.get("schema") != SCHEMA:
            raise ColormapError(f"色图 schema 须为 {SCHEMA}")
        value = value.get("points")
    if not isinstance(value, list) or not 2 <= len(value) <= 32:
        raise ColormapError("控制点数量须为 2–32")
    points = []
    for item in value:
        if not isinstance(item, dict) or "position" not in item or "color" not in item:
            raise ColormapError("每个控制点需要 position 和 color")
        try:
            position = float(item["position"])
        except (TypeError, ValueError) as error:
            raise ColormapError("position 必须是数值") from error
        if not math.isfinite(position) or not 0 <= position <= 1:
            raise ColormapError("position 必须在 [0,1]")
        points.append({"position": position, "color": _color(item["color"])})
    points.sort(key=lambda point: point["position"])
    if points[0]["position"] != 0 or points[-1]["position"] != 1:
        raise ColormapError("首尾控制点必须位于 0 和 1")
    if any(points[index]["position"] <= points[index - 1]["position"] for index in range(1, len(points))):
        raise ColormapError("控制点 position 必须严格递增且不能重复")
    return points


def _rgb(hex_color: str) -> list[float]:
    return [int(hex_color[index:index + 2], 16) / 255 for index in (1, 3, 5)]


def sample_control_points(value, count: int = 256, invert: bool = False) -> list[tuple[float, float, float]]:
    if not 2 <= count <= 4096:
        raise ColormapError("采样数须为 2–4096")
    points = normalize_control_points(value)
    positions = np.asarray([point["position"] for point in points], dtype=float)
    colors = np.asarray([_rgb(point["color"]) for point in points], dtype=float)
    coordinates = np.linspace(0, 1, count)
    rgb = np.column_stack([np.interp(coordinates, positions, colors[:, channel])
                           for channel in range(3)])
    result = [tuple(float(channel) for channel in row) for row in rgb]
    return list(reversed(result)) if invert else result


def canonical_colormap_json(value) -> str:
    return json.dumps({"schema": SCHEMA, "points": normalize_control_points(value)},
                      ensure_ascii=False, indent=2) + "\n"


def _number(value, label, *, unit=False):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ColormapError(f"{label} 必须是有限数值") from error
    if not math.isfinite(number) or (unit and not 0 <= number <= 1):
        raise ColormapError(f"{label} 必须在 [0,1]" if unit else f"{label} 必须是有限数值")
    return number


def _advanced_points(value, *, opacity_only=False):
    if not isinstance(value, list) or not 2 <= len(value) <= 512:
        raise ColormapError("控制点数量须为 2–512")
    points = []
    for item in value:
        if not isinstance(item, dict):
            raise ColormapError("每个控制点必须是对象")
        point = {"position": _number(item.get("position"), "position", unit=True)}
        if not opacity_only:
            color = item.get("color")
            if isinstance(color, (tuple, list)) and len(color) == 3:
                point["color"] = [_number(channel, "RGB", unit=True) for channel in color]
            else:
                point["color"] = _color(color)
        if opacity_only or "opacity" in item:
            point["opacity"] = _number(item.get("opacity"), "opacity", unit=True)
        points.append(point)
    points.sort(key=lambda point: point["position"])
    positions = [point["position"] for point in points]
    if positions[0] != 0 or positions[-1] != 1 or len(set(positions)) != len(positions):
        raise ColormapError("控制点需要唯一位置，且首尾必须位于 0 和 1")
    return points


def normalize_colormap(value):
    if isinstance(value, str):
        if len(value.encode('utf-8')) > MAX_COLORMAP_BYTES:
            raise ColormapError("色图文件超过 256 KiB")
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, RecursionError) as error:
            raise ColormapError("控制点必须是合法 JSON") from error
    if isinstance(value, dict) and value.get("schema") not in (SCHEMA, ADVANCED_SCHEMA):
        raise ColormapError(f"色图 schema 须为 {SCHEMA} 或 {ADVANCED_SCHEMA}")
    if not isinstance(value, dict) or value.get("schema") != ADVANCED_SCHEMA:
        return {"schema": SCHEMA, "points": normalize_control_points(value)}
    space = value.get("space", "RGB")
    if space not in COLOR_SPACES:
        raise ColormapError("请选择受支持的插值颜色空间")
    levels = value.get("levels", 0)
    if type(levels) is not int or (levels != 0 and not 2 <= levels <= 256):
        raise ColormapError("色阶数须为 0（连续）或 2–256")
    result = {"schema": ADVANCED_SCHEMA, "space": space, "levels": levels,
              "points": _advanced_points(value.get("points"))}
    if "opacity_points" in value:
        result["opacity_points"] = _advanced_points(value["opacity_points"], opacity_only=True)
    if "nan_color" in value:
        result["nan_color"] = _color(value["nan_color"])
    if "name" in value:
        name = value["name"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or not name.isprintable():
            raise ColormapError("色图名称须为 1–120 个可显示字符")
        result["name"] = name.strip()
    return result


def colormap_json(document):
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def sample_colormap(document, invert=False):
    """Return the actual RGBA table, preserving the v1 256-entry RGB path."""
    if document["schema"] == SCHEMA:
        return [(*color, 1.0) for color in sample_control_points(document, invert=invert)]
    from vtkmodules.vtkCommonDataModel import vtkPiecewiseFunction
    from vtkmodules.vtkRenderingCore import vtkColorTransferFunction

    color = vtkColorTransferFunction()
    space = document["space"]
    selector = {"Wrapped": "HSV", "Lab/CIEDE2000": "LabCIEDE2000"}.get(space, space)
    getattr(color, "SetColorSpaceTo" + selector)()
    color.SetHSVWrap(space == "Wrapped")
    opacity = vtkPiecewiseFunction()
    for point in document["points"]:
        rgb = point["color"]
        color.AddRGBPoint(point["position"], *(_rgb(rgb) if isinstance(rgb, str) else rgb))
    for point in document.get("opacity_points", document["points"]):
        opacity.AddPoint(point["position"], point.get("opacity", 1.0))
    levels = document["levels"] or 256
    coordinates = np.linspace(1 if invert else 0, 0 if invert else 1, levels)
    return [(*color.GetColor(float(x)), opacity.GetValue(float(x))) for x in coordinates]


def parse_paraview_colormap(data):
    if len(data) > MAX_COLORMAP_BYTES or re.search(rb"<!\s*(?:DOCTYPE|ENTITY)", data, re.I):
        raise ColormapError("XML 须小于 256 KiB 且使用普通 ParaView 色图元素")
    try:
        # Accept the same UTF-8 contract as the case JSON editor.
        root = ET.fromstring(data.decode('utf-8-sig'))
    except (ET.ParseError, UnicodeError) as error:
        raise ColormapError("XML 须为合法的 UTF-8 ParaView 色图") from error
    maps = [root] if root.tag == "ColorMap" else root.findall("ColorMap") if root.tag == "ColorMaps" else []
    if len(maps) != 1:
        raise ColormapError("XML 文件需要包含一个 ColorMap")
    entry = maps[0]
    if entry.get("indexedLookup", "false").lower() not in {"false", "0"}:
        raise ColormapError("此处需要连续标量色图，色阶可在编辑器中设置")
    points = entry.findall("Point")
    coordinates = [_number(point.get("x"), "XML x") for point in points]
    if len(set(coordinates)) < 2 or not math.isfinite(max(coordinates) - min(coordinates)):
        raise ColormapError("XML 色图需要至少两个不同且有效的标量位置")
    low, high = min(coordinates), max(coordinates)
    space = entry.get("space", "RGB")
    raw = {"schema": ADVANCED_SCHEMA, "space": "RGB" if space == "NoChange" else space, "points": [
        {"position": (x - low) / (high - low), "color": [point.get(c) for c in "rgb"],
         "opacity": point.get("o", "1")} for x, point in zip(coordinates, points)]}
    if entry.get("name"):
        raw["name"] = entry.get("name")
    nan = entry.find("NaN")
    if nan is not None:
        raw["nan_color"] = [_number(nan.get(channel), "NaN RGB", unit=True) for channel in "rgb"]
    return normalize_colormap(raw)


def paraview_colormap_xml(document):
    maps = ET.Element("ColorMaps")
    entry = ET.SubElement(maps, "ColorMap", name=document.get("name", "VibeFlow custom"),
                          space=document.get("space", "RGB"), indexedLookup="false")
    points = document["points"]
    opacity_points = document.get("opacity_points")
    if opacity_points:
        opacities = np.interp([point["position"] for point in points],
                              [point["position"] for point in opacity_points],
                              [point["opacity"] for point in opacity_points]).tolist()
    else:
        opacities = [point.get("opacity", 1.0) for point in points]
    for point, opacity in zip(points, opacities):
        rgb = point["color"]
        r, g, b = _rgb(rgb) if isinstance(rgb, str) else rgb
        ET.SubElement(entry, "Point", x=str(point["position"]), r=str(r), g=str(g), b=str(b),
                      o=str(opacity))
    if document.get("nan_color"):
        r, g, b = _rgb(document["nan_color"])
        ET.SubElement(entry, "NaN", r=str(r), g=str(g), b=str(b))
    ET.indent(maps)
    return ET.tostring(maps, encoding="unicode") + "\n"
