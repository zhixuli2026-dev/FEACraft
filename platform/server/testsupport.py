"""Disposable API/SQLite/fs harness; never uses the developer database or port 8000."""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

SERVER = Path(__file__).resolve().parent


async def execution_seed(url):
    """Keep migration history, but do not place an unverified old writer in the execution case."""
    from sqlalchemy import update
    from workflow_migration_smoke import engine_for, legacy
    from app.models import Case, Job
    from app import storage
    from app.services.casedict import SCAFFOLD_FILES
    await legacy(url,seed=True)
    engine=engine_for(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(Case.__table__.insert().values(id=9901,workspace_id=101,
                name='Unverified migration history',s3_prefix='migration/legacy-history/'))
            await connection.execute(update(Job).where(Job.id==101).values(case_id=9901))
        for name,content in SCAFFOLD_FILES.items():storage.put_text('migration/legacy-history/',name,content)
    finally:await engine.dispose()


def intercept_owned_spawn(original, transform):
    """Inject payload faults while retaining the real production launch gates."""
    from app.services.owned_process import GATE
    async def spawn(*args,**kwargs):
        if (len(args)>9 and tuple(args[1:4])==('-I','-S','-u')
                and Path(str(args[4])).resolve()==GATE and '--' in args):
            boundary=args.index('--')+1
            prefix,payload=args[:boundary],args[boundary:]
            async def forward(*new_args,**new_kwargs):
                return await original(*prefix,*new_args,**new_kwargs)
            return await transform(forward,*payload,**kwargs)
        return await transform(original,*args,**kwargs)
    return spawn


class IsolatedServer:
    def __init__(self, label="integrity", *, app="app.main:app", database_url=None, tls=False, access_log=False):
        self.app = app
        self.access_log = access_log
        self.tls_context = None
        self.tls_args = []
        if database_url is not None:
            import re
            from sqlalchemy.engine import make_url
            parsed = make_url(database_url)
            if (parsed.drivername != "postgresql+asyncpg" or parsed.host not in {"127.0.0.1", "localhost", "::1"}
                    or not re.fullmatch(r"tmp_vf_[a-z]+_[0-9a-f]{32}", parsed.database or "")):
                raise ValueError("Explicit test database must be a named disposable local PostgreSQL database")
        self.database_url = database_url
        evidence_root = SERVER.parent.parent / "tmp"
        evidence_root.mkdir(exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix=f"{label}-", dir=evidence_root))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.token = None
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if tls:
            import ipaddress
            import ssl
            from datetime import datetime, timedelta, timezone
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from cryptography.x509.oid import NameOID
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Isolated API fixture')])
            certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
                .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
            cert_path, key_path = self.root / 'fixture-cert.pem', self.root / 'fixture-key.pem'
            cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            os.chmod(key_path, 0o600)
            self.tls_args = ['--ssl-certfile', str(cert_path), '--ssl-keyfile', str(key_path)]
            self.tls_context = ssl.create_default_context(cafile=str(cert_path))
            self.base = f'https://127.0.0.1:{self.port}'
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=self.tls_context))
        self.proc = None

    def __enter__(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8",
                   VIBEFLOW_DATABASE_URL=self.database_url or "sqlite+aiosqlite:///" + (self.root / "test.sqlite").as_posix(),
                   VIBEFLOW_STORAGE_BACKEND="fs", VIBEFLOW_STORAGE_FS_ROOT=str(self.root / "files"),
                   VIBEFLOW_INDEX_ROOT=str(self.root / "index"),
                   VIBEFLOW_AUDIT_DIR=str(self.root / "audit"),
                   VIBEFLOW_RUNTIME_CONFIG=str(self.root / "runtime.json"),
                   VIBEFLOW_CODEX_STATE_ROOT=str(self.root / "cli-state"),
                    VIBEFLOW_SKILLS_STORAGE_ROOT=str(self.root / "isolated-skills"),
                   VIBEFLOW_JWT_SECRET=uuid.uuid4().hex, VIBEFLOW_DISTRIBUTED_JOBS="false",
                   VIBEFLOW_STORAGE_MAINTENANCE_ENABLED="false",
                   VIBEFLOW_RATE_LIMIT_API_PER_MINUTE="10000")
        self.env = env
        env["PYTHONPATH"] = str(self.root) + os.pathsep + env.get("PYTHONPATH", "")
        self.log = (self.root / "api.log").open("w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", self.app,
            "--app-dir", str(SERVER), "--host", "127.0.0.1", "--port", str(self.port),
            *([] if self.access_log else ['--no-access-log']), *self.tls_args], cwd=self.root, env=env, stdout=self.log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        for _ in range(150):
            if self.proc.poll() is not None:
                self.__exit__(RuntimeError, None, None)
                raise RuntimeError((self.root / "api.log").read_text(encoding="utf-8")[-5000:])
            try:
                if self.call("GET", "/api/health", api_prefix=False).get("ok"):
                    print(f"ISOLATED_API={self.base} EVIDENCE={self.root}", flush=True)
                    return self
            except (OSError, ValueError):
                time.sleep(0.1)
        self.__exit__(TimeoutError, None, None)
        raise TimeoutError(f"Isolated API did not become ready: {self.root}")

    def __exit__(self, error_type, error, _traceback):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.log.close()
        (self.root / "result.json").write_text(json.dumps({
            "status": "failed" if error_type else "passed", "isolated_database": True,
            "isolated_storage": True, "api": self.base}, indent=2), encoding="utf-8")

    def call(self, method, path, body=None, *, expect=200, raw=False, api_prefix=True, extra_headers=None):
        url = self.base + ("/api/v1" if api_prefix else "") + path
        headers = {"Authorization": "Bearer " + self.token} if self.token else {}
        headers.update(extra_headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            response = self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            payload = response.read()
            if response.code not in ((expect,) if isinstance(expect, int) else expect):
                raise AssertionError(f"{method} {path}: expected {expect}, got {response.code}: "
                                     + payload.decode("utf-8", "replace")[:1500])
            return payload if raw else json.loads(payload)

    def create_case(self, name):
        registration = self.call("POST", "/auth/register", {
            "email": f"{uuid.uuid4().hex}@example.com", "password": "isolated-test-123", "name": name})
        self.token = registration["token"]
        workspace = self.call("GET", "/workspaces")[0]
        case = self.call("POST", f"/workspaces/{workspace['id']}/cases", {"name": name})
        self.case_id = case["id"]
        self.prefix = f"/cases/{self.case_id}"
        return case

    def wait_job(self, job_id, timeout=180, expected="succeeded"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.call("GET", f"/jobs/{job_id}")
            if job["status"] not in ("queued", "running"):
                if job["status"] != expected:
                    raise AssertionError(f"Job {job_id}: {job['status']}\n{job.get('log_backlog', '')}")
                return job
            time.sleep(0.1)
        self.call("POST", f"/jobs/{job_id}:cancel")
        raise TimeoutError(f"Job {job_id} exceeded {timeout}s")

    def put(self, path, content):
        return self.call("PUT", self.prefix + "/file", {"path": path, "content": content})

    def advance(self, target, *, confirmed=True, expected_revision=None, expect=200):
        if expected_revision is None:
            expected_revision = self.call("GET", self.prefix + "/caseDict")["dict_revision"]
        return self.call("POST", self.prefix + "/caseDict:advance", {
            "state": target, "confirmed": confirmed, "expected_revision": expected_revision}, expect=expect)
