"""SSRF-safe web_fetch：公网 DNS 固定连接、重定向复查、TLS 校验与有界读取。"""
from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import ipaddress
import socket

import aiohttp
from yarl import URL

MAX_FETCH_BYTES = 8 * 1024 * 1024
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
ALLOWED_CONTENT_TYPES = {"application/json", "application/xml", "application/xhtml+xml"}


class FetchSecurityError(ValueError):
    pass


def _require_public(ip: str):
    address = ipaddress.ip_address(ip)
    if not address.is_global or address.is_multicast or address.is_reserved:
        raise FetchSecurityError(f"目标是非公网地址 {address}，已拒绝")


def _validate_url(value: str) -> URL:
    try:
        url = URL(value)
        if url.scheme not in ("http", "https") or not url.host:
            raise FetchSecurityError("仅允许绝对 http/https URL")
        if url.user is not None or url.password is not None:
            raise FetchSecurityError("URL 不得包含凭据")
        _ = url.port  # 让 yarl 对非法端口立即报错
        try:
            _require_public(url.host)
        except ValueError as error:
            if isinstance(error, FetchSecurityError):
                raise
        return url
    except FetchSecurityError:
        raise
    except (ValueError, TypeError) as error:
        raise FetchSecurityError(f"URL 无效: {error}") from error


class PublicOnlyResolver(aiohttp.abc.AbstractResolver):
    """任一解析地址非公网即拒绝；连接只使用这里返回的固定 IP。"""

    def __init__(self, loop=None):
        self._loop = loop

    async def close(self):
        pass

    async def resolve(self, host: str, port: int = 0,
                      family: socket.AddressFamily = socket.AF_UNSPEC) -> list[dict]:
        loop = self._loop or asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM, family=family)
        if not infos:
            raise FetchSecurityError("DNS 未返回地址")
        resolved = []
        for info in infos:
            ip = info[4][0]
            _require_public(ip)
            resolved.append({"hostname": host, "host": ip, "port": info[4][1],
                             "family": info[0], "proto": info[2],
                             "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV})
        return resolved


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "template") or self._ignored:
            self._ignored += 1

    def handle_endtag(self, tag):
        if self._ignored:
            self._ignored -= 1

    def handle_data(self, data):
        if not self._ignored and data.strip():
            self.parts.append(data.strip())


def _visible_text(body: bytes, content_type: str, charset: str | None) -> str:
    try:
        text = body.decode(charset or "utf-8", "replace")
    except LookupError:
        text = body.decode("utf-8", "replace")
    if content_type not in ("text/html", "application/xhtml+xml"):
        return text
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()
    return "\n".join(parser.parts)


async def safe_web_fetch(url: str, resolver=None, timeout_seconds: float = 15,
                         max_redirects: int = 3) -> dict:
    """返回可直接作为工具结果的字典；所有网络失败均转为显式 error。"""
    current = url
    connector = aiohttp.TCPConnector(resolver=resolver or PublicOnlyResolver(),
                                     use_dns_cache=False, enable_cleanup_closed=True)
    timeout = aiohttp.ClientTimeout(total=timeout_seconds,
                                    sock_connect=min(10, timeout_seconds),
                                    sock_read=min(10, timeout_seconds))
    try:
        async with asyncio.timeout(timeout_seconds), aiohttp.ClientSession(
            connector=connector, timeout=timeout, trust_env=False,
            auto_decompress=False,
            cookie_jar=aiohttp.DummyCookieJar(),
            headers={"User-Agent": "VibeFlow-WebFetch/1.0",
                     "Accept": "text/html,text/plain,application/json,application/xml;q=0.9,*/*;q=0.1",
                     "Accept-Encoding": "identity"},
        ) as session:
            for redirect_index in range(max_redirects + 1):
                parsed = _validate_url(current)
                async with session.get(parsed, allow_redirects=False, ssl=True) as response:
                    if response.status in REDIRECT_STATUSES:
                        location = response.headers.get("Location")
                        if not location or redirect_index >= max_redirects:
                            return {"ok": False, "status": "error", "url": str(parsed),
                                    "result": "重定向缺少 Location 或超过次数上限"}
                        current = str(parsed.join(URL(location)))
                        continue
                    if response.status < 200 or response.status >= 300:
                        return {"ok": False, "status": "error", "url": str(parsed),
                                "status_code": response.status,
                                "result": f"web_fetch HTTP {response.status}"}
                    content_type = response.content_type.lower()
                    if not content_type.startswith("text/") and content_type not in ALLOWED_CONTENT_TYPES:
                        return {"ok": False, "status": "error", "url": str(parsed),
                                "result": f"不支持的内容类型: {content_type}"}
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        return {"ok": False, "status": "error", "url": str(parsed),
                                "result": "不支持压缩响应，目标需允许 identity 编码"}
                    if response.content_length is not None and response.content_length > MAX_FETCH_BYTES:
                        return {"ok": False, "status": "error", "url": str(parsed),
                                "result": "响应超过 8 MiB 上限"}
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        if len(body) + len(chunk) > MAX_FETCH_BYTES:
                            return {"ok": False, "status": "error", "url": str(parsed),
                                    "result": "响应超过 8 MiB 上限"}
                        body.extend(chunk)
                    text = _visible_text(bytes(body), content_type, response.charset)
                    return {"ok": True, "status": "completed", "url": str(parsed),
                            "status_code": response.status,
                            "result": f"[web_fetch] {parsed} · HTTP {response.status}\n{text}"}
            return {"ok": False, "status": "error", "url": current, "result": "重定向次数超过上限"}
    except FetchSecurityError as error:
        return {"ok": False, "status": "error", "url": current, "result": str(error)}
    except (asyncio.TimeoutError, TimeoutError):
        return {"ok": False, "status": "error", "url": current, "result": "web_fetch 请求超时"}
    except (aiohttp.ClientError, OSError, ValueError) as error:
        return {"ok": False, "status": "error", "url": current,
                "result": f"web_fetch 失败: {type(error).__name__}: {error}"}


async def cancellable_web_fetch(url: str, cancel_event: asyncio.Event,
                                timeout_seconds: float = 15, fetcher=None) -> dict:
    task = asyncio.create_task((fetcher or safe_web_fetch)(url, timeout_seconds=timeout_seconds))
    cancel_wait = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({task, cancel_wait}, return_when=asyncio.FIRST_COMPLETED)
        if cancel_wait in done:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            return {"ok": False, "status": "cancelled", "url": url,
                    "result": "web_fetch 已取消"}
        return task.result()
    finally:
        task.cancel()
        cancel_wait.cancel()
        await asyncio.gather(task, cancel_wait, return_exceptions=True)
