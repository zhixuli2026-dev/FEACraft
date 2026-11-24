"""F67：用户图片附件、文件事实源与双协议图像块。"""
import base64
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://localhost:8000/api/v1")
PORT = 0
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z3XcAAAAASUVORK5CYII=")
DATA = base64.b64encode(PNG).decode()


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = None
    if body is not None:
        request.add_header("Content-Type", "application/json")
        data = json.dumps(body, ensure_ascii=False).encode()
    try:
        with urllib.request.urlopen(request, data, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"_err": error.code, "detail": error.read().decode("utf-8", "replace")}


def sse(cid, profile_id, token):
    body = {"content": "look", "provider_profile_id": profile_id, "model": "mock-image",
            "images": [{"name": "pixel.png", "mime": "image/png", "data": DATA}]}
    request = urllib.request.Request(f"{B}/conversations/{cid}/messages", method="POST",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    events = []
    with urllib.request.urlopen(request, timeout=30) as response:
        for line in response:
            line = line.decode().strip()
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


class ImageMock(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        content = request["messages"][-1]["content"]
        if self.path == "/v1/chat/completions":
            assert any(part.get("type") == "image_url" and part["image_url"]["url"].endswith(DATA)
                       for part in content), content
        else:
            assert any(part.get("type") == "image" and part["source"]["data"] == DATA and
                       part["source"]["media_type"] == "image/png" for part in content), content
        if self.path == "/v1/chat/completions":
            events = [{"choices": [{"delta": {"content": "IMAGE_DONE"}, "index": 0}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}]}]
        else:
            events = [{"type": "message_start", "message": {"id": "image"}},
                      {"type": "content_block_delta", "index": 0,
                       "delta": {"type": "text_delta", "text": "IMAGE_DONE"}},
                      {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                      {"type": "message_stop"}]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events).encode()
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *args):
        pass


email = f"image-{time.time_ns()}@vibeflow.cn"
token = call("POST", "/auth/register", {"email": email, "name": "image-smoke", "password": "image-pass-123"})["token"]
workspace = call("POST", "/workspaces", {"name": f"image-{time.time_ns()}"}, token=token)
case = call("POST", f"/workspaces/{workspace['id']}/cases", {"name": f"image-{time.time_ns()}"}, token=token)
mock = ThreadingHTTPServer(("127.0.0.1", PORT), ImageMock)
PORT = mock.server_port
threading.Thread(target=mock.serve_forever, daemon=True).start()
try:
    for provider in ("custom", "anthropic"):
        profile = call("POST", "/provider-profiles", {
            "provider": provider, "name": f"image-{provider}", "base_url": f"http://127.0.0.1:{PORT}",
            "default_model": "mock-image", "api_key": "mock-key"}, token=token)
        cid = call("POST", "/conversations", {"case_id": case["id"], "title": f"image-{provider}",
                                              "provider_profile_id": profile["id"], "model": "mock-image"}, token=token)["id"]
        events = sse(cid, profile["id"], token)
        answer = "".join(event.get("text", "") for event in events if event["type"] == "text.delta")
        assert "IMAGE_DONE" in answer, events
        detail = call("GET", f"/conversations/{cid}", token=token)
        user = next(event["payload"] for event in detail["events"]
                    if event["type"] == "message" and event["payload"].get("role") == "user")
        image = user["images"][0]
        assert image["mime"] == "image/png" and image["size"] == len(PNG) and "data" not in image, image
        request = urllib.request.Request(f"{B}/cases/{case['id']}/raw?path={image['path']}",
                                         headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request, timeout=30) as response:
            assert response.read() == PNG
        print(f"1. {provider} 图像块、附件元数据与原始字节 [OK]")

    profile_id = profile["id"]
    cid = call("POST", "/conversations", {"case_id": case["id"], "title": "invalid",
                                          "provider_profile_id": profile_id, "model": "mock-image"}, token=token)["id"]
    invalid = call("POST", f"/conversations/{cid}/messages", {
        "content": "bad", "provider_profile_id": profile_id,
        "images": [{"name": "fake.png", "mime": "image/png", "data": "not-base64"}]}, token=token)
    assert invalid.get("_err") == 400, invalid
    too_many = call("POST", f"/conversations/{cid}/messages", {
        "content": "bad", "provider_profile_id": profile_id,
        "images": [{"name": "pixel.png", "mime": "image/png", "data": DATA}] * 4}, token=token)
    assert too_many.get("_err") == 400, too_many
    oversized = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * (4 * 1024 * 1024)).decode()
    large = call("POST", f"/conversations/{cid}/messages", {
        "content": "bad", "provider_profile_id": profile_id,
        "images": [{"name": "large.png", "mime": "image/png", "data": oversized}]}, token=token)
    assert large.get("_err") == 413, large
    print("2. 非法 Base64、超过 3 张和超过 4 MiB 均 fail closed [OK]")
    print("\n=== Agent 用户图片附件冒烟通过 ===")
finally:
    mock.shutdown()
    mock.server_close()
