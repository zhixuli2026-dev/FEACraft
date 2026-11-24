"""H02B：独立 uvicorn 真实停止/重启后，running 孤儿作业恢复为 interrupted。

只使用本次临时 SQLite、临时存储与空闲端口，不操作开发 PostgreSQL。
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(tempfile.mkdtemp(prefix="vibeflow-job-recovery-"))
SERVER_ROOT = Path(__file__).resolve().parent
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
BASE = f"http://127.0.0.1:{port}"
ENV = os.environ.copy()
ENV.update({
    "VIBEFLOW_DATABASE_URL": f"sqlite+aiosqlite:///{(ROOT / 'recovery.db').as_posix()}",
    "VIBEFLOW_STORAGE_BACKEND": "fs",
    "VIBEFLOW_STORAGE_FS_ROOT": str(ROOT / "files"),
    "VIBEFLOW_JWT_SECRET": "job-recovery-test-only-secret-32-bytes",
})
servers = []


def call(method, path, body=None, token=None):
    request = urllib.request.Request(BASE + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request, data, timeout=10) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise AssertionError(f"{method} {path} → {error.code}: {error.read().decode('utf-8', 'replace')}") from error


def start_server():
    process = subprocess.Popen([
        sys.executable, "-m", "uvicorn", "app.main:app",
        "--host", "127.0.0.1", "--port", str(port),
    ], cwd=SERVER_ROOT, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
       text=True, encoding="utf-8", errors="replace")
    servers.append(process)
    for _ in range(150):
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise AssertionError(f"独立服务启动失败 rc={process.returncode}: {output[-2000:]}")
        try:
            with urllib.request.urlopen(BASE + "/api/health", timeout=1) as response:
                if response.status == 200:
                    return process
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise AssertionError("独立服务健康检查超时")


def stop_server(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if process.stdout:
        process.stdout.close()


try:
    first = start_server()
    token = call("POST", "/api/v1/auth/register", {
        "email": "recovery@vibeflow.cn", "name": "recovery", "password": "recovery-pass-123",
    })["token"]
    workspace = call("POST", "/api/v1/workspaces", {"name": "recovery"}, token=token)
    case = call("POST", f"/api/v1/workspaces/{workspace['id']}/cases", {"name": "recovery"}, token=token)
    job = call("POST", f"/api/v1/cases/{case['id']}/jobs", {
        "type": "shell", "runtime": "windows", "command": "ping -n 6 127.0.0.1 >nul",
    }, token=token)
    state = job
    for _ in range(50):
        state = call("GET", f"/api/v1/jobs/{job['id']}", token=token)
        if state["status"] == "running":
            break
        time.sleep(0.1)
    assert state["status"] == "running", state

    stop_server(first)
    second = start_server()
    recovered = call("GET", f"/api/v1/jobs/{job['id']}", token=token)
    assert recovered["status"] == "interrupted", recovered
    assert recovered["exit_code"] is None, recovered
    print(f"Real uvicorn restart orphan recovery: OK (job={job['id']}, running→interrupted)")
    stop_server(second)
finally:
    for process in servers:
        stop_server(process)
    shutil.rmtree(ROOT, ignore_errors=True)
