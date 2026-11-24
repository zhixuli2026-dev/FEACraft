"""Real cross-process trash, workspace membership and stale-authorization races."""
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile

from fault_fixture import FileBarrier
from testsupport import SERVER


def install_barriers(root):
    from app.services import trash, case_provision
    old_save, old_seal = trash._save, case_provision.seal

    def save(data):
        armed = root / "trash.armed"
        if armed.exists():
            key = armed.read_text(encoding="utf-8")
            if key in data["cases"] or key.removeprefix("ws-") in data["workspaces"]:
                FileBarrier(root, "trash-before-index", timeout=20).wait()
        if (root / "trash-save-fail.armed").exists():
            raise OSError("injected trash index failure before publication")
        return old_save(data)

    async def seal(db, case, plan):
        result = await old_seal(db, case, plan)
        if plan['kind'] == 'create' and case.name == "CREATE_BEFORE_TRASH":
            FileBarrier(root, "case-created", timeout=20).wait()
        return result

    trash._save, case_provision.seal = save, seal


def verify_trash(api, peer):
    checks = []

    def workspace(name):
        return peer.call("POST", "/workspaces", {"name": "trash-"+name})["id"]

    def new_case(ws, name):
        return peer.call("POST", f"/workspaces/{ws}/cases", {"name": name})["id"]

    def root(cid):
        return next((api.root / "files/workspaces").glob(f"*/cases/{cid}"))

    def arm(key):
        for suffix in ("ready", "release"):
            (api.root / f"trash-before-index.{suffix}").unlink(missing_ok=True)
        (api.root / "trash.armed").write_text(str(key), encoding="utf-8")

    def wait(name, future):
        for _ in range(1500):
            if (api.root / (name+".ready")).exists():
                return
            if future.done():
                raise AssertionError((name, future.result()))
            time.sleep(0.01)
        raise AssertionError("Barrier not reached: "+name)

    def release(name="trash-before-index"):
        (api.root / "trash.armed").unlink(missing_ok=True)
        (api.root / (name+".release")).write_text("release", encoding="utf-8")

    def import_to(ws, archive, expect):
        boundary = uuid.uuid4().hex
        data = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="queued-import.zip"\r\n'
            'Content-Type: application/zip\r\n\r\n').encode()+archive+f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(peer.base+f"/api/v1/workspaces/{ws}/cases:import", data=data, method="POST",
            headers={"Authorization": "Bearer "+peer.token, "Content-Type": "multipart/form-data; boundary="+boundary})
        try:
            response = peer.opener.open(req, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            payload = response.read()
            assert response.code == expect, (response.code, payload[:1000])
            return json.loads(payload)

    ws, other_ws = workspace("first"), workspace("second")
    cid, other = new_case(ws, "first"), new_case(other_ws, "second")
    source = root(cid)
    peer.call("PUT", f"/cases/{cid}/file", {"path": "Ref/keep.txt", "content": "KEEP_BEFORE_TRASH"})
    arm(cid)
    with ThreadPoolExecutor(max_workers=5) as pool:
        deleting = pool.submit(api.call, "DELETE", f"/cases/{cid}")
        try:
            wait("trash-before-index", deleting)
            writing = pool.submit(peer.call, "PUT", f"/cases/{cid}/file", {"path": "Ref/stale.txt", "content": "MUST_NOT_EXIST"}, expect=409)
            job = pool.submit(peer.call, "POST", f"/cases/{cid}/jobs", {"type": "shell", "command": "echo MUST_NOT_RUN"}, expect=409)
            copying = pool.submit(peer.call, "POST", f"/cases/{cid}:copy", {"name": "must-not-copy"}, expect=409)
            other_delete = pool.submit(peer.call, "DELETE", f"/cases/{other}")
            time.sleep(0.3)
            assert not any(future.done() for future in (writing, job, copying, other_delete))
        finally:
            release()
        deleting.result(timeout=20)
        for future in (writing, job, copying):
            assert future.result(timeout=20)["detail"]["code"] == "case_unavailable"
        other_delete.result(timeout=20)
    trashed = peer.call("GET", "/trash")
    assert {cid, other}.issubset({row["id"] for row in trashed["cases"]})
    assert not source.exists()
    peer.call("POST", f"/cases/{cid}:restore")
    peer.call("POST", f"/cases/{other}:restore")
    assert (source / "Ref/keep.txt").read_text(encoding="utf-8") == "KEEP_BEFORE_TRASH"
    assert not (source / "Ref/stale.txt").exists()
    assert peer.call("GET", f"/cases/{cid}/jobs") == []
    assert [row["name"] for row in peer.call("GET", f"/workspaces/{ws}/cases")] == ["first"]
    checks.append("trash_waiting_writer_copy_and_job_recheck_visibility_and_never_recreate_deleted_case")
    checks.append("two_API_deletes_in_distinct_workspaces_preserve_both_shared_trash_index_entries")

    # A save that wins first must be retained in the trashed/restored directory.
    for suffix in ("ready", "release"):
        (api.root / ("after-publish."+suffix)).unlink(missing_ok=True)
    original = peer.call("GET", f"/cases/{cid}/caseDict")
    content = original["content"]+"\n// CASE_WRITE_BARRIER retained-before-delete\n"
    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(api.call, "PUT", f"/cases/{cid}/caseDict", {"content": content})
        try:
            wait("after-publish", writing)
            deleting = pool.submit(peer.call, "DELETE", f"/cases/{cid}")
            time.sleep(0.2)
            assert not deleting.done()
        finally:
            release("after-publish")
        saved = writing.result(timeout=20)
        deleting.result(timeout=20)
    peer.call("POST", f"/cases/{cid}:restore")
    restored = peer.call("GET", f"/cases/{cid}/caseDict")
    assert restored["content"] == content and restored["dict_revision"] == saved["dict_revision"]
    checks.append("delete_waits_for_committed_save_and_restore_retains_exact_content_and_revision")

    # Workspace membership changes all take the workspace lock before touching cases.
    destination = workspace("membership-target")
    resident = new_case(destination, "resident")
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("0-caseDict/caseDict", "workflowState A;\ncaseMeta {}\n")
    arm("ws-"+str(destination))
    with ThreadPoolExecutor(max_workers=5) as pool:
        deleting = pool.submit(api.call, "DELETE", f"/workspaces/{destination}")
        try:
            wait("trash-before-index", deleting)
            creating = pool.submit(peer.call, "POST", f"/workspaces/{destination}/cases", {"name": "late-create"}, expect=409)
            moving = pool.submit(peer.call, "POST", f"/cases/{other}:move", {"workspace_id": destination}, expect=409)
            copying = pool.submit(peer.call, "POST", f"/cases/{cid}:copy", {"workspace_id": destination, "name": "late-copy"}, expect=409)
            importing = pool.submit(import_to, destination, archive.getvalue(), 409)
            time.sleep(0.3)
            assert not any(future.done() for future in (creating, moving, copying, importing))
        finally:
            release()
        deleting.result(timeout=20)
        for future in (creating, moving, copying, importing):
            assert future.result(timeout=20)["detail"]["code"] == "workspace_unavailable"
    assert peer.call("GET", f"/cases/{other}")["workspace_id"] == other_ws
    peer.call("POST", f"/workspaces/{destination}:restore")
    assert [row["id"] for row in peer.call("GET", f"/workspaces/{destination}/cases")] == [resident]
    checks.append("workspace_delete_blocks_stale_create_import_move_and_copy_without_orphan_members")

    # Opposite race: deletion must enumerate after a previously-started creation commits.
    before = workspace("create-first")
    seed = new_case(before, "seed")
    with ThreadPoolExecutor(max_workers=2) as pool:
        creating = pool.submit(api.call, "POST", f"/workspaces/{before}/cases", {"name": "CREATE_BEFORE_TRASH"})
        try:
            wait("case-created", creating)
            deleting = pool.submit(peer.call, "DELETE", f"/workspaces/{before}")
            time.sleep(0.2)
            assert not deleting.done()
        finally:
            release("case-created")
        created = creating.result(timeout=20)
        deleting.result(timeout=20)
    peer.call("POST", f"/workspaces/{before}:restore")
    assert {row["id"] for row in peer.call("GET", f"/workspaces/{before}/cases")} == {seed, created["id"]}
    checks.append("workspace_delete_waits_for_prior_create_and_includes_new_member_in_restore")

    first_root, second_root = root(seed), root(created["id"])
    peer.call("DELETE", f"/workspaces/{before}")
    second_root.mkdir(parents=True)
    obstruction = second_root / "external-marker.txt"
    obstruction.write_text("DO_NOT_REPLACE", encoding="utf-8")
    peer.call("POST", f"/workspaces/{before}:restore", expect=409)
    assert not first_root.exists() and obstruction.read_text(encoding="utf-8") == "DO_NOT_REPLACE"
    assert any(row["id"] == before for row in peer.call("GET", "/trash")["workspaces"])
    obstruction.unlink()
    second_root.rmdir()
    peer.call("POST", f"/workspaces/{before}:restore")
    assert first_root.is_dir() and second_root.is_dir()
    checks.append("workspace_restore_conflict_preserves_existing_target_and_rolls_back_earlier_moves")

    index = api.root / "files/_trash/index.json"
    previous_index = index.read_bytes()
    (api.root / "trash-save-fail.armed").write_text("armed", encoding="utf-8")
    try:
        peer.call("DELETE", f"/cases/{cid}", expect=500, raw=True)
    finally:
        (api.root / "trash-save-fail.armed").unlink()
    assert index.read_bytes() == previous_index and source.is_dir()
    assert peer.call("GET", f"/cases/{cid}/caseDict")["content"] == content
    checks.append("trash_index_publication_failure_restores_source_directory_and_preserves_index")

    # Shared members can write files, but cannot move/delete an owner's case into
    # a trash area that only the member could list and only the owner could restore.
    other_user = peer.call("POST", "/auth/register", {"email": uuid.uuid4().hex+"@example.com",
        "password": "trash-member-123", "name": "shared member"})
    headers = {"Authorization": "Bearer "+other_user["token"]}
    member = peer.call("GET", "/auth/me", extra_headers=headers)
    script = api.root / "seed-trash-member.py"
    script.write_text('''import asyncio, sys
from sqlalchemy import select
from app.db import SessionLocal, engine
from app.models import Membership, Workspace
async def main():
    async with SessionLocal() as db:
        workspace = await db.get(Workspace, int(sys.argv[1]))
        org = await db.scalar(select(Membership.org_id).where(Membership.user_id == workspace.user_id,
            Membership.role == "admin").order_by(Membership.id))
        db.add(Membership(org_id=org, user_id=int(sys.argv[2]), role="member"))
        await db.commit()
        print(org)
    await engine.dispose()
asyncio.run(main())
''', encoding="utf-8")
    result = subprocess.run([sys.executable, str(script), str(ws), str(member["id"])], cwd=SERVER,
        env=dict(api.env, PYTHONPATH=str(SERVER)+os.pathsep+api.env["PYTHONPATH"]),
        capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    org = int(result.stdout.strip().splitlines()[-1])
    peer.call("PUT", f"/workspaces/{ws}/sharing", {"org_id": org, "role": "member", "enabled": True})
    peer.call("PUT", f"/cases/{cid}/file", {"path": "Ref/shared-write.txt", "content": "SHARED_WRITE"}, extra_headers=headers)
    own = peer.call("GET", "/workspaces", extra_headers=headers)
    member_ws = next(row["id"] for row in own if not row["shared"])
    peer.call("DELETE", f"/cases/{cid}", expect=404, extra_headers=headers)
    peer.call("POST", f"/cases/{cid}:move", {"workspace_id": member_ws}, expect=404, extra_headers=headers)
    assert peer.call("GET", f"/cases/{cid}")["workspace_id"] == ws
    checks.append("shared_member_file_writes_remain_allowed_but_owner_case_delete_and_move_are_denied")
    return checks
