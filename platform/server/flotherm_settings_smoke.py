"""I06：settings.json 覆盖、单位、网格参数与 blocker golden。"""
import hashlib
import json
from pathlib import Path
import tempfile

from app.services.flotherm_ir import build_ir
from app.services.flotherm_settings import apply_settings, load_settings

objects = [{"id": "chip", "type": "block", "material": "silicon", "mapping": "converted", "power_w": 1}]
materials = {"silicon": {"conductivity_w_mk": 130}}
ir = build_ir("input/demo.ecxml", b"demo", objects, materials)
settings = {
    "domain": [-1, -2, -3, 4, 5, 6],
    "materials": {"copper": {"conductivity_w_mk": 400}},
    "objectOverrides": {"chip": {"material": "copper", "refinement": 4}},
    "power": {"chip": {"value": 2500, "unit": "mW"}},
    "parallelSubdomains": 8, "mergeTouchingBoxes": True,
    "maxSkewness": 2.5, "scotch": True, "startFrom": "latest",
}
updated = apply_settings(ir, settings)
obj = updated["objects"][0]
assert obj["material"] == "copper" and obj["refinement"] == 4 and obj["power_w"] == 2.5, obj
assert updated["coverage"]["power_w"] == 2.5
assert updated["settings"]["parallelSubdomains"] == 8 and updated["settings"]["scotch"] is True
assert ir["objects"][0]["material"] == "silicon", "不得修改原 IR"
for bad in (
    {"unknownOption": True},
    {"power": {"missing": 1}},
    {"objectOverrides": {"chip": {"material": "missing"}}},
    {"objectOverrides": {"chip": {"refinement": 99}}},
    {"power": {"chip": {"value": 1, "unit": "BTU"}}},
    {"domain": [0, 0, 0, 0, 1, 1]},
    {"parallelSubdomains": 0},
):
    try:
        apply_settings(ir, bad)
    except ValueError:
        pass
    else:
        raise AssertionError(f"非法 settings 未拒绝: {bad}")
with tempfile.TemporaryDirectory(prefix="vf-settings-i06-") as temp:
    path = Path(temp) / "settings.json"
    raw = json.dumps(settings).encode()
    path.write_bytes(raw)
    loaded = load_settings(ir, path)
    assert loaded["settings"]["source"]["sha256"] == hashlib.sha256(raw).hexdigest()
print("I06 whitelist, object/material/refinement overrides, mW conversion, mesh/decomposition and blockers: OK")
print("\n=== I06 Flotherm settings 冒烟通过 ===")
