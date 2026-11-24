"""F21：web_fetch SSRF、重定向、固定 IP、8 MiB 与超时边界。"""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
import urllib.request

from app.services.webfetch import FetchSecurityError, PublicOnlyResolver, cancellable_web_fetch, safe_web_fetch

PORT = 9010
MAX = 8 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        type(self).requests.append((self.path, self.headers.get("Host")))
        if self.path == "/html":
            body = "<html><head><style>x{}</style><script>SECRET_SCRIPT</script></head><body><h1>VISIBLE_TEXT</h1><p>正文</p></body></html>".encode()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path == "/redirect":
            self.send_response(302); self.send_header("Location", "/html"); body = b""
        elif self.path == "/redirect-private":
            self.send_response(302); self.send_header("Location", f"http://127.0.0.1:{PORT}/html"); body = b""
        elif self.path == "/loop":
            self.send_response(302); self.send_header("Location", "/loop"); body = b""
        elif self.path == "/large":
            body = b"x" * (MAX + 1)
            self.send_response(200); self.send_header("Content-Type", "text/plain")
        elif self.path == "/binary":
            body = b"binary"; self.send_response(200); self.send_header("Content-Type", "application/octet-stream")
        elif self.path == "/slow":
            time.sleep(0.5); body = b"slow"; self.send_response(200); self.send_header("Content-Type", "text/plain")
        else:
            body = b"missing"; self.send_response(404); self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def log_message(self, *args):
        pass


class TestResolver(PublicOnlyResolver):
    """仅给测试域名映射本地服务；其他地址仍走生产公网校验。"""
    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        if host == "public.test":
            return [{"hostname": host, "host": "127.0.0.1", "port": port,
                     "family": socket.AF_INET, "proto": socket.IPPROTO_TCP,
                     "flags": socket.AI_NUMERICHOST | socket.AI_NUMERICSERV}]
        return await super().resolve(host, port, family)


class FakeLoop:
    def __init__(self, addresses):
        self.addresses = addresses

    async def getaddrinfo(self, host, port, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))
                for ip in self.addresses]


async def resolver_checks():
    mixed = PublicOnlyResolver(loop=FakeLoop(["93.184.215.14", "127.0.0.1"]))
    try:
        await mixed.resolve("example.test", 443)
        raise AssertionError("混合公网/私网 DNS 应拒绝")
    except FetchSecurityError:
        pass
    public = PublicOnlyResolver(loop=FakeLoop(["93.184.215.14"]))
    resolved = await public.resolve("example.test", 443)
    assert resolved[0]["host"] == "93.184.215.14" and resolved[0]["hostname"] == "example.test", resolved
    print("1. DNS 任一私网地址即拒绝，公网结果保留原 hostname [OK]")


async def main():
    await resolver_checks()
    before = len(Handler.requests)
    private = await safe_web_fetch(f"http://127.0.0.1:{PORT}/html")
    assert not private["ok"] and "非公网" in private["result"], private
    assert len(Handler.requests) == before, "私网 URL 不得建立连接"
    for invalid in ("file:///etc/passwd", "https://user:pass@example.com", "http://2130706433/"):
        rejected = await safe_web_fetch(invalid)
        assert not rejected["ok"], (invalid, rejected)
    print("2. 直接私网 IP 在连接前拒绝，file/凭据/非规范 IP 均 fail closed [OK]")

    resolver = TestResolver()
    html = await safe_web_fetch(f"http://public.test:{PORT}/html", resolver=resolver)
    assert html["ok"] and "VISIBLE_TEXT" in html["result"] and "SECRET_SCRIPT" not in html["result"], html
    assert Handler.requests[-1] == ("/html", f"public.test:{PORT}"), Handler.requests[-1]
    print("3. 固定解析连接保持原 Host，HTML 去脚本 [OK]")

    redirected = await safe_web_fetch(f"http://public.test:{PORT}/redirect", resolver=resolver)
    assert redirected["ok"] and "VISIBLE_TEXT" in redirected["result"], redirected
    blocked = await safe_web_fetch(f"http://public.test:{PORT}/redirect-private", resolver=resolver)
    assert not blocked["ok"] and "非公网" in blocked["result"], blocked
    loop = await safe_web_fetch(f"http://public.test:{PORT}/loop", resolver=resolver)
    assert not loop["ok"] and "重定向" in loop["result"], loop
    print("4. 重定向复查私网目标并限制次数 [OK]")

    large = await safe_web_fetch(f"http://public.test:{PORT}/large", resolver=resolver)
    assert not large["ok"] and "8 MiB" in large["result"], large
    binary = await safe_web_fetch(f"http://public.test:{PORT}/binary", resolver=resolver)
    assert not binary["ok"] and "内容类型" in binary["result"], binary
    slow = await safe_web_fetch(f"http://public.test:{PORT}/slow", resolver=resolver, timeout_seconds=0.05)
    assert not slow["ok"] and "超时" in slow["result"], slow
    print("5. 8 MiB、内容类型和超时 fail closed [OK]")

    async def slow_fetch(url, timeout_seconds):
        await asyncio.sleep(10)
        return {"ok": True, "status": "completed", "url": url, "result": "too late"}

    cancel = asyncio.Event()
    pending = asyncio.create_task(cancellable_web_fetch("https://example.com", cancel, fetcher=slow_fetch))
    await asyncio.sleep(0.02)
    cancel.set()
    cancelled = await asyncio.wait_for(pending, timeout=1)
    assert cancelled["status"] == "cancelled", cancelled
    print("6. web_fetch 可随回合取消且不等待慢响应 [OK]")


