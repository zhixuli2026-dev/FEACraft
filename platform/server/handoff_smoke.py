"""F16/F37B：新会话交接摘要、模型消息恢复和有界文本。"""
import json
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.routers.agent import _build_messages

B = "http://localhost:8000/api/v1"
PORT = 9002


class MockLLM(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length) or b"{}")
        if req.get("stream") is False:
            body = {"choices": [{"message": {"role": "assistant", "content": "COMPRESSED_SUMMARY"}}]}
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: {\"choices\":[{\"delta\":{\"content\":\"ok\"},\"index\":0}]}\n\n")
            self.wfile.write(b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\",\"index\":0}]}\n\n")

    def log_message(self, *args):
        pass


def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        req.add_header("Content-Type", "application/json")
        data = json.dumps(body, ensure_ascii=False).encode()
    try:
        with urllib.request.urlopen(req, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def sse(cid, body, token):
    req = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as response:
        response.read()


mock = ThreadingHTTPServer(("127.0.0.1", PORT), MockLLM)
import threading
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    email = f"handoff-{time.time_ns()}@vibeflow.cn"
    token = call("POST", "/auth/register", {"email": email, "name": "handoff-smoke", "password": "handoff-pass-123"})["token"]
    workspace = call("POST", "/workspaces", {"name": f"handoff-ws-{time.time_ns()}"}, token=token)
    case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"handoff-{time.time_ns()}"}, token=token)
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "handoff-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-handoff", "api_key": "mock-key"}, token=token)
    cid = call("POST", "/conversations", {
        "case_id": case["id"], "title": "source", "provider_profile_id": profile["id"],
        "model": "mock-handoff", "permission_mode": "full_access"}, token=token)["id"]
    for text in ("first context", "second context", "third context"):
        sse(cid, {"content": text, "provider_profile_id": profile["id"], "model": "mock-handoff"}, token)
    compressed = call("POST", f"/conversations/{cid}/compress",
                      {"provider_profile_id": profile["id"], "keep_last": 2}, token=token)
    assert compressed.get("ok") is True, compressed
    handoff = call("POST", f"/conversations/{cid}/handoff", {"title": "HandoffThread"}, token=token)
    assert handoff.get("id") and handoff["model"] == "mock-handoff", handoff
    assert handoff["permission_mode"] == "full_access" and handoff["provider_profile_id"] == profile["id"], handoff
    detail = call("GET", f"/conversations/{handoff['id']}", token=token)
    assert len(detail["events"]) == 1, detail["events"]
    event = detail["events"][0]
    assert event["type"] == "system" and event["payload"]["kind"] == "handoff", event
    assert event["payload"]["text"] == "COMPRESSED_SUMMARY", event
    assert event["payload"]["source_conversation_id"] == cid, event
    model_messages = _build_messages([{**event, "id": event["payload"]["covers_max_id"]}], "openai")
    assert any("COMPRESSED_SUMMARY" in message.get("content", "") for message in model_messages), model_messages
    print("1. 压缩摘要进入新会话事件与模型消息 [OK]")

    raw = call("POST", "/conversations", {
        "case_id": case["id"], "title": "raw", "provider_profile_id": profile["id"],
        "model": "mock-handoff"}, token=token)["id"]
    sse(raw, {"content": "x" * 1400, "provider_profile_id": profile["id"], "model": "mock-handoff"}, token)
    limited = call("POST", f"/conversations/{raw}/handoff", {"keep_chars": 1000}, token=token)
    limited_detail = call("GET", f"/conversations/{limited['id']}", token=token)
    assert len(limited_detail["events"]) == 1, limited_detail["events"]
    text = limited_detail["events"][0]["payload"]["text"]
    assert len(text) <= 1000 and text.endswith("assistant: ok"), (len(text), text[-20:])
    print("2. 无摘要时按 keep_chars 有界交接且不复制完整历史 [OK]")

    other = call("POST", "/auth/register", {"email": f"handoff-other-{time.time_ns()}@vibeflow.cn",
                                            "name": "handoff-other", "password": "other-pass-123"})
    foreign = call("POST", f"/conversations/{cid}/handoff", {}, token=other["token"])
    assert foreign.get("_err") == 404, foreign
    print("3. 跨用户交接 404 [OK]")
    print("\n=== 会话交接冒烟通过 ===")
finally:
    mock.shutdown()
