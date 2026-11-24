"""F49：版本化 JSONL 会话导出，含祖先/完整事件而非显示窗口。"""
import asyncio
import json
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from app.db import SessionLocal, engine
from app.routers.agent import _append_event

B = "http://localhost:8000/api/v1"
PORT = 9015


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body).encode()
    try:
        with urllib.request.urlopen(request, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def sse(cid, text, profile_id, token):
    request = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps({"content": text, "provider_profile_id": profile_id,
                         "model": "mock-export"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        response.read()


class ExportMock(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        json.loads(self.rfile.read(length) or b"{}")
        body = b'data: {"choices":[{"delta":{"content":"exported answer"},"index":0}]}\n\n' \
               b'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}\n\n'
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *args):
        pass


async def seed(cid, count):
    async with SessionLocal() as db:
        for index in range(count):
            await _append_event(db, cid, "message", {"role": "user", "content": f"extra-{index}"})
        await db.commit()
    await engine.dispose()


email = f"export-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "export-smoke", "password": "export-pass-123"})["token"]
other = call("POST", "/auth/register", {"email": f"export-other-{time.time_ns()}@vibeflow.cn",
                                        "name": "export-other", "password": "other-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"export-ws-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"export-{time.time_ns()}"}, token=token)
mock = ThreadingHTTPServer(("127.0.0.1", PORT), ExportMock)
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "export-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-export", "api_key": "mock-key"}, token=token)
    cid = call("POST", "/conversations", {
        "case_id": case["id"], "title": "export session", "provider_profile_id": profile["id"],
        "model": "mock-export"}, token=token)["id"]
    sse(cid, "first", profile["id"], token)
    sse(cid, "second", profile["id"], token)
    asyncio.run(seed(cid, 300))
    request = urllib.request.Request(f"{B}/conversations/{cid}/export",
                                     headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        assert response.status == 200
        assert response.headers.get_content_type() == "application/x-ndjson", response.headers
        assert f"conversation-{cid}.jsonl" in response.headers.get("Content-Disposition", "")
        text = response.read().decode("utf-8")
    lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert lines[0]["schema"] == "vibeflow.agent.session.v1", lines[0]
    assert lines[0]["session"]["id"] == cid and lines[0]["session"]["title"] == "export session"
    events = lines[1:]
    assert len(events) == 314, len(events)  # 两轮各 7 个含 Run/Request 生命周期事件 + 300 直接事件
    assert all(event["schema"] == "vibeflow.agent.event.v1" for event in events)
    assert all(event["event_id"] and event["created_at"] for event in events)
    assert all(event["eventId"] == event["event_id"] and event["time"] == event["created_at"]
               for event in events)
    assert all(event["workingDirectory"] and event["mode"] and event["permission"] and event["model"]
               for event in events)
    first_user = next(event for event in events if event["payload"].get("role") == "user")
    first_started = next(event for event in events if event["type"] == "run_state" and
                         event["payload"].get("status") == "started")
    assert first_user["model"] == first_started["model"] == "mock-export"
    assert first_user["permission"] == first_started["permission"] == "approve_writes"
    assert first_user["workingDirectory"] == first_started["workingDirectory"]
    assert all(lines[0]["session"][key] for key in ("workingDirectory", "mode", "permission", "model"))
    assert [event["seq"] for event in events] == list(range(1, 315))
    assert "mock-key" not in text and "key_ciphertext" not in text
    print("1. v1 元数据、314 完整事件、event_id/created_at/seq 与无密钥导出 [OK]")
    foreign = call("GET", f"/conversations/{cid}/export", token=other)
    assert foreign.get("_err") == 404, foreign
    print("2. 跨用户导出 404 [OK]")
    print("\n=== 会话 JSONL 导出冒烟通过 ===")
finally:
    mock.shutdown()