B = "http://localhost:8000/api/v1"
AGENT_PORT = 9006


class AgentFetchMock(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        if request["messages"][-1]["role"] == "tool":
            events = [{"choices": [{"delta": {"content": "FETCH_DONE"}, "index": 0}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}]
        else:
            assert any(tool["function"]["name"] == "web_fetch" for tool in request.get("tools", []))
            events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_fetch",
                "function": {"name": "web_fetch", "arguments": json.dumps({"url": f"http://127.0.0.1:{PORT}/html"})}}]}, "index": 0}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]}]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


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


def agent_integration():
    email = f"web-fetch-{time.time_ns()}@vibeflow.cn"
    token = call("POST", "/auth/register", {"email": email, "name": "web-fetch-smoke", "password": "web-fetch-pass-123"})["token"]
    workspace = call("POST", "/workspaces", {"name": f"web-fetch-ws-{time.time_ns()}"}, token=token)
    case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"web-fetch-{time.time_ns()}"}, token=token)
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "web-fetch-mock", "base_url": f"http://127.0.0.1:{AGENT_PORT}",
        "default_model": "mock-fetch", "api_key": "mock-key"}, token=token)
    cid = call("POST", "/conversations", {
        "case_id": case["id"], "title": "web fetch", "provider_profile_id": profile["id"],
        "model": "mock-fetch"}, token=token)["id"]
    request = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps({"content": "fetch", "provider_profile_id": profile["id"],
                         "model": "mock-fetch"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(request, timeout=30) as response:
        for line in response:
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("data:"):
                events.append(json.loads(text[5:].strip()))
    result = next(event for event in events if event["type"] == "tool.result")
    assert not result["ok"] and result["status"] == "error" and "非公网" in result["result"], result
    answer = "".join(event.get("text", "") for event in events if event["type"] == "text.delta")
    assert "FETCH_DONE" in answer, events
    detail = call("GET", f"/conversations/{cid}", token=token)
    assert any(row["status"] == "executed" for row in detail["tool_calls"]), detail["tool_calls"]
    print("7. Agent web_fetch 私网负例、真实 error 工具结果与续跑 [OK]")


server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
agent_mock = ThreadingHTTPServer(("127.0.0.1", AGENT_PORT), AgentFetchMock)
threading.Thread(target=server.serve_forever, daemon=True).start()
threading.Thread(target=agent_mock.serve_forever, daemon=True).start()
try:
    asyncio.run(main())
    before_agent = len(Handler.requests)
    agent_integration()
    assert len(Handler.requests) == before_agent, "Agent 私网抓取不得连接到本地服务"
    live = asyncio.run(safe_web_fetch("https://example.com", timeout_seconds=8))
    if live["ok"]:
        assert "Example Domain" in live["result"], live["result"][:500]
        print("8. LIVE_OK example.com 公网 DNS/固定 IP/TLS/证书链 [OK]")
    else:
        print(f"8. LIVE_LIMITED 公网抓取未成功，不记在线成功: {live['result'][:160]}")
    public = asyncio.run(safe_web_fetch("https://api.crossref.org/works?query=OpenFOAM&rows=1", timeout_seconds=8))
    if public["ok"]:
        assert '"items"' in public["result"], public["result"][:500]
        print("9. LIVE_OK Crossref 公网 safe_web_fetch 返回真实 JSON [OK]")
    else:
        print(f"9. LIVE_LIMITED Crossref 公网抓取未成功: {public['result'][:160]}")
    print("\n=== web_fetch 安全边界冒烟通过（在线结果见 LIVE_ 标记）===")
finally:
    agent_mock.shutdown()
    server.shutdown()
