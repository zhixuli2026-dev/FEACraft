"""I05：Monitor/Region、热源功率与转换覆盖报告 golden。"""
import json
from pathlib import Path
import tempfile

from app.services.flotherm_ir import build_ir
from app.services.flotherm_coverage import SCHEMA, build_coverage_report, write_coverage_report

objects = [
    {"id": "chip", "type": "block", "material": "silicon", "mapping": "converted", "power_w": 5},
    {"id": "board", "type": "pcb", "material": "fr4", "mapping": "equivalent", "power_w": 2},
    {"id": "fan", "type": "fan", "material": "fr4", "mapping": "unsupported", "power_w": 3,
     "unsupported_reason": "缺少曲线"},
    {"id": "shell", "type": "enclosure", "material": "fr4", "power_w": 1,
     "missing_fields": ["thickness"]},
]
materials = {"silicon": {"conductivity_w_mk": 130}, "fr4": {"conductivity_w_mk": [0.3, 0.3, 1.2]}}
monitors = [{"type": "region-temperature", "objects": ["chip"]}]
ir = build_ir("input/thermal.ecxml", b"thermal-input", objects, materials, monitors=monitors)
report = build_coverage_report(ir)
assert report["schema"] == SCHEMA
assert report["summary"] == {"objects": 4, "input_power_w": 11.0, "mapped_power_w": 7.0,
                             "unmapped_power_w": 4.0, "balanced": True}, report["summary"]
assert len(report["blockers"]) == 2 and report["blockers"][0]["reason"] == "缺少曲线", report["blockers"]
assert report["blockers"][1]["missing_fields"] == ["thickness"]
assert report["monitors"] == monitors
with tempfile.TemporaryDirectory(prefix="vf-coverage-i05-") as temp:
    root = Path(temp)
    path = write_coverage_report(root, ir)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == report
    markdown = (root / "conversion-coverage.md").read_text(encoding="utf-8")
    assert "输入功率：11 W" in markdown and "未映射功率：4 W" in markdown
    assert "| fan | fan | unsupported | 3 | 缺少曲线 |" in markdown
print("I05 coverage rows, blockers, monitor and power balance JSON/Markdown: OK")
print("\n=== I05 Flotherm 转换覆盖报告冒烟通过 ===")
