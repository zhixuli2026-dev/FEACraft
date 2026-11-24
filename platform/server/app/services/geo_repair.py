"""OCCT 几何修复、对称与周期变换。"""
from __future__ import annotations

import math

from .geo_validation import validate_shape


def repair_shape(shape, operation: str = "sew", axis: str = "XY",
                 period: float = 1.0, count: int = 2, factor=1.0):
    import cadquery as cq
    if hasattr(shape, "val") and callable(shape.val):
        shape = shape.val()
    if operation == "sew":
        if shape.Solids():
            return shape.fix()
        return cq.Shell.makeShell(shape.Faces())
    if operation == "fix":
        return shape.fix()
    if operation == "mirror":
        if axis not in {"XY", "YZ", "XZ"}:
            raise ValueError("对称平面只支持 XY/YZ/XZ")
        return shape.mirror(axis)
    if operation == "scale":
        factors = [factor] if isinstance(factor, (int, float)) else list(factor)
        if len(factors) not in {1, 3} or any(
                not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0
                for value in factors):
            raise ValueError("缩放因子必须是正数或三个正数")
        if len(factors) == 1:
            return shape.scale(factors[0])
        from OCP.BRepBuilderAPI import BRepBuilderAPI_GTransform
        matrix = cq.Matrix([[factors[0], 0, 0, 0],
                            [0, factors[1], 0, 0],
                            [0, 0, factors[2], 0]])
        return cq.Shape.cast(BRepBuilderAPI_GTransform(shape.wrapped, matrix.wrapped, True).Shape())
    if operation == "periodic":
        if axis not in {"X", "Y", "Z"}:
            raise ValueError("周期方向只支持 X/Y/Z")
        if not math.isfinite(period) or period <= 0:
            raise ValueError("周期必须为正数")
        if not isinstance(count, int) or not 1 <= count <= 100:
            raise ValueError("周期数量必须是 1–100 的整数")
        direction = {"X": (period, 0, 0), "Y": (0, period, 0), "Z": (0, 0, period)}[axis]
        shapes = [shape]
        for _ in range(1, count):
            shapes.append(shape.translate(direction))
        return cq.Compound.makeCompound(shapes)
    raise ValueError("未知几何修复操作")


def repair_report(shape, operation: str, axis: str, period: float, count: int, factor=1.0) -> dict:
    repaired = repair_shape(shape, operation, axis, period, count, factor)
    return {"shape": repaired, "validation": validate_shape(repaired)}
