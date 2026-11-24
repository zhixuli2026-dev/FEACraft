"""I17/I20：SCNET HMAC、地址、错误/列表、诊断、链接和 DPAPI golden。"""
import hashlib
import hmac
import json
from pathlib import Path
import tempfile

from app.services.scnet import (compact_sign, dpapi_protect, dpapi_unprotect,
    header_diagnostics, parse_error, parse_list, redact, safe_link, service_url, SessionManager)

payload = {"accessKey": "ak", "expireTime": 123, "nonce": "n"}
expected = hmac.new(b"sk", b'{"accessKey":"ak","expireTime":123,"nonce":"n"}', hashlib.sha256).hexdigest()
assert compact_sign(payload, "sk") == expected
assert service_url("https://cancon.example:65016", "hpc") == "https://cancon.example:65016/hpc"
assert service_url("https://cancon.example:65016/", "ai") == "https://cancon.example:65016/ai"
assert service_url("https://cancon.example/hpc", "hpc") == "https://cancon.example/hpc"
assert service_url("https://cancon.example/custom", "hpc") == "https://cancon.example/custom"
assert service_url("https://api.scnet.cn/api/user", "platform") == "https://api.scnet.cn/api/user"
for bad in ("http://cancon.example", "https://user:pass@example.com", "https://example.com?token=x"):
    try:
        service_url(bad, "hpc")
    except ValueError:
        pass
    else:
        raise AssertionError(bad)
assert service_url("http://127.0.0.1:9129", "hpc") == "http://127.0.0.1:9129/hpc"
assert parse_list({"data": {"records": [{"id": 1}]}}) == [{"id": 1}]
assert parse_list({"fileList": []}) == []
try:
    parse_list({"unexpected": []})
except ValueError:
    pass
else:
    raise AssertionError("未知列表应拒绝")
error = parse_error(400, "GET", "https://example.com/hpc/openapi/v2/cluster?token=secret-token",
                    b'{"code":10009,"message":"token is invalid"}', ["secret-token"])
assert error["code"] == 10009 and "token" not in error["url"]
assert "secret-token" not in error["message"] and error["hint"]
nested = parse_error(431, "POST", "http://127.0.0.1/x", b'{"error":{"msg":"too large"}}')
assert nested["message"] == "too large" and nested["hint"]
text = parse_error(500, "GET", "http://127.0.0.1/x", b"Nginx Request Header Or Cookie Too Large")
assert "Nginx" in text["message"]
diag = header_diagnostics({"token": "abc", "X-Test": "12345"})
assert diag["token_bytes"] == 3 and diag["total_bytes"] == 27, diag
assert redact("token=secret-token", ["secret-token"]) == "token=***"
assert safe_link("https://example.com/hpc", "/novnc?token=temporary") == "https://example.com/novnc?token=temporary"
try:
    safe_link("https://example.com", "javascript:alert(1)")
except ValueError:
    pass
else:
    raise AssertionError("javascript link")
protected = dpapi_protect(b"test-secret")
assert "test-secret" not in protected and dpapi_unprotect(protected) == b"test-secret"
with tempfile.TemporaryDirectory(prefix="vf-scnet-session-") as temp:
    preference = Path(temp) / "session.bin"
    session = SessionManager()
    generation = session.login("ak", "sk", "token-a", "region-a")
    assert session.valid(generation)
    old = session.switch_region("region-b", "token-b")
    assert not session.valid(generation) and session.valid(old)
    session.save(preference, True)
    assert "secret" not in preference.read_text() and "token-b" not in preference.read_text()
    restored = SessionManager()
    assert restored.auto_connect(preference) and restored.region == "region-b"
    restored.logout(preference)
    assert not restored.valid(old) and not preference.exists()
    assert not SessionManager().auto_connect(preference)
print("I17/I20 HMAC, endpoint, list/error, header diagnostics, redaction, links and DPAPI: OK")
print("\n=== SCNET 协议与 DPAPI 冒烟通过 ===")
