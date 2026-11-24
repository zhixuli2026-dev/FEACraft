"""G11：客户端错误上报脱敏、限量、append-only 与管理员读取。"""
import json
import os
from pathlib import Path
import shutil
import tempfile
import urllib.error
import urllib.request

ROOT = Path(tempfile.mkdtemp(prefix="vibeflow-client-error-"))
os.environ["VIBEFLOW_CLIENT_ERROR_LOG"] = str(ROOT / "client-errors.jsonl")
from app.services.client_errors import recent_client_errors, report_client_error

try:
    entry = report_client_error({
        "message": "render failed Bearer test-secret-value",
        "stack": "x" * 9000,
        "url": "http://localhost/case",
        "user_agent": "test-agent",
    })
    assert "test-secret-value" not in entry["message"]
    assert len(entry["stack"]) == 8000
    recent = recent_client_errors(1)
    assert recent[0]["id"] == entry["id"] and recent[0]["schema"] == "vibeflow.client-error.v1"
    print("1. Bearer 脱敏、stack 上限与 append-only 回读 [OK]")

    def call(method, path, body=None, token=None, expected=200):
        request = urllib.request.Request("http://localhost:8000/api/v1" + path, method=method)
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        data = None
        if body is not None:
            request.add_header("Content-Type", "application/json")
            data = json.dumps(body).encode()
        try:
            with urllib.request.urlopen(request, data, timeout=30) as response:
                assert response.status == expected
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            payload = error.read().decode("utf-8", "replace")
            assert error.code == expected, (error.code, payload)
            return json.loads(payload)

    token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
    created = call("POST", "/runtime/client-error", {
        "message": "CLIENT_TEST_MARK Bearer test-secret-value", "stack": "browser stack"}, token=token)
    assert created["ok"]
    items = call("GET", "/runtime/client-errors?limit=5", token=token)["items"]
    assert any(item["message"].startswith("CLIENT_TEST_MARK") for item in items), items
    assert all("test-secret-value" not in item["message"] for item in items)
    call("POST", "/runtime/client-error", {"message": ""}, token=token, expected=400)
    print("2. live 上报、管理员读取、脱敏与空消息 400 [OK]")
finally:
    shutil.rmtree(ROOT, ignore_errors=True)

print("\n=== 客户端错误上报冒烟通过 ===")
