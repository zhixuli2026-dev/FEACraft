"""caseMeta 支持量纲的确定性单位换算。"""
from __future__ import annotations

import math

_SCALE = {
    "length": {"m": 1.0, "mm": 0.001, "inch": 0.0254},
    "mass": {"kg": 1.0},
    "time": {"s": 1.0},
    "angle": {"rad": 1.0, "deg": math.pi / 180.0},
    "force": {"N": 1.0},
    "pressure": {"Pa": 1.0},
    "energy": {"J": 1.0},
}
DIMENSIONS = set(_SCALE) | {"temperature"}


def convert_unit(dimension: str, value: float, source: str, target: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("数值必须有限")
    if dimension == "temperature":
        if source not in {"K", "degC"} or target not in {"K", "degC"}:
            raise ValueError("温度单位只支持 K/degC")
        if source == target:
            return number
        result = number + (273.15 if source == "degC" else -273.15)
    else:
        scale = _SCALE.get(dimension)
        if scale is None or source not in scale or target not in scale:
            raise ValueError("不支持的量纲或单位")
        result = number * (scale[source] / scale[target])
    if not math.isfinite(result):
        raise ValueError("换算结果超出有限数值范围")
    return result
