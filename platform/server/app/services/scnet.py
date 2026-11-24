"""SCNET 协议、脱敏与可选 Windows DPAPI 工具。"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from .redaction import redact_text


REDIRECT_STATUSES = {301, 302, 303, 307, 308}
MAX_REDIRECTS = 3


def compact_sign(payload: dict, secret: str) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()


def _port(parts):
    try:
        return parts.port
    except ValueError:
        raise ValueError('服务端口无效') from None


def service_url(root: str, kind: str) -> str:
    if kind not in {"hpc", "ai", "platform", "files"}:
        raise ValueError("未知服务类型")
    root = root.strip()
    if '\\' in root or any(ord(char) < 32 for char in root):
        raise ValueError('服务地址包含无效字符')
    parts = urlsplit(root)
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ValueError("服务地址不能包含凭据/query/fragment")
    host = parts.hostname
    if not host or any(char.isspace() for char in host) or _port(parts) == 0:
        raise ValueError('服务地址需要有效主机名和端口')
    local = host in {"localhost", "127.0.0.1", "::1"}
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise ValueError("服务发现只接受 HTTPS（本机测试可用回环 HTTP）")
    path = parts.path.rstrip("/")
    if kind in {"hpc", "ai"} and not path:
        path = "/" + kind
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def parse_list(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "list", "fileList", "records"):
            if key in data:
                return parse_list(data[key])
    raise ValueError("未知列表结构")


def redact(value, secrets=()):
    original = str(value)
    text = redact_text(original, secrets, limit=len(original.encode('utf-8')) * 3)
    return re.sub(r'''(?i)(\b(?:password|authorization|access[_-]?key|signature)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;&}\]]+)''',
                  r"\1***", text)


def parse_error(status: int, method: str, url: str, body: bytes, secrets=()) -> dict:
    parts = urlsplit(str(url))
    clean_url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            error = data.get('error')
            nested = error if isinstance(error, dict) else {}
            message = data.get('msg') or data.get('message') or nested.get('msg') or nested.get('message') or error or data
            code = data.get('code', nested.get('code'))
        else:
            message, code = data, None
    except (ValueError, UnicodeDecodeError):
        message, code = body.decode("utf-8", "replace")[:1000], None
    if isinstance(message, (dict, list)):
        message = json.dumps(message, ensure_ascii=False)
    code = redact(code, secrets)[:1000] if isinstance(code, (str, dict, list)) else code
    return {"method": method, "url": clean_url, "status": status, "code": code,
            "message": redact(message, secrets)[:1000],
            "hint": "请求头或 Cookie 过大；请检查网关与请求字段，不能据此断定 token 过大" if status in {400, 431, 494} else ""}


def header_diagnostics(headers: dict) -> dict:
    lengths = {key: len(key.encode()) + 2 + len(str(value).encode()) + 2
               for key, value in headers.items()}
    token = next((value for key, value in headers.items() if key.lower() == 'token'), '')
    return {"token_bytes": len(str(token).encode("utf-8")),
            "total_bytes": sum(lengths.values()), "field_lengths": lengths}


def safe_link(base: str, link: str) -> str:
    if link.startswith(("http://", "https://")):
        candidate = link
    elif link.startswith("/"):
        parts = urlsplit(base)
        candidate = urlunsplit((parts.scheme, parts.netloc, link, "", ""))
    else:
        raise ValueError("访问链接必须是绝对 HTTP(S) 或根相对路径")
    parts = urlsplit(candidate)
    if (parts.scheme not in {'http', 'https'} or not parts.hostname or _port(parts) == 0
            or parts.username is not None or parts.password is not None or '\\' in candidate
            or any(ord(char) < 32 for char in candidate)):
        raise ValueError("访问链接协议或凭据无效")
    return candidate


def safe_redirect(current: str, target: str) -> str:
    if not target or '\\' in target or any(ord(char) < 32 for char in target):
        raise ValueError('重定向地址无效')
    resolved = safe_link(current, urljoin(current, target))
    source, dest = urlsplit(current), urlsplit(resolved)
    def origin(parts):
        return parts.scheme, parts.hostname, _port(parts) or (443 if parts.scheme == 'https' else 80)

    if origin(source) != origin(dest):
        raise ValueError("只允许同源重定向")
    if dest.scheme != "https" and dest.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("重定向只接受 HTTPS")
    return resolved


def require_confirmation(operation: str, confirmed: bool):
    mutating = ("jobs.submit", "jobs.cancel", "files.mkdir", "files.upload", "files.rename",
                "files.copy", "files.move", "files.delete", "containers.create", "containers.start",
                "containers.restart", "containers.stop", "containers.delete", "containers.exec",
                "notebooks.create", "notebooks.start", "notebooks.stop", "notebooks.release",
                "notebooks.rename", "desktops.create", "desktops.delete")
    if operation in mutating and not confirmed:
        raise ValueError("变更/删除/付费资源操作需要 confirmed=true；取消请求不代表远端回滚")


# Windows 当前用户 DPAPI；返回 Base64，便于私有文件保存，非 KMS。
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


_CRYPTPROTECT_UI_FORBIDDEN = 0x1

def _blob(data: bytes):
    buf = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    return buf, _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))


def dpapi_protect(data: bytes) -> str:
    if os.name != "nt":
        raise ValueError("DPAPI 仅支持 Windows 当前用户")
    source, source_blob = _blob(data)
    output = _Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source_blob), None, None, None, None,
                                                _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output)):
        raise OSError("CryptProtectData 失败")
    try:
        return base64.b64encode(ctypes.string_at(output.pbData, output.cbData)).decode()
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


def dpapi_unprotect(encoded: str) -> bytes:
    if os.name != "nt":
        raise ValueError("DPAPI 仅支持 Windows 当前用户")
    source, source_blob = _blob(base64.b64decode(encoded, validate=True))
    output = _Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source_blob), None, None, None, None,
                                                  _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(output)):
        raise OSError("CryptUnprotectData 失败")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output.pbData)


class SessionManager:
    """单进程 SCNET 会话；generation 用于使旧区域/注销前回调失效。"""
    def __init__(self):
        self.credentials = None
        self.token = None
        self.region = None
        self.generation = 0
        self.logged_out = False

    def login(self, access_key: str, secret_key: str, token: str, region: str):
        self.credentials = {"access_key": access_key, "secret_key": secret_key}
        self.token, self.region = token, region
        self.logged_out = False
        self.generation += 1
        return self.generation

    def switch_region(self, region: str, token: str):
        self.region, self.token = region, token
        self.generation += 1
        return self.generation

    def valid(self, generation: int) -> bool:
        return generation == self.generation and not self.logged_out and self.token is not None

    def logout(self, preference: Path | None = None):
        self.credentials = self.token = self.region = None
        self.logged_out = True
        self.generation += 1
        if preference and preference.exists():
            preference.unlink()

    def save(self, preference: Path, auto_connect: bool):
        if not auto_connect or not self.credentials:
            if preference.exists():
                preference.unlink()
            return
        data = dict(self.credentials, token=self.token, region=self.region)
        preference.parent.mkdir(parents=True, exist_ok=True)
        preference.write_text(dpapi_protect(json.dumps(data).encode()), encoding="ascii")

    def auto_connect(self, preference: Path):
        if self.logged_out or not preference.is_file():
            return False
        try:
            data = json.loads(dpapi_unprotect(preference.read_text(encoding="ascii")))
            self.login(data["access_key"], data["secret_key"], data["token"], data["region"])
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False
