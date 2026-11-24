"""C16：临时本地 SSH server 的 known_hosts+公钥 executionProfile golden。"""
import asyncio
import os
from pathlib import Path
import tempfile
import time
import asyncssh
import httpx

B = os.environ.get("VIBEFLOW_TEST_BASE", "http://127.0.0.1:8000/api/v1")
if os.environ.get("VIBEFLOW_TEST_REMOTE") != "1":
    print("默认无远程 allowlist：C16 SSH 专项由 VIBEFLOW_TEST_REMOTE=1 启用 [SKIP]")
    raise SystemExit(0)
os.environ["VIBEFLOW_REMOTE_HOSTS"] = "127.0.0.1"
CLIENT_KEY = None


class Server(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def public_key_auth_supported(self):
        return True

    def validate_public_key(self, username, key):
        return key.export_public_key() == CLIENT_KEY.export_public_key()


async def handle_process(process):
    proc = await asyncio.create_subprocess_shell(
        process.command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    process.stdout.write(out.decode("utf-8", "replace"))
    process.stderr.write(err.decode("utf-8", "replace"))
    process.exit(proc.returncode)


async def main():
    global CLIENT_KEY
    with tempfile.TemporaryDirectory(prefix="vf-ssh-c16-") as temp:
        root = Path(temp)
        host_key = asyncssh.generate_private_key("ssh-rsa")
        client_key = asyncssh.generate_private_key("ssh-rsa")
        CLIENT_KEY = client_key
        client_path = root / "client_key"
        client_path.write_bytes(client_key.export_private_key())
        known = root / "known_hosts"
        known.write_bytes(b"127.0.0.1 " + host_key.export_public_key())
        server = await asyncssh.listen("127.0.0.1", 9128, server_factory=Server,
                                       server_host_keys=[host_key], process_factory=handle_process)
        try:
            async with httpx.AsyncClient(base_url=B, timeout=30) as client:
                suffix = time.time_ns()
                response = await client.post("/auth/register", json={
                    "email": f"remote-{suffix}@vibeflow.cn", "name": "remote", "password": "remote-pass-123"})
                token = response.json()["token"]
                headers = {"Authorization": f"Bearer {token}"}
                profile = {"host": "127.0.0.1", "port": 9128, "username": "tester",
                           "workdir": "/", "client_key": str(client_path), "known_hosts": str(known),
                           "command": "printf REMOTE_SSH_OK", "timeout_seconds": 10}
                response = await client.post("/remote:execute", json=profile, headers=headers)
                assert response.status_code == 200, response.text
                result = response.json()
                assert result["exit_status"] == 0 and "REMOTE_SSH_OK" in result["stdout"], result
                bad = dict(profile, host="127.0.0.2")
                response = await client.post("/remote:execute", json=bad, headers=headers)
                assert response.status_code == 400, response.text
                bad = dict(profile, known_hosts=str(root / "missing"))
                response = await client.post("/remote:execute", json=bad, headers=headers)
                assert response.status_code == 400, response.text
                print("C16 real SSH key auth, known_hosts, command output and host allowlist: OK")
        finally:
            server.close()
            await server.wait_closed()


asyncio.run(main())
