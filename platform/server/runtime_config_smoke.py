"""G09：管理员 Setup 持久化、路径约束、重置与 live 配置刷新。"""
import json
import os
from pathlib import Path
import shutil
import tempfile
import urllib.error
import urllib.request

ROOT = Path(tempfile.mkdtemp(prefix="vibeflow-runtime-setup-"))
os.environ["VIBEFLOW_RUNTIME_CONFIG"] = str(ROOT / "runtime-config.json")
from app.services.runtime_config import get_runtime_settings, update_runtime_settings

try:
    fake_python = ROOT / "python.exe"
    fake_python.write_text("mock", encoding="utf-8")
    updated = update_runtime_settings({"python": str(fake_python), "wsl_distro": "vibeflow-linux"})
    assert updated["python"] == str(fake_python)
    assert get_runtime_settings()["python"] == str(fake_python)
    print("1. 合法 Setup 原子持久化与重读 [OK]")

    for patch in ({"wsl_distro": "unauthorized-distro"},
                  {"openfoam_bashrc": "/tmp/malicious.sh"},
                  {"python": str(ROOT / "missing.exe")}):
        try:
            update_runtime_settings(patch)
            raise AssertionError(f"非法 Setup 被接受: {patch}")
        except ValueError:
            pass
    assert get_runtime_settings()["python"] == str(fake_python)
    print("2. 非授权发行版/非官方 bashrc/不存在文件均拒绝且不修改配置 [OK]")

    update_runtime_settings({"python": ""})
    assert get_runtime_settings()["python"] != str(fake_python)
    print("3. 空值恢复默认路径 [OK]")

    def call(method, path, body=None, token=None):
        request = urllib.request.Request("http://localhost:8000/api/v1" + path, method=method)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        data = None
        if body is not None:
            request.add_header("Content-Type", "application/json")
            data = json.dumps(body).encode()
        with urllib.request.urlopen(request, data, timeout=60) as response:
            return json.loads(response.read())

    token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
    current = call("GET", "/runtime/config", token=token)
    for key in ("python", "pandoc", "wsl_distro", "openfoam_bashrc"):
        assert key in current, (key, current)
    updated_live = call("PATCH", "/runtime/config", current, token=token)
    assert updated_live == current, (updated_live, current)
    capabilities = call("GET", "/runtime/capabilities", token=token)
    assert capabilities["storage"]["ok"] and capabilities["wsl"]["openfoam"], capabilities["wsl"]
    print("4. admin 原值 PATCH 与 capabilities 重新探测 [OK]")
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print("\n=== 管理员运行时 Setup 冒烟通过 ===")
