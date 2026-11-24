"""F60：$skill -> get_skill -> 技能正文进入下一次模型请求。"""
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

B = "http://localhost:8000/api/v1"
PORT = 9020


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body, ensure_ascii=False).encode()
    with urllib.request.urlopen(request, data, timeout=30) as response:
        return json.loads(response.read())


class SkillMock(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        if request["messages"][-1]["role"] == "tool":
            context = json.dumps(request, ensure_ascii=False)
            assert "OpenFOAM 求解契约" in context and "case-Re1000" in context, context[-2000:]
            events = [{"choices": [{"delta": {"content": "SKILL_DONE"}, "index": 0}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}]
        else:
            events = [{"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_skill",
                "function": {"name": "get_skill", "arguments": json.dumps({"name": "vibeflow-openfoam"})}}]}, "index": 0}]},
                {"choices": [{"delta": {}, "finish_reason": "tool_calls", "index": 0}]}]
        body = "".join(f"data: {json.dumps(event, ensure_ascii=False)}\n\n" for event in events).encode()
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *args):
        pass


email = f"skill-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "skill-smoke", "password": "skill-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"skill-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"skill-{time.time_ns()}"}, token=token)
mock = ThreadingHTTPServer(("127.0.0.1", PORT), SkillMock)
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    profile = call("POST", "/provider-profiles", {
        "provider": "custom", "name": "skill-mock", "base_url": f"http://127.0.0.1:{PORT}",
        "default_model": "mock-skill", "api_key": "mock-key"}, token=token)
    cid = call("POST", "/conversations", {"case_id": case["id"], "title": "skill",
                                          "provider_profile_id": profile["id"], "model": "mock-skill"}, token=token)["id"]
    request = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps({"content": "$vibeflow-openfoam 求解", "provider_profile_id": profile["id"],
                         "model": "mock-skill"}, ensure_ascii=False).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(request, timeout=30) as response:
        for line in response:
            line = line.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    result = next((event for event in events if event["type"] == "tool.result"), None)
    assert result, events
    assert result["ok"] and "OpenFOAM 求解契约" in result["result"], result
    answer = "".join(event.get("text", "") for event in events if event["type"] == "text.delta")
    assert "SKILL_DONE" in answer, events
    detail = call("GET", f"/conversations/{cid}", token=token)
    assert any(event["type"] == "tool_result" and "case-Re1000" in event["payload"]["result"]
               for event in detail["events"])
    print("1. get_skill 返回完整技能并驱动后续模型回答 [OK]")
    print("\n=== $skill 发现/调用/续跑冒烟通过 ===")
finally:
    mock.shutdown()
