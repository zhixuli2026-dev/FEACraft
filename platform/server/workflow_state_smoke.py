"""Real DB CAS, state/event rollback and process-crash outbox recovery."""
from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from workflow_migration_smoke import alembic, engine_for, legacy

SERVER = Path(__file__).resolve().parent


async def delivery_child(root, crash):
    from app.services.durable_events import deliver_pending
    engine = engine_for(os.environ["VIBEFLOW_DATABASE_URL"])
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async def publisher(event):
        with (root / "receipts.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event_id":event.id, "seq":event.seq})+"\n")
            handle.flush()
            os.fsync(handle.fileno())
        if crash:
            os._exit(23)  # Real crash after delivery, before outbox acknowledgement.
    try:
        await deliver_pending(sessions, publisher=publisher, limit=1, lease_seconds=0.3, delivery_timeout=0.1)
    finally:
        await engine.dispose()


async def verify(url, root):
    root.mkdir(parents=True, exist_ok=False)
    await asyncio.to_thread(alembic, url, root, "upgrade", "head")
    await legacy(url, seed=True)
    from app.models import Case, CaseEventCounter, DurableEvent, EventOutbox, Job, JobAttempt, WorkflowRun, WorkflowStep
    from app.services.durable_events import append_event, deliver_pending, now
    from app.services.workflow_state import ExecutionConflict, transition
    engine = engine_for(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    checks = []

    async def run_record(*, status="queued", step_status="pending", case_id=101):
        run_id, step_id = str(uuid.uuid4()), str(uuid.uuid4())
        async with sessions() as db:
            await db.execute(WorkflowRun.__table__.insert().values(id=run_id, case_id=case_id, org_id=101, created_by=101,
                target="report", status=status, request_hash="a"*64, expected_revision=0, run_dir="C-run/test"))
            await db.execute(WorkflowStep.__table__.insert().values(id=step_id, run_id=run_id,
                step_key="geometry", handler="geometry", status=step_status))
            await db.commit()
        return run_id, step_id

    async def count_events():
        async with sessions() as db:
            return await db.scalar(select(func.count()).select_from(DurableEvent))

    async def change(kind, identifier, target, version, **kwargs):
        async with sessions() as db:
            result = await transition(db, kind, identifier, target, expected_version=version, **kwargs)
            await db.commit()
            return result

    try:
        run_id, step_id = await run_record()
        await change("run", run_id, "running", 0)
        before = await count_events()
        try:
            await change("run", run_id, "succeeded", 1)
            raise AssertionError("Run ignored unfinished step")
        except ExecutionConflict as error:
            assert error.code == "dependency_failed"
        await change("run", run_id, "failed", 1, reason_code="process_exit_failed", detail="solver exited", exit_code=7)
        async with sessions() as db:
            run = await db.get(WorkflowRun, run_id)
            assert run.version == 2 and run.ended_at and run.error_code == "process_exit_failed"
        try:
            await change("run", run_id, "running", 2)
            raise AssertionError("Terminal run reopened")
        except ExecutionConflict:
            pass
        assert await count_events() == before+1
        checks.append("terminal_immutable_reason_and_ended_at")

        run_id, step_id = await run_record()
        before = await count_events()
        async with sessions() as db:
            await transition(db, "run", run_id, "running", expected_version=0)
            await db.rollback()
        async with sessions() as db:
            assert (await db.get(WorkflowRun, run_id)).status == "queued"
            assert (await db.get(WorkflowRun, run_id)).version == 0
        assert await count_events() == before, "State/event survived parent rollback"
        checks.append("state_event_counter_outbox_rollback_together")

        async def competitor(target):
            try:
                await change("run", run_id, target, 0, **({"reason_code":"resource_limit"} if target == "failed" else {}))
                return "won"
            except ExecutionConflict:
                return "conflict"
        outcomes = await asyncio.gather(competitor("running"), competitor("failed"))
        assert sorted(outcomes) == ["conflict", "won"], outcomes
        assert await count_events() == before+1
        checks.append("concurrent_version_cas_has_one_winner_and_event")

        run_id, step_id = await run_record(status="running")
        await change("step", step_id, "ready", 0)
        await change("step", step_id, "running", 1)
        await change("step", step_id, "finalizing", 2)
        await change("step", step_id, "succeeded", 3)
        run = await change("run", run_id, "succeeded", 0)
        assert run.ended_at and run.version == 1
        checks.append("run_success_requires_all_steps_succeeded")

        run_id, step_id = await run_record(status="awaiting_confirmation", step_status="awaiting_confirmation")
        try:
            await change("step", step_id, "succeeded", 0)
            raise AssertionError("Generic transition bypassed engineering confirmation")
        except ExecutionConflict as error:
            assert error.code == "confirmation_required"
        checks.append("generic_state_changes_cannot_confirm_engineering")

        cancelled_run, _ = await run_record()
        cancelling = await change("run", cancelled_run, "cancelling", 0)
        assert cancelling.cancel_requested_at and cancelling.ended_at is None
        cancelled = await change("run", cancelled_run, "cancelled", 1)
        assert cancelled.cancel_requested_at and cancelled.ended_at and cancelled.error_code == "cancel_requested"
        checks.append("cancellation_request_and_terminal_reason_persist")

        attempt_id = str(uuid.uuid4())
        async with sessions() as db:
            db.add(JobAttempt(id=attempt_id, job_id=101, number=1, phase="claimed", worker_id="worker1", claim_token="current"))
            await db.commit()
        try:
            await change("attempt", attempt_id, "starting", 0, claim_token="stale")
            raise AssertionError("Stale worker mutated attempt")
        except ExecutionConflict:
            pass
        try:
            await change("attempt", attempt_id, "claimed", 0, claim_token="stale")
            raise AssertionError("Stale worker passed no-op claim check")
        except ExecutionConflict:
            pass
        for version, phase in enumerate(("starting", "running", "finalizing", "succeeded")):
            attempt = await change("attempt", attempt_id, phase, version, claim_token="current", exit_code=0 if phase=="succeeded" else None)
        assert attempt.started_at and attempt.ended_at and attempt.exit_code == 0 and attempt.version == 4
        checks.append("worker_fencing_and_attempt_lifecycle")

        # Uniform terminal semantics are checked on each real stored entity,
        # including cancellation's intermediate state and lost-worker interruption.
        attempt_number = 1
        for kind in ("run", "step", "attempt"):
            for terminal in ("failed", "cancelled", "interrupted"):
                owner = {}
                if kind == "attempt":
                    attempt_number += 1
                    identifier = str(uuid.uuid4())
                    async with sessions() as db:
                        db.add(JobAttempt(id=identifier, job_id=101, number=attempt_number,
                            phase="claimed", worker_id="terminal-worker", claim_token="owner"))
                        await db.commit()
                    owner = {"claim_token":"owner"}
                    await change(kind, identifier, "starting", 0, **owner)
                    await change(kind, identifier, "running", 1, **owner)
                    version = 2
                else:
                    run, step = await run_record(status="running", step_status="running")
                    identifier = run if kind == "run" else step
                    version = 0
                if terminal == "cancelled":
                    await change(kind, identifier, "cancelling", version, **owner)
                    version += 1
                options = {"reason_code":"process_exit_failed"} if terminal == "failed" else {}
                result = await change(kind, identifier, terminal, version, **owner, **options)
                code = result.reason_code if kind == "attempt" else result.error_code
                assert result.ended_at and code == {"failed":"process_exit_failed",
                    "cancelled":"cancel_requested", "interrupted":"recovery_required"}[terminal]
                ended, count = result.ended_at, await count_events()
                repeated = await change(kind, identifier, terminal, result.version, **owner)
                assert repeated.ended_at == ended and await count_events() == count
                try:
                    await change(kind, identifier, "running", result.version, **owner)
                    raise AssertionError("Terminal entity reopened")
                except ExecutionConflict:
                    pass
        checks.append("all_three_entities_preserve_failure_cancel_interruption_and_idempotent_terminal")

        _, blocked_step = await run_record()
        blocked = await change("step", blocked_step, "blocked", 0, reason_code="dependency_failed")
        assert blocked.ended_at and blocked.error_code == "dependency_failed"
        try:
            await change("step", blocked_step, "ready", 1)
            raise AssertionError("Failed dependency was silently cleared")
        except ExecutionConflict:
            pass
        checks.append("blocked_dependency_is_terminal_and_explained")

        async with sessions() as db:
            db.add(Case(id=102, workspace_id=101, name="other", s3_prefix="other/"))
            await db.commit()
        other_run, _ = await run_record(case_id=102)
        before = await count_events()
        async with sessions() as db:
            for case_id, payload, linked_run in ((101, {}, other_run), (101, {"nested":{"claim_token":"private"}}, run_id)):
                try:
                    await append_event(db, case_id, "test.invalid", payload, run_id=linked_run)
                    raise AssertionError("Invalid event accepted")
                except ValueError:
                    await db.rollback()
        assert await count_events() == before
        checks.append("cross_case_and_credential_events_rejected")

        delivered_ids = []
        async def collect(event):
            delivered_ids.append(event.id)
        assert await deliver_pending(sessions, publisher=collect) == before
        assert len(set(delivered_ids)) == before
        assert await deliver_pending(sessions, publisher=collect) == 0
        checks.append("committed_events_delivered_once_after_ack")

        # The event and pending outbox survive process death/reconnection.
        async with sessions() as db:
            event = await append_event(db, 101, "test.crash", {"message":"committed before crash"})
            event_id = event.id
            await db.commit()
        child_env = dict(os.environ, VIBEFLOW_DATABASE_URL=url, PYTHONIOENCODING="utf-8")
        command = [sys.executable, "-B", str(Path(__file__).resolve()), "--delivery-child", str(root)]
        crashed = await asyncio.to_thread(subprocess.run, command+["--crash"], env=child_env, cwd=SERVER,
            capture_output=True, timeout=15)
        assert crashed.returncode == 23, crashed.stderr.decode("utf-8", "replace")
        async with sessions() as db:
            row = (await db.scalars(select(EventOutbox).where(EventOutbox.event_id == event_id))).one()
            assert row.status == "delivering" and row.delivered_at is None
        await asyncio.sleep(0.4)
        recovered = await asyncio.to_thread(subprocess.run, command, env=child_env, cwd=SERVER,
            capture_output=True, timeout=15)
        assert recovered.returncode == 0, recovered.stderr.decode("utf-8", "replace")
        receipts = [json.loads(line) for line in (root / "receipts.jsonl").read_text().splitlines()]
        assert [receipt["event_id"] for receipt in receipts] == [event_id, event_id]
        async with sessions() as db:
            row = (await db.scalars(select(EventOutbox).where(EventOutbox.event_id == event_id))).one()
            assert row.status == "delivered" and row.attempts == 2 and row.delivered_at
        checks.append("real_process_crash_before_ack_redelivers_same_event_id")

        # Two live deliverers cannot both own an unexpired delivery claim.
        async with sessions() as db:
            await append_event(db, 101, "test.race", {})
            await db.commit()
        entered, released = asyncio.Event(), asyncio.Event()
        owned = []
        async def controlled_publisher(event):
            owned.append(event.id)
            entered.set()
            await released.wait()
        first = asyncio.create_task(deliver_pending(sessions, publisher=controlled_publisher))
        await asyncio.wait_for(entered.wait(), 5)
        assert await deliver_pending(sessions, publisher=collect) == 0
        released.set()
        assert await first == 1 and len(owned) == 1
        checks.append("concurrent_outbox_claim_is_fenced")

        async with sessions() as db:
            events = (await db.scalars(select(DurableEvent).where(DurableEvent.case_id == 101).order_by(DurableEvent.seq))).all()
            assert [event.seq for event in events] == list(range(1, len(events)+1))
            assert (await db.get(CaseEventCounter, 101)).next_seq == len(events)+1
            assert await db.scalar(select(func.count()).select_from(EventOutbox)) == await count_events()
        checks.append("case_sequence_contiguous_and_one_outbox_per_event")
    finally:
        await engine.dispose()
    result = {"passed":True, "checks":checks, "count":len(checks)}
    (root / "result.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--delivery-child", type=Path)
    parser.add_argument("--crash", action="store_true")
    args = parser.parse_args()
    if args.delivery_child:
        await delivery_child(args.delivery_child, args.crash)
        return
    root = SERVER.parents[1] / "tmp"
    root.mkdir(exist_ok=True)
    report = Path(tempfile.mkdtemp(prefix="workflow-state-", dir=root))
    pg_url = None
    if args.postgres:
        from app.config import Settings
        pg_url = make_url(Settings(_env_file=SERVER / ".env").database_url)
        assert pg_url.drivername == "postgresql+asyncpg" and pg_url.host in {"127.0.0.1", "localhost", "::1"}
    sqlite_url = "sqlite+aiosqlite:///"+(report / "test.db").as_posix()
    os.environ["VIBEFLOW_DATABASE_URL"] = sqlite_url
    results = {"sqlite":await verify(sqlite_url, report / "sqlite")}
    if pg_url:
        import asyncpg
        name = "tmp_vf_state_" + uuid.uuid4().hex
        assert re.fullmatch(r"tmp_vf_state_[0-9a-f]{32}", name)
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
    print("WORKFLOW_STATE_OK "+json.dumps({"databases":list(results), "evidence":str(report)}))


if __name__ == "__main__":
    asyncio.run(main())
