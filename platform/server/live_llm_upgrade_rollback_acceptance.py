"""Run two real application releases against disposable PostgreSQL and restore a cold backup."""
import argparse
import asyncio
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile

import asyncpg
import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url

from app.config import Settings
from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer, SERVER

ROOT = SERVER.parents[1]
PREVIOUS = "c20e87f143b9"
CURRENT = "d31f98a254ca"
PACKAGE = ROOT / "platform/backups/mainline-N5-07-native-lineage-real-ui-20261110.zip"

LEGACY_INPUT = '''
from fastapi import Depends
from sqlalchemy import select
from app.db import get_db
from app.deps import get_current_user
from app.models import JobAttempt
from app.routers.workspaces import get_owned_case
from app.services import job_delivery, job_execution
from app.services.case_write_capacity import committing

@app.post("/api/v1/testing/rollback-legacy-input/{case_id}")
async def legacy_input(case_id: int, user=Depends(get_current_user), db=Depends(get_db)):
    case = await get_owned_case(case_id, user, db, write=True)
    async with committing(db, case):
        spec = {"command":"echo LEGACY", "runtime":"windows", "argv":None, "case_prefix":case.s3_prefix,
                "timeout_seconds":30, "max_log_bytes":1048576, "evidence_run_dir":None}
        job, _ = await job_execution.submit(db, case_id=case.id, user_id=user.id, key=None,
            digest=job_execution.request_hash(spec), job_type="shell", command=spec["command"],
            spec=spec, input_paths=["Ref/rollback.txt"])
        attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id))
        manifest = json.loads(attempt.input_manifest_json)
        del manifest["workspace"]
        attempt.input_manifest_json = json.dumps(manifest)
    assert await job_delivery.deliver(job.id) == "input_recovery_required"
    return {"id": job.id}
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def inventory(root):
    return {p.relative_to(root).as_posix(): digest(p.read_bytes()) for p in root.rglob("*") if p.is_file()}


def extract_previous(destination):
    checksum = digest(PACKAGE.read_bytes())
    assert checksum == Path(str(PACKAGE) + ".sha256").read_text().split()[0]
    with zipfile.ZipFile(PACKAGE) as archive:
        for name in archive.namelist():
            prefix = "platform/server/"
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            relative = Path(name[len(prefix):])
            if relative.parts[0] not in {"app", "migrations", "alembic.ini"} or relative.name == ".gitkeep":
                continue
            path = (destination / relative).resolve()
            assert path.is_relative_to(destination) and not any(p.startswith(".") for p in relative.parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(name))
    for code, expected in ((destination, PREVIOUS), (SERVER, CURRENT)):
        cfg = Config(str(code / "alembic.ini"))
        cfg.set_main_option("script_location", str(code / "migrations"))
        assert ScriptDirectory.from_config(cfg).get_current_head() == expected
    return checksum


@contextmanager
def application(code, url, state, report, label, store=None):
    api = IsolatedServer("rollback-" + label, app="rollback_fixture:app", database_url=url)
    settings = {key: str(state / val) for key, val in (
        ("VIBEFLOW_STORAGE_FS_ROOT", "files"), ("VIBEFLOW_AUDIT_DIR", "audit"), ("VIBEFLOW_INDEX_ROOT", "index"),
        ("VIBEFLOW_RUNTIME_CONFIG", "runtime.json"), ("VIBEFLOW_VAULT_KEY_PATH", "vault.key"))}
    if store:
        settings.update(store.settings())
    # Older releases resolve the vault relative to the application. Use the same
    # private key location in both releases and copy it with the cold backup.
    fixture = FIXTURE.replace("llm._VAULT_KEY_PATH=root/'.vault_key'",
                              f"llm._VAULT_KEY_PATH=Path({str(state / 'vault.key')!r})")
    prelude = f"import os,sys\nsys.path.insert(0,{str(code)!r})\nos.environ.update({settings!r})\n"
    prelude += f"from pathlib import Path\nos.environ['VIBEFLOW_JWT_SECRET']=Path({str(state / 'jwt.key')!r}).read_text()\n"
    if label == "recovery-history":
        fixture += LEGACY_INPUT
    (api.root / "rollback_fixture.py").write_text(prelude + fixture +
        f"\nassert Path(llm.__file__).resolve().is_relative_to(Path({str(code)!r}))\n", encoding="utf8")
    record = {"phase": label, "evidence": str(api.root)}
    report["servers"].append(record)
    try:
        with api:
            yield api
    finally:
        record["exited"] = api.proc is not None and api.proc.poll() is not None


async def snapshot(connection):
    tables = await connection.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
    result = {}
    for table, in tables:
        assert re.fullmatch("[a-z_]+", table)
        rows = await connection.fetch(f'SELECT to_jsonb(t)::text FROM "{table}" t')
        result[table] = {"rows": len(rows), "sha256": digest("\n".join(sorted(r[0] for r in rows)).encode())}
    return result


async def sequences(connection):
    values = {}
    for name, in await connection.fetch("SELECT sequencename FROM pg_sequences WHERE schemaname='public'"):
        assert re.fullmatch("[a-z_]+", name)
        values[name] = tuple(await connection.fetchrow(f'SELECT last_value, is_called FROM "{name}"'))
    return values


def login(api, credentials):
    api.token = api.call("POST", "/auth/login", credentials)["token"]


def seed(api, credentials, configuration, proof):
    owner, member, _ = credentials
    for user in credentials:
        api.call("POST", "/auth/register", {**user, "name": user["email"].split("@")[0]})
    login(api, owner)
    workspace = api.call("GET", "/workspaces")[0]["id"]
    org = api.call("GET", "/org/mine")["org"]["id"]
    case = api.call("POST", f"/workspaces/{workspace}/cases", {"name": "Cross-release archive"})["id"]
    prefix = f"/cases/{case}"
    api.call("PUT", prefix + "/file", {"path": "Ref/rollback.txt", "content": proof})
    api.call("POST", f"/org/{org}/members", {"email": member["email"], "role": "viewer"}, expect=201)
    login(api, member)
    api.call("POST", f"/org/{org}/invitation:accept", {})
    login(api, owner)
    api.call("PUT", f"/workspaces/{workspace}/sharing", {"org_id": org, "role": "viewer", "enabled": True})
    profile = api.call("POST", "/provider-profiles", {"provider": "deepseek", "name": "Rollback drill",
        "base_url": configuration["base_url"], "default_model": configuration["model"], "api_key": configuration["key"]})
    return {"workspace": workspace, "org": org, "case": case, "profile": profile["id"], "session": api.token}


def access(api, credentials, ids, proof):
    api.token = ids["session"]
    assert api.call("GET", "/auth/me")["email"] == credentials[0]["email"]
    members = api.call("GET", f'/org/{ids["org"]}/members')["members"]
    assert sorted(row["role"] for row in members) == ["admin", "viewer"], [r["role"] for r in members]
    login(api, credentials[1])
    prefix = f'/cases/{ids["case"]}'
    backup = f'/workspaces/{ids["workspace"]}/backup'
    assert api.call("GET", prefix + "/raw?path=Ref/rollback.txt", raw=True) == proof.encode()
    api.call("PUT", prefix + "/file", {"path": "Ref/rollback.txt", "content": "viewer edit"}, expect=(403, 404))
    api.call("GET", backup, expect=404)
    login(api, credentials[2])
    api.call("GET", prefix + "/raw?path=Ref/rollback.txt", expect=404)
    api.call("GET", backup, expect=404)
    login(api, credentials[0])
    assert api.call("GET", prefix + "/raw?path=Ref/rollback.txt", raw=True) == proof.encode()


def readback(api, ids, model, proof):
    conv = api.call("POST", "/conversations", {"case_id": ids["case"], "title": "Database version readback",
        "permission_mode": "plan", "provider_profile_id": ids["profile"], "model": model})
    events = []
    with httpx.Client(base_url=api.base + "/api/v1", headers={"Authorization": "Bearer " + api.token},
                      timeout=180, trust_env=False) as client:
        with client.stream("POST", f'/conversations/{conv["id"]}/messages', json={
            "content": "请仅调用一次 read_text_file 读取 Ref/rollback.txt，然后原样输出文件中的完整字符串。",
            "provider_profile_id": ids["profile"], "model": model}) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:]))
    result, = [event for event in events if event.get("type") == "tool.result"]
    assert result["ok"] and result["tool"] == "read_text_file" and proof in result["result"]
    assert "".join(e.get("text", "") for e in events if e.get("type") == "text.delta") == proof
    assert any(e.get("type") == "run.completed" for e in events)
    receipts = [json.loads(line) for line in (api.root / "upstream.jsonl").read_text().splitlines()]
    calls = [r for r in receipts if r["kind"] == "http" and r["path"].endswith("/chat/completions")]
    assert calls and all(r["status"] == 200 and r["host"] == "api.deepseek.com" for r in calls)
    return {"model": model, "calls": len(calls), "conversation": conv["id"], "proof_digest": digest(proof.encode())}


def create_recovery(api, ids):
    job = api.call("POST", f'/testing/rollback-legacy-input/{ids["case"]}', {})
    route = "/jobs/" + str(job["id"])
    plan = api.call("GET", route + "/input-recovery")
    assert plan["available"]
    body = {key: plan[key] for key in ("source_attempt_id", "source_snapshot_sha256", "expected_revision",
                                        "expected_event_sequence")}
    body.update(action="reprepare_current_inputs", command="echo ROLLBACK_HISTORY", runtime="windows",
                timeout_seconds=30, input_paths=["Ref/rollback.txt"])
    replacement = api.call("POST", route + ":recover-inputs", body, expect=202,
                           extra_headers={"Idempotency-Key": uuid.uuid4().hex})
    assert api.wait_job(replacement["id"])["attempt"]["process_stop"]["verified"]
    receipt = replacement["input_recovery"]
    assert receipt["source_job_id"] == job["id"] and receipt["target_job_id"] == replacement["id"]
    return receipt


async def run(profile, s3):
    configuration = await source(profile)
    url = make_url(Settings(_env_file=SERVER / ".env").database_url)
    assert url.drivername == "postgresql+asyncpg" and url.host in {"127.0.0.1", "::1", "localhost"}
    pg = Path(r"C:\Program Files\PostgreSQL\16\bin")
    root = Path(tempfile.mkdtemp(prefix="cross-release-", dir=ROOT / "tmp"))
    print("ROLLBACK_EVIDENCE", root, flush=True)
    state = root / "state"
    state.mkdir()
    (state / "jwt.key").write_text(uuid.uuid4().hex + uuid.uuid4().hex, encoding="ascii")
    old = root / "previous"
    report = {"passed": False, "storage": "s3" if s3 else "fs", "servers": [], "object_stores": [],
              "checks": [], "readbacks": [], "databases_removed": []}
    def check(label):
        report["checks"].append(label)
        (root / "result.json").write_text(json.dumps(report, indent=2), encoding="utf8")
        print("ROLLBACK_PASS", label, flush=True)
    report["package_sha256"] = extract_previous(old)
    original, restored = ["tmp_vf_rollback_" + uuid.uuid4().hex for _ in range(2)]
    connect = dict(host=url.host, port=url.port or 5432, user=url.username, password=url.password)
    admin = await asyncpg.connect(**connect, database="postgres")
    created = []
    def database(name):
        assert name in created
        return url.set(database=name).render_as_string(hide_password=False)
    def command(name, code, args, *, refusal=False):
        env = dict(os.environ, VIBEFLOW_DATABASE_URL=database(name), PGPASSWORD=url.password or "",
                   PGCLIENTENCODING="UTF8", PYTHONIOENCODING="utf-8")
        proc = subprocess.run(args, cwd=code, env=env, capture_output=True, timeout=180)
        if refusal:
            assert proc.returncode and b"would discard job input recovery history" in proc.stderr
        elif proc.returncode:
            raise RuntimeError(f"{Path(args[0]).name} {args[1:4]} failed: " +
                               proc.stderr.decode("utf8", "replace")[-2000:].replace(url.password or "\0", "[redacted]"))
    def migrate(code, direction, revision, refusal=False):
        command(original, code, [sys.executable, "-B", "-m", "alembic", direction, revision], refusal=refusal)
    async def rows(name, reader=snapshot):
        db = await asyncpg.connect(**connect, database=name)
        try:
            return await reader(db)
        finally:
            await db.close()
    credentials = [{"email": who + "@example.com", "password": uuid.uuid4().hex} for who in ("owner", "reader", "outsider")]
    proof = "ROLLBACK_" + uuid.uuid4().hex
    pg_args = ["--host", url.host, "--port", str(url.port or 5432), "--username", url.username, "--no-password"]
    services = ExitStack()
    def open_store(checkpoint=None):
        from s3testsupport import ObjectStore
        instance = services.enter_context(ObjectStore(executable=ROOT / "third_party/seaweed-4.52-runtime/weed",
                                                     volume_max=8, restore_from=checkpoint))
        report["object_stores"].append(str(instance.root))
        return instance
    def server(code, db_name, data, phase):
        return application(code, database(db_name), data, report, phase, store)

    try:
        store = open_store() if s3 else None
        for name in (original, restored):
            assert re.fullmatch(r"tmp_vf_rollback_[0-9a-f]{32}", name)
            await admin.execute(f'CREATE DATABASE "{name}"')
            created.append(name)
        migrate(old, "upgrade", "head")
        with server(old, original, state, "original") as api:
            ids = seed(api, credentials, configuration, proof)
            access(api, credentials, ids, proof)
        if store:
            checkpoint = store.root
            services.close()
            assert store.proc.poll() is not None
        before = await rows(original)
        before_sequences = await rows(original, sequences)
        (root / "before-table-hashes.json").write_text(json.dumps(before, indent=2), encoding="utf8")
        cold = root / "cold-state"
        command(original, SERVER, [str(pg / "pg_dump.exe"), *pg_args, "--format=custom", "--no-owner", "--no-acl",
                                   "--file", str(root / "before.pgdump"), original])
        shutil.copytree(state, cold)
        originals = inventory(cold)
        assert originals == inventory(state) and {"vault.key", "jwt.key"} <= originals.keys()
        report.update(previous=PREVIOUS, upgraded=CURRENT, tables=len(before), files=len(originals),
                      dump_sha256=digest((root / "before.pgdump").read_bytes()))
        check("old_application_and_consistent_org_database_files_vault_backup")
        if s3:
            store = open_store(checkpoint)
        migrate(SERVER, "upgrade", "head")
        upgraded = await rows(original)
        assert set(upgraded) == set(before) | {"job_input_recoveries"}
        assert all(upgraded[key] == value for key, value in before.items() if key != "alembic_version")
        with server(SERVER, original, state, "upgraded") as api:
            access(api, credentials, ids, proof)
            report["readbacks"].append(readback(api, ids, configuration["model"], proof))
            api.call("PUT", f'/cases/{ids["case"]}/file', {"path": "Ref/new-version.txt", "content": "after upgrade"})
        after_work = await rows(original)
        migrate(SERVER, "downgrade", PREVIOUS)
        down = await rows(original)
        assert set(down) == set(before) and down["alembic_version"] == before["alembic_version"]
        assert all(value == after_work[key] for key, value in down.items() if key != "alembic_version")
        with server(old, original, state, "downgraded") as api:
            access(api, credentials, ids, proof)
            assert api.call("GET", f'/cases/{ids["case"]}/raw?path=Ref/new-version.txt', raw=True) == b"after upgrade"
            conv = report["readbacks"][0]["conversation"]
            assert proof in json.dumps(api.call("GET", f"/conversations/{conv}"))
        check("in_place_upgrade_and_downgrade_preserve_compatible_new_work")
        migrate(SERVER, "upgrade", "head")
        with server(SERVER, original, state, "recovery-history") as api:
            login(api, credentials[0])
            receipt = create_recovery(api, ids)
        with_history = await rows(original)
        assert with_history["job_input_recoveries"]["rows"] == 1
        migrate(SERVER, "downgrade", PREVIOUS, refusal=True)
        assert await rows(original) == with_history
        with server(SERVER, original, state, "after-refusal") as api:
            login(api, credentials[0])
            assert api.call("GET", f'/jobs/{receipt["target_job_id"]}')["input_recoveries"] == [receipt]
        report["protected_recovery"] = {"source": receipt["source_job_id"], "target": receipt["target_job_id"]}
        check("populated_downgrade_refused_with_all_table_contents_and_public_recovery_link_intact")
        if s3:
            services.close()
            assert store.proc.poll() is not None
            store = open_store(checkpoint)
        command(restored, old, [str(pg / "pg_restore.exe"), *pg_args, "--no-owner", "--no-acl",
                                "--exit-on-error", "--dbname", restored, str(root / "before.pgdump")])
        rollback = root / "rollback-state"
        shutil.copytree(cold, rollback)
        assert await rows(restored) == before and inventory(rollback) == originals
        assert await rows(restored, sequences) == before_sequences
        report["restored_sequences"] = len(before_sequences)
        with server(old, restored, rollback, "restored") as api:
            access(api, credentials, ids, proof)
            api.call("GET", f'/cases/{ids["case"]}/raw?path=Ref/new-version.txt', expect=404)
            report["readbacks"].append(readback(api, ids, configuration["model"], proof))
        check("cold_backup_restores_every_table_file_org_role_and_original_provider_on_old_code")
    except BaseException as error:
        report["failure"] = {"type": type(error).__name__, "detail": str(error)[:1500].replace(configuration["key"], "[redacted]")}
        raise
    finally:
        try:
            for name in created:
                await admin.execute(f'DROP DATABASE "{name}"')
                report["databases_removed"].append(name)
        finally:
            await admin.close()
            try:
                services.close()
                assert all(item["exited"] for item in report["servers"])
                if s3 and report["checks"][-1:] == ["cold_backup_restores_every_table_file_org_role_and_original_provider_on_old_code"]:
                    for path in report["object_stores"][1:]:
                        assert json.loads((Path(path) / "restore-final.json").read_text())["original_unchanged"]
                report["passed"] = (len(report["checks"]) == 4 and "failure" not in report
                                    and len(report["databases_removed"]) == 2)
            finally:
                (root / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-profile-id", type=int, required=True)
    parser.add_argument("--s3", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.source_profile_id, args.s3))
