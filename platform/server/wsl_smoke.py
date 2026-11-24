"""F24/F25：run_shell_command 的 Windows/WSL/PowerShell runtime 与危险命令底线。"""
import json
import time
import urllib.error
import urllib.request

from app.services.tools import ToolExecutor

B = "http://localhost:8000/api/v1"


def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
case = call("POST", "/workspaces/1/cases", {"name": f"wsl-tool-{time.time_ns()}"}, token=token)
prefix = case.get("s3_prefix") or f"workspaces/1/cases/{case['id']}"
executor = ToolExecutor(prefix)
wsl = executor.execute("run_shell_command", {
    "command": "source /usr/lib/openfoam/openfoam2406/etc/bashrc && printf 'WM_PROJECT_DIR=%s\\n' \"$WM_PROJECT_DIR\"",
    "runtime": "wsl", "timeout_seconds": 30,
})
assert wsl["ok"] and "WM_PROJECT_DIR=/usr/lib/openfoam/openfoam2406" in wsl["result"], wsl
assert "--allow-run-as-root" not in wsl["result"]
print("1. WSL v2406 source 后展开变量且工作目录有效 [OK]")

windows = executor.execute("run_shell_command", {"command": "echo WIN_OK", "runtime": "windows"})
assert windows["ok"] and "WIN_OK" in windows["result"], windows
print("2. 默认 Windows runtime 保持 [OK]")

powershell = executor.execute("run_shell_command", {
    "command": "Write-Output PS_OK", "runtime": "powershell", "timeout_seconds": 30,
})
assert powershell["ok"] and "exit=0" in powershell["result"] and "PS_OK" in powershell["result"], powershell
print("3. PowerShell -NoProfile -Command 实际执行 [OK]")

invalid = executor.execute("run_shell_command", {"command": "echo X", "runtime": "linux"})
assert not invalid["ok"] and "runtime" in invalid["result"], invalid
blocked = executor.execute("run_shell_command", {"command": "echo format C:", "runtime": "wsl"})
assert "拦截" in blocked["result"] and "高危命令" in blocked["result"], blocked
blocked_ps = executor.execute("run_shell_command", {"command": "echo format C:", "runtime": "powershell"})
assert "拦截" in blocked_ps["result"] and "高危命令" in blocked_ps["result"], blocked_ps
print("4. 非法 runtime 与各 runtime 危险模式 fail closed [OK]")
print("\n=== Windows/WSL/PowerShell runtime 冒烟通过 ===")
