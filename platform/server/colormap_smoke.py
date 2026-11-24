"""Color Map 自由控制点：规范化、插值、导入导出与非法输入。"""
import json

from app.services.colormap import (ColormapError, canonical_colormap_json,
                                   normalize_control_points, sample_control_points)

points = normalize_control_points([
    {"position": 0, "color": "#0000ff"},
    {"position": 0.5, "color": "#ffff00"},
    {"position": 1, "color": "#000000"},
])
assert points[0]["color"] == "#0000FF" and points[1]["position"] == 0.5
samples = sample_control_points(points, 5)
assert samples[0] == (0, 0, 1) and samples[-1] == (0, 0, 0)
assert samples[2] == (1, 1, 0)
assert samples[1] == (0.5, 0.5, 0.5), samples[1]
inverted = sample_control_points(points, 5, invert=True)
assert inverted[0] == samples[-1] and inverted[-1] == samples[0]
print("1. 控制点规范化、分段插值和反转 [OK]")

document = json.loads(canonical_colormap_json(points))
assert document["schema"] == "vibeflow.colormap.v1" and len(document["points"]) == 3
roundtrip = normalize_control_points(document)
assert roundtrip == points
print("2. v1 导入导出无损往返 [OK]")

invalid = [
    [],
    [{"position": 0, "color": "#000000"}],
    [{"position": 0.2, "color": "#000000"}, {"position": 1, "color": "#ffffff"}],
    [{"position": 0, "color": "#000000"}, {"position": 0.9, "color": "#ffffff"}],
    [{"position": 0, "color": "#000000"}, {"position": 0, "color": "#ffffff"}, {"position": 1, "color": "#ff0000"}],
    [{"position": 0, "color": "#000000"}, {"position": 1, "color": "bad"}],
    [{"position": 0, "color": "#000000"}, {"position": 1.2, "color": "#ffffff"}],
]
for value in invalid:
    try:
        normalize_control_points(value)
        raise AssertionError(f"非法控制点被接受: {value}")
    except ColormapError:
        pass
print("3. 点数、端点、顺序、重复、颜色和范围均 fail closed [OK]")

print("\n=== Color Map 自由控制点冒烟通过 ===")
