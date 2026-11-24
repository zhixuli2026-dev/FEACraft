"""OpenFOAM 标量时间边界 profile 生成与参数校验。"""
from __future__ import annotations

import math


def _number(value, name: str, positive: bool = False) -> str:
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        raise ValueError(f"{name} 必须是有限{'正' if positive else ''}数")
    return f"{number:.12g}"


def constant(value) -> str:
    return f"constant {_number(value, 'value')};"


def table(points: list[tuple[float, float]]) -> str:
    if not points:
        raise ValueError("table 至少需要一个时间点")
    ordered = sorted((float(time), float(value)) for time, value in points)
    if any(not math.isfinite(time) or not math.isfinite(value) or time < 0
           for time, value in ordered):
        raise ValueError("table 时间和值必须有限，时间不能为负")
    if any(later[0] == earlier[0] for earlier, later in zip(ordered, ordered[1:])):
        raise ValueError("table 时间点不能重复")
    return "table (" + " ".join(f"({_number(time, 'time')} {_number(value, 'value')})"
                                for time, value in ordered) + ");"


def harmonic(amplitude, frequency, phase=0.0, level=0.0) -> str:
    return ("sine { "
            f"amplitude {_number(amplitude, 'amplitude')}; "
            f"frequency {_number(frequency, 'frequency', True)}; "
            f"phase {_number(phase, 'phase')}; "
            f"level {_number(level, 'level')}; "
            "}")


def ramp(start, duration, amplitude) -> str:
    return (f"ramp {{ start {_number(start, 'start')}; "
            f"duration {_number(duration, 'duration', True)}; "
            f"amplitude {_number(amplitude, 'amplitude')}; "
            "}")


def pulse(start, duration, amplitude, frequency) -> str:
    return (f"square {{ start {_number(start, 'start')}; "
            f"duration {_number(duration, 'duration', True)}; "
            f"amplitude {_number(amplitude, 'amplitude')}; "
            f"frequency {_number(frequency, 'frequency', True)}; "
            "}")
