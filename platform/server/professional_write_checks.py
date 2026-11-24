"""Real API races for professional writers; barriers exist only in this test app."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid

from fault_fixture import FileBarrier
from testsupport import SERVER


def install_barriers(root):
    from app import storage
    from app.routers import geometry, mesh, udf, postprocess, reports, mpi, cadscene, files
    original = storage._fs_put

    def put(prefix, path, data, *args, **kwargs):
        result = original(prefix, path, data, *args, **kwargs)
        armed = root / "professional-write.armed"
        if armed.exists():
            target = json.loads(armed.read_text(encoding="utf-8"))
            if prefix.rstrip("/").endswith("/"+str(target["case_id"])) and path == target["path"]:
                FileBarrier(root, "professional-write", timeout=20).wait()
        return result

    storage._fs_put = put
    from app.services import file_transactions
    original_apply = file_transactions._apply

    def applying(case, directory, index, entry, side, expected):
        result = original_apply(case, directory, index, entry, side, expected)
        armed = root / 'professional-write.armed'
        if side == 'after' and armed.exists():
            target = json.loads(armed.read_text(encoding='utf8'))
            if case.id == target['case_id'] and entry['path'] == target['path']:
                FileBarrier(root, 'professional-write', timeout=20).wait()
        return result

    file_transactions._apply = applying
    owned = geometry.get_owned_case

    async def authorized(case_id, *args, **kwargs):
        case = await owned(case_id, *args, **kwargs)
        armed = root / "professional-auth.armed"
        if armed.exists() and armed.read_text(encoding="utf-8") == str(case_id):
            (root / f"professional-auth-{case_id}-{uuid.uuid4().hex}.ready").touch()
        return case

    for module in (geometry, mesh, udf, postprocess, reports, mpi, cadscene, files):
        module.get_owned_case = authorized


def verify_professional(api, peer):
    from app.services.casedict import set_block
    checks = []
    ws = peer.call("POST", "/workspaces", {"name": "professional-writes"})["id"]

    def new_case(name):
        return peer.call("POST", f"/workspaces/{ws}/cases", {"name": name})["id"]

    def arm(cid, path):
        for suffix in ("ready", "release"):
            (api.root / f"professional-write.{suffix}").unlink(missing_ok=True)
        (api.root / "professional-write.armed").write_text(json.dumps({"case_id": cid, "path": path}), encoding="utf-8")

    def wait(name, future):
        for _ in range(2000):
            if (api.root / (name+".ready")).exists():
                return
            if future.done():
                raise AssertionError((name, future.result()))
            time.sleep(0.01)
        raise AssertionError("Barrier not reached: "+name)

    def release(name="professional-write"):
        (api.root / (name+".armed")).unlink(missing_ok=True)
        (api.root / (name+".release")).touch()

    cid = new_case("scaffold")
    arm(cid, "UDF/fixture/myFunctionObject.H")
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(api.call, "POST", f"/cases/{cid}/udf/scaffold", {"name": "fixture"})
        try:
            wait("professional-write", first)
            overwrite = pool.submit(peer.call, "PUT", f"/cases/{cid}/file", {
                "path": "UDF/fixture/myFunctionObject.H", "content": "MUST_NOT_OVERWRITE", "expected_sha256": None}, expect=409)
            copying = pool.submit(peer.call, "POST", f"/cases/{cid}:copy", {"name": "complete-udf-copy"})
            time.sleep(0.25)
            assert not overwrite.done() and not copying.done()
        finally:
            release()
        generated = first.result(timeout=20)
        assert len(generated["files"]) == 9
        assert overwrite.result(timeout=20)["detail"]["code"] == "file_conflict"
        copied = copying.result(timeout=20)
    paths = {item["path"] for item in peer.call("GET", f"/cases/{copied['id']}/tree")["entries"]}
    assert all("UDF/fixture/"+path in paths for path in generated["files"])
    checks.append("UDF_scaffold_blocks_competing_edit_and_copy_until_all_nine_files_committed")

    peer.call("PUT", f"/cases/{cid}/file", {"path": "UDF/partial/Make/options", "content": "KEEP_PARTIAL"})
    peer.call("POST", f"/cases/{cid}/udf/scaffold", {"name": "partial"}, expect=409)
    assert not peer.call("GET", f"/cases/{cid}/file:version?path=UDF/partial/myFunctionObject.C")["exists"]
    assert peer.call("GET", f"/cases/{cid}/file?path=UDF/partial/Make/options")["content"] == "KEEP_PARTIAL"
    checks.append("partial_UDF_tree_conflict_rejected_before_any_generated_file")

    # Mesh policy must be read after the case lock, not from a stale request.
    mesh_id = new_case("mesh-policy")
    peer.call("PUT", f"/cases/{mesh_id}/file", {"path": "C-run/case-1/checkMesh.log", "content": "Mesh OK.\n"})
    original = peer.call("GET", f"/cases/{mesh_id}/caseDict")
    changed = set_block(original["content"], "mesh", {"qualityTargets": {"maxNonOrtho": 51}})+"\n// CASE_WRITE_BARRIER latest-policy\n"
    for suffix in ("ready", "release"):
        (api.root / f"after-publish.{suffix}").unlink(missing_ok=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        saving = pool.submit(api.call, "PUT", f"/cases/{mesh_id}/caseDict", {"content": changed})
        try:
            wait("after-publish", saving)
            evaluating = pool.submit(peer.call, "POST", f"/cases/{mesh_id}/mesh:evaluate", {})
            time.sleep(0.25)
            assert not evaluating.done()
        finally:
            release("after-publish")
        saving.result(timeout=20)
        report = evaluating.result(timeout=20)
    assert float(report["quality_targets"]["maxNonOrtho"]) == 51
    checks.append("mesh_evaluation_waits_for_caseDict_commit_and_uses_latest_policy")

    # Real CadQuery conversion: neither a second repair nor metadata readers can
    # observe the first STEP publication before its validation file is written.
    import cadquery as cq
    box_path = api.root / "professional-input.step"
    cq.exporters.export(cq.Workplane("XY").box(1, 2, 3), str(box_path))
    source = box_path.read_text(encoding="utf-8")
    geo = new_case("repair")
    peer.call("PUT", f"/cases/{geo}/file", {"path": "A-geo/input.step", "content": source})
    body = {"path": "A-geo/input.step", "operation": "fix"}
    arm(geo, "A-geo/exports/repaired-fix.step")
    with ThreadPoolExecutor(max_workers=3) as pool:
        repairing = pool.submit(api.call, "POST", f"/cases/{geo}/geometry/repair", body)
        try:
            wait("professional-write", repairing)
            duplicate = pool.submit(peer.call, "POST", f"/cases/{geo}/geometry/repair", body, expect=409)
            reading = pool.submit(peer.call, "GET", f"/cases/{geo}/file?path=A-geo/exports/repaired-fix-validation.json")
            time.sleep(0.25)
            assert not duplicate.done() and not reading.done()
        finally:
            release()
        assert repairing.result(timeout=20)["result"]["watertight"]
        duplicate.result(timeout=20)
        validation = json.loads(reading.result(timeout=20)["content"])
    assert validation["sourceSha256"] == hashlib.sha256(source.encode()).hexdigest()
    checks.append("real_STEP_repair_and_validation_publish_under_one_lock_and_duplicate_is_rejected")

    gmsh = "$MeshFormat\n2.2 0 8\n$EndMeshFormat\n$Nodes\n3\n1 0 0 0\n2 1 0 0\n3 0 1 0\n$EndNodes\n$Elements\n1\n1 2 0 1 2 3\n$EndElements\n"
    peer.call("PUT", f"/cases/{geo}/file", {"path": "B-mesh/sample.msh", "content": gmsh})
    converted = peer.call("POST", f"/cases/{geo}/geometry/msh-preview", {"path": "B-mesh/sample.msh"})
    assert converted["points"] == 3 and converted["cells"] == 1
    assert peer.call("GET", f"/cases/{geo}/file:version?path="+converted["path"])["exists"]
    checks.append("real_Gmsh_preview_conversion_publishes_readable_VTU")

    old = peer.call("GET", f"/cases/{cid}/file:version?path=E-report/report.md")
    peer.call("PUT", f"/cases/{cid}/file", {"path": "E-report/report.md", "content": "KEEP_LATEST_REPORT"})
    rejected = peer.call("POST", f"/cases/{cid}/report:compose", {
        "overwrite": True, "expected_sha256": old["content_sha256"]}, expect=409)
    assert rejected["detail"]["code"] == "file_conflict"
    assert peer.call("GET", f"/cases/{cid}/file?path=E-report/report.md")["content"] == "KEEP_LATEST_REPORT"
    checks.append("report_compose_stale_output_hash_rejected_without_overwrite_or_backup")

    # All eleven HTTP writers pass authorization while deletion is pending, then
    # must recheck deletion after the lock. No validation shortcut may recreate it.
    deleted = new_case("stale-professional-requests")
    endpoints = [("mesh:evaluate", {}), ("geometry/repair", {"path": "A-geo/input.step"}),
        ("geometry/msh-preview", {"path": "B-mesh/sample.msh"}), ("geometry:preview", {}),
        ("geometry:export", {}), ("udf/scaffold", {"name": "fixture"}), ("udf:build", {"name": "fixture"}),
        ("udf:integrate", {"name": "fixture", "run_dir": "C-run/case-1"}),
        ("mpi:run", {}), ("postprocess:run", {}), ("report:compose", {"overwrite": True}),
        ("file:version?path=A-geo/scene.json", None)]
    for suffix in ("ready", "release"):
        (api.root / f"trash-before-index.{suffix}").unlink(missing_ok=True)
    (api.root / "trash.armed").write_text(str(deleted), encoding="utf-8")
    (api.root / "professional-auth.armed").write_text(str(deleted), encoding="utf-8")
    with ThreadPoolExecutor(max_workers=13) as pool:
        removing = pool.submit(api.call, "DELETE", f"/cases/{deleted}")
        try:
            wait("trash-before-index", removing)
            waiting = [pool.submit(peer.call, "GET" if path.startswith("file:version?") else "POST",
                f"/cases/{deleted}/"+path, body, expect=409) for path, body in endpoints]
            for _ in range(1000):
                if len(list(api.root.glob(f"professional-auth-{deleted}-*.ready"))) == len(endpoints):
                    break
                if any(future.done() for future in waiting):
                    raise AssertionError([future.result() for future in waiting if future.done()])
                time.sleep(0.01)
            assert len(list(api.root.glob(f"professional-auth-{deleted}-*.ready"))) == len(endpoints)
            assert not any(future.done() for future in waiting)
        finally:
            (api.root / "trash.armed").unlink(missing_ok=True)
            (api.root / "professional-auth.armed").unlink(missing_ok=True)
            release("trash-before-index")
        removing.result(timeout=20)
        assert all(future.result(timeout=20)["detail"]["code"] == "case_unavailable" for future in waiting)
    assert not list((api.root / "files/workspaces").glob(f"*/cases/{deleted}"))
    peer.call("POST", f"/cases/{deleted}:restore")
    assert peer.call("GET", f"/cases/{deleted}/jobs") == []
    checks.append("eleven_professional_writers_and_file_metadata_recheck_deletion_after_queued_authorization")

    async def socket_checks():
        import websockets
        scene = new_case("socket")
        uri = peer.base.replace("http", "ws", 1)+f"/ws/cadscene?case_id={scene}&token={peer.token}"
        async with websockets.connect(uri) as socket:
            assert json.loads(await socket.recv())["type"] == "ready"
            await socket.send(json.dumps({"type": "scene.clear", "expected_sha256": None}))
            receipt = json.loads(await socket.recv())
            assert receipt["type"] == "scene.clear" and len(receipt["content_sha256"]) == 64
            version = peer.call("GET", f"/cases/{scene}/file:version?path=A-geo/scene.json")
            assert receipt["content_sha256"] == version["content_sha256"]
            peer.call("PUT", f"/cases/{scene}/file", {"path": "A-geo/scene.json", "content": "KEEP_HTTP_EDIT"})
            await socket.send(json.dumps({"type": "scene.clear", "expected_sha256": receipt["content_sha256"]}))
            rejected = json.loads(await socket.recv())
            assert rejected["type"] == "error" and rejected["code"] == "file_conflict"
            assert peer.call("GET", f"/cases/{scene}/file?path=A-geo/scene.json")["content"] == "KEEP_HTTP_EDIT"
            peer.call("DELETE", f"/cases/{scene}")
            await socket.send(json.dumps({"type": "scene.clear"}))
            rejected = json.loads(await socket.recv())
            assert rejected["code"] == "case_unavailable"
        assert not list((api.root / "files/workspaces").glob(f"*/cases/{scene}"))
        checks.append("CAD_socket_hash_receipt_conflicts_with_HTTP_edits_and_cannot_resurrect_deleted_case")

        account = peer.call("POST", "/auth/register", {"email": uuid.uuid4().hex+"@example.com", "password": "socket-user-123", "name": "socket user"})
        headers = {"Authorization": "Bearer "+account["token"]}
        own = peer.call("GET", "/workspaces", extra_headers=headers)[0]["id"]
        scene = peer.call("POST", f"/workspaces/{own}/cases", {"name": "disabled-user"}, extra_headers=headers)["id"]
        uri = peer.base.replace("http", "ws", 1)+f"/ws/cadscene?case_id={scene}&token={account['token']}"
        async with websockets.connect(uri) as socket:
            assert json.loads(await socket.recv())["type"] == "ready"
            script = api.root / "disable-socket-test-user.py"
            script.write_text('''import asyncio, sys
from app.db import SessionLocal, engine
from app.models import User
async def main():
    async with SessionLocal() as db:
        user = await db.get(User, int(sys.argv[1]))
        user.status = "disabled"
        await db.commit()
    await engine.dispose()
asyncio.run(main())
''', encoding="utf-8")
            result = subprocess.run([sys.executable, str(script), str(account["user"]["id"])], cwd=SERVER,
                env=dict(api.env, PYTHONPATH=str(SERVER)+os.pathsep+api.env["PYTHONPATH"]), capture_output=True, timeout=20)
            assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
            await socket.send(json.dumps({"type": "scene.clear"}))
            assert json.loads(await socket.recv())["code"] == "case_unavailable"
        source = next((api.root / "files/workspaces").glob(f"*/cases/{scene}"))
        assert not (source / "A-geo/scene.json").exists()
        checks.append("CAD_socket_rechecks_disabled_user_on_each_message")

    asyncio.run(socket_checks())
    return checks
