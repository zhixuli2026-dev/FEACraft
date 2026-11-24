"""Test-only HTTP barrier, preserving signed headers and real backend replies."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from urllib.parse import urlsplit


class DelayedRequest:
    def __init__(self, endpoint):
        self.target = urlsplit(endpoint)
        self.select = None
        self.ready = threading.Event()
        self.release = threading.Event()
        self.forwarded = threading.Event()
        self.status = None
        proxy = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *args):pass
            def request(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                delayed = proxy.select is not None and proxy.select(self.command, self.path, dict(self.headers), body)
                if delayed:
                    proxy.select = None
                    proxy.ready.set()
                    if not proxy.release.wait(55):raise TimeoutError('Delayed HTTP request release')
                connection = http.client.HTTPConnection(proxy.target.hostname, proxy.target.port, timeout=20)
                try:
                    connection.request(self.command, self.path, body=body, headers=dict(self.headers))
                    response = connection.getresponse();data = response.read()
                    if delayed:proxy.status = response.status;proxy.forwarded.set()
                    try:
                        self.send_response(response.status)
                        for key, value in response.getheaders():
                            if key.lower() not in {'transfer-encoding','connection','content-length'}:
                                self.send_header(key, value)
                        length = response.getheader('Content-Length','0') if self.command=='HEAD' else str(len(data))
                        self.send_header('Content-Length', length)
                        self.send_header('Connection', 'close');self.end_headers()
                        if self.command!='HEAD':self.wfile.write(data)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):pass
                finally:connection.close()
            do_GET = do_HEAD = do_PUT = do_DELETE = do_POST = request
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.endpoint = 'http://127.0.0.1:'+str(self.server.server_port)

    def __enter__(self):
        self.thread.start();return self

    def __exit__(self, *args):
        self.release.set();self.server.shutdown();self.server.server_close();self.thread.join(5)
