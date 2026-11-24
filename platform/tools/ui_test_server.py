"""Temporary API + built web preview for browser acceptance; stop by creating root/stop."""
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

PLATFORM = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLATFORM / "server"))
from testsupport import IsolatedServer


with IsolatedServer("core-browser") as api:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = dict(api.env, VITE_API_TARGET=api.base)
    with (api.root / "web.log").open("w", encoding="utf-8") as log:
        web = subprocess.Popen(["node", str(PLATFORM / "web/node_modules/vite/bin/vite.js"),
            "preview", "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
            cwd=PLATFORM / "web", env=env, stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
        try:
            url = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    urllib.request.urlopen(url, timeout=1).close()
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("Web preview did not become ready")
            print(f"UI_URL={url} STOP_FILE={api.root / 'stop'}", flush=True)
            for _ in range(900):
                if (api.root / "stop").exists():
                    break
                if web.poll() is not None:
                    raise RuntimeError("Web preview stopped")
                time.sleep(1)
        finally:
            web.terminate()
            web.wait(timeout=10)
