"""Real two-process launch race, retry fencing and submission transactions.

Uses disposable SQLite and optional uniquely named local PostgreSQL databases.
The configured developer database is never migrated or written.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid
from unittest.mock import patch

from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from workflow_migration_smoke import SERVER, alembic, engine_for, legacy


async def child(root, job_id, label):
    from app.db import SessionLocal, engine
    from app.models import JobSubmissionRequest
    from app.services import job_execution, jobrunner
    from fault_fixture import FileBarrier
    async with SessionLocal() as db:
        row = await db.scalar(select(JobSubmissionRequest).where(JobSubmissionRequest.job_id == job_id))
        spec = json.loads(row.execution_spec_json)
    (root / (label+".ready")).touch()
    for _ in range(1000):
        if (root / "race.release").exists():
            break
        await asyncio.sleep(0.01)
    else:
        raise TimeoutError("Parent did not release worker")
    try:
        proc = await jobrunner.start_job(job_id, spec["command"], spec["case_prefix"],
            runtime=spec["runtime"], argv=spec["argv"], timeout_seconds=spec["timeout_seconds"],
            max_log_bytes=spec["max_log_bytes"], evidence_run_dir=spec["evidence_run_dir"],
            on_finish=lambda *_: FileBarrier(root, "finalizing").wait_async())
        await proc.wait()
        print("OWNER_FINISHED")
    except job_execution.JobAlreadyClaimed:
        assert jobrunner.get_proc(job_id) is None
        print("DUPLICATE_REJECTED")
    finally:
        await engine.dispose()


async def wait_file(path):
    for _ in range(1000):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("Missing barrier: " + str(path))


async def verify(url, root):
    root.mkdir(parents=True)
    await asyncio.to_thread(alembic, url, root, "upgrade", "head")
    await legacy(url, seed=True)
    from app import db as app_db
    from app.models import Case, CaseEventCounter, DurableEvent, EventOutbox, Job, JobAttempt, JobExecutionHead
    from app.services import job_execution as service
    from app.services.workflow_state import ExecutionConflict, transition
    engine = engine_for(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    checks = []
    marker = root / "native-starts.txt"
    spec = {"command":"owned marker", "runtime":"windows", "case_prefix":"workspaces/101/cases/101/",
        "argv":[sys.executable, "-c", "from pathlib import Path; p=Path("+repr(str(marker))+
            "); f=p.open('a'); f.write('native-start\\n'); f.close(); print('owned worker'); raise SystemExit(1)"],
        "timeout_seconds":30, "max_log_bytes":1024*1024, "evidence_run_dir":None}
    digest = service.request_hash(spec)
    async def submit(key="first", request_digest=digest):
        async with sessions() as db:
            result = await service.submit(db, case_id=101, user_id=101, key=key,
                digest=request_digest, job_type="shell", command=spec["command"], spec=spec)
            await db.commit()
            return result
    try:
        async with sessions() as db:
            await db.execute(update(Case).where(Case.id == 101).values(s3_prefix=spec["case_prefix"]))
            await db.commit()
        submissions = await asyncio.gather(*[submit() for _ in range(8)])
        job = submissions[0][0]
        assert len({row.id for row, _ in submissions}) == 1 and sum(created for _, created in submissions) == 1
        checks.append("eight_concurrent_submissions_one_job_and_attempt")
        try:
            await submit(request_digest="d"*64)
            raise AssertionError("Different request reused the same key")
        except ExecutionConflict as error:
            assert error.code == "idempotency_conflict"
        for key in ("", "bad key", "x"*129, "中文"):
            try:
                await submit(key)
                raise AssertionError("Invalid key accepted")
            except ValueError:
                pass
        checks.append("changed_payload_and_invalid_keys_rejected")
        async with sessions() as db:
            before = await db.scalar(select(func.count()).select_from(DurableEvent))
            await service.submit(db, case_id=101, user_id=101, key="rolled-back", digest=digest,
                job_type="shell", command=spec["command"], spec=spec)
            await db.rollback()
        async with sessions() as db:
            assert await service.lookup_submission(db, 101, 101, "rolled-back", digest) is None
            assert await db.scalar(select(func.count()).select_from(DurableEvent)) == before
            assert (await db.get(CaseEventCounter, 101)).next_seq == before+1
        checks.append("submission_rollback_includes_attempt_event_and_counter")
        async with sessions() as db:
            try:
                await service.claim(db, job.id, dict(spec, command="wrong delivery"))
                raise AssertionError("Altered execution specification accepted")
            except ExecutionConflict as error:
                assert error.code == "invalid_request"
            await db.rollback()
        checks.append("delivery_specification_checked_before_claim")

        env = dict(os.environ, VIBEFLOW_DATABASE_URL=url, PYTHONIOENCODING="utf-8",
            VIBEFLOW_STORAGE_BACKEND="fs", VIBEFLOW_STORAGE_FS_ROOT=str(root / "files"),
            VIBEFLOW_INDEX_ROOT=str(root / "index"), VIBEFLOW_RUNTIME_CONFIG=str(root / "runtime.json"))
        children = []
        try:
            for label in ("worker-a", "worker-b"):
                children.append(subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()),
                    "--child", str(root), "--job", str(job.id), "--label", label], cwd=SERVER,
                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0))
            await asyncio.gather(wait_file(root / "worker-a.ready"), wait_file(root / "worker-b.ready"))
            (root / "race.release").touch()
            await wait_file(root / "finalizing.ready")
            async with sessions() as db:
                attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id))
                assert attempt.phase == "finalizing" and attempt.started_at and attempt.ended_at is None
                assert (await db.get(Job, job.id)).status == "running"
                assert json.loads(attempt.process_identity_json)["pid"] > 0
                old_owner = service.Claim(job.id, attempt.id, attempt.root_job_id, attempt.number, attempt.claim_token)
            assert marker.read_text().splitlines() == ["native-start"]
            (root / "finalizing.release").touch()
            outputs = await asyncio.gather(*[asyncio.to_thread(proc.communicate, timeout=15) for proc in children])
            for proc, (out, err) in zip(children, outputs):
                assert proc.returncode == 0, err.decode("utf-8", "replace")
            combined = b"".join(out for out, _ in outputs)
            assert combined.count(b"OWNER_FINISHED") == 1 and combined.count(b"DUPLICATE_REJECTED") == 1
        finally:
            for index, proc in enumerate(children):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)
                out, err = proc.communicate(timeout=10)
                (root / f"worker-{index}.log").write_bytes(out+b"\n"+err)
        checks.append("two_real_workers_spawn_exactly_one_native_process")
        checks.append("finalizing_phase_committed_before_finish_hook_and_ack")
        async with sessions() as db:
            old = await db.get(Job, job.id)
            attempt = await db.get(JobAttempt, old_owner.attempt_id)
            assert old.status == attempt.phase == "failed" and old.ended_at and attempt.ended_at
            old_ended = old.ended_at
            original_log = root / "files" / spec["case_prefix"] / old.log_s3_key
            old_bytes = original_log.read_bytes()
            retry, next_attempt = await service.allocate_retry(db, old_owner.attempt_id)
            await db.commit()
            assert retry.id != old.id and next_attempt.number == 2
            assert next_attempt.retry_of_attempt_id == old_owner.attempt_id
            assert retry.log_s3_key != old.log_s3_key
        with patch.object(app_db, "SessionLocal", sessions):
            for owner in (old_owner, replace(old_owner, token="forged")):
                try:
                    await service.finish(owner, "succeeded", 0)
                    raise AssertionError("Stale callback overwrote retry")
                except ExecutionConflict:
                    pass
            async with sessions() as db:
                try:
                    await transition(db, "attempt", old_owner.attempt_id, "failed",
                        expected_version=attempt.version, claim_token=old_owner.token, reason_code="process_exit_failed")
                    raise AssertionError("Superseded no-op was accepted")
                except ExecutionConflict:
                    await db.rollback()
            async with sessions() as db:
                new_owner = await service.claim(db, retry.id, spec)
                await db.commit()
            try:
                await service.phase(replace(new_owner, token="wrong"), "starting")
                raise AssertionError("Forged ownership accepted")
            except ExecutionConflict:
                pass
            await service.phase(new_owner, "starting")
            # No process for this retry: injected startup failure tests owner-only completion.
            assert await service.finish(new_owner, "failed", -1, reason="process_start_failed") == "failed"
        async with sessions() as db:
            old = await db.get(Job, job.id)
            assert old.status == "failed" and old.ended_at == old_ended and original_log.read_bytes() == old_bytes
            head = await db.get(JobExecutionHead, job.id)
            assert head.next_number == 3 and head.active_attempt_id == next_attempt.id
        checks.append("retry_allocates_new_job_number_and_preserves_old_terminal_and_log")
        checks.append("superseded_and_forged_callbacks_cannot_change_new_attempt")
        checks.append("startup_failure_ends_owned_attempt")
        assert (await submit())[0].id == job.id
        unkeyed = await asyncio.gather(submit(None), submit(None))
        assert unkeyed[0][0].id != unkeyed[1][0].id
        checks.append("durable_replay_returns_original_job_and_null_keys_are_independent")
        with patch.object(app_db, "SessionLocal", sessions):
            from app.services import jobrunner
            queued_job = unkeyed[0][0]
            await jobrunner._persist_status(queued_job.id, "cancelled")
            async with sessions() as db:
                try:
                    await service.claim(db, queued_job.id, spec)
                    raise AssertionError("Cancelled queue item claimed")
                except service.JobAlreadyClaimed:
                    await db.rollback()
            claimed_job = unkeyed[1][0]
            owner = await service.claim_job(claimed_job.id, spec)
            await jobrunner._persist_status(claimed_job.id, "cancelled")
            try:
                await service.phase(owner, "starting")
                raise AssertionError("Cancelled claim advanced to spawn")
            except service.JobAlreadyClaimed:
                pass
            checks.append("queued_and_claimed_cancellation_blocks_native_start")
            await submit("claim-rollback")
            from sqlalchemy.ext.asyncio import AsyncSession
            async def broken_commit(_db):
                raise OSError("Injected claim commit failure")
            pending, _ = await submit("claim-rollback")
            with patch.object(AsyncSession, "commit", broken_commit), \
                    patch.object(asyncio, "create_subprocess_exec") as spawn:
                try:
                    await jobrunner.start_job(pending.id, spec["command"], spec["case_prefix"], argv=spec["argv"],
                        timeout_seconds=spec["timeout_seconds"], max_log_bytes=spec["max_log_bytes"])
                    raise AssertionError("Launch proceeded despite failed claim commit")
                except OSError:
                    pass
                spawn.assert_not_called()
            async with sessions() as db:
                rolled = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == pending.id))
                assert rolled.phase == "queued" and rolled.claim_token is None
            checks.append("failed_claim_commit_rolls_back_and_never_spawns")
        async with sessions() as db:
            events = (await db.scalars(select(DurableEvent).where(DurableEvent.case_id == 101)
                .order_by(DurableEvent.seq))).all()
            assert [e.seq for e in events] == list(range(1, len(events)+1))
            assert await db.scalar(select(func.count()).select_from(EventOutbox)) == len(events)
            assert all(old_owner.token not in e.payload_json for e in events)
        checks.append("contiguous_events_with_outbox_and_no_claim_credentials")
    finally:
        await engine.dispose()
    result = {"passed":True, "checks":checks, "count":len(checks)}
    (root / "result.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--child", type=Path)
    parser.add_argument("--job", type=int)
    parser.add_argument("--label")
    args = parser.parse_args()
    if args.child:
        await child(args.child, args.job, args.label)
        return
    report = Path(tempfile.mkdtemp(prefix="job-claim-", dir=SERVER.parents[1] / "tmp"))
    pg_url = None
    if args.postgres:
        from app.config import Settings
        pg_url = make_url(Settings(_env_file=SERVER / ".env").database_url)
        assert pg_url.drivername == "postgresql+asyncpg" and pg_url.host in {"127.0.0.1", "localhost", "::1"}
    sqlite_url = "sqlite+aiosqlite:///"+(report / "test.db").as_posix()
    os.environ["VIBEFLOW_DATABASE_URL"] = sqlite_url
    os.environ.update(VIBEFLOW_STORAGE_BACKEND="fs", VIBEFLOW_STORAGE_FS_ROOT=str(report / "files"))
    results = {"sqlite":await verify(sqlite_url, report / "sqlite")}
    if pg_url:
        import asyncpg
        name = "tmp_vf_claim_"+uuid.uuid4().hex
        assert re.fullmatch(r"tmp_vf_claim_[0-9a-f]{32}", name)
        admin = await asyncpg.connect(host=pg_url.host, port=pg_url.port or 5432, user=pg_url.username,
            password=pg_url.password, database="postgres", timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"')
            created = True
            results["postgresql"] = await verify(pg_url.set(database=name).render_as_string(hide_password=False), report / "postgresql")
        finally:
            if created:
                assert await admin.fetchval("SELECT current_database()") == "postgres"
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results["postgresql"]["temporary_database_removed"] = True
    (report / "result.json").write_text(json.dumps(results, indent=2)+"\n", encoding="utf-8")
    print("JOB_CLAIM_OK "+json.dumps({"databases":list(results), "evidence":str(report)}))


if __name__ == "__main__":
    asyncio.run(main())
