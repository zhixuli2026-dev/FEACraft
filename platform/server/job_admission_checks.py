"""Cross-process admission/publication tests; all fault switches are test-only."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import time
import uuid

from fault_fixture import FileBarrier


def install_barriers(root):
    from app import storage
    from app.routers import reports, jobs, calculix, su2, paraview, flotherm
    from app.services import jobrunner, job_execution

    for module in (reports, jobs, calculix, su2, paraview, flotherm):
        original = module.get_owned_case

        async def authorized(case_id, *args, _original=original, **kwargs):
            case = await _original(case_id, *args, **kwargs)
            armed = root / "admission-auth.armed"
            if armed.exists() and armed.read_text() == str(case_id):
                (root / f"admission-auth-{case_id}-{uuid.uuid4().hex}.ready").touch()
            return case

        module.get_owned_case = authorized

    original_put = storage._fs_put

    def put(prefix, path, data, *args, **kwargs):
        result = original_put(prefix, path, data, *args, **kwargs)
        if path == ".vibeflow/job-batches/index.json":
            if (root / "batch-index-fail.armed").exists():
                (root / "batch-index-fail.armed").unlink()
                raise OSError("Injected failure after batch index publication")
            if (root / "batch-index.armed").exists():
                FileBarrier(root, "batch-index", timeout=20).wait()
        return result

    storage._fs_put = put
    from app.services import file_transactions
    original_apply = file_transactions._apply

    def applying(case, directory, index, entry, side, expected):
        result = original_apply(case, directory, index, entry, side, expected)
        if side == 'after' and entry['path'] == '.vibeflow/job-batches/index.json':
            if (root/'batch-index-fail.armed').exists():
                (root/'batch-index-fail.armed').unlink()
                raise OSError('Injected failure after journaled batch index publication')
            if (root/'batch-index.armed').exists():
                FileBarrier(root, 'batch-index', timeout=20).wait()
        return result

    file_transactions._apply = applying
    from app.services.report_handler import ReportHandler
    from app.services import attempt_workspace
    original_prepare = ReportHandler.complete_preparation

    async def prepare(self, *args, **kwargs):
        result = await original_prepare(self, *args, **kwargs)
        if (root / "report-prepare.armed").exists():
            await FileBarrier(root, "report-prepare", timeout=20).wait_async()
        return result

    ReportHandler.complete_preparation = prepare
    original_snapshot = attempt_workspace.Snapshot.prepare

    def snapshot(self):
        if (root / "snapshot-existing.armed").exists():
            self.target.mkdir(parents=True)
            (self.target / "KEEP.txt").write_text("prior operation evidence")
        result = original_snapshot(self)
        if (root / "seal-prepare.armed").exists():
            FileBarrier(root, "seal-prepare", timeout=20).wait()
        return result

    attempt_workspace.Snapshot.prepare = snapshot
    original_files = attempt_workspace.seal

    async def files(*args, **kwargs):
        watcher = None
        if (root / "report-cancel.armed").exists():
            caller = asyncio.current_task()

            async def cancel_when_prepared():
                while not (root / "seal-prepare.ready").exists():
                    await asyncio.sleep(0.01)
                caller.cancel()
                await asyncio.sleep(0.03)
                caller.cancel()
                (root / "report-cancel.ready").touch()

            watcher = asyncio.create_task(cancel_when_prepared())
        try:
            return await original_files(*args, **kwargs)
        finally:
            if watcher is not None and not watcher.done():
                watcher.cancel()

    attempt_workspace.seal = files
    original_start = jobrunner.start_job

    async def start(job_id, command, prefix, **kwargs):
        if (root / "report-claim.armed").exists():
            (root / "report-claim-job.json").write_text(json.dumps({"job_id": job_id}))
            await FileBarrier(root, "report-claim", timeout=20).wait_async()
        return await original_start(job_id, command, prefix, **kwargs)

    jobrunner.start_job = start
    original_claim = job_execution.claim_job

    async def claim(job_id, spec):
        owner = await original_claim(job_id, spec)
        if (root / "job-claimed.armed").exists():
            (root / "claimed-job.json").write_text(json.dumps({"job_id": job_id}))
            await FileBarrier(root, "job-claimed", timeout=20).wait_async()
        return owner

    job_execution.claim_job = claim


def verify_admissions(api, peer):
    checks = []
    ws = peer.call("POST", "/workspaces", {"name": "job-admission"})["id"]

    def case(name):
        return peer.call("POST", f"/workspaces/{ws}/cases", {"name": name})["id"]

    def root(cid):
        # Storage namespace is the owner ID, not the logical workspace ID.
        return api.root / f"files/workspaces/1/cases/{cid}"

    def arm(name):
        for suffix in ("ready", "release"):
            (api.root / f"{name}.{suffix}").unlink(missing_ok=True)
        (api.root / f"{name}.armed").touch()

    def release(name):
        (api.root / f"{name}.armed").unlink(missing_ok=True)
        (api.root / f"{name}.release").touch()

    def wait(name, future):
        for _ in range(1800):
            if (api.root / f"{name}.ready").exists():
                return
            if future.done():
                raise AssertionError((name, future.result()))
            time.sleep(0.01)
        raise AssertionError("Barrier not reached: "+name)

    def put(cid, path, content):
        return peer.call("PUT", f"/cases/{cid}/file", {"path": path, "content": content})

    cid = case("batch-publication")
    endpoint = f"/cases/{cid}/jobs:batch"
    arm("batch-index")
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(api.call, "POST", endpoint, {"commands": ["echo FIRST"]}, expect=202)
        try:
            wait("batch-index", first)
            second = pool.submit(peer.call, "POST", endpoint, {"commands": ["echo SECOND"]}, expect=202)
            listing = pool.submit(peer.call, "GET", f"/cases/{cid}/job-batches")
            time.sleep(0.2)
            assert not second.done() and not listing.done()
            assert peer.call("GET", f"/cases/{cid}/jobs") == []
        finally:
            release("batch-index")
        batches = [first.result(timeout=20), second.result(timeout=20)]
        assert all(item["total"] == 1 for item in listing.result(timeout=20))
    indexed = peer.call("GET", f"/cases/{cid}/job-batches")
    assert {item["id"] for item in indexed} == {item["id"] for item in batches}
    for item in batches:
        for job in item["jobs"]:
            api.wait_job(job["id"])
    checks.append("batch_two_API_submissions_preserve_both_indexes_and_readers_wait_for_committed_jobs")

    index = root(cid) / ".vibeflow/job-batches/index.json"
    before = index.read_bytes()
    manifests = {p.name for p in index.parent.iterdir()}
    job_ids = {j["id"] for j in peer.call("GET", f"/cases/{cid}/jobs")}
    arm("batch-index-fail")
    api.call("POST", endpoint, {"commands": ["echo MUST_NOT_START"]}, expect=500, raw=True)
    assert (root(cid)/'.vibeflow/file-transactions/pending').exists(), 'Interrupted publication must retain its journal'
    peer.call('GET', f'/cases/{cid}/job-batches')  # Real coordinated recovery after SQL rollback.
    assert index.read_bytes() == before and {p.name for p in index.parent.iterdir()} == manifests
    assert {j["id"] for j in peer.call("GET", f"/cases/{cid}/jobs")} == job_ids
    checks.append("batch_post_publication_failure_restores_exact_index_removes_manifest_and_rolls_back_jobs")
    put(cid, ".vibeflow/job-batches/index.json", '{"broken": true}')
    api.call("POST", endpoint, {"commands": ["echo MUST_NOT_START"]}, expect=500)
    assert index.read_text() == '{"broken": true}'
    assert {j["id"] for j in peer.call("GET", f"/cases/{cid}/jobs")} == job_ids
    put(cid, ".vibeflow/job-batches/index.json", before.decode())
    checks.append("corrupt_batch_index_preserved_without_creating_jobs")

    cid = case("frozen-report")
    put(cid, "E-report/report.md", "# ORIGINAL_SOURCE\n")
    arm("report-prepare")
    with ThreadPoolExecutor(max_workers=3) as pool:
        building = pool.submit(api.call, "POST", f"/cases/{cid}/report:build", {}, expect=409)
        try:
            wait("report-prepare", building)
            editing = pool.submit(put, cid, "E-report/report.md", "# UPDATED_SOURCE\n")
            metadata = pool.submit(peer.call, "GET", f"/cases/{cid}/file:version?path=E-report/report.md")
            editing.result(timeout=3)
            assert metadata.result(timeout=3)["exists"]
            assert peer.call("GET", f"/cases/{cid}/jobs") == []
        finally:
            release("report-prepare")
        result = building.result(timeout=20)
    assert result['detail']['code'] == 'input_stale', result
    assert peer.call("GET", f"/cases/{cid}/jobs") == []
    assert not (root(cid) / "E-report/build/report-manifest.json").exists()
    assert not list((root(cid) / '.vibeflow/attempts').glob('*'))
    checks.append("registered_report_preparation_allows_editors_and_readers_then_changed_source_rejects_job_and_publication")

    for latex in (False, True):
        cid = case("cancel-before-claim-"+str(latex))
        put(cid, "E-report/latex/main.tex" if latex else "E-report/report.md",
            "\\documentclass{article}\\begin{document}Hello\\end{document}" if latex else "# Report\n")
        arm("report-claim")
        with ThreadPoolExecutor(max_workers=1) as pool:
            building = pool.submit(api.call, "POST", f"/cases/{cid}/"+("latex:build" if latex else "report:build"), {}, expect=409)
            try:
                wait("report-claim", building)
                job_id = json.loads((api.root / "report-claim-job.json").read_text())["job_id"]
                cancelled = peer.call("POST", f"/jobs/{job_id}:cancel", {})
                assert cancelled["status"] == "cancelled"
                peer.call("DELETE", f"/cases/{cid}")
            finally:
                release("report-claim")
            building.result(timeout=20)
        assert not root(cid).exists()
        peer.call("POST", f"/cases/{cid}:restore")
        assert peer.call("GET", f"/jobs/{job_id}")["status"] == "cancelled"
        checks.append(("latex" if latex else "report")+"_cancelled_then_trashed_before_claim_keeps_terminal_status_and_does_not_recreate_case")

    for latex in (False, True):
        cid = case("old-snapshot-"+str(latex))
        put(cid, "E-report/latex/main.tex" if latex else "E-report/report.md",
            "\\documentclass{article}\\begin{document}Collision\\end{document}" if latex else "# source")
        arm("snapshot-existing")
        try:
            api.call("POST", f"/cases/{cid}/"+("latex:build" if latex else "report:build"), {}, expect=409)
        finally:
            release("snapshot-existing")
        assert peer.call("GET", f"/cases/{cid}/jobs") == []
        kept = list((root(cid) / ".vibeflow").rglob("KEEP.txt"))
        assert len(kept) == 1 and kept[0].read_text() == "prior operation evidence"
    checks.append("report_and_latex_preexisting_snapshot_preserved_with_no_new_job")

    cid = case("failed-preparation")
    put(cid, "E-report/report.md", "![remote](https://invalid.example/plot.png)\n")
    api.call("POST", f"/cases/{cid}/report:build", {}, expect=400)
    assert peer.call("GET", f"/cases/{cid}/jobs") == []
    assert not list((root(cid) / ".vibeflow/attempts").glob("*"))
    checks.append("invalid_remote_report_source_is_rejected_before_job_or_attempt_directory")

    cid = case("cancel-preparation")
    put(cid, "E-report/report.md", "# Cancel during snapshot\n")
    arm("seal-prepare")
    arm("report-cancel")
    with ThreadPoolExecutor(max_workers=2) as pool:
        building = pool.submit(api.call, "POST", f"/cases/{cid}/report:build", {}, expect=500, raw=True)
        try:
            wait("seal-prepare", building)
            wait("report-cancel", building)
            editing = pool.submit(put, cid, "E-report/report.md", "# Edit after cleanup\n")
            time.sleep(0.2)
            assert not building.done() and not editing.done()
        finally:
            release("seal-prepare")
            release("report-cancel")
        building.result(timeout=20)
        editing.result(timeout=20)
    assert peer.call("GET", f"/cases/{cid}/jobs") == []
    # Sealing now retains a durable plan until the SQL outcome is known. A
    # cancelled request must drain its thread, then recovery proves rollback;
    # immediate recursive cleanup would destroy unknown-commit evidence.
    pending = list((root(cid) / '.vibeflow/attempt-provisions/pending').glob('*.json'))
    assert len(pending) == 1
    identifier = json.loads(pending[0].read_text(encoding='utf8'))['attempt_id']
    assert (root(cid) / '.vibeflow/attempts' / identifier / 'manifest.json').exists()
    recovered = peer.call('POST', f'/cases/{cid}/attempt-preparations:recover')
    assert any(row.get('attempt_id') == identifier and row['status'] == 'rolled_back'
               for row in recovered['results']), recovered
    assert not list((root(cid) / '.vibeflow/attempts').glob('*'))
    assert not pending[0].exists()
    assert (root(cid) / 'E-report/report.md').read_text() == '# Edit after cleanup\n'
    checks.append('repeated_request_cancel_drains_snapshot_then_durable_recovery_preserves_later_editor')

    cid = case("claimed-cancellation")
    arm("job-claimed")
    with ThreadPoolExecutor(max_workers=1) as pool:
        # The durable scanner may own the claim before the request calls start.
        # In that case HTTP already accepted the same Job; cancellation still
        # must prevent native release when the actual claimant leaves the gate.
        starting = pool.submit(api.call, "POST", f"/cases/{cid}/jobs", {"command": "echo CLAIM_OWNER"}, expect=(200, 409))
        try:
            wait("job-claimed", starting)
            job_id = json.loads((api.root / "claimed-job.json").read_text())["job_id"]
            cancelled = peer.call("POST", f"/jobs/{job_id}:cancel", {})
            assert cancelled["ok"] and cancelled["status"] == "cancelled", cancelled
            assert peer.call("GET", f"/jobs/{job_id}")["status"] == "cancelled"
        finally:
            release("job-claimed")
        starting.result(timeout=20)
    final = peer.call("GET", f"/jobs/{job_id}")
    assert final['status'] == 'cancelled' and 'CLAIM_OWNER' not in final['log_backlog']
    checks.append("claimed_cancellation_is_durable_and_prevents_native_launch_after_claim_barrier")

    cid = case("library-preflight")
    for path in ("Make/files", "Make/options", "fixture.C"):
        put(cid, "Ref/helper/"+path, "test")
    api.call("POST", f"/cases/{cid}/flotherm:build-library",
        {"source_dir": "Ref/helper", "destination_dir": "Ref/helper/nested"}, expect=400)
    assert not (root(cid) / "Ref/helper/nested").exists()
    for number in range(18):
        put(cid, f"Ref/helper/{number}.H", "test")
    api.call("POST", f"/cases/{cid}/flotherm:build-library", {"source_dir": "Ref/helper"}, expect=413)
    assert not (root(cid) / "UDF/flotherm-helper").exists()
    assert peer.call("GET", f"/cases/{cid}/jobs") == []
    checks.append("library_nested_destination_and_oversize_file_count_rejected_before_copy_or_job")

    cid = case("stale-admissions")
    endpoints = [("report:build", {}), ("latex:build", {}), ("ppt:build", {"slides": []}),
        ("pdf:number", {"input": "Ref/input.pdf"}), ("calculix:solve", {"input_path": "C-run/test.inp"}),
        ("su2:solve", {"config_path": "C-run/test.cfg"}), ("paraview:run", {"script_path": "Ref/test.py"}),
        ("flotherm:build-library", {"source_dir": "Ref/helper"}),
        ("flotherm:convert", {"ir_path": "Ref/input.json"}), ("jobs:batch", {"commands": ["echo NO_START"]})]
    arm("trash-before-index")
    (api.root / "trash.armed").write_text(str(cid))
    (api.root / "admission-auth.armed").write_text(str(cid))
    with ThreadPoolExecutor(max_workers=11) as pool:
        removing = pool.submit(api.call, "DELETE", f"/cases/{cid}")
        try:
            wait("trash-before-index", removing)
            waiting = [pool.submit(peer.call, "POST", f"/cases/{cid}/"+path, body, expect=409)
                       for path, body in endpoints]
            for _ in range(1000):
                if len(list(api.root.glob(f"admission-auth-{cid}-*.ready"))) == len(endpoints):
                    break
                if any(future.done() for future in waiting):
                    raise AssertionError([future.result() for future in waiting if future.done()])
                time.sleep(0.01)
            assert len(list(api.root.glob(f"admission-auth-{cid}-*.ready"))) == len(endpoints)
            assert not any(future.done() for future in waiting)
        finally:
            (api.root / "trash.armed").unlink(missing_ok=True)
            (api.root / "admission-auth.armed").unlink(missing_ok=True)
            release("trash-before-index")
        removing.result(timeout=20)
        assert all(future.result(timeout=20)["detail"]["code"] == "case_unavailable" for future in waiting)
    assert not root(cid).exists()
    peer.call("POST", f"/cases/{cid}:restore")
    assert peer.call("GET", f"/cases/{cid}/jobs") == []
    checks.append("ten_job_admissions_recheck_deleted_case_after_authorization_without_new_files_or_jobs")
    return checks
