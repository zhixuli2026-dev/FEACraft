"""F59：环境变量注册表、显式错误 fail closed 与阶段最小检查。"""
import json
from pathlib import Path
import tempfile
import urllib.request

from app.services.runtime_registry import environment_registry, stage_check, stage_checks

root = Path(tempfile.mkdtemp(prefix="vibeflow-runtime-registry-"))
try:
    fake_python = root / "python.exe"
    fake_python.write_text("mock", encoding="utf-8")
    env = {"VIBEFLOW_PYTHON": str(fake_python), "VIBEFLOW_PANDOC": str(root / "missing-pandoc")}
    entries = environment_registry(env)
    python = next(item for item in entries if item["tool"] == "python")
    assert python["source"] == "explicit" and python["available"] and python["value"] == str(fake_python), python
    pandoc = next(item for item in entries if item["tool"] == "pandoc")
    assert pandoc["source"] == "explicit_error" and not pandoc["available"], pandoc
    assert any(item["tool"] == "wsl_distro" and item["source"] == "fallback" for item in entries)
    print("1. 显式路径优先、显式错误 fail closed、设置 fallback [OK]")

    good_wsl = {"openfoam": True, "wmake": True, "latexmk": True}
    assert stage_check("A", good_wsl, {"cadquery": True})["ok"]
    assert not stage_check("A", good_wsl, {"cadquery": False})["ok"]
    assert stage_check("B", good_wsl, {})["ok"] and stage_check("C", good_wsl, {})["ok"]
    assert not stage_check("C", {**good_wsl, "wmake": False}, {})["ok"]
    matrix = stage_checks(good_wsl, {"cadquery": True, "vtk": True, "numpy": True, "matplotlib": True})
    assert set(matrix) == {"A", "B", "C", "D", "E", "V"}
    print("2. A–E/V 最小阶段检查与缺失项 [OK]")

    request = urllib.request.Request("http://localhost:8000/api/v1/auth/login",
        data=json.dumps({"email": "dev@vibeflow.cn", "password": "devpass123"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        token = json.loads(response.read())["token"]
    request = urllib.request.Request("http://localhost:8000/api/v1/runtime/capabilities",
                                     headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        capabilities = json.loads(response.read())
    assert "environment_registry" in capabilities and "stage_checks" in capabilities
    assert capabilities["stage_checks"]["V"]["ok"], capabilities["stage_checks"]["V"]
    assert capabilities["stage_checks"]["A"]["ok"], capabilities["stage_checks"]["A"]
    assert capabilities["stage_checks"]["B"]["ok"], capabilities["stage_checks"]["B"]
    assert capabilities["stage_checks"]["C"]["ok"], capabilities["stage_checks"]["C"]
    print("3. live capabilities 注册表与 A/B/C/V 阶段检查 [OK]")
finally:
    import shutil
    shutil.rmtree(root, ignore_errors=True)

print("\n=== 环境变量注册表与阶段检查冒烟通过 ===")
