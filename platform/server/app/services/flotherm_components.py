"""Flotherm 部件到工程等价对象映射；不生成虚构的缺失几何。"""
from __future__ import annotations

import math


def _number(component: dict, key: str, missing: list[str], minimum=0.0):
    value = component.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= minimum:
        missing.append(key)
        return None
    return float(value)


def _missing(component: dict, missing: list[str], reason="缺少部件参数") -> dict:
    return {"id": component.get("id", "unknown"), "mapping": "missing-info",
            "missing_fields": sorted(set(missing)), "reason": reason,
            "material": component.get("material", "default"), "power_w": component.get("power_w", 0)}


def map_component(component: dict) -> dict:
    kind = str(component.get("type", "")).lower().replace("_", "-")
    missing = []
    result = {"id": component.get("id", "unknown"), "type": kind,
              "material": component.get("material", "default"),
              "power_w": component.get("power_w", 0), "mapping": "converted"}
    if kind == "box":
        dimensions = [_number(component, key, missing) for key in ("dx", "dy", "dz")]
        result["shape"] = {"kind": "box", "dimensions": dimensions}
    elif kind == "cylinder":
        radius = _number(component, "radius", missing)
        height = _number(component, "height", missing)
        axis = component.get("axis")
        if axis not in {"x", "y", "z"}:
            missing.append("axis")
        result["shape"] = {"kind": "cylinder", "radius": radius, "height": height, "axis": axis}
    elif kind in {"convex-prism", "prism"}:
        points = component.get("polygon")
        height = _number(component, "height", missing)
        if not isinstance(points, list) or len(points) < 3 or any(
                not isinstance(point, list) or len(point) != 2 or
                any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point)
                for point in points):
            missing.append("polygon")
        result["shape"] = {"kind": "convex-prism", "polygon": points, "height": height}
        result["type"] = "convex-prism"
    elif kind in {"tetrahedron", "tetra"}:
        vertices = component.get("vertices")
        valid = isinstance(vertices, list) and len(vertices) == 4
        if valid:
            try:
                a, b, c, d = vertices
                ab = [b[i] - a[i] for i in range(3)]
                ac = [c[i] - a[i] for i in range(3)]
                ad = [d[i] - a[i] for i in range(3)]
                cross = [ab[1] * ac[2] - ab[2] * ac[1], ab[2] * ac[0] - ab[0] * ac[2],
                         ab[0] * ac[1] - ab[1] * ac[0]]
                volume6 = abs(sum(cross[i] * ad[i] for i in range(3)))
                if not math.isfinite(volume6) or volume6 <= 1e-18:
                    valid = False
            except (TypeError, IndexError):
                valid = False
        if not valid:
            missing.append("vertices")
        result["shape"] = {"kind": "tetrahedron", "vertices": vertices}
        result["type"] = "tetrahedron"
    elif kind in {"thin-plate", "pcb"}:
        thickness = _number(component, "thickness", missing)
        result.update({"shape": {"kind": kind, "thickness": thickness},
                       "mapping": "equivalent", "engineering_model": "thin-conducting-layer"})
    elif kind == "fan":
        model = component.get("fan_model")
        if model == "2R":
            r1 = _number(component, "r1", missing)
            r2 = _number(component, "r2", missing)
            result["fan"] = {"model": "2R", "r1": r1, "r2": r2}
        elif model == "PQ":
            curve = component.get("curve")
            if not isinstance(curve, list) or len(curve) < 2:
                missing.append("curve")
            else:
                flows = [point.get("flow") if isinstance(point, dict) else None for point in curve]
                pressures = [point.get("pressure") if isinstance(point, dict) else None for point in curve]
                if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
                       for v in flows + pressures) or any(x <= 0 for x in flows) or any(x < 0 for x in pressures):
                    missing.append("curve")
                elif any(flows[i] >= flows[i + 1] for i in range(len(flows) - 1)):
                    missing.append("curve")
            result["fan"] = {"model": "PQ", "curve": curve}
        else:
            missing.append("fan_model")
        result["mapping"] = "equivalent"
    elif kind == "resistance":
        value = _number(component, "resistance", missing)
        result.update({"resistance": value, "mapping": "equivalent"})
    elif kind == "heatsink":
        fin_count = component.get("fin_count")
        if isinstance(fin_count, bool) or not isinstance(fin_count, int) or fin_count < 1:
            missing.append("fin_count")
        base = _number(component, "base_thickness", missing)
        result.update({"heatsink": {"fin_count": fin_count, "base_thickness": base},
                       "mapping": "equivalent"})
    elif kind == "enclosure":
        thickness = _number(component, "thickness", missing)
        result.update({"shape": {"kind": "enclosure", "thickness": thickness},
                       "mapping": "equivalent"})
    else:
        return _missing(component, ["type"], "未知部件类型")
    if missing:
        return _missing(component, missing)
    return result
