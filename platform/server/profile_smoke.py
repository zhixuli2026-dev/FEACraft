"""F11/F12：profile 编辑、连接测试、动态模型列表与失败回退。"""
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from app.routers.llm import redact_error

B = "http://localhost:8000/api/v1"
PORT = 8991


class ModelMock(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/v1/models":
            self.send_response(404)
            self.end_headers()
            return
        key = self.headers.get("Authorization") or self.headers.get("x-api-key") or ""
        if key.startswith("Bearer "):
            key = key[7:]
        if key != self.server.expected_key:
            self.send_response(401)
            self.end_headers()
            return
        models = [{"id": "model-a"}, {"id": "model-b"}] if self.server.provider == "custom" else [{"id": "claude-a"}]
        body = json.dumps({"data": models}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


mock = ThreadingHTTPServer(("127.0.0.1", PORT), ModelMock)
mock.expected_key = "mock-key"
mock.provider = "custom"
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
    case = call("POST", "/workspaces/1/cases", {"name": f"profile-{time.time_ns()}"}, token=token)
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "editable", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "model-a", "api_key": "mock-key"}, token=token)
    pid = profile["id"]
    models = call("GET", f"/llm/models?provider_profile_id={pid}", token=token)
    assert models.get("fallback") is False and [m["id"] for m in models["models"]] == ["model-a", "model-b"], models
    print("1. OpenAI 兼容动态模型列表 [OK]")

    updated = call("PATCH", f"/provider-profiles/{pid}",
                   {"name": "edited", "default_model": "model-b"}, token=token)
    assert updated.get("name") == "edited" and updated.get("default_model") == "model-b", updated
    assert updated.get("has_key") is True, updated
    tested = call("POST", f"/provider-profiles/{pid}:test", {}, token=token)
    assert tested.get("ok") is True and tested.get("model_count") == 2, tested
    print("2. 编辑保留密钥、连接测试 [OK]")

    mock.provider = "anthropic"
    anthropic = call("POST", "/provider-profiles", {
        "provider": "anthropic", "name": "anthropic-editable", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "claude-a", "api_key": "mock-key"}, token=token)
    models = call("GET", f"/llm/models?provider_profile_id={anthropic['id']}", token=token)
    assert models.get("fallback") is False and models["models"][0]["id"] == "claude-a", models
    tested = call("POST", f"/provider-profiles/{anthropic['id']}:test", {}, token=token)
    assert tested.get("ok") is True, tested
    print("3. Anthropic 动态模型与连接测试 [OK]")

    mock.provider = "custom"
    mock.expected_key = "new-key"
    changed = call("PATCH", f"/provider-profiles/{pid}", {"api_key": "new-key"}, token=token)
    assert changed.get("has_key") is True, changed
    tested = call("POST", f"/provider-profiles/{pid}:test", {}, token=token)
    assert tested.get("ok") is True, tested
    print("4. 更换密钥后使用新密钥连接 [OK]")

    failing = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "offline", "base_url": "http://127.0.0.1:1",
        "default_model": "offline-model", "api_key": "mock-key"}, token=token)
    fallback = call("GET", f"/llm/models?provider_profile_id={failing['id']}", token=token)
    assert fallback.get("fallback") is True and fallback.get("error"), fallback
    assert [m["id"] for m in fallback["models"]] == ["offline-model"], fallback
    failed_test = call("POST", f"/provider-profiles/{failing['id']}:test", {}, token=token)
    assert failed_test.get("_err") == 502, failed_test
    print("5. 离线模型列表回退、连接测试 502 [OK]")

    other = call("POST", "/auth/register", {"email": f"profile-other-{time.time_ns()}@vibeflow.cn",
                                            "name": "profile-other", "password": "other-pass-123"})
    for method, path in (("GET", f"/llm/models?provider_profile_id={pid}"),
                         ("PATCH", f"/provider-profiles/{pid}"),
                         ("POST", f"/provider-profiles/{pid}:test")):
        response = call(method, path, {}, token=other["token"])
        assert response.get("_err") == 404, (method, path, response)
    bad = call("POST", "/provider-profiles", {"provider": "custom", "name": "bad", "base_url": "",
                                              "default_model": "x", "api_key": "x"}, token=token)
    assert bad.get("_err") == 400, bad
    userinfo = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "userinfo", "base_url": "http://user:pass@127.0.0.1",
        "default_model": "x", "api_key": "x"}, token=token)
    assert userinfo.get("_err") == 400, userinfo
    remote_http = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "remote-http", "base_url": "http://example.com",
        "default_model": "x", "api_key": "x"}, token=token)
    assert remote_http.get("_err") == 400, remote_http
    secure = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "secure-example", "base_url": "https://example.com",
        "default_model": "x", "api_key": "x"}, token=token)
    assert secure.get("id"), secure
    deleted = call("DELETE", f"/provider-profiles/{secure['id']}", token=token)
    assert deleted.get("ok") is True, deleted
    masked = redact_error(RuntimeError("bad secret-token"), "secret-token")
    assert "secret-token" not in masked and "***" in masked, masked
    print("6. 跨用户 404、custom 空 URL/URL 内嵌凭据 400 [OK]")
    print("7. 非回环 HTTP 拒绝、HTTPS 允许、错误脱敏 [OK]")
    print("\n=== Profile 编辑/连接测试/动态模型冒烟通过 ===")
finally:
    mock.shutdown()
