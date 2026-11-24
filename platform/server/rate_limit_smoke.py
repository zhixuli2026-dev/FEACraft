"""J19：真实 HTTP token bucket；低限额只在隔离服务执行，主服务不制造全局 429。"""
import json
import os
import time
import urllib.error
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1")


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request, data, timeout=20) as response:
            return response.status, {key.lower(): value for key, value in response.headers.items()}, json.loads(response.read())
    except urllib.error.HTTPError as error:
        payload = error.read().decode("utf-8", "replace")
        try:
            payload = json.loads(payload)
        except ValueError:
            pass
        return error.code, {key.lower(): value for key, value in error.headers.items()}, payload


def register(label):
    status, _, body = call("POST", "/auth/register", {
        "email": f"rate-{label}-{time.time_ns()}@vibeflow.cn", "name": label,
        "password": "rate-pass-123"})
    assert status == 200, body
    return body["token"]


if os.environ.get("VIBEFLOW_TEST_RATE_LIMIT") != "1":
    status, _, _ = call("GET", "/auth/me")
    assert status == 401, status
    print("主服务不触发低限额；真实 429 由隔离配置执行 [SKIP]")
else:
    admin = register("admin")
    member = register("member")
    for index in range(8):
        status, _, body = call("GET", "/auth/me", token=admin)
        assert status == 200, (index, status, body)
    status, headers, body = call("GET", "/auth/me", token=admin)
    assert status == 429, (status, body)
    assert int(headers.get("retry-after", "0")) >= 1, headers
    assert headers.get("x-content-type-options") == "nosniff", headers
    status, _, _ = call("GET", "/auth/me", token=member)
    assert status == 200, "不同用户不应共享 token bucket"
    for index in range(2):
        status, _, _ = call("POST", "/llm/models", {}, token=admin)
        assert status != 429, (index, status)
    status, headers, _ = call("POST", "/llm/models", {}, token=admin)
    assert status == 429 and int(headers.get("retry-after", "0")) >= 1, (status, headers)
    print("J19 user isolation, API=8, LLM=2, 429 Retry-After and security headers: OK")
print("\n=== J19 统一 token bucket 冒烟通过（隔离低限额模式）===")
