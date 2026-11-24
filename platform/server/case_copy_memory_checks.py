"""Cross-process memory/copy scenarios, hosted by case_write_smoke's isolated API."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
import time

from fault_fixture import FileBarrier
from testsupport import SERVER


def install_barriers(root):
    """Test application only; no production fault flags or routes."""
    from app.services import memory, case_provision
    original_read = memory.read_memory
    original_put = case_provision.write_file

    def memory_read(prefix):
        result = original_read(prefix)
        if (root / "memory.armed").exists():
            FileBarrier(root, "memory-read", timeout=20).wait()
        return result

    def copy_put(plan, path, data, *args, **kwargs):
        result = original_put(plan, path, data, *args, **kwargs)
        prefix = plan['binding']['prefix']
        armed = root / "copy.armed"
        if armed.exists() and path == "Ref/00-copy-barrier.txt":
            target = json.loads(armed.read_text(encoding="utf-8"))
            if prefix != target["source"] and data == target["marker"].encode():
                FileBarrier(root, "copy-publish", timeout=20).wait()
        return result

    memory.read_memory = memory_read
    case_provision.write_file = copy_put


def verify_memory_and_copy(api, peer, proposal_endpoint):
    checks = []

    def reset(name):
        for suffix in ("ready", "release"):
            (api.root / f"{name}.{suffix}").unlink(missing_ok=True)

    def wait_ready(name, future):
        for _ in range(1500):
            if (api.root / f"{name}.ready").exists():
                return
            if future.done():
                raise AssertionError((name, future.result()))
            time.sleep(0.01)
        raise AssertionError(f"{name} barrier never reached")

    def release(name):
        (api.root / f"{name}.release").write_text("release", encoding="utf-8")

    def outcome(raw):
        result = next(json.loads(line[5:]) for line in raw.decode().splitlines()
                      if line.startswith("data:") and json.loads(line[5:])["type"] == "tool.result")
        assert result["ok"], result
        return result

    memory = "0-caseDict/memory.md"
    peer.call("PUT", api.prefix+"/file", {"path": memory, "content": "编辑器原内容\n"})
    baseline = peer.call("GET", api.prefix+"/file?path="+memory)
    append_a = proposal_endpoint("update_memory", {"content": "Agent 第一次经验"})
    append_b = proposal_endpoint("update_memory", {"content": "Agent 第二次经验"})
    reset("memory-read")
    (api.root / "memory.armed").write_text("armed", encoding="utf-8")
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(api.call, "POST", append_a, {}, raw=True)
        try:
            wait_ready("memory-read", first)
            second = pool.submit(peer.call, "POST", append_b, {}, raw=True)
            editor = pool.submit(peer.call, "PUT", api.prefix+"/file", {
                "path": memory, "content": "stale editor", "expected_sha256": baseline["content_sha256"]}, expect=409)
            time.sleep(0.25)
            assert not second.done() and not editor.done()
        finally:
            (api.root / "memory.armed").unlink(missing_ok=True)
            release("memory-read")
        outcome(first.result(timeout=20))
        outcome(second.result(timeout=20))
        assert editor.result(timeout=20)["detail"]["code"] == "file_conflict"
    merged = peer.call("GET", api.prefix+"/file?path="+memory)["content"]
    assert all(merged.count(value) == 1 for value in ("编辑器原内容", "Agent 第一次经验", "Agent 第二次经验"))
    checks.append("two_Agent_memory_appends_serialize_and_preserve_editor_and_both_entries")
    checks.append("stale_editor_waits_for_memory_append_then_conflicts_without_overwrite")
    compact = proposal_endpoint("update_memory", {"action": "compact"})
    outcome(peer.call("POST", compact, {}, raw=True))
    assert peer.call("GET", api.prefix+"/file?path="+memory)["content"] == merged
    invalid = proposal_endpoint("update_memory", {"content": "\u0000"})
    raw = peer.call("POST", invalid, {}, raw=True).decode()
    assert any(json.loads(line[5:]).get("ok") is False for line in raw.splitlines() if line.startswith("data:"))
    assert peer.call("GET", api.prefix+"/file?path="+memory)["content"] == merged
    checks.append("memory_compact_and_invalid_input_preserve_committed_content")

    workspace = peer.call("POST", "/workspaces", {"name": "copy-coordination-source"})
    ws = workspace["id"]
    source = peer.call("POST", f"/workspaces/{ws}/cases", {"name": "copy-source-one"})
    other = peer.call("POST", f"/workspaces/{ws}/cases", {"name": "copy-source-two"})
    cid, second_id = source["id"], other["id"]
    source_route = f"/cases/{cid}"
    original = peer.call("GET", source_route+"/caseDict")
    peer.call("PUT", source_route+"/file", {"path": "Ref/00-copy-barrier.txt", "content": "COPY_ONE"})
    peer.call("PUT", f"/cases/{second_id}/file", {"path": "Ref/00-copy-barrier.txt", "content": "COPY_TWO"})
    source_root = next((api.root / "files/workspaces").glob(f"*/cases/{cid}"))
    second_root = next((api.root / "files/workspaces").glob(f"*/cases/{second_id}"))

    def arm_copy(root, marker):
        reset("copy-publish")
        (api.root / "copy.armed").write_text(json.dumps({
            "source": root.relative_to(api.root / "files").as_posix(), "marker": marker}), encoding="utf-8")

    def release_copy():
        (api.root / "copy.armed").unlink(missing_ok=True)
        release("copy-publish")

    arm_copy(source_root, "COPY_ONE")
    with ThreadPoolExecutor(max_workers=2) as pool:
        copying = pool.submit(api.call, "POST", source_route+":copy", {"name": "coherent-copy"})
        try:
            wait_ready("copy-publish", copying)
            editing = pool.submit(peer.call, "PUT", source_route+"/caseDict", {
                "content": original["content"]+"\n// after copy\n", "expected_revision": original["dict_revision"]})
            time.sleep(0.25)
            assert not editing.done(), "Writer bypassed source copy lock"
        finally:
            release_copy()
        copied = copying.result(timeout=20)
        edited = editing.result(timeout=20)
    actual = peer.call("GET", f"/cases/{copied['id']}/caseDict")
    assert actual["content"] == original["content"] and actual["dict_revision"] == original["dict_revision"]
    assert edited["dict_revision"] == original["dict_revision"]+1
    checks.append("case_copy_holds_source_lock_until_destination_commit_and_preserves_dictionary_revision")

    # Copy begins while a new dictionary file exists but its DB mirror is uncommitted.
    reset("after-publish")
    current = peer.call("GET", source_route+"/caseDict")
    with ThreadPoolExecutor(max_workers=2) as pool:
        editing = pool.submit(api.call, "PUT", source_route+"/caseDict", {
            "content": current["content"]+"\n// CASE_WRITE_BARRIER before copy\n"})
        try:
            wait_ready("after-publish", editing)
            copying = pool.submit(peer.call, "POST", source_route+":copy", {"name": "wait-for-copy"})
            time.sleep(0.25)
            assert not copying.done()
        finally:
            release("after-publish")
        saved = editing.result(timeout=20)
        copied = copying.result(timeout=20)
    actual = peer.call("GET", f"/cases/{copied['id']}/caseDict")
    assert actual["dict_revision"] == saved["dict_revision"] and actual["content_sha256"] == saved["content_sha256"]
    checks.append("case_copy_refreshes_source_mirror_after_waiting_for_other_writer")

    arm_copy(source_root, "COPY_ONE")
    with ThreadPoolExecutor(max_workers=2) as pool:
        copying = pool.submit(api.call, "POST", f"/workspaces/{ws}:copy", {"name": "coherent-workspace", "include_cases": True})
        try:
            wait_ready("copy-publish", copying)
            editing = pool.submit(peer.call, "PUT", f"/cases/{second_id}/file", {
                "path": "Ref/00-copy-barrier.txt", "content": "COPY_TWO_NEW"})
            time.sleep(0.25)
            assert not editing.done(), "Workspace copy did not lock all sources before copying"
        finally:
            release_copy()
        copied_ws = copying.result(timeout=20)
        editing.result(timeout=20)
    children = peer.call("GET", f"/workspaces/{copied_ws['id']}/cases")
    second_copy = next(c for c in children if c["name"] == "copy-source-two")
    assert peer.call("GET", f"/cases/{second_copy['id']}/file?path=Ref/00-copy-barrier.txt")["content"] == "COPY_TWO"
    checks.append("workspace_copy_locks_all_sources_in_order_before_destination_creation")

    def file_set():
        # Creation claims and recovery receipts intentionally survive rollback.
        return {p.relative_to(api.root / "files").as_posix() for p in (api.root / "files").rglob("*")
                if p.is_file() and '.case-provisions' not in p.relative_to(api.root / "files").parts}

    before_files = file_set()
    before_cases = peer.call("GET", f"/workspaces/{ws}/cases")
    arm_copy(source_root, "COPY_ONE")
    with ThreadPoolExecutor(max_workers=1) as pool:
        copying = pool.submit(api.call, "POST", source_route+":copy", {"name": "external-change-copy"}, expect=409)
        try:
            wait_ready("copy-publish", copying)
            (source_root / "Ref/00-copy-barrier.txt").write_text("external native edit", encoding="utf-8")
        finally:
            release_copy()
        copying.result(timeout=20)
    recovery = peer.call("POST", "/case-provisions:recover")
    assert any(row['status'] == 'rolled_back' for row in recovery['results']), recovery
    assert all(row['status'] in {'committed', 'rolled_back'} for row in recovery['results']), recovery
    assert file_set() == before_files
    assert peer.call("GET", f"/workspaces/{ws}/cases") == before_cases
    checks.append("observed_external_source_change_aborts_case_copy_without_destination_files_or_rows")

    # First child fully copied, second child changes outside the lock: all roll back.
    before_files = file_set()
    before_workspaces = peer.call("GET", "/workspaces")
    arm_copy(second_root, "COPY_TWO_NEW")
    with ThreadPoolExecutor(max_workers=1) as pool:
        copying = pool.submit(api.call, "POST", f"/workspaces/{ws}:copy", {"name": "failed-workspace", "include_cases": True}, expect=409)
        try:
            wait_ready("copy-publish", copying)
            (second_root / "Ref/00-copy-barrier.txt").write_text("second external edit", encoding="utf-8")
        finally:
            release_copy()
        copying.result(timeout=20)
    recovery = peer.call("POST", "/case-provisions:recover")
    assert len(recovery['results']) == 2 and all(row['status'] == 'rolled_back' for row in recovery['results']), recovery
    assert file_set() == before_files
    assert peer.call("GET", "/workspaces") == before_workspaces
    checks.append("workspace_copy_failure_rolls_back_all_children_and_cleans_already_copied_prefixes")

    # Seed known-active states without starting a native process or touching a real DB.
    seed = api.root / "seed-copy-activity.py"
    seed.write_text('''import asyncio, sys
from app.db import SessionLocal, engine
from app.models import Job, JobAttempt
async def main():
    async with SessionLocal() as db:
        job = Job(case_id=int(sys.argv[1]), type="test", status=sys.argv[2])
        db.add(job)
        await db.flush()
        if sys.argv[3] != "none":
            db.add(JobAttempt(job_id=job.id, number=1, phase=sys.argv[3]))
        await db.commit()
    await engine.dispose()
asyncio.run(main())
''', encoding="utf-8")
    for status, phase in (("queued", "none"), ("succeeded", "finalizing")):
        candidate = peer.call("POST", f"/workspaces/{ws}/cases", {"name": f"active-{phase}"})
        result = subprocess.run([sys.executable, str(seed), str(candidate["id"]), status, phase], cwd=SERVER,
            env=dict(api.env, PYTHONPATH=str(SERVER)+os.pathsep+api.env["PYTHONPATH"]),
            capture_output=True, text=True, encoding="utf-8", timeout=20)
        assert result.returncode == 0, result.stderr
        peer.call("POST", f"/cases/{candidate['id']}:copy", {"name": "active-copy-"+phase}, expect=409)
    checks.append("copy_rejects_queued_jobs_and_finalizing_attempts_even_with_terminal_legacy_status")
    return checks
