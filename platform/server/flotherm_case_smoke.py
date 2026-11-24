"""I08：IR→OpenFOAM case、预检、功率与真实 blockMesh/topoSet golden。"""
import json
from pathlib import Path
import subprocess
import tempfile

from app.services.flotherm_case import generate_case, record_mesh_gate
from app.services.flotherm_ir import build_ir
from app.services.jobrunner import win_to_wsl_path

objects = [{"id": "chip", "type": "box", "material": "silicon", "mapping": "converted", "power_w": 5,
            "shape": {"kind": "box", "origin": [0, 0, 0], "dimensions": [1, 1, 1]}}]
materials = {"silicon": {"conductivity_w_mk": 130, "rho_cp": 1000}}
ir = build_ir("input/chip.ecxml", b"chip-input", objects, materials)
with tempfile.TemporaryDirectory(prefix="vf-case-i08-", dir="D:/tmp") as temp:
    output = Path(temp) / "case"
    manifest = generate_case(ir, output)
    assert manifest["heat_sources"][0]["su"] == 0.005
    assert json.loads((output / "preflight.json").read_text())["ok"] is True
    assert json.loads((output / "power-balance.json").read_text())["balanced"] is True
    for path in ("system/blockMeshDict", "system/topoSetDict", "constant/fvOptions", "case-manifest.json"):
        assert (output / path).is_file(), path
    command = (f"source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd '{win_to_wsl_path(output)}' "
               "&& blockMesh && topoSet")
    result = subprocess.run(["wsl.exe", "-d", "vibeflow-linux", "--exec", "bash", "-lc", command],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (output / "constant" / "polyMesh" / "points").is_file()
    assert "DT [0 2 -1 0 0 0 0] 130.0;" in (output / "constant" / "transportProperties").read_text()
    record_mesh_gate(output, True, {"blockMesh": True, "topoSet": True})
    assert json.loads((output / "mesh_gate.json").read_text())["ok"] is True

with tempfile.TemporaryDirectory(prefix="vf-case-i08-override-") as temp:
    bad = build_ir("input/fan.ecxml", b"fan", [{"id": "fan", "type": "fan", "material": "silicon",
                   "mapping": "equivalent", "power_w": 3, "shape": {"kind": "fan"}}], materials)
    manifest = generate_case(bad, Path(temp) / "case")
    assert manifest["required_overrides"], manifest
    assert json.loads((Path(temp) / "case" / "preflight.json").read_text())["ok"] is False

with tempfile.TemporaryDirectory(prefix="vf-case-i10c-channel-") as temp:
    channel_ir = build_ir("input/channel.ecxml", b"channel", objects, materials,
                          monitors=[{"type": "channel-flow", "objects": ["chip"]}])
    generate_case(channel_ir, Path(temp) / "case")
    overrides = json.loads((Path(temp) / "case" / "required-overrides.json").read_text())
    assert any("没有 channel 流量场 phi" in item["reason"] for item in overrides), overrides

with tempfile.TemporaryDirectory(prefix="vf-case-i10b-materials-") as temp:
    anisotropic_materials = {"fr4": {"conductivity_w_mk": [0.3, 0.3, 1.2], "rho_cp": 1000}}
    anisotropic = build_ir("input/board.ecxml", b"board", [{"id": "board", "type": "box",
                           "material": "fr4", "mapping": "equivalent", "power_w": 1,
                           "shape": {"kind": "box", "origin": [0, 0, 0], "dimensions": [1, 1, 1]}}], anisotropic_materials)
    generate_case(anisotropic, Path(temp) / "anisotropic")
    overrides = json.loads((Path(temp) / "anisotropic" / "required-overrides.json").read_text())
    assert any("正交各向异性" in item["reason"] for item in overrides), overrides
    multi = build_ir("input/multi.ecxml", b"multi", [
        {"id": "chip", "type": "box", "material": "silicon", "mapping": "converted", "power_w": 1,
         "shape": {"kind": "box", "origin": [0, 0, 0], "dimensions": [1, 1, 1]}},
        {"id": "copper", "type": "box", "material": "copper", "mapping": "equivalent", "power_w": 0,
         "shape": {"kind": "box", "origin": [1, 0, 0], "dimensions": [1, 1, 1]}}],
        {"silicon": {"conductivity_w_mk": 130, "rho_cp": 1000},
         "copper": {"conductivity_w_mk": 400, "rho_cp": 1000}})
    generate_case(multi, Path(temp) / "multi")
    overrides = json.loads((Path(temp) / "multi" / "required-overrides.json").read_text())
    assert any("多种扩散率" in item["reason"] for item in overrides), overrides
print("I08 case manifests/overrides/power/preflight and real v2406 blockMesh+topoSet: OK")
print("\n=== I08 Flotherm OpenFOAM case 冒烟通过 ===")
