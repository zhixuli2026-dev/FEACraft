"""G05：codex-wsl profile API 与真实 WSL 环境变量（不执行 Codex）。"""
import json
import subprocess
import urllib.request

B = "http://localhost:8000/api/v1"


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(request, data, timeout=30) as response:
        return json.loads(response.read())


token = call("POST", "/auth/login", {"email": "dev@vibeflow.cn", "password": "devpass123"})["token"]
profiles = call("GET", "/runtime/cli-profiles", token=token)
profile = profiles["codex-wsl"]
assert profile["shell"] == "wsl"
assert profile["env"] == {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "TERM": "xterm-256color"}, profile
exports = " && ".join(f"export {key}={value}" for key, value in profile["env"].items())
command = exports + " && printf 'UTF8=%s\\nENC=%s\\nTERM=%s\\n' \"$PYTHONUTF8\" \"$PYTHONIOENCODING\" \"$TERM\""
result = subprocess.run(["wsl.exe", "-d", "vibeflow-linux", "--exec", "bash", "-lc", command],
                        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
assert result.returncode == 0, result.stdout + result.stderr
assert "UTF8=1\nENC=utf-8\nTERM=xterm-256color" in result.stdout, result.stdout
print("codex-wsl API profile and real WSL UTF8/TERM environment: OK")
