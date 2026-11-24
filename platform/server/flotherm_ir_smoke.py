"""I07：Flotherm IR manifest、来源、覆盖率、关联和功率 golden。"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile

from app.services.flotherm_ir import SCHEMA, build_ir, read_ir, validate_ir, write_ir

source = b"<ecxml>fixture</ecxml>"
objects = [
    {"id": "board", "type": "pcb", "material": "fr4", "mapping": "equivalent", "power_w": 0},
    {"id": "chip", "type": "block", "material": "silicon", "mapping": "converted", "power_w": 7.5},
    {"id": "unknown", "type": "custom", "material": "fr4"},
]
materials = {"fr4": {"conductivity_w_mk": [0.3, 0.3, 1.2]},
             "silicon": {"conductivity_w_mk": 130}}
links = [{"parent": "board", "child": "chip"}]
monitors = [{"type": "region-temperature", "objects": ["chip"]}]
with tempfile.TemporaryDirectory(prefix="vf-ir-i07-") as temp:
    root = Path(temp)
    data = build_ir("input/demo.ecxml", source, objects, materials, links, monitors)
    assert data["schema"] == SCHEMA
    assert data["source"]["sha256"] == hashlib.sha256(source).hexdigest()
    assert data["coverage"] == {"objects": 3, "converted": 1, "equivalent": 1,
                                "unsupported": 0, "missing-info": 1, "power_w": 7.5}
    assert data["assembly"]["roots"] == ["board", "unknown"]
    path = write_ir(root, data)
    assert path.name == "flotherm-ir.json" and read_ir(root) == data
    assert json.loads(path.read_text())["monitors"][0]["type"] == "region-temperature"

    bad = copy.deepcopy(data)
    bad["associations"].append({"parent": "chip", "child": "board"})
    try:
        validate_ir(bad)
    except ValueError as error:
        assert "循环" in str(error)
    else:
        raise AssertionError("assembly 循环未拒绝")
    bad = copy.deepcopy(data)
    bad["monitors"][0]["objects"] = ["missing"]
    try:
        validate_ir(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("未知 monitor 引用未拒绝")
    bad = copy.deepcopy(data)
    bad["objects"][1]["power_w"] = float("nan")
    try:
        validate_ir(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("NaN 功率未拒绝")
    bad = copy.deepcopy(data)
    bad["coverage"]["converted"] = 99
    try:
        validate_ir(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("coverage 篡改未拒绝")
print("I07 IR schema/source hash/coverage/power/assembly/monitor/roundtrip and negative validation: OK")
print("\n=== I07 Flotherm IR 冒烟通过 ===")
