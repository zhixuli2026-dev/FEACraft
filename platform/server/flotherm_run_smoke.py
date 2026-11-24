"""I10：Allrun 串行真实 laplacianFoam、时刻产物与 Allclean。"""
from pathlib import Path
import subprocess
import tempfile

from app.services.flotherm_case import generate_case, write_run_scripts
from app.services.flotherm_ir import build_ir
from app.services.jobrunner import win_to_wsl_path

objects = [{"id": "chip", "type": "box", "material": "silicon", "mapping": "converted", "power_w": 5,
            "shape": {"kind": "box", "origin": [0, 0, 0], "dimensions": [1, 1, 1]}}]
materials = {"silicon": {"conductivity_w_mk": 130, "rho_cp": 1000}}
ir = build_ir("input/chip.ecxml", b"chip-input", objects, materials,
              monitors=[{"type": "region-temperature", "objects": ["chip"]},
                        {"type": "probe", "objects": ["chip"], "locations": [[0.5, 0.5, 0.5]]}])
with tempfile.TemporaryDirectory(prefix="vf-run-i10-", dir="D:/tmp") as temp:
    output = Path(temp) / "case"
    generate_case(ir, output)
    write_run_scripts(output)
    for name in ("Allrun.pre", "Allrun.solve", "Allrun.serial", "Allrun.parallel", "Allclean"):
        assert (output / name).is_file()
        assert "allow-run-as-root" not in (output / name).read_text()
    wsl = win_to_wsl_path(output)
    result = subprocess.run(["wsl.exe", "-d", "vibeflow-linux", "--exec", "bash", "-lc",
                             f"source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd '{wsl}' && bash Allrun.serial > log.serial 2>&1"],
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    log = (output / "log.serial").read_text(encoding="utf-8", errors="replace") if (output / "log.serial").is_file() else ""
    assert result.returncode == 0, result.stdout + result.stderr + log
    assert (output / "100" / "T").is_file(), log
    assert "End" in log, log[-2000:]
    region = output / "postProcessing" / "monitor_0_region_temperature" / "0" / "volFieldValue.dat"
    probe = output / "postProcessing" / "monitor_1_probe" / "0" / "T"
    assert region.is_file() and "volAverage" in region.read_text(encoding="utf-8", errors="replace"), log[-2000:]
    assert probe.is_file() and probe.stat().st_size > 0, log[-2000:]
    result = subprocess.run(["wsl.exe", "-d", "vibeflow-linux", "--exec", "bash", "-lc",
                             f"cd '{wsl}' && bash Allclean"], capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert not (output / "100").exists()
    assert (output / "system" / "controlDict").is_file()
    assert (output / "case-manifest.json").is_file()
print("I10 Allrun.pre/solve/serial, real laplacianFoam time=100 and Allclean preservation: OK")
print("\n=== I10 Flotherm Allrun 冒烟通过 ===")
