"""Real subprocess + SQLite fault windows; no developer DB/Redis/services used."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from unittest.mock import patch

from fault_fixture import FileBarrier, intercept


async def ready(gate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if gate.ready.exists():
            return
        await asyncio.sleep(0.01)
    raise TimeoutError("Barrier not reached: " + gate.name)


async def main():
    evidence = Path(__file__).resolve().parents[2] / "tmp"
    evidence.mkdir(exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="job-finalization-", dir=evidence))
    os.environ.update(VIBEFLOW_DATABASE_URL="sqlite+aiosqlite:///" + (root / "test.db").as_posix(),
        VIBEFLOW_STORAGE_BACKEND="fs", VIBEFLOW_STORAGE_FS_ROOT=str(root / "files"),
        VIBEFLOW_INDEX_ROOT=str(root / "index"), VIBEFLOW_AUDIT_DIR=str(root / "audit"),
        VIBEFLOW_RUNTIME_CONFIG=str(root / "runtime.json"))
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.db import SessionLocal, init_db, engine
    from app.models import Case, Job, User, Workspace
    from app.services import distributed_queue, eventbus, jobrunner
    from app.services.durable_events import deliver_pending
    from app.routers.jobs import _job_out, job_log_stream

    def persisted(job_id):
        with sqlite3.connect(root / "test.db") as connection:
            return connection.execute("SELECT status, ended_at FROM jobs WHERE id=?", (job_id,)).fetchone()

    await init_db()
    async with SessionLocal() as db:
        user = User(email="test@example.com", password_hash="no-login", name="Test")
        db.add(user)
        await db.flush()
        workspace = Workspace(user_id=user.id, name="isolated")
        db.add(workspace)
        await db.flush()
        case = Case(workspace_id=workspace.id, name="finalization", s3_prefix="workspaces/1/cases/1/")
        db.add(case)
        await db.commit()

    async def new_job():
        async with SessionLocal() as db:
            job = Job(case_id=case.id, type="shell", command="test")
            db.add(job)
            await db.flush()
            job.log_s3_key = f".vibeflow/jobs/job-{job.id}.log"
            await db.commit()
            return job

    events = []
    original_publish = eventbus.publish
    def observe_event(user_id, event):
        if event.get("type") == "jobs.updated":
            with sqlite3.connect(root / "test.db") as connection:
                durable = connection.execute("SELECT payload_json FROM durable_events WHERE id=?", (event["event_id"],)).fetchone()
            assert durable is not None
            events.append((event["job_id"], event["status"], json.loads(durable[0])["status"]))
        return original_publish(user_id, event)

    results = []
    try:
        with patch.object(eventbus, "publish", observe_event):
            job = await new_job()
            finish_gate = FileBarrier(root, "finish")
            commit_gate = FileBarrier(root, "terminal_commit")
            original_start = jobrunner.start_job
            async def controlled_start(*args, **kwargs):
                async def finish(status, code):
                    assert status == "succeeded" and code == 0
                    await finish_gate.wait_async()
                    (root / "published.txt").write_text("validated output", encoding="utf-8")
                kwargs.update(argv=[sys.executable, "-u", "-c", "print('real worker output')"], on_finish=finish)
                return await original_start(*args, **kwargs)
            with patch.object(jobrunner, "start_job", controlled_start):
                # This suite creates direct-runner fixtures without durable
                # delivery requests. The legacy Redis adapter now correctly
                # refuses those; delivery admission has its own fault suites.
                active = {}
                async def run_direct():
                    proc = await jobrunner.start_job(job.id, 'test', case.s3_prefix)
                    active[job.id] = proc
                    try:
                        return await proc.wait()
                    finally:
                        active.pop(job.id, None)
                worker = asyncio.create_task(run_direct())
                await ready(finish_gate)
                proc = active[job.id]
                assert proc.phase == "finalizing" and proc.public_status == "running"
                assert not worker.done() and persisted(job.id)[0] == "running"
                assert not (root / "published.txt").exists()
                with intercept(AsyncSession, "commit", commit_gate):
                    finish_gate.release()
                    await ready(commit_gate)
                    assert (root / "published.txt").is_file()
                    assert not worker.done() and proc.ended_at is None and not proc.finished
                    assert persisted(job.id) == ("running", None)
                    queue, _ = proc.subscribe()
                    assert queue.empty(), "Late subscriber got premature done"
                    assert _job_out(job, proc)["status"] == "running"
                    commit_gate.release()
                    await asyncio.wait_for(worker, 10)
                assert proc.finished and proc.phase == "completed" and proc.public_status == "succeeded"
                assert persisted(job.id)[0] == "succeeded" and persisted(job.id)[1]
                assert job.id not in active
                assert await asyncio.wait_for(queue.get(), 1) is None
                proc.unsubscribe(queue)
            results.append("direct_runner_wait_after_real_publish_and_db_commit")

            # Native exit 0 plus invalid scientific evidence is failed, not successful.
            job = await new_job()
            gate = FileBarrier(root, "evidence")
            proc = await jobrunner.start_job(job.id, "test", case.s3_prefix,
                argv=[sys.executable, "-c", "print('End')"], on_finish=lambda *_: gate.wait_async())
            await ready(gate)
            proc.evidence_snapshot = {"deliberately_invalid": True}
            gate.release()
            assert await proc.wait() == "failed" and persisted(job.id)[0] == "failed"
            results.append("invalid_evidence_prevents_success")

            # Failed synchronization remains failed and staging is retained.
            job = await new_job()
            gate = FileBarrier(root, "sync")
            proc = await jobrunner.start_job(job.id, "test", case.s3_prefix,
                argv=[sys.executable, "-c", "print('output')"], on_finish=lambda *_: gate.wait_async())
            await ready(gate)
            proc.staging_prefix, proc.source_prefix = "test-stage", case.s3_prefix
            with patch('app.services.staging_sync.sync_job', side_effect=OSError("injected upload failure")), \
                    patch.object(jobrunner, "cleanup_job_staging") as cleanup:
                gate.release()
                assert await proc.wait() == "failed" and persisted(job.id)[0] == "failed"
                cleanup.assert_not_called()
            results.append("sync_failure_retains_staging_and_fails")

            # A failed real terminal commit cannot be swallowed into a successful acknowledgement.
            job = await new_job()
            gate = FileBarrier(root, "db_failure")
            proc = await jobrunner.start_job(job.id, "test", case.s3_prefix,
                argv=[sys.executable, "-c", "print('output')"], on_finish=lambda *_: gate.wait_async())
            await ready(gate)
            async def fail_commit(_self):
                raise OSError("injected DB outage")
            with patch.object(AsyncSession, "commit", fail_commit):
                gate.release()
                try:
                    await proc.wait()
                    raise AssertionError("Persistence failure acknowledged")
                except OSError:
                    pass
            assert not proc.finished and proc.completion_error and proc.public_status != "succeeded"
            assert proc.phase == "finalization_failed" and persisted(job.id) == ("running", None)
            async with SessionLocal() as db:
                response = await job_log_stream(job.id, user=user, db=db)
                chunks = [chunk async for chunk in response.body_iterator]
                payloads = [json.loads(chunk.removeprefix("data: ").strip()) for chunk in chunks]
                assert any(item.get("code") == "persistence_failed" for item in payloads)
                assert not any(item.get("type") == "done" for item in payloads)
            results.append("db_failure_propagates_and_sse_reports_error")

            # Terminal races use a conditional DB update: exactly one outcome wins.
            job = await new_job()
            await jobrunner._persist_status(job.id, "running")
            outcomes = await asyncio.gather(jobrunner._persist_status(job.id, "succeeded", 0),
                jobrunner._persist_status(job.id, "cancelled", -1), return_exceptions=True)
            assert sum(isinstance(item, jobrunner.JobStateConflict) for item in outcomes) == 1, outcomes
            winner = persisted(job.id)
            loser = "failed"
            try:
                await jobrunner._persist_status(job.id, loser, 3)
                raise AssertionError("Terminal status overwritten")
            except jobrunner.JobStateConflict:
                pass
            assert persisted(job.id) == winner
            results.append("conditional_terminal_race_and_late_callback")
            job = await new_job()
            gate = FileBarrier(root, "cancel_wins")
            proc = await jobrunner.start_job(job.id, "test", case.s3_prefix,
                argv=[sys.executable, "-c", "print('output')"], on_finish=lambda *_: gate.wait_async())
            await ready(gate)
            await jobrunner._persist_status(job.id, "cancelled", -1)
            gate.release()
            assert await proc.wait() == "cancelled" and proc.finished
            assert persisted(job.id)[0] == "cancelled"
            results.append("durable_cancellation_wins_over_late_success")

            job = await new_job()
            gate = FileBarrier(root, "notification_failure")
            proc = await jobrunner.start_job(job.id, "test", case.s3_prefix,
                argv=[sys.executable, "-c", "print('output')"], on_finish=lambda *_: gate.wait_async())
            await ready(gate)
            with patch.object(eventbus, "publish", side_effect=OSError("injected notification outage")):
                gate.release()
                assert await proc.wait() == "succeeded" and proc.finished
                assert await deliver_pending(SessionLocal) == 0
            assert persisted(job.id)[0] == "succeeded"
            results.append("optional_notification_failure_keeps_committed_result")
            # Replay deferred notifications after the outage; historical status
            # may differ from current Job status but must have committed evidence.
            from sqlalchemy import update
            from app.models import EventOutbox
            from app.services.durable_events import now
            async with SessionLocal() as db:
                await db.execute(update(EventOutbox).where(EventOutbox.status == "pending").values(next_attempt_at=now()))
                await db.commit()
            assert await deliver_pending(SessionLocal) > 0 and events
            assert all(status == committed for _id, status, committed in events), events
            results.append("notifications_follow_durable_state")
    finally:
        for proc in list(jobrunner._registry.values()):
            if proc.proc and proc.proc.returncode is None:
                proc.cancel()
        await engine.dispose()
    (root / "result.json").write_text(json.dumps({"passed": results, "count": len(results),
        "database": "isolated SQLite", "subprocess": "real Python", "redis": "not used"}, indent=2), encoding="utf-8")
    print("JOB_FINALIZATION_OK " + json.dumps({"checks": len(results), "evidence": str(root)}))


if __name__ == "__main__":
    asyncio.run(main())
