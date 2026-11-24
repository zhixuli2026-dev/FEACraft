"""I04：部件几何/工程等价映射 golden。"""
from app.services.flotherm_components import map_component

cases = [
    ({"id": "box", "type": "box", "dx": 1, "dy": 2, "dz": 3}, "converted"),
    ({"id": "cyl", "type": "cylinder", "radius": 1, "height": 2, "axis": "z"}, "converted"),
    ({"id": "prism", "type": "convex-prism", "polygon": [[0, 0], [1, 0], [0, 1]], "height": 2}, "converted"),
    ({"id": "tet", "type": "tetrahedron", "vertices": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]]}, "converted"),
    ({"id": "plate", "type": "thin-plate", "thickness": 0.01}, "equivalent"),
    ({"id": "pcb", "type": "pcb", "thickness": 0.0016}, "equivalent"),
    ({"id": "2r", "type": "fan", "fan_model": "2R", "r1": 1, "r2": 2}, "equivalent"),
    ({"id": "pq", "type": "fan", "fan_model": "PQ", "curve": [
        {"flow": 1, "pressure": 10}, {"flow": 2, "pressure": 5}]}, "equivalent"),
    ({"id": "res", "type": "resistance", "resistance": 2.5}, "equivalent"),
    ({"id": "sink", "type": "heatsink", "fin_count": 8, "base_thickness": 0.01}, "equivalent"),
    ({"id": "case", "type": "enclosure", "thickness": 0.002}, "equivalent"),
]
for component, expected in cases:
    mapped = map_component(component)
    assert mapped["mapping"] == expected, (component, mapped)
    assert mapped["id"] == component["id"]
assert map_component(cases[2][0])["shape"]["kind"] == "convex-prism"
assert map_component(cases[3][0])["shape"]["kind"] == "tetrahedron"
assert map_component(cases[4][0])["engineering_model"] == "thin-conducting-layer"
for bad in (
    {"id": "bad-box", "type": "box", "dx": 1},
    {"id": "bad-tet", "type": "tetrahedron", "vertices": [[0, 0, 0]] * 4},
    {"id": "bad-fan", "type": "fan", "fan_model": "PQ", "curve": [{"flow": 2, "pressure": 1}, {"flow": 1, "pressure": 2}]},
    {"id": "bad-sink", "type": "heatsink", "fin_count": 0, "base_thickness": 1},
    {"id": "unknown", "type": "magic"},
):
    mapped = map_component(bad)
    assert mapped["mapping"] == "missing-info" and mapped["missing_fields"], mapped
print("I04 box/cylinder/prism/tetra/plate/PCB/fans/resistance/heatsink/enclosure and missing-info: OK")
print("\n=== I04 Flotherm 部件映射冒烟通过 ===")
