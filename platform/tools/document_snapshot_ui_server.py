"""Disposable real API/UI for published document reader acceptance."""
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request

PLATFORM = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLATFORM/'server'))
from testsupport import IsolatedServer

with IsolatedServer('document-snapshot-browser') as api:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    with (api.root/'web.log').open('w', encoding='utf8') as log:
        web = subprocess.Popen(['node', str(PLATFORM/'web/node_modules/vite/bin/vite.js'),
            '--host', '127.0.0.1', '--port', str(port), '--strictPort'], cwd=PLATFORM/'web',
            env=dict(api.env, VITE_API_TARGET=api.base), stdout=log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            url = f'http://127.0.0.1:{port}'
            for _ in range(100):
                try:
                    urllib.request.urlopen(url, timeout=1).close()
                    break
                except OSError:
                    time.sleep(.1)
            else:
                raise RuntimeError('Disposable UI unavailable')
            print(f'UI_URL={url} STOP_FILE={api.root/"stop"}', flush=True)
            for _ in range(1800):
                if (api.root/'stop').exists():
                    break
                if web.poll() is not None:
                    raise RuntimeError('UI stopped')
                time.sleep(1)
            else:
                raise TimeoutError('Browser acceptance did not stop its owned server')
        finally:
            web.terminate()
            web.wait(timeout=10)
