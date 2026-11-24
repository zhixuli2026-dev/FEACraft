"""Real CadQuery + persistent dependency/confirmation tests on disposable DBs."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid
from unittest.mock import patch

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from fault_fixture import FileBarrier
from workflow_migration_smoke import SERVER, alembic, engine_for, legacy


async def verify(url, root):
    root.mkdir(parents=True)
    await asyncio.to_thread(alembic, url, root, "upgrade", "head")
    from testsupport import execution_seed
    await execution_seed(url)
    from app import db as app_db, storage
    from app.main import app
    from app.models import Case, DurableEvent, EventOutbox, Job, JobAttempt, User, WorkflowRun, WorkflowStep
    from app.security import create_token
    from app.services import jobrunner, workflow_scheduler as scheduler
    from app.services.casedict import CASE_DICT_PATH, SCAFFOLD_FILES, set_block
    from app.services.geo_script import __file__ as geo_script_path
    from app.services.workflow_state import ExecutionConflict, transition
    engine = engine_for(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    checks = []
    async def get_db():
        async with sessions() as db:
            yield db
    async def case_fixture(identifier):
        prefix = f"{root.name}/workspaces/101/cases/{identifier}"
        async with sessions() as db:
            case = await db.get(Case, identifier)
            if case is None:
                case = Case(id=identifier, workspace_id=101, name=f"scheduler-{identifier}", s3_prefix=prefix)
                db.add(case)
            else:
                case.s3_prefix = prefix
            for path, content in SCAFFOLD_FILES.items():
                storage.put_text(prefix, path, content)
            storage.put_text(prefix, "A-geo/geometry.py", "import cadquery as cq\ndef build_geometry(ctx):\n    return cq.Workplane('XY').box(1,2,3)\n")
            await db.commit()
            return case
    async def create(case_id=101, key="first", target="geometry", revision=0):
        async with sessions() as db:
            user = await db.get(User, 101)
            run, created = await scheduler.create_run(db, case_id=case_id, user=user, target=target,
                run_dir="C-run/case-1", expected_revision=revision, idempotency_key=key)
            await db.commit()
            return run, created
    async def reconcile(run):
        async with sessions() as db:
            steps = await scheduler.reconcile(db, run.id)
            await db.commit()
            return {step.step_key:step for step in steps}
    async def preflight(run):
        steps = await reconcile(run)
        async with sessions() as db:
            steps = await scheduler.complete_preflight(db, run.id, expected_version=steps["preflight"].version)
            await db.commit()
            return {step.step_key:step for step in steps}
    async def queue(run, step, spec):
        async with sessions() as db:
            job, created = await scheduler.queue_step(db, run.id, step.id,
                expected_version=step.version, spec=spec)
            await db.commit()
            return job, created
    def spec_for(case, *, fail=False):
        argv = [sys.executable, "-c", "raise SystemExit(7)"] if fail else [
            sys.executable, geo_script_path, "--case", str(storage._fs_path(case.s3_prefix, ".")), "--mode", "preview"]
        return {"command":"scheduler native geometry" if not fail else "scheduler failing native job",
            "case_prefix":case.s3_prefix, "runtime":"windows", "argv":argv, "timeout_seconds":60,
            "max_log_bytes":1024*1024, "evidence_run_dir":None}
    async def launch(job, spec, finish=None):
        return await jobrunner.start_job(job.id, spec["command"], spec["case_prefix"],
            argv=spec["argv"], timeout_seconds=spec["timeout_seconds"], max_log_bytes=spec["max_log_bytes"],
            on_finish=finish)
    async def event_count():
        async with sessions() as db:
            return await db.scalar(select(func.count()).select_from(DurableEvent))
    app.dependency_overrides[app_db.get_db] = get_db
    try:
        with patch.object(app_db, "SessionLocal", sessions):
            case = await case_fixture(101)
            assert [len(scheduler.make_plan(target, "A")["steps"]) for target in scheduler.TARGET_END] == [3,5,7,9,10]
            assert len(scheduler.make_plan("geometry", "B")["steps"]) == 1
            try:
                scheduler.make_plan("report", "V")
                raise AssertionError("Development state used without explicit transition")
            except ExecutionConflict:
                pass
            checks.append("server_generated_target_graphs_and_explicit_development_exit")
            submissions = await asyncio.gather(*[create() for _ in range(6)])
            run = submissions[0][0]
            assert len({item.id for item, _ in submissions}) == 1 and sum(new for _, new in submissions) == 1
            try:
                await create(target="report")
                raise AssertionError("Workflow key reused for changed target")
            except ExecutionConflict as error:
                assert error.code == "idempotency_conflict"
            checks.append("concurrent_workflow_submission_and_payload_conflict")
            before = await event_count()
            async with sessions() as db:
                user = await db.get(User, 101)
                rolled, _ = await scheduler.create_run(db, case_id=101, user=user, target="report",
                    run_dir="C-run/case-1", expected_revision=0, idempotency_key="rollback")
                rolled_id = rolled.id
                await db.rollback()
            async with sessions() as db:
                assert await db.get(WorkflowRun, rolled_id) is None
                assert not list(await db.scalars(select(WorkflowStep).where(WorkflowStep.run_id == rolled_id)))
            assert await event_count() == before
            checks.append("creation_rollback_removes_run_steps_events")
            steps = await reconcile(run)
            assert [step.status for step in steps.values()] == ["ready", "pending", "pending"]
            try:
                await queue(run, steps["geometry"], spec_for(case))
                raise AssertionError("Unfinished dependency was dispatched")
            except ExecutionConflict:
                pass
            steps = await preflight(run)
            assert steps["geometry"].status == "ready" and steps["confirm_B"].status == "pending"
            checks.append("dependencies_block_dispatch_until_preflight_succeeds")
            spec = spec_for(case)
            outcomes = await asyncio.gather(queue(run, steps["geometry"], spec),
                queue(run, steps["geometry"], spec), return_exceptions=True)
            assert sum(isinstance(item, ExecutionConflict) for item in outcomes) == 1, outcomes
            job = next(item[0] for item in outcomes if isinstance(item, tuple))
            gate = FileBarrier(root, "geometry-finalizing")
            proc = await launch(job, spec, lambda *_: gate.wait_async())
            for _ in range(3000):
                if gate.ready.exists():
                    break
                if proc.completion_error:
                    raise AssertionError(proc.completion_error)
                await asyncio.sleep(0.01)
            assert gate.ready.exists(), proc.backlog()
            during = await reconcile(run)
            assert during["geometry"].status == "finalizing" and during["confirm_B"].status == "pending"
            gate.release()
            assert await proc.wait() == "succeeded", proc.backlog()
            async with sessions() as db:
                assert (await db.get(WorkflowRun, run.id)).status == "awaiting_confirmation", "Worker completion did not reconcile workflow"
            steps = await reconcile(run)
            confirmation = steps["confirm_B"]
            assert confirmation.status == "awaiting_confirmation"
            async with sessions() as db:
                assert (await db.get(WorkflowRun, run.id)).status == "awaiting_confirmation"
                assert (await db.get(Case, 101)).workflow_state == "A"
                attempt_events = list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id == proc.owner.attempt_id)))
                assert attempt_events and all(event.run_id == run.id and event.step_id == steps["geometry"].id for event in attempt_events)
            before = await event_count()
            await reconcile(run)
            assert await event_count() == before
            checks.append("one_queued_attempt_and_real_CadQuery_waits_for_durable_finalization")
            checks.append("reconnected_scheduler_waits_for_confirmation_without_reexecution")
            checks.append("attempt_and_job_events_link_to_run_and_step")

            async with sessions() as db:
                try:
                    await transition(db, "step", confirmation.id, "succeeded", expected_version=confirmation.version)
                    raise AssertionError("Generic state change bypassed engineering gate")
                except ExecutionConflict as error:
                    assert error.code == "confirmation_required"
                    await db.rollback()
                db.add(User(id=202, email="scheduler-other@example.com", name="Other", password_hash="no-login"))
                await db.commit()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://scheduler-test",
                                         headers={"Authorization":"Bearer "+create_token(101)}) as client:
                path = "/api/v1/cases/101/caseDict:advance"
                body = {"state":"B", "confirmed":True, "expected_revision":0,
                    "workflow_step_id":confirmation.id, "expected_step_version":confirmation.version}
                response = await client.post(path, json=body, headers={"Authorization":"Bearer "+create_token(202)})
                assert response.status_code == 404, response.text
                assert (await client.post(path, json=dict(body, confirmed=False))).status_code == 409
                assert (await client.post(path, json=dict(body, state="C"))).status_code == 409
                assert (await client.post(path, json=dict(body, expected_step_version=None))).status_code == 422
                assert (await client.post(path, json=dict(body, workflow_step_id="missing"))).status_code == 404
                checks.append("confirmation_HTTP_authorization_target_and_version_required")
                original = storage.get_bytes(case.s3_prefix, CASE_DICT_PATH).decode()
                changed = await client.put("/api/v1/cases/101/caseDict", json={
                    "content":original+"\n// edited while confirmation was waiting\n", "expected_revision":0})
                assert changed.status_code == 200, changed.text
                stale = await client.post(path, json=body)
                assert stale.status_code == 409 and stale.json()["detail"]["code"] == "revision_conflict", stale.text
                async with sessions() as db:
                    confirmation = await scheduler.refresh_confirmation(db, run.id, confirmation.id,
                        expected_version=confirmation.version)
                    await db.commit()
                assert (await client.post(path, json=dict(body, expected_revision=1))).status_code == 409
                body.update(expected_revision=1, expected_step_version=confirmation.version)
                responses = await asyncio.gather(client.post(path, json=body), client.post(path, json=body))
                assert sorted(response.status_code for response in responses) == [200,409], [response.text for response in responses]
                assert (await client.post(path, json=body)).status_code == 409
            async with sessions() as db:
                final = await db.get(WorkflowRun, run.id)
                confirmed_case = await db.get(Case, 101)
                assert final.status == "succeeded" and final.ended_at
                assert confirmed_case.workflow_state == "B" and confirmed_case.dict_revision == 2
            assert (await create())[0].id == run.id
            checks.append("revision_change_invalidates_confirmation_and_refresh_requires_new_version")
            checks.append("concurrent_explicit_confirmation_advances_case_and_completes_run_once")
            reused, _ = await create(key="reuse", revision=2)
            reused_steps = await preflight(reused)
            assert len(reused_steps) == 1 and reused_steps["preflight"].status == "succeeded"
            storage.put_text(case.s3_prefix, "A-geo/geometry.py", "raise RuntimeError('changed source')\n")
            stale_run, _ = await create(key="reuse-stale", revision=2)
            stale_steps = await preflight(stale_run)
            assert stale_steps["preflight"].status == "failed"
            checks.append("reuse_requires_current_professional_evidence_not_stage_label")

            successor_case = await case_fixture(909)
            successor_run, _ = await create(909, "successor", "report")
            successor_steps = await preflight(successor_run)
            successor_spec = spec_for(successor_case)
            successor_job, _ = await queue(successor_run, successor_steps["geometry"], successor_spec)
            successor_proc = await launch(successor_job, successor_spec)
            assert await successor_proc.wait() == "succeeded"
            successor_steps = await reconcile(successor_run)
            gate_step = successor_steps["confirm_B"]
            async with sessions() as db:
                await scheduler.confirm_step(db, run_id=successor_run.id, step_id=gate_step.id,
                    case_id=909, user_id=101, target="B", confirmed=True,
                    expected_revision=gate_step.confirmation_revision, expected_version=gate_step.version)
                await db.commit()
            successor_steps = await reconcile(successor_run)
            assert successor_steps["mesh"].status == "ready"
            assert successor_steps["confirm_C"].status == successor_steps["solve"].status == "pending"
            empty_spec = dict(successor_spec, command="exit zero without scientific mesh evidence",
                argv=[sys.executable, "-c", "print('No mesh generated')"])
            empty_job, _ = await queue(successor_run, successor_steps["mesh"], empty_spec)
            empty_proc = await launch(empty_job, empty_spec)
            assert await empty_proc.wait() == "succeeded"
            successor_steps = await reconcile(successor_run)
            mesh_gate = successor_steps["confirm_C"]
            async with sessions() as db:
                try:
                    await scheduler.confirm_step(db, run_id=successor_run.id, step_id=mesh_gate.id,
                        case_id=909, user_id=101, target="C", confirmed=True,
                        expected_revision=mesh_gate.confirmation_revision, expected_version=mesh_gate.version)
                    raise AssertionError("Exit zero was accepted as scientific mesh evidence")
                except ExecutionConflict as error:
                    assert error.code == "evidence_invalid"
                    await db.rollback()
                assert (await db.get(Case, 909)).workflow_state == "B"
                active = await db.get(WorkflowRun, successor_run.id)
                await scheduler.request_cancel(db, active.id, expected_version=active.version)
                await db.commit()
            checks.append("confirmation_releases_exactly_the_next_dependency")
            checks.append("successful_exit_without_mesh_evidence_cannot_advance_engineering_stage")

            failed_case = await case_fixture(303)
            failed_run, _ = await create(303, "failure", "report")
            steps = await preflight(failed_run)
            failed_spec = spec_for(failed_case, fail=True)
            failed_job, _ = await queue(failed_run, steps["geometry"], failed_spec)
            failed_proc = await launch(failed_job, failed_spec)
            assert await failed_proc.wait() == "failed"
            failed_steps = await reconcile(failed_run)
            assert failed_steps["geometry"].status == "failed"
            assert all(step.status == "blocked" for key, step in failed_steps.items() if key not in {"preflight", "geometry"})
            async with sessions() as db:
                assert (await db.get(WorkflowRun, failed_run.id)).status == "failed"
                assert await db.scalar(select(func.count()).select_from(Job).where(Job.case_id == 303)) == 1
            checks.append("real_native_failure_blocks_all_downstream_steps_without_launch")
            for identifier, terminal in ((404,"cancelled"),(505,"interrupted")):
                scoped = await case_fixture(identifier)
                controlled_run, _ = await create(identifier, "controlled", "report")
                waiting = await preflight(controlled_run)
                queued_job, _ = await queue(controlled_run, waiting["geometry"], spec_for(scoped))
                if terminal == "cancelled":
                    async with sessions() as db:
                        active = await db.get(WorkflowRun, controlled_run.id)
                        await scheduler.request_cancel(db, active.id, expected_version=active.version)
                        await db.commit()
                        assert (await db.get(Job, queued_job.id)).status == "cancelled"
                else:
                    await jobrunner._persist_status(queued_job.id, terminal)
                await reconcile(controlled_run)
                async with sessions() as db:
                    assert (await db.get(WorkflowRun, controlled_run.id)).status == terminal
            checks.append("durable_cancel_and_interruption_propagate_without_requeue")
            running_case = await case_fixture(707)
            running_run, _ = await create(707, "running-cancel", "report")
            running_steps = await preflight(running_run)
            running_spec = spec_for(running_case)
            running_spec.update(command="owned cancel heartbeat fixture", argv=[sys.executable,
                "-u", "-c", "import time; print('RUNNING'); time.sleep(60)"])
            running_job, _ = await queue(running_run, running_steps["geometry"], running_spec)
            running_proc = await launch(running_job, running_spec)
            async with sessions() as db:
                active = await db.get(WorkflowRun, running_run.id)
                await scheduler.request_cancel(db, active.id, expected_version=active.version)
                await db.commit()
                assert (await db.get(WorkflowRun, running_run.id)).status == "cancelling"
            assert await asyncio.wait_for(running_proc.wait(), timeout=20) == "cancelled"
            async with sessions() as db:
                assert (await db.get(WorkflowRun, running_run.id)).status == "cancelled"
            assert running_proc.proc.returncode is not None
            checks.append("running_worker_observes_durable_cancel_and_reconciles_after_exit")
            changed_case = await case_fixture(606)
            changed_run, _ = await create(606, "changed", "report")
            changed_steps = await preflight(changed_run)
            try:
                await queue(changed_run, changed_steps["geometry"], dict(spec_for(changed_case), timeout_seconds=86401))
                raise AssertionError("Step exceeded the workflow budget")
            except ExecutionConflict as error:
                assert error.code == "resource_limit"
            text = storage.get_bytes(changed_case.s3_prefix, CASE_DICT_PATH).decode()
            storage.put_text(changed_case.s3_prefix, CASE_DICT_PATH, set_block(text, "geometry", {"externalDimension":9}))
            try:
                await queue(changed_run, changed_steps["geometry"], spec_for(changed_case))
                raise AssertionError("Changed engineering inputs dispatched")
            except ExecutionConflict as error:
                assert error.code == "input_stale"
            checks.append("changed_engineering_inputs_refuse_dispatch")
            atomic_case = await case_fixture(808)
            atomic_run, _ = await create(808, "commit-failure", "report")
            atomic_steps = await preflight(atomic_run)
            atomic_spec = spec_for(atomic_case)
            before = await event_count()
            async with sessions() as db:
                rolled_job, _ = await scheduler.queue_step(db, atomic_run.id, atomic_steps["geometry"].id,
                    expected_version=atomic_steps["geometry"].version, spec=atomic_spec)
                rolled_job_id = rolled_job.id
                await db.rollback()
            async with sessions() as db:
                assert await db.get(Job, rolled_job_id) is None
                assert (await db.get(WorkflowStep, atomic_steps["geometry"].id)).active_attempt_id is None
            assert await event_count() == before
            checks.append("queue_rollback_preserves_ready_step_and_leaves_no_attempt_or_job")
            atomic_job, _ = await queue(atomic_run, atomic_steps["geometry"], atomic_spec)
            atomic_gate = FileBarrier(root, "atomic-finalizing")
            atomic_proc = await launch(atomic_job, atomic_spec, lambda *_: atomic_gate.wait_async())
            for _ in range(3000):
                if atomic_gate.ready.exists():
                    break
                await asyncio.sleep(0.01)
            assert atomic_gate.ready.exists()
            before = await event_count()
            from sqlalchemy.ext.asyncio import AsyncSession
            async def fail_commit(_db):
                raise OSError("Injected workflow completion commit failure")
            with patch.object(AsyncSession, "commit", fail_commit):
                atomic_gate.release()
                try:
                    await atomic_proc.wait()
                    raise AssertionError("Workflow completion acknowledged a failed transaction")
                except OSError:
                    pass
            async with sessions() as db:
                assert (await db.get(Job, atomic_job.id)).status == "running"
                assert (await db.get(JobAttempt, atomic_proc.owner.attempt_id)).phase == "finalizing"
                assert (await db.get(WorkflowStep, atomic_steps["geometry"].id)).status == "finalizing"
                assert (await db.get(WorkflowRun, atomic_run.id)).status == "running"
            assert await event_count() == before and not atomic_proc.finished
            checks.append("failed_terminal_commit_rolls_back_job_attempt_step_run_and_events")
            async with sessions() as db:
                events = list(await db.scalars(select(DurableEvent).order_by(DurableEvent.case_id, DurableEvent.seq)))
                assert await db.scalar(select(func.count()).select_from(EventOutbox)) == len(events)
                for case_id in {event.case_id for event in events}:
                    selected = [event.seq for event in events if event.case_id == case_id]
                    assert selected == list(range(1, len(selected)+1))
            checks.append("all_scheduler_events_atomic_with_contiguous_case_sequence_and_outbox")
    finally:
        app.dependency_overrides.pop(app_db.get_db, None)
        await engine.dispose()
    result = {"passed":True, "count":len(checks), "checks":checks}
    (root / "result.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--postgres", action="store_true")
    args = parser.parse_args()
    report = Path(tempfile.mkdtemp(prefix="workflow-scheduler-", dir=SERVER.parents[1] / "tmp"))
    pg_url = None
    if args.postgres:
        from app.config import Settings
        pg_url = make_url(Settings(_env_file=SERVER / ".env").database_url)
        assert pg_url.drivername == "postgresql+asyncpg" and pg_url.host in {"127.0.0.1","localhost","::1"}
    sqlite_url = "sqlite+aiosqlite:///"+(report / "test.db").as_posix()
    os.environ.update(VIBEFLOW_DATABASE_URL=sqlite_url, VIBEFLOW_STORAGE_BACKEND="fs",
        VIBEFLOW_STORAGE_FS_ROOT=str(report / "files"), VIBEFLOW_INDEX_ROOT=str(report / "index"),
        VIBEFLOW_AUDIT_DIR=str(report / "audit"), VIBEFLOW_JWT_SECRET=uuid.uuid4().hex,
        VIBEFLOW_RUNTIME_CONFIG=str(report / "runtime.json"))
    results = {"sqlite":await verify(sqlite_url, report / "sqlite")}
    if pg_url:
        import asyncpg
        name = "tmp_vf_scheduler_"+uuid.uuid4().hex
        assert re.fullmatch(r"tmp_vf_scheduler_[0-9a-f]{32}", name)
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
    print("WORKFLOW_SCHEDULER_OK "+json.dumps({"databases":list(results), "evidence":str(report)}))


if __name__ == "__main__":
    asyncio.run(main())
