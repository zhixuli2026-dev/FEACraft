"""G13：fs 存储真实写读删探测与 live capabilities。"""
import json
from pathlib import Path
import shutil
import tempfile
import urllib.request

from app.config import Settings
from app.routers.runtime import _storage_probe

root = Path(tempfile.mkdtemp(prefix="vibeflow-storage-probe-"))
try:
    result = _storage_probe(Settings(storage_backend="fs", storage_fs_root=str(root)))
    assert result["ok"], result
    assert not list((root / ".vibeflow" / "capabilities").glob("probe-*.tmp"))
    print("1. 临时 fs 写读删成功且 probe 文件清理 [OK]")

    not_a_directory = root / "not-a-directory"
    not_a_directory.write_text("x", encoding="utf-8")
    failed = _storage_probe(Settings(storage_backend="fs", storage_fs_root=str(not_a_directory)))
    assert not failed["ok"] and "error" in failed, failed
    print("2. 非目录存储根返回错误而非 ok(true) [OK]")

    request = urllib.request.Request("http://localhost:8000/api/v1/auth/login",
        data=json.dumps({"email": "dev@vibeflow.cn", "password": "devpass123"}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        token = json.loads(response.read())["token"]
    request = urllib.request.Request("http://localhost:8000/api/v1/runtime/capabilities",
                                     headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        capabilities = json.loads(response.read())
    assert capabilities["storage"]["ok"] and capabilities["storage"]["backend"] == "fs", capabilities["storage"]
    assert capabilities["storage"]["latency_ms"] >= 0
    print("3. live capabilities 存储真实连通性 [OK]")
finally:
    shutil.rmtree(root, ignore_errors=True)

print("\n=== 存储连通性冒烟通过 ===")
