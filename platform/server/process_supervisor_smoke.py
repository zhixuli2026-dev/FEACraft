"""G14：Job/PTY/viz/Agent 按 case 统一登记、取消与清理。"""
import asyncio
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import aiohttp

B = "http://localhost:8000/api/v1"
PORT = 8995


class DelayedMock(BaseHTTPRequestHandler):
    received = threading.Event()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        chunks = ["partial"]
        try:
            for index, text in enumerate(chunks):
                body = f"data: {json.dumps({'choices': [{'delta': {'content': text}, 'index': 0}]})}\n\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                self.received.set()
                time.sleep(5)
            self.wfile.write(b"data: {\"choices\":[{\"delta\":{},\"finish_reason\":\"stop\",\"index\":0}]}\n\n")
        except (BrokenPipeError, ConnectionResetError):
            pass

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
    try:
        with urllib.request.urlopen(request, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def sse_collect(cid, body, token):
    request = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(request, timeout=30) as response:
        for line in response:
            text = line.decode("utf-8", "replace").strip()
            if text.startswith("data:"):
                events.append(json.loads(text[5:].strip()))
    return events


mock = ThreadingHTTPServer(("127.0.0.1", PORT), DelayedMock)
threading.Thread(target=mock.serve_forever, daemon=True).start()


async def main():
    token = call("POST", "/auth/register", {
        "email": f"supervisor-{time.time_ns()}@vibeflow.cn", "name": "supervisor", "password": "supervisor-pass"})["token"]
    workspace = call("POST", "/workspaces", {"name": f"supervisor-{time.time_ns()}"}, token=token)
    case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"supervisor-{time.time_ns()}"}, token=token)
    case_id = case["id"]
    job = call("POST", f"/cases/{case_id}/jobs", {"type": "shell", "command": "ping -n 30 127.0.0.1 >nul"}, token=token)
    stl = ("solid tri\nfacet normal 0 0 1\nouter loop\n"
           "vertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid\n")
    call("PUT", f"/cases/{case_id}/file", {"path": "D-post/result.stl", "content": stl}, token=token)
    viz = call("POST", f"/cases/{case_id}/viz:sessions", {"file_path": "D-post/result.stl"}, token=token)
    assert "id" in viz, viz
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "supervisor-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-supervisor", "api_key": "mock-key"}, token=token)
    cid = call("POST", "/conversations", {
        "case_id": case_id, "title": "supervisor", "provider_profile_id": profile["id"],
        "model": "mock-supervisor"}, token=token)["id"]
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("events", sse_collect(cid, {
        "content": "slow", "provider_profile_id": profile["id"], "model": "mock-supervisor"}, token)))
    thread.start()
    async with aiohttp.ClientSession() as session:
        ws = await session.ws_connect(f"ws://127.0.0.1:8000/ws/terminal?cols=80&rows=24&shell=cmd&case={case_id}&token={token}")
        ready = json.loads((await ws.receive()).data)
        terminal_id = ready["session"]
        assert DelayedMock.received.wait(5), "Agent 未启动"
        for _ in range(50):
            processes = call("GET", f"/runtime/processes?case_id={case_id}", token=token)["processes"]
            kinds = {item["kind"] for item in processes}
            if {"job", "terminal", "viz", "agent"} <= kinds:
                break
            await asyncio.sleep(0.1)
        assert {"job", "terminal", "viz", "agent"} <= kinds, processes
        cancelled = call("POST", "/runtime/processes:cancel", {"case_id": case_id}, token=token)
        assert cancelled["ok"], cancelled
        for kind in ("job", "terminal", "viz", "agent"):
            assert cancelled["cancelled"].get(kind, 0) >= 1, cancelled
        thread.join(8)
        assert not thread.is_alive(), "Agent 取消后未结束"
        assert any(event["type"] == "run.cancelled" for event in result["events"]), result["events"]
        for _ in range(50):
            state = call("GET", f"/jobs/{job['id']}", token=token)
            if state["status"] == "cancelled":
                break
            await asyncio.sleep(0.1)
        assert state["status"] == "cancelled", state
        sessions = call("GET", "/terminal/sessions", token=token)["sessions"]
        assert not any(item["id"] == terminal_id for item in sessions), sessions
        assert call("GET", f"/cases/{case_id}/viz:sessions", token=token) == []
        for _ in range(50):
            remaining = call("GET", f"/runtime/processes?case_id={case_id}", token=token)["processes"]
            if not remaining:
                break
            await asyncio.sleep(0.1)
        assert not remaining, remaining
        await ws.close()
    print("G14 job/PTY/viz/Agent unified cancel and cleanup: OK", cancelled["cancelled"])


try:
    asyncio.run(main())
finally:
    mock.shutdown()
