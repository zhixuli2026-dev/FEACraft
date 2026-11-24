"""Disposable DB, native-process and HTTP proofs for versioned attempt inputs."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import uuid
from unittest.mock import patch

import httpx
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from workflow_migration_smoke import SERVER, alembic, engine_for, legacy


async def verify(url, root):
    root.mkdir(parents=True)
    await asyncio.to_thread(alembic, url, root, "upgrade", "head")
    from testsupport import execution_seed
    await execution_seed(url)
    from app import db as app_db, storage
    from app.main import app
    from app.models import Case, Job, JobAttempt, User, Workspace
    from app.security import create_token
    from app.services import attempt_workspace as ws, job_execution as execution, jobrunner
    from app.services.case_write import CaseWriteConflict
    from app.services.casedict import SCAFFOLD_FILES
    from app.services.tools import ToolExecutor
    engine = engine_for(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    prefix = f"{root.name}/workspaces/101/cases/101"
    checks = []
    async def get_db():
        async with sessions() as db:
            yield db
    async def count():
        async with sessions() as db:
            return await db.scalar(select(func.count()).select_from(Job))
    def spec_for(code, runtime="windows"):
        return {"command":"attempt workspace native probe", "runtime":runtime, "case_prefix":prefix,
            "argv":[sys.executable, "-c", code], "timeout_seconds":30,
            "max_log_bytes":1024*1024, "evidence_run_dir":None}
    async def submit(paths, code="print('workspace native')", key=None):
        spec = spec_for(code)
        async with sessions() as db:
            job, created = await execution.submit(db, case_id=101, user_id=101, key=key,
                digest=execution.request_hash({"spec":spec,"input_paths":paths}), job_type="shell",
                command=spec["command"], spec=spec, input_paths=paths)
            await db.commit()
            attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id))
            return job, attempt, spec, created
    async def launch(submission):
        job, _, spec, _ = submission
        proc = await jobrunner.start_job(job.id, spec["command"], prefix,
            argv=spec["argv"], timeout_seconds=spec["timeout_seconds"], max_log_bytes=spec["max_log_bytes"])
        await proc.wait()
        return proc
    def workspace(submission):
        return json.loads(submission[1].input_manifest_json)["workspace"]
    app.dependency_overrides[app_db.get_db] = get_db
    try:
        with ExitStack() as patches:
            # Services imported during the earlier SQLite pass may have captured
            # its factory. Every imported app factory must follow this test DB.
            for name, module in list(sys.modules.items()):
                if name.startswith('app.') and hasattr(module, 'SessionLocal'):
                    patches.enter_context(patch.object(module, 'SessionLocal', sessions))
            async with sessions() as db:
                case = await db.get(Case, 101)
                case.s3_prefix = prefix
                case.workflow_state = "A"
                db.add(User(id=202, email="attempt-other@example.com", name="Other", password_hash="not-for-login"))
                await db.commit()
            for name, content in SCAFFOLD_FILES.items():
                storage.put_text(prefix, name, content)
            for run, value in (("one","first"), ("two","second")):
                for path in ("0/U", "0.orig/U", "constant/polyMesh/points", "system/controlDict"):
                    storage.put_text(prefix, f"C-run/{run}/{path}", value+":"+path)
            original = storage._fs_path(prefix, ".")
            (original / "C-run/one/empty-required-directory").mkdir()
            paths = ["C-run/one", "0-caseDict/caseDict"]
            code = "from pathlib import Path; p=Path('C-run/one/0/U'); print(p.read_text()); p.write_text('changed run only'); Path('out.txt').write_text('native output')"
            first = await submit(paths, code, "sealed-first")
            manifest = workspace(first)
            inputs = original / manifest["inputs_path"]
            run = original / manifest["run_path"]
            assert (inputs / "C-run/one/0.orig/U").read_text() == "first:0.orig/U"
            assert not (run / "C-run/two").exists()
            assert (run / "C-run/one/empty-required-directory").is_dir()
            assert "C-run/one/empty-required-directory" in manifest["directories"]
            assert not (inputs / "C-run/one/0/U").samefile(run / "C-run/one/0/U")
            assert manifest["source"]["case_id"] == 101 and manifest["toolchain"]["host_python"]
            host = manifest['toolchain']
            assert host['schema']=='vibeflow.attempt-host.v1' and host['scope']=='submission_host'
            assert host['host_executable']=={'path':sys.executable,'status':'observed',**ws.digest_file(Path(sys.executable))}
            assert host['native_tool_versions']=='not_observed_at_seal' and 'solver_versions' not in host
            checks.append("selected_inputs_preserve_initial_fields_mesh_source_and_toolchain_without_hardlinks")
            replay = await submit(paths, code, "sealed-first")
            assert replay[0].id == first[0].id and not replay[3]
            try:
                await submit(["C-run/two"], code, "sealed-first")
                raise AssertionError("Changed selection replayed")
            except execution.ExecutionConflict as error:
                assert error.code == "idempotency_conflict"
            checks.append("idempotent_selection_replays_same_workspace_and_changed_selection_conflicts")
            storage.put_text(prefix, "C-run/one/0/U", "edited after seal")
            second = await submit(["C-run/two"], "from pathlib import Path; print(Path('C-run/two/0/U').read_text()); Path('out.txt').write_text('other attempt')")
            processes = await asyncio.gather(launch(first), launch(second))
            assert all(proc.status == "succeeded" for proc in processes)
            assert "first:0/U" in processes[0].backlog() and "second:0/U" in processes[1].backlog()
            assert (run / "out.txt").read_text() == "native output"
            assert (original / workspace(second)["run_path"] / "out.txt").read_text() == "other attempt"
            assert storage.get_bytes(prefix, "C-run/one/0/U") == b"edited after seal"
            assert not (original / "out.txt").exists()
            assert (inputs / "C-run/one/0/U").read_text() == "first:0/U"
            async with sessions() as db:
                attempt = await db.get(JobAttempt, first[1].id)
                outputs = json.loads(attempt.output_manifest_json)
                assert outputs["files"]["out.txt"]["size"] == 13 and attempt.phase == "succeeded"
            checks.append("concurrent_native_attempts_use_distinct_working_copies_and_keep_originals_and_frozen_inputs")
            checks.append("output_manifest_commits_before_success_and_uses_attempt_relative_paths")

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://attempt-test",
                    headers={"Authorization":"Bearer "+create_token(101)}) as client:
                detail = await client.get(f"/api/v1/jobs/{first[0].id}")
                assert detail.status_code == 200 and detail.json()["attempt"]["workspace"] == manifest
                foreign = await client.get(f"/api/v1/jobs/{first[0].id}", headers={"Authorization":"Bearer "+create_token(202)})
                assert foreign.status_code == 404
                frozen_file = manifest["inputs_path"]+"/C-run/one/0/U"
                for method, endpoint, kwargs in [
                    ("PUT", "file", {"json":{"path":frozen_file,"content":"bad"}}),
                    ("DELETE", "file", {"params":{"path":frozen_file}}),
                    ("POST", "upload", {"params":{"path":frozen_file}, "files":{"file":("U", b"bad")}}),
                    ("POST", "uploads", {"json":{"path":frozen_file,"size":3,"overwrite":True}}),
                    ("PUT", "file", {"json":{"path":manifest["run_path"]+"/out.txt","content":"bad"}}),
                ]:
                    response = await client.request(method, "/api/v1/cases/101/"+endpoint, **kwargs)
                    assert response.status_code == 409 and response.json()["detail"]["code"] == "input_sealed", response.text
                assert (inputs / "C-run/one/0/U").read_text() == "first:0/U"
                fetched = await client.get("/api/v1/cases/101/raw", params={"path":frozen_file})
                assert fetched.status_code == 200 and fetched.content == b"first:0/U"
                checks.append("HTTP_owner_read_access_and_unconditional_text_binary_chunk_delete_reject_sealed_versions")
                response = await client.post("/api/v1/cases/101/jobs", json={"command":"echo API_ISOLATED > api-output.txt", "input_paths":["C-run/two"]},
                    headers={"Idempotency-Key":"api-isolated"})
                assert response.status_code == 200, response.text
                public_job = response.json()["id"]
                proc = jobrunner.get_proc(public_job)
                assert await proc.wait() == "succeeded", proc.backlog()
                assert (proc.cwd / "api-output.txt").read_text().strip() == "API_ISOLATED"
                assert not (original / "api-output.txt").exists()
                response = await client.post("/api/v1/cases/101/jobs", json={"command":"echo API_ISOLATED > api-output.txt", "input_paths":["C-run/two"]},
                    headers={"Idempotency-Key":"api-isolated"})
                assert response.status_code == 200 and response.json()["id"] == public_job
                bad = await client.post("/api/v1/cases/101/jobs", json={"command":"echo bad", "input_paths":["missing"]})
                assert bad.status_code == 400, bad.text
                checks.append("public_Job_API_executes_in_frozen_workspace_and_replay_does_not_spawn_again")
                response = await client.post("/api/v1/cases/101/jobs", json={"command":"printf WSL_ISOLATED > wsl-output.txt",
                    "runtime":"wsl", "input_paths":["C-run/two"]})
                assert response.status_code == 200, response.text
                wsl_proc = jobrunner.get_proc(response.json()["id"])
                assert await wsl_proc.wait() == "succeeded", wsl_proc.backlog()
                assert (wsl_proc.cwd / "wsl-output.txt").read_text() == "WSL_ISOLATED"
                assert not (original / "wsl-output.txt").exists()
                checks.append("real_WSL_process_uses_attempt_cwd_and_retains_outputs_without_live_publication")
            try:
                ToolExecutor(prefix)._write_text(frozen_file, "Agent overwrite")
                raise AssertionError("Agent overwrote frozen input")
            except CaseWriteConflict as error:
                assert error.code == "input_sealed"
            for alias in (frozen_file.upper(), frozen_file.replace(".vibeflow", ".vibeflow. ")):
                try:
                    storage.put_bytes(prefix, alias, b"alias overwrite")
                    raise AssertionError("Reserved alias allowed")
                except CaseWriteConflict:
                    pass
            checks.append("Agent_and_Windows_case_trailing_dot_aliases_cannot_overwrite_reserved_versions")

            failure = await submit(["C-run/one"], "from pathlib import Path; print(Path('C-run/one/0/U').read_text()); raise SystemExit(7)")
            assert (await launch(failure)).status == "failed"
            storage.put_text(prefix, "C-run/one/0/U", "newer current input")
            async with sessions() as db:
                retry_job, retry_attempt = await execution.allocate_retry(db, failure[1].id)
                await db.commit()
            retry = retry_job, retry_attempt, failure[2], True
            assert workspace(retry)["source"]["retry_of_attempt_id"] == failure[1].id
            assert workspace(retry)["inputs_path"] != workspace(failure)["inputs_path"]
            retry_proc = await launch(retry)
            assert retry_proc.status == "failed" and "edited after seal" in retry_proc.backlog()
            assert "newer current input" not in retry_proc.backlog()
            checks.append("retry_creates_new_attempt_from_original_sealed_inputs_despite_live_edits")

            damaged = await submit(["C-run/two"])
            damaged_input = original / workspace(damaged)["inputs_path"] / "C-run/two/0/U"
            damaged_input.write_text("tampered native input")
            with patch.object(asyncio, "create_subprocess_exec") as spawn:
                try:
                    await launch(damaged)
                    raise AssertionError("Damaged seal spawned")
                except CaseWriteConflict as error:
                    assert error.code == "input_stale"
                spawn.assert_not_called()
            async with sessions() as db:
                assert (await db.get(Job, damaged[0].id)).status == "failed"
                assert (await db.get(JobAttempt, damaged[1].id)).reason_code == "input_stale"
            checks.append("DB_manifest_detects_prelaunch_tampering_and_records_failure_without_native_spawn")
            during = await submit(["C-run/two"], "from pathlib import Path; Path('../inputs/C-run/two/0/U').write_text('native tampering')")
            assert (await launch(during)).status == "failed"
            async with sessions() as db:
                assert (await db.get(JobAttempt, during[1].id)).reason_code == "evidence_invalid"
            checks.append("native_mutation_of_sealed_inputs_cannot_be_reported_as_success")

            changed_run = await submit(["C-run/two"])
            (original / workspace(changed_run)["run_path"] / "injected.py").write_text("print('not selected')")
            with patch.object(asyncio, "create_subprocess_exec") as spawn:
                try:
                    await launch(changed_run)
                    raise AssertionError("Unsealed working copy launched")
                except CaseWriteConflict:
                    pass
                spawn.assert_not_called()
            checks.append("extra_unsealed_working_files_are_rejected_before_launch")
            cancelled_start = await submit(["C-run/two"])
            entered_start, release_start = threading.Event(), threading.Event()
            actual_verify = ws.verify
            def blocked_verify(*args):
                result = actual_verify(*args)
                entered_start.set()
                assert release_start.wait(20)
                return result
            with patch.object(ws, "verify", blocked_verify), patch.object(asyncio, "create_subprocess_exec") as spawn:
                starting = asyncio.create_task(launch(cancelled_start))
                try:
                    assert await asyncio.to_thread(entered_start.wait, 15)
                    starting.cancel()
                    await asyncio.sleep(0.05)
                    starting.cancel()
                    assert not starting.done()
                finally:
                    release_start.set()
                    try:
                        await starting
                        raise AssertionError("Startup cancellation swallowed")
                    except asyncio.CancelledError:
                        pass
                spawn.assert_not_called()
            async with sessions() as db:
                assert (await db.get(Job, cancelled_start[0].id)).status == "cancelled"
                assert (await db.get(JobAttempt, cancelled_start[1].id)).phase == "cancelled"
            assert jobrunner.get_proc(cancelled_start[0].id) is None
            checks.append("startup_verification_cancellation_finishes_thread_and_durably_cancels_without_spawn")

            for wrong_owner in ("attempt", "case"):
                mismatched = await submit(["C-run/two"])
                async with sessions() as db:
                    row = await db.get(JobAttempt, mismatched[1].id)
                    document = json.loads(row.input_manifest_json)
                    if wrong_owner == "attempt":
                        document["workspace"] = manifest
                    else:
                        document["workspace"]["source"]["case_id"] = 999
                    row.input_manifest_json = json.dumps(document)
                    await db.commit()
                with patch.object(asyncio, "create_subprocess_exec") as spawn:
                    try:
                        await launch(mismatched)
                        raise AssertionError("Foreign manifest used")
                    except CaseWriteConflict as error:
                        assert error.code == "state_conflict"
                    spawn.assert_not_called()
            checks.append("manifest_identity_is_bound_to_DB_attempt_and_case_before_any_native_launch")

            before = await count()
            for invalid in (["../escape"], ["."], [".vibeflow/attempts"], ["C-run/two/CON"], ["missing"]):
                try:
                    await submit(invalid)
                    raise AssertionError("Invalid input selection accepted")
                except ValueError:
                    pass
            assert await count() == before
            checks.append("invalid_paths_and_missing_inputs_roll_back_submission_and_attempt")
            original_prepare = ws.Snapshot.prepare
            async def recover_preparation(identifier):
                from app.services.attempt_seal_journal import recover_locked
                from app.services.case_write import coordinate
                async with sessions() as db:
                    case = await db.get(Case, 101)
                    async with coordinate(db, case, content_write=False):
                        result = await recover_locked(db, case)
                    await db.commit()
                return result
            captured = []
            def fail_after_publish(snapshot):
                result = original_prepare(snapshot)
                captured.append(snapshot)
                raise OSError("injected before DB manifest flush")
            with patch.object(ws.Snapshot, "prepare", fail_after_publish):
                try:
                    await submit(["C-run/two"])
                    raise AssertionError("Injected failure succeeded")
                except OSError:
                    pass
            assert await count() == before and captured[0].target.exists()
            recovered = await recover_preparation(captured[0].target.name)
            assert any(row.get('attempt_id') == captured[0].target.name and row['status'] == 'rolled_back' for row in recovered), recovered
            assert not captured[0].target.exists() and not captured[0].stage.exists()
            checks.append("precommit_preparation_failure_rolls_back_DB_then_recovers_owned_version")
            entered, release = threading.Event(), threading.Event()
            def slow_prepare(snapshot):
                result = original_prepare(snapshot)
                captured.append(snapshot)
                entered.set()
                assert release.wait(20)
                return result
            with patch.object(ws.Snapshot, "prepare", slow_prepare):
                task = asyncio.create_task(submit(["C-run/two"]))
                try:
                    assert await asyncio.to_thread(entered.wait, 15)
                    task.cancel()
                    await asyncio.sleep(0.05)
                    task.cancel()
                    assert not task.done(), "Cancellation released ownership before thread ended"
                finally:
                    release.set()
                    try:
                        await task
                        raise AssertionError("Cancellation swallowed")
                    except asyncio.CancelledError:
                        pass
            assert await count() == before and captured[-1].target.exists()
            recovered = await recover_preparation(captured[-1].target.name)
            assert any(row.get('attempt_id') == captured[-1].target.name and row['status'] == 'rolled_back' for row in recovered), recovered
            assert not captured[-1].target.exists()
            checks.append("repeated_request_cancellation_drains_copy_then_proven_rollback_cleanup")

            from app.services import quota
            from fastapi import HTTPException
            with patch.object(quota, "check_storage_quota", side_effect=HTTPException(402, "injected quota")), \
                    patch.object(ws.Snapshot, "prepare") as preparing:
                try:
                    await submit(["C-run/two"])
                    raise AssertionError("Quota was ignored")
                except HTTPException as error:
                    assert error.status_code == 402
                preparing.assert_not_called()
            assert await count() == before
            checks.append("two_copy_storage_quota_is_checked_before_creating_attempt_directories")
            def mutate_before_copy(snapshot):
                captured.append(snapshot)
                storage.put_text(prefix, "C-run/two/0/U", "external change during sealing")
                return original_prepare(snapshot)
            with patch.object(ws.Snapshot, "prepare", mutate_before_copy):
                try:
                    await submit(["C-run/two"])
                    raise AssertionError("Mixed snapshot accepted")
                except CaseWriteConflict as error:
                    assert error.code == "input_stale"
            assert await count() == before and captured[-1].stage.exists() and not captured[-1].target.exists()
            recovered = await recover_preparation(captured[-1].target.name)
            assert any(captured[-1].target.name in row.get('record', '') and row['status'] == 'recovery_required' for row in recovered), recovered
            assert captured[-1].stage.exists()
            checks.append("observed_external_source_change_aborts_DB_job_and_retains_unverified_partial_copy")
            def collision_before_publish(snapshot):
                captured.append(snapshot)
                snapshot.target.mkdir()
                (snapshot.target / "other-owner.txt").write_text("keep")
                return original_prepare(snapshot)
            with patch.object(ws.Snapshot, "prepare", collision_before_publish):
                try:
                    await submit(["C-run/two"])
                    raise AssertionError("Existing attempt overwritten")
                except CaseWriteConflict:
                    pass
            assert await count() == before and (captured[-1].target / "other-owner.txt").read_text() == "keep"
            checks.append("existing_attempt_collision_never_cleans_or_overwrites_other_owners_directory")
            link = original / "C-run/two/hard-linked-input"
            os.link(original / "C-run/two/0/U", link)
            try:
                try:
                    await submit(["C-run/two"])
                    raise AssertionError("Hardlinked source accepted")
                except ValueError as error:
                    assert "hard links" in str(error)
            finally:
                link.unlink()
            checks.append("hardlinked_sources_are_rejected_before_copying")

            # The internal scheduler uses the same concrete input selection and
            # native runner. Professional publication/gate adaptation is N3.
            from app.services import workflow_scheduler as scheduler
            from app.services.geo_script import __file__ as geometry_worker
            storage.put_text(prefix, "A-geo/geometry.py",
                "import cadquery as cq\ndef build_geometry(ctx):\n    return cq.Workplane('XY').box(1,2,3)\n")
            async with sessions() as db:
                case = await db.get(Case,101)
                workflow, _ = await scheduler.create_run(db, case_id=101, user=await db.get(User,101),
                    target="geometry", run_dir="C-run/one", expected_revision=case.dict_revision)
                await db.commit()
            async with sessions() as db:
                steps = await scheduler.reconcile(db, workflow.id)
                await db.commit()
            async with sessions() as db:
                steps = await scheduler.complete_preflight(db, workflow.id, expected_version=steps[0].version)
                await db.commit()
            geometry = next(step for step in steps if step.step_key == "geometry")
            spec = spec_for("unused")
            spec["argv"] = [sys.executable, geometry_worker, "--case", ".", "--mode", "preview"]
            async with sessions() as db:
                geometry_job, created = await scheduler.queue_step(db, workflow.id, geometry.id,
                    expected_version=geometry.version, spec=spec, input_paths=["0-caseDict", "A-geo"])
                await db.commit()
                geometry_attempt = await db.scalar(select(JobAttempt).where(JobAttempt.job_id == geometry_job.id))
            geometry_proc = await launch((geometry_job, geometry_attempt, spec, created))
            assert geometry_proc.status == "succeeded", geometry_proc.backlog()
            async with sessions() as db:
                geometry_attempt = await db.get(JobAttempt, geometry_attempt.id)
                outputs = json.loads(geometry_attempt.output_manifest_json)["files"]
                assert "A-geo/scene.json" in outputs and "A-geo/validation.json" in outputs, outputs
                scene = json.loads((geometry_proc.cwd / "A-geo/scene.json").read_text(encoding="utf-8"))
                assert scene["schema"] == "vibeflow.cadquery.scene.v1" and scene["objects"]
                assert (await db.get(Case,101)).workflow_state == "A"
            assert not (original / "A-geo/scene.json").exists()
            checks.append("scheduler_runs_real_CadQuery_in_selected_snapshot_and_does_not_publish_or_advance_stage")

            from app.services.case_admin import copy_case
            from app.services.backup import create_backup
            from app.services.backup_manifest import load_manifest
            async with sessions() as db:
                copied, copy_prefix, _ = await copy_case(db, await db.get(Case,101), await db.get(Workspace,101),
                    await db.get(User,101), "copied without execution ownership")
                assert not storage._fs_path(copy_prefix, ws.PREFIX).exists()
                assert storage.exists(copy_prefix, "C-run/one/0.orig/U")
            async with sessions() as db:
                archive = await create_backup(db, await db.get(Workspace,101))
                backup = load_manifest(archive)
                assert not any(row["path"].startswith(ws.PREFIX+"/") for row in backup["files"])
            checks.append("case_copy_and_backup_v1_exclude_attempt_directories_without_transplanting_DB_ownership")
    finally:
        app.dependency_overrides.pop(app_db.get_db, None)
        await engine.dispose()
    result = {"passed":True,"count":len(checks),"checks":checks}
    (root / "result.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--postgres", action="store_true")
    parser.add_argument("--postgres-only", action="store_true")
    args = parser.parse_args()
    report = Path(tempfile.mkdtemp(prefix="attempt-workspace-", dir=SERVER.parents[1] / "tmp"))
    pg_url = None
    if args.postgres or args.postgres_only:
        from app.config import Settings
        pg_url = make_url(Settings(_env_file=SERVER / ".env").database_url)
        assert pg_url.drivername == "postgresql+asyncpg" and pg_url.host in {"127.0.0.1","localhost","::1"}
    sqlite_url = "sqlite+aiosqlite:///"+(report / "test.db").as_posix()
    os.environ.update(VIBEFLOW_DATABASE_URL=sqlite_url, VIBEFLOW_STORAGE_BACKEND="fs",
        VIBEFLOW_STORAGE_FS_ROOT=str(report / "files"), VIBEFLOW_INDEX_ROOT=str(report / "index"),
        VIBEFLOW_AUDIT_DIR=str(report / "audit"), VIBEFLOW_JWT_SECRET=uuid.uuid4().hex,
        VIBEFLOW_RUNTIME_CONFIG=str(report / "runtime.json"), VIBEFLOW_DISTRIBUTED_JOBS="false")
    results = {} if args.postgres_only else {"sqlite":await verify(sqlite_url, report / "sqlite")}
    if pg_url:
        import asyncpg
        name = "tmp_vf_attempt_"+uuid.uuid4().hex
        assert re.fullmatch(r"tmp_vf_attempt_[0-9a-f]{32}", name)
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
                await admin.execute('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=$1', name)
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results["postgresql"]["temporary_database_removed"] = True
    (report / "result.json").write_text(json.dumps(results, indent=2)+"\n", encoding="utf-8")
    print("ATTEMPT_WORKSPACE_OK "+json.dumps({"databases":list(results),"evidence":str(report)}))


if __name__ == "__main__":
    asyncio.run(main())
