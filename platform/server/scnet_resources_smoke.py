"""I12–I16：SCNET B/S 资源生命周期与 mock server 真实 HTTP golden。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
from io import BytesIO
import json
import os
import threading
import time
from urllib.parse import unquote, urlsplit
import zipfile
import httpx

from app.services.scnet import compact_sign

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1")


class Mock(BaseHTTPRequestHandler):
    state = {"jobs": {}, "files": {}, "containers": {}, "notebooks": {}, "desktops": {}}

    def _send(self, data, status=200):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")

    def _auth(self):
        expected = compact_sign({"accessKey": "ak", "token": "session-token"}, "sk")
        return self.headers.get("X-Access-Key") == "ak" and self.headers.get("X-Signature") == expected

    def _handle(self, method):
        if not self._auth():
            self._send({"code": 10009, "message": "invalid signature"}, 401)
            return
        parsed = urlsplit(self.path)
        path = parsed.path
        if path.startswith(('/hpc/', '/ai/')):
            path = '/' + path.split('/', 2)[2]
        parts = path.strip("/").split("/")
        if parts[:2] != ["openapi", "v2"] or len(parts) < 3:
            self._send({"message": "bad endpoint"}, 404)
            return
        resource, tail = parts[2], parts[3:]
        if ":" in resource:
            resource, action = resource.split(":", 1)
            tail = [action]
        if resource == "job-history":
            resource, tail = "jobs", ["history"]
        if tail and ":" in tail[0]:
            tail = tail[0].split(":", 1)
        state = self.state
        try:
            if resource == "jobs":
                if not tail:
                    if method == "GET":
                        self._send({"data": list(state["jobs"].values())})
                    else:
                        body = self._body()
                        ident = str(len(state["jobs"]) + 1)
                        state["jobs"][ident] = {"id": ident, "name": body.get("name", "job"), "status": "running"}
                        self._send(state["jobs"][ident])
                elif tail[0] == "history":
                    self._send({"records": list(state["jobs"].values())})
                elif len(tail) == 2 and tail[1] == "cancel":
                    state["jobs"][tail[0]]["status"] = "stopped"
                    self._send({"ok": True})
                else:
                    self._send(state["jobs"][tail[0]])
                return
            if resource == "files":
                action = tail[0] if tail else "list"
                body = self._body() if method == "POST" else {}
                if action == "list":
                    self._send({"fileList": [{"path": path, "size": len(content)} for path, content in state["files"].items()]})
                elif action == "mkdir":
                    state["files"][body["path"] + "/"] = ""
                    self._send({"ok": True})
                elif action == "upload":
                    state["files"][body["path"]] = body["content"]
                    self._send({"ok": True})
                elif action == "download":
                    path = unquote(parsed.query.split("=", 1)[1])
                    if path.endswith("/"):
                        buffer = BytesIO()
                        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                            for name, content in state["files"].items():
                                if name.startswith(path) and not name.endswith("/"):
                                    archive.writestr(name, content)
                        self._send({"content": base64.b64encode(buffer.getvalue()).decode(), "format": "zip"})
                    else:
                        self._send({"content": state["files"][path]})
                elif action == "rename":
                    state["files"][body["target"]] = state["files"].pop(body["path"])
                    self._send({"ok": True})
                elif action == "copy":
                    state["files"][body["target"]] = state["files"][body["path"]]
                    self._send({"ok": True})
                elif action == "move":
                    state["files"][body["target"]] = state["files"].pop(body["path"])
                    self._send({"ok": True})
                elif action == "delete":
                    state["files"].pop(body["path"], None)
                    self._send({"ok": True})
                return
            if resource in {"containers", "notebooks", "desktops"}:
                items = state[resource]
                if not tail:
                    if method == "GET":
                        self._send({"data": list(items.values())})
                    else:
                        body = self._body()
                        ident = str(len(items) + 1)
                        items[ident] = {"id": ident, "name": body.get("name", resource[:-1]), "status": "created"}
                        self._send(items[ident])
                else:
                    ident, action = tail[0], tail[1] if len(tail) > 1 else "detail"
                    if action == "detail":
                        self._send(items[ident])
                    elif action == "open":
                        self._send({"url": f"/connect/{resource}/{ident}?token=temporary"})
                    elif action == "exec":
                        self._body()
                        self._send({"output": "EXEC_OK"})
                    elif action == "delete" or action == "release":
                        items.pop(ident, None)
                        self._send({"ok": True})
                    elif action == "rename":
                        items[ident]["name"] = self._body()["name"]
                        self._send(items[ident])
                    elif action == "stop" and resource == "notebooks":
                        items[ident]["saved"] = bool(self._body().get("save_environment"))
                        items[ident]["status"] = "stopped"
                        self._send(items[ident])
                    else:
                        items[ident]["status"] = "stopped" if action == "stop" else "running"
                        self._send(items[ident])
                return
            self._send({"message": "unknown resource"}, 404)
        except (KeyError, IndexError) as error:
            self._send({"message": str(error)}, 400)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, *_):
        pass


async def api(client, operation, token, args=None):
    response = await client.post(f"/scnet/{operation}", headers={"Authorization": f"Bearer {token}"},
        json={"base_url": "http://127.0.0.1:9129", "access_key": "ak", "secret_key": "sk",
              "token": "session-token", "args": args or {}, "confirmed": True})
    assert response.status_code == 200, (operation, response.text)
    return response.json()


async def main():
    server = ThreadingHTTPServer(("127.0.0.1", 9129), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        async with httpx.AsyncClient(base_url=B, timeout=30, trust_env=False) as client:
            response = await client.post("/auth/register", json={
                "email": f"scnet-{time.time_ns()}@vibeflow.cn", "name": "scnet", "password": "scnet-pass-123"})
            token = response.json()["token"]
            response = await client.post("/scnet/jobs.submit", headers={"Authorization": f"Bearer {token}"},
                json={"base_url": "http://127.0.0.1:9129", "access_key": "ak", "secret_key": "sk",
                      "args": {"job": {"name": "unconfirmed"}}})
            assert response.status_code == 400, response.text
            job = await api(client, "jobs.submit", token, {"job": {"name": "cfd"}})
            assert job["status"] == "running"
            assert (await api(client, "jobs.list", token))[0]["name"] == "cfd"
            assert (await api(client, "jobs.history", token))[0]["id"] == job["id"]
            assert (await api(client, "jobs.detail", token, {"id": job["id"]}))["status"] == "running"
            assert (await api(client, "jobs.cancel", token, {"id": job["id"]}))["ok"]
            await api(client, "files.mkdir", token, {"path": "Ref"})
            await api(client, "files.upload", token, {"path": "Ref/a.txt", "content": "ABC"})
            await api(client, "files.rename", token, {"path": "Ref/a.txt", "target": "Ref/b.txt"})
            await api(client, "files.copy", token, {"path": "Ref/b.txt", "target": "Ref/c.txt"})
            await api(client, "files.move", token, {"path": "Ref/c.txt", "target": "Ref/d.txt"})
            assert (await api(client, "files.download", token, {"path": "Ref/b.txt"})) == "ABC"
            assert len(await api(client, "files.list", token)) == 3
            await api(client, "files.delete", token, {"path": "Ref/d.txt"})
            for resource in ("containers", "notebooks", "desktops"):
                created = await api(client, f"{resource}.create", token, {"resource": {"name": resource}})
                ident = created["id"]
                assert (await api(client, f"{resource}.list", token))[0]["id"] == ident
                assert (await api(client, f"{resource}.detail", token, {"id": ident}))["id"] == ident
                if resource != "desktops":
                    assert (await api(client, f"{resource}.start", token, {"id": ident}))["status"] == "running"
                if resource == "containers":
                    assert (await api(client, "containers.restart", token, {"id": ident}))["status"] == "running"
                    assert (await api(client, "containers.exec", token, {"id": ident, "command": "echo"}))["output"] == "EXEC_OK"
                if resource == "notebooks":
                    stopped = await api(client, "notebooks.stop", token, {"id": ident, "save_environment": True})
                    assert stopped["saved"] is True
                    assert (await api(client, "notebooks.rename", token, {"id": ident, "name": "renamed"}))["name"] == "renamed"
                elif resource != "desktops":
                    await api(client, f"{resource}.stop", token, {"id": ident})
                opened = await api(client, f"{resource}.open", token, {"id": ident})
                assert opened["url"].startswith("http://127.0.0.1:9129/connect/")
                if resource == "notebooks":
                    await api(client, "notebooks.release", token, {"id": ident})
                else:
                    await api(client, f"{resource}.delete", token, {"id": ident})
        print("I12 jobs, I13 files, I14 containers, I15 notebooks, I16 desktops lifecycle: OK")
    finally:
        server.shutdown()
        server.server_close()


import asyncio

if __name__ == "__main__":
    asyncio.run(main())
