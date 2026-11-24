"""F42/F48：checkpoint、断开恢复与每轮持久化计时。"""
import asyncio
import json
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from app.db import SessionLocal, engine
from app.routers.agent import _append_event, _append_request_state, recover_interrupted_runs

B = "http://localhost:8000/api/v1"
PORT = 9016


class SlowMock(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        json.loads(self.rfile.read(length) or b"{}")
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        try:
            body = json.dumps({"choices": [{"delta": {"content": "partial-" + "x" * 20000, "index": 0}}]})
            self.wfile.write(f"data: {body}\n\n".encode()); self.wfile.flush()
            for index in range(100):
                self.wfile.write(b'data: {"choices":[{"delta":{"content":"tail"},"index":0}]}\n\n')
                self.wfile.flush(); time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
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
    with urllib.request.urlopen(request, data, timeout=30) as response:
        return json.loads(response.read())


async def seed_and_recover(cid, run_id):
    async with SessionLocal() as db:
        await _append_event(db, cid, "run_state", {"run_id": run_id, "status": "started"})
        await _append_request_state(db, cid, run_id, f"{run_id}-request", "started", request_index=0)
        await _append_event(db, cid, "run_checkpoint", {"run_id": run_id, "content": "recover-me", "reasoning": ""})
        await db.commit()
        recovered = await recover_interrupted_runs(db, run_id)
    await engine.dispose()
    return recovered


email = f"checkpoint-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "checkpoint-smoke", "password": "checkpoint-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"checkpoint-ws-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"checkpoint-{time.time_ns()}"}, token=token)
mock = ThreadingHTTPServer(("127.0.0.1", PORT), SlowMock)
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "checkpoint-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-checkpoint", "api_key": "mock-key"}, token=token)
    cid = call("POST", "/conversations", {
        "case_id": case["id"], "title": "checkpoint", "provider_profile_id": profile["id"],
        "model": "mock-checkpoint"}, token=token)["id"]
    request = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps({"content": "slow", "provider_profile_id": profile["id"],
                         "model": "mock-checkpoint"}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    response = urllib.request.urlopen(request, timeout=30)
    response.readline()
    checkpoint = None
    for _ in range(50):
        detail = call("GET", f"/conversations/{cid}", token=token)
        checkpoint = next((event for event in detail["events"] if event["type"] == "run_checkpoint"), None)
        if checkpoint:
            break
        time.sleep(0.1)
    assert checkpoint and len(checkpoint["payload"]["content"]) >= 16000, checkpoint
    response.close()
    states = []
    for _ in range(50):
        detail = call("GET", f"/conversations/{cid}", token=token)
        states = [event for event in detail["events"] if event["type"] == "run_state"]
        if states and states[-1]["payload"]["status"] == "interrupted":
            break
        time.sleep(0.1)
    assert states and states[-1]["payload"]["status"] == "interrupted", states
    assert states[-1]["payload"]["run_id"] == checkpoint["payload"]["run_id"]
    started, ended = states[0]["payload"], states[-1]["payload"]
    assert started["started_at"] > 0 and ended["started_at"] == started["started_at"], states
    assert ended["ended_at"] >= started["started_at"] and ended["duration_ms"] >= 0, states
    request_states = [event["payload"] for event in detail["events"]
                      if event["type"] == "request_state" and
                      event["payload"].get("run_id") == checkpoint["payload"]["run_id"]]
    assert request_states and request_states[-1]["status"] == "interrupted", request_states
    assert request_states[0]["request_id"] == request_states[-1]["request_id"], request_states
    print("1. 16 KiB checkpoint 落盘，断开后 Run/Request interrupted 并保留起终点/耗时 [OK]")

    fake = f"recovery-{time.time_ns()}"
    assert asyncio.run(seed_and_recover(cid, fake)) == 1
    detail = call("GET", f"/conversations/{cid}", token=token)
    recovered = [event for event in detail["events"] if event["type"] == "run_state" and event["payload"]["run_id"] == fake]
    assert recovered[-1]["payload"]["status"] == "interrupted", recovered
    assert recovered[-1]["payload"]["started_at"] == recovered[0]["payload"]["started_at"], recovered
    assert recovered[-1]["payload"]["duration_ms"] >= 0, recovered
    recovered_requests = [event["payload"] for event in detail["events"]
                          if event["type"] == "request_state" and
                          event["payload"].get("run_id") == fake]
    assert [p["status"] for p in recovered_requests] == ["started", "interrupted"], recovered_requests
    assert recovered_requests[-1]["request_id"] == f"{fake}-request", recovered_requests
    assert any(event["type"] == "run_checkpoint" and event["payload"].get("content") == "recover-me"
               for event in detail["events"])
    print("2. started Run/Request 启动恢复为 interrupted，checkpoint 可读取 [OK]")
    print("\n=== checkpoint/中断恢复冒烟通过 ===")
finally:
    mock.shutdown()
