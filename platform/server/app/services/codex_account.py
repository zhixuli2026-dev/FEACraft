"""Codex 登录状态机与环境展示缓存；不读取或保存认证令牌。"""
from __future__ import annotations

import copy
import time

from .codex_stdio import CodexStdioClient


class CodexLoginSession:
    def __init__(self, client: CodexStdioClient):
        self.client = client
        self.login_id: str | None = None
        self.status = "idle"
        self.auth_url: str | None = None
        self.verification_url: str | None = None
        self.user_code: str | None = None
        self.error: str | None = None

    async def start_browser(self) -> dict:
        result = await self.client.start_browser_login()
        if result.get("type") != "chatgpt" or not result.get("loginId") or not result.get("authUrl"):
            raise RuntimeError("浏览器登录响应缺少 loginId/authUrl")
        self.login_id = result["loginId"]
        self.auth_url = result["authUrl"]
        self.verification_url = None
        self.user_code = None
        self.status = "waiting"
        self.error = None
        return {"loginId": self.login_id, "authUrl": self.auth_url}

    async def start_device_code(self) -> dict:
        result = await self.client.start_device_code_login()
        if (result.get("type") != "chatgptDeviceCode" or not result.get("loginId") or
                not result.get("verificationUrl") or not result.get("userCode")):
            raise RuntimeError("设备码登录响应缺少 loginId/verificationUrl/userCode")
        self.login_id = result["loginId"]
        self.auth_url = None
        self.verification_url = result["verificationUrl"]
        self.user_code = result["userCode"]
        self.status = "waiting"
        self.error = None
        return {"loginId": self.login_id, "verificationUrl": self.verification_url,
                "userCode": self.user_code}

    def handle_event(self, event: dict) -> bool:
        if self.status != "waiting" or event.get("method") != "account/login/completed":
            return False
        params = event.get("params", {})
        if params.get("loginId") != self.login_id:
            return False
        if params.get("success") is True:
            self.status = "completed"
            self.error = None
        else:
            self.status = "failed"
            self.error = str(params.get("error") or "登录失败")
        self.auth_url = None
        self.verification_url = None
        self.user_code = None
        return True

    async def cancel(self) -> dict:
        if not self.login_id:
            raise ValueError("当前没有可取消的登录")
        result = await self.client.cancel_login(self.login_id)
        self.status = "cancelled"
        self.auth_url = None
        self.verification_url = None
        self.user_code = None
        return result


class CodexDisplayCache:
    ALLOWED_FIELDS = {"account", "rate_limits", "models", "program"}
    MAX_ENTRIES = 20

    def __init__(self):
        self._entries: dict[tuple[str, str, str], dict] = {}

    @staticmethod
    def key(runtime: str, program_path: str, distro: str = "") -> tuple[str, str, str]:
        return runtime, program_path, distro

    def read(self, key: tuple[str, str, str]) -> dict | None:
        entry = self._entries.get(key)
        return copy.deepcopy(entry) if entry is not None else None

    def update_success(self, key: tuple[str, str, str], **fields) -> None:
        if any(field not in self.ALLOWED_FIELDS for field in fields):
            raise ValueError("展示缓存包含不允许的字段")
        entry = self._entries.setdefault(key, {"errors": {}})
        entry.update(copy.deepcopy(fields))
        entry["cached_at"] = time.time()
        for field in fields:
            entry["errors"].pop(field, None)
        while len(self._entries) > self.MAX_ENTRIES:
            oldest = min(self._entries, key=lambda item: self._entries[item].get("cached_at", 0))
            del self._entries[oldest]

    def update_error(self, key: tuple[str, str, str], field: str, message: str) -> None:
        if field not in self.ALLOWED_FIELDS:
            raise ValueError("未知展示缓存字段")
        entry = self._entries.setdefault(key, {"errors": {}})
        entry["errors"][field] = message[:500]
        entry.setdefault("cached_at", time.time())
        while len(self._entries) > self.MAX_ENTRIES:
            oldest = min(self._entries, key=lambda item: self._entries[item].get("cached_at", 0))
            del self._entries[oldest]

    def clear(self, key: tuple[str, str, str]) -> None:
        self._entries.pop(key, None)
