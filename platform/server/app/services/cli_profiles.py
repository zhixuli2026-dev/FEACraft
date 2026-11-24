"""CLI 兼容配置档；只描述环境，不执行 CLI 或读取凭据。"""

CODEX_WSL_ENV = {
    "PYTHONUTF8": "1",
    "PYTHONIOENCODING": "utf-8",
    "TERM": "xterm-256color",
}


def cli_profiles() -> dict:
    return {"codex-wsl": {"shell": "wsl", "env": dict(CODEX_WSL_ENV)}}
