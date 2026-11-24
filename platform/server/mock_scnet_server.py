"""仅用于本地浏览器验收的 SCNET mock 服务，不用于生产。"""
from scnet_resources_smoke import Mock, ThreadingHTTPServer

if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 9129), Mock)
    print("SCNET mock ready on 127.0.0.1:9129", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
