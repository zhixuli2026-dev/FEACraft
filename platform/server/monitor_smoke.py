"""J20：真实运行时监控与 Prometheus 指标。"""
import json
import os
import time
import urllib.error
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1")


def call(method, path, body=None, token=None, raw=False):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, data, timeout=30) as response:
            payload = response.read()
            return response.status, payload if raw else json.loads(payload)
    except urllib.error.HTTPError as error:
        return error.code, error.read()


status, auth = call("POST", "/auth/register", {
    "email": f"monitor-{time.time_ns()}@vibeflow.cn", "name": "monitor", "password": "monitor-pass-123"})
assert status == 200, auth
token = auth["token"]
assert call("GET", "/runtime/monitor")[0] == 401
status, monitor = call("GET", "/runtime/monitor", token=token)
assert status == 200, monitor
assert monitor["schema"] == "vibeflow.runtime-monitor.v1", monitor
assert monitor["database"]["ok"] is True and monitor["database"]["latency_ms"] >= 0, monitor
assert monitor["storage"]["ok"] is True and monitor["storage"]["latency_ms"] >= 0, monitor
assert isinstance(monitor["jobs"], dict) and isinstance(monitor["processes"], dict), monitor
assert monitor["rate_limit"]["backend"] == "memory", monitor
assert isinstance(monitor["alerts"], list), monitor
status, metrics = call("GET", "/runtime/metrics", token=token, raw=True)
text = metrics.decode()
assert status == 200, text
for marker in ("vibeflow_database_up 1", "vibeflow_storage_up 1",
               "vibeflow_database_latency_ms", "vibeflow_jobs_total", "vibeflow_rate_limit_buckets"):
    assert marker in text, (marker, text)
print("J20 admin monitor, real DB/storage probes, jobs/process/rate metrics and Prometheus: OK")
print("\n=== J20 运行时监控与指标冒烟通过 ===")
