"""J18：API 安全响应头与 CORS 负例；nginx/uvicorn 日志策略由 compose_smoke 检查。"""
import json
import os
import urllib.error
import urllib.request

BASE = os.environ.get("VIBEFLOW_TEST_ORIGIN", "http://127.0.0.1:8000")


def request(path, method="GET", origin=None):
    headers = {}
    if origin:
        headers["Origin"] = origin
        if method == "OPTIONS":
            headers["Access-Control-Request-Method"] = "GET"
    req = urllib.request.Request(BASE + path, method=method, headers=headers)
    try:
        return urllib.request.urlopen(req, timeout=20)
    except urllib.error.HTTPError as error:
        return error


for path in ("/api/health", "/api/not-found"):
    response = request(path)
    assert response.headers.get("X-Content-Type-Options") == "nosniff", (path, response.headers)
    assert response.headers.get("X-Frame-Options") == "SAMEORIGIN", (path, response.headers)
    assert response.headers.get("Referrer-Policy") == "no-referrer", (path, response.headers)
    assert "geolocation=()" in response.headers.get("Permissions-Policy", ""), response.headers
    if path.startswith("/api/"):
        assert response.headers.get("Content-Security-Policy") == "default-src 'none'; frame-ancestors 'self'; base-uri 'none'", response.headers
    response.close()

allowed = request("/api/health", origin="http://localhost:5173")
assert allowed.headers.get("Access-Control-Allow-Origin") == "http://localhost:5173", allowed.headers
allowed.close()
for origin in ("https://attacker.invalid", "null"):
    response = request("/api/health", "OPTIONS", origin)
    assert response.headers.get("Access-Control-Allow-Origin") is None, (origin, response.headers)
    response.close()
print("J18 security headers on success/error and CORS negative origins: OK")
print("\n=== J18 API 安全头与 CORS 冒烟通过 ===")
