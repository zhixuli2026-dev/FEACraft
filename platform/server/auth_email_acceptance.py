"""Verify email authentication and concurrent registration on isolated SQLite."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys

import httpx
import jwt

from testsupport import IsolatedServer, SERVER

EMAIL = "concurrent-auth@example.com"
PASSWORD = "isolated-register-duplicate-123"
SECRET = "isolated-test-auth-signing-secret-800404"
FIXTURE = """
import os
from threading import Barrier
os.environ['VIBEFLOW_JWT_SECRET'] = {secret!r}
from app.main import app
from app.routers import auth

both_checked = Barrier(2)
real_hash = auth.hash_password

def after_initial_lookup(password):
    if password == {password!r}:
        both_checked.wait(timeout=10)
    return real_hash(password)

auth.hash_password = after_initial_lookup
"""


def status(client, path, expected, *, method="GET", token=None, body=None):
    headers = {"Authorization": "Bearer " + token} if token is not None else {}
    result = client.request(method, path, headers=headers, json=body)
    assert result.status_code == expected, (path, expected, result.status_code)
    return result.json()


def exercise(api, report, baseline):
    credentials = {"email": EMAIL, "password": PASSWORD}
    with httpx.Client(base_url=api.base + "/api/v1", timeout=25, trust_env=False) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = [pool.submit(client.post, "/auth/register", json={**credentials, "name": name})
                       for name in ("Primary signup", "Parallel signup")]
            received = [future.result(timeout=25) for future in pending]
        report["concurrent_register_http"] = sorted(item.status_code for item in received)
        assert report["concurrent_register_http"] == [200, 500 if baseline else 409], report
        winning = next(item.json() for item in received if item.status_code == 200)
        if baseline:
            return
        rejected = next(item.json() for item in received if item.status_code == 409)
        assert rejected == {"detail": "邮箱已注册"}
        token, account = winning["token"], winning["user"]
        assert account["email"] == EMAIL
        assert status(client, "/auth/me", 200, token=token) == account
        workspaces = status(client, "/workspaces", 200, token=token)
        assert len(workspaces) == 1 and workspaces[0]["name"] == "Default"
        status(client, "/auth/register", 409, method="POST", body=credentials)
        status(client, "/auth/register", 422, method="POST", body={"email": "invalid", "password": PASSWORD})
        status(client, "/auth/login", 401, method="POST", body={**credentials, "password": PASSWORD + "-invalid"})
        assert status(client, "/auth/login", 200, method="POST", body=credentials)["user"] == account
        status(client, "/auth/me", 401)
        now = datetime.now(timezone.utc)
        expired = jwt.encode({"sub": str(account["id"]), "iat": now - timedelta(hours=1),
                             "exp": now - timedelta(minutes=1)}, SECRET, algorithm="HS256")
        status(client, "/auth/me", 401, token=expired)
        bad_signature = jwt.encode({"sub": str(account["id"]), "iat": now,
                                    "exp": now + timedelta(hours=1)}, SECRET + "-wrong", algorithm="HS256")
        status(client, "/auth/me", 401, token=bad_signature)

        with closing(sqlite3.connect(api.root / "test.sqlite")) as db:
            rows = db.execute("SELECT id, password_hash FROM users WHERE email=?", (EMAIL,)).fetchall()
            assert len(rows) == 1 and rows[0][0] == account["id"]
            assert rows[0][1].startswith("pbkdf2$") and PASSWORD not in rows[0][1]
            db.execute("UPDATE users SET status='disabled' WHERE id=?", (account["id"],))
            db.commit()
            status(client, "/auth/login", 401, method="POST", body=credentials)
            status(client, "/auth/me", 401, token=token)
            status(client, "/workspaces", 401, token=token)
            db.execute("UPDATE users SET status='active' WHERE id=?", (account["id"],))
            db.commit()
        assert status(client, "/auth/login", 200, method="POST", body=credentials)["user"] == account
        report["checks"] = ["atomic_email_registration", "default_workspace_created_once",
                            "valid_and_invalid_passwords", "missing_expired_and_bad_signature_tokens",
                            "disabled_status_blocks_login_and_existing_bearer", "active_status_recovers"]


def verify(baseline):
    api = IsolatedServer("email-auth-j01", app="auth_email_fixture:app")
    (api.root / "auth_email_fixture.py").write_text(FIXTURE.format(secret=SECRET, password=PASSWORD), encoding="utf8")
    result = {"passed": False, "baseline": baseline, "model_requests": 0, "isolated_api_port": api.port}
    print("EMAIL_AUTH_EVIDENCE", api.root, flush=True)
    try:
        with api:
            exercise(api, result, baseline)
        result["api_exit"] = api.proc.poll()
        assert result["api_exit"] is not None
        result["passed"] = True
    except BaseException as error:
        result["failure"] = {"type": type(error).__name__, "message": str(error)[:700]}
        raise
    finally:
        (api.root / "email-auth-result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf8")


if __name__ == "__main__":
    sys.path.insert(0, str(SERVER.parent / "tools"))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    verify(parser.parse_args().baseline)
