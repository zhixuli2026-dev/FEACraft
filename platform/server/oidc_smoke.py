"""J10：本地假 OIDC provider 的真实 discovery/PKCE/token/userinfo 流程。"""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1")


class Provider(BaseHTTPRequestHandler):
    challenge = ""
    verified = True

    def _json(self, data, status=200):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/.well-known/openid-configuration":
            self._json({"authorization_endpoint": "http://127.0.0.1:9127/authorize",
                        "token_endpoint": "http://127.0.0.1:9127/token",
                        "userinfo_endpoint": "http://127.0.0.1:9127/userinfo"})
        elif self.path == "/userinfo":
            self._json({"sub": "oidc-sub-1", "email": "oidc-user@vibeflow.cn",
                        "email_verified": self.verified})
        else:
            self._json({"error": "not_found"}, 404)

    def do_POST(self):
        if self.path != "/token":
            self._json({"error": "not_found"}, 404)
            return
        form = urllib.parse.parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        verifier = form["code_verifier"][0]
        actual = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if actual != self.challenge or form["code"][0] != "mock-code":
            self._json({"error": "invalid_grant"}, 400)
            return
        self._json({"access_token": "mock-access-token", "token_type": "Bearer"})

    def log_message(self, *_):
        pass


def call(method, path, body=None, token=None):
    request = urllib.request.Request(B + path, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    data = json.dumps(body).encode() if body is not None else None
    if data:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, data, timeout=20) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


if os.environ.get("VIBEFLOW_TEST_OIDC") != "1":
    print("主服务未启用 mock OIDC；隔离 provider 流程 [SKIP]")
else:
    server = ThreadingHTTPServer(("127.0.0.1", 9127), Provider)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, providers = call("GET", "/auth/oidc/providers")
        assert status == 200 and providers[0]["id"] == "default", providers
        status, start = call("POST", "/auth/oidc/default/start", {})
        assert status == 200, start
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(start["authorization_url"]).query)
        Provider.challenge = query["code_challenge"][0]
        assert query["code_challenge_method"] == ["S256"], query
        state = query["state"][0]
        status, auth = call("POST", "/auth/oidc/default/callback", {"code": "mock-code", "state": state})
        assert status == 200 and auth["user"]["email"] == "oidc-user@vibeflow.cn", auth
        status, me = call("GET", "/auth/me", token=auth["token"])
        assert status == 200 and me["id"] == auth["user"]["id"], me
        status, _ = call("POST", "/auth/oidc/default/callback", {"code": "mock-code", "state": state})
        assert status == 400, "state 不能重放"
        status, _ = call("POST", "/auth/oidc/default/callback", {"code": "mock-code", "state": "invalid"})
        assert status == 400

        _, start2 = call("POST", "/auth/oidc/default/start", {})
        query2 = urllib.parse.parse_qs(urllib.parse.urlsplit(start2["authorization_url"]).query)
        Provider.challenge = query2["code_challenge"][0]
        status, second = call("POST", "/auth/oidc/default/callback",
                              {"code": "mock-code", "state": query2["state"][0]})
        assert status == 200 and second["user"]["id"] == auth["user"]["id"], second

        _, start3 = call("POST", "/auth/oidc/default/start", {})
        query3 = urllib.parse.parse_qs(urllib.parse.urlsplit(start3["authorization_url"]).query)
        Provider.challenge = query3["code_challenge"][0]
        Provider.verified = False
        status, _ = call("POST", "/auth/oidc/default/callback",
                         {"code": "mock-code", "state": query3["state"][0]})
        assert status == 400, "未验证邮箱不得登录"
        print("J10 discovery, PKCE S256, token/userinfo, state single-use, account link and verified email: OK")
    finally:
        server.shutdown()
        server.server_close()
print("\n=== J10 OIDC 协议冒烟通过（本地 mock provider）===")
