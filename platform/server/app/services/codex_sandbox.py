"""Codex Windows 沙盒状态与显式初始化会话。"""
from __future__ import annotations

from .codex_stdio import CodexStdioClient


def readiness_value(value):
    """Project only the installed app-server readiness contract, never diagnostics.

    The response reports readiness for the active Codex configuration.
    MXC availability and effective execution boundaries are checked separately.
    """
    if (not isinstance(value, dict) or not isinstance(value.get('status'), str)
            or value['status'] not in {'ready', 'notConfigured', 'updateRequired'}):
        raise ValueError('Invalid Windows sandbox readiness response')
    return {'status': value['status']}


class CodexSandboxSession:
    def __init__(self, client: CodexStdioClient):
        self.client = client
        self.status = "idle"
        self.mode: str | None = None
        self.error: str | None = None

    async def readiness(self) -> dict:
        return readiness_value(await self.client.windows_sandbox_readiness())

    async def setup(self, mode: str, cwd: str | None = None, *,
                    confirmed: bool = False) -> dict:
        if self.status == "running":
            raise RuntimeError("已有 Windows 沙盒初始化正在执行")
        result = await self.client.windows_sandbox_setup(mode, cwd, confirmed=confirmed)
        if result.get("started") is not True:
            raise RuntimeError("Windows 沙盒初始化未启动")
        self.mode = mode
        self.status = "running"
        self.error = None
        return result

    def handle_event(self, event: dict) -> bool:
        if self.status != "running" or event.get("method") != "windowsSandbox/setupCompleted":
            return False
        params = event.get("params", {})
        if params.get("mode") != self.mode:
            return False
        if params.get("success") is True:
            self.status = "ready"
            self.error = None
        else:
            self.status = "failed"
            self.error = str(params.get("error") or "沙盒初始化失败")
        return True
