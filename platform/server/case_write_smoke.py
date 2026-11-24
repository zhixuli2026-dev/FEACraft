"""Two actual API processes sharing DB/files: revision/hash conflicts and read coherence."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from sqlalchemy.engine import make_url

from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic

CONTROLLED = '''from pathlib import Path
from fault_fixture import FileBarrier
from app.routers import files
original = files.write_case_dict
async def controlled(db, case, content, *args, **kwargs):
    result = await original(db, case, content, *args, **kwargs)
    if "CASE_WRITE_BARRIER" in content:
        FileBarrier(Path(__file__).parent, "after-publish", timeout=20).wait()
    return result
files.write_case_dict = controlled
from case_copy_memory_checks import install_barriers
install_barriers(Path(__file__).parent)
from attachment_backup_checks import install_barriers as install_backup_barriers
install_backup_barriers(Path(__file__).parent)
from trash_coordination_checks import install_barriers as install_trash_barriers
install_trash_barriers(Path(__file__).parent)
from professional_write_checks import install_barriers as install_professional_barriers
install_professional_barriers(Path(__file__).parent)
from job_admission_checks import install_barriers as install_admission_barriers
install_admission_barriers(Path(__file__).parent)
from session_write_checks import install_barriers as install_session_barriers
install_session_barriers(Path(__file__).parent)
from app.routers import agent
async def no_llm(*args, **kwargs):
    yield agent.sse({"type":"stream.done", "reason":"test-no-model"})
agent._run_loop = no_llm
from app.main import app
'''

SEED_TOOL = '''import asyncio, json, sys
from pathlib import Path
from app.db import SessionLocal, engine
from app.models import ToolCall
async def seed():
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    async with SessionLocal() as db:
        db.add(ToolCall(**payload))
        await db.commit()
    await engine.dispose()
asyncio.run(seed())
'''


def verify(database_url=None):
    api = IsolatedServer("case-write", app="controlled_case_write:app", database_url=database_url)
    (api.root / "controlled_case_write.py").write_text(CONTROLLED, encoding="utf-8")
    (api.root / "seed_tool.py").write_text(SEED_TOOL, encoding="utf-8")
    peer = IsolatedServer("case-write-peer")
    checks = []
    with api:
        api.create_case("concurrent-writes")
        peer.token = api.token
        peer.log = (peer.root / "api.log").open("w", encoding="utf-8")
        peer.proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "controlled_case_write:app", "--app-dir", str(SERVER),
            "--host", "127.0.0.1", "--port", str(peer.port), "--no-access-log"], cwd=peer.root,
            env=api.env, stdout=peer.log, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            for _ in range(150):
                if peer.proc.poll() is not None:
                    raise AssertionError((peer.root / "api.log").read_text(encoding="utf-8"))
                try:
                    if peer.call("GET", "/api/health", api_prefix=False).get("ok"):
                        break
                except OSError:
                    pass
                time.sleep(0.1)
            else:
                raise TimeoutError("Peer API did not start")
            path = api.prefix+"/caseDict"
            original = api.call("GET", path)
            assert original["content_sha256"] == hashlib.sha256(original["content"].encode()).hexdigest()
            first = {"content":original["content"]+"\n// CASE_WRITE_BARRIER winner\n",
                "expected_revision":original["dict_revision"], "expected_sha256":original["content_sha256"]}
            second = dict(first, content=original["content"]+"\n// losing client must not overwrite\n")
            with ThreadPoolExecutor(max_workers=4) as pool:
                winner = pool.submit(api.call, "PUT", path, first)
                for _ in range(1000):
                    if (api.root / "after-publish.ready").exists():
                        break
                    if winner.done():
                        raise AssertionError(winner.result())
                    time.sleep(0.01)
                assert (api.root / "after-publish.ready").exists()
                loser = pool.submit(peer.call, "PUT", path, second, expect=409)
                reader = pool.submit(peer.call, "GET", path)
                metadata = pool.submit(peer.call, "GET", api.prefix+"/file:version?path=0-caseDict/caseDict")
                time.sleep(0.2)
                assert not loser.done() and not reader.done() and not metadata.done(), "Peer observed/published before the control transaction committed"
                (api.root / "after-publish.release").write_text("release", encoding="utf-8")
                saved = winner.result(timeout=15)
                conflict = loser.result(timeout=15)
                snapshot = reader.result(timeout=15)
                version = metadata.result(timeout=15)
            assert saved["dict_revision"] == original["dict_revision"]+1
            assert saved["content_sha256"] == hashlib.sha256(first["content"].encode()).hexdigest()
            assert conflict["detail"]["code"] == "revision_conflict"
            assert snapshot["content"] == first["content"] and snapshot["dict_revision"] == saved["dict_revision"]
            assert snapshot["content_sha256"] == hashlib.sha256(first["content"].encode()).hexdigest()
            events = api.call("GET", api.prefix+"/events")
            assert events["total"] == 2
            checks.append("two_real_API_writers_same_revision_exactly_one_winner")
            checks.append("peer_reader_waits_for_coherent_content_hash_and_revision")
            checks.append("losing_writer_leaves_file_and_case_event_timeline_unchanged")
            assert version["protected"] and version["content_sha256"] == saved["content_sha256"]
            assert version["dict_revision"] == saved["dict_revision"]
            checks.append("file_version_waits_for_committed_hash_revision_and_marks_protected_dictionary")

            raw_path = api.root / f"files/workspaces/1/cases/{api.case_id}/0-caseDict/caseDict"
            # External editor bypasses the API and therefore does not bump DB revision.
            external = first["content"].replace("CASE_WRITE_BARRIER", "completed-barrier")+"\n// external edit\n"
            raw_path.write_text(external, encoding="utf-8")
            stale = peer.call("PUT", path, {"content":second["content"],
                "expected_revision":saved["dict_revision"], "expected_sha256":snapshot["content_sha256"]}, expect=409)
            assert stale["detail"]["code"] == "file_conflict"
            assert raw_path.read_text(encoding="utf-8") == external
            assert api.call("GET", api.prefix+"/events")["total"] == 2
            refreshed = peer.call("GET", path)
            final_content = external+"\n// based on the external content\n"
            final = peer.call("PUT", path, {"content":final_content,
                "expected_revision":refreshed["dict_revision"], "expected_sha256":refreshed["content_sha256"]})
            assert final["dict_revision"] == saved["dict_revision"]+1
            assert raw_path.read_text(encoding="utf-8") == final_content
            checks.append("external_edit_detected_by_hash_without_revision_change")
            checks.append("explicit_refetch_then_save_preserves_external_content")
            before = api.call("GET", api.prefix+"/events")["total"]
            peer.call("PUT", path, {"content":final_content, "expected_sha256":"invalid"}, expect=422)
            peer.call("PUT", path, {"content":final_content, "expected_revision":-1}, expect=422)
            peer.call("PUT", path, {"content":final_content.replace("workflowState A", "workflowState B"),
                "workflow_state":"B", "expected_revision":final["dict_revision"]}, expect=409)
            assert raw_path.read_text(encoding="utf-8") == final_content
            assert api.call("GET", api.prefix+"/events")["total"] == before
            checks.append("invalid_conditions_and_stage_bypass_have_no_file_or_event_effect")
            # Old callers remain valid; sealing/mandatory conditions are separate N2-02/03 work.
            legacy = peer.call("PUT", path, {"content":final_content+"\n// legacy save\n"})
            assert legacy["dict_revision"] == final["dict_revision"]+1
            checks.append("unsealed_legacy_client_compatibility_preserved")

            def assert_receipt(receipt):
                actual = peer.call("GET", path)
                assert receipt["content_sha256"] == hashlib.sha256(actual["content"].encode()).hexdigest()
                assert receipt["dict_revision"] == actual["dict_revision"]

            profile = peer.call("POST", "/provider-profiles", {"provider":"custom", "name":"unused-test-provider",
                "base_url":"http://127.0.0.1:1", "default_model":"unused", "api_key":"test-no-model"})

            def proposal_endpoint(name, args):
                conversation = peer.call("POST", "/conversations", {"case_id":api.case_id,
                    "title":"write coordination", "provider_profile_id":profile["id"], "model":"unused"})
                cid = conversation["id"]
                tool_id = f"{cid}:write-proposal"
                payload = api.root / "tool-seed.json"
                payload.write_text(json.dumps({"id":tool_id, "conversation_id":cid, "tool":name,
                    "args":json.dumps(args), "status":"pending"}), encoding="utf-8")
                seeded = subprocess.run([sys.executable, str(api.root / "seed_tool.py"), str(payload)], cwd=SERVER,
                    env=dict(api.env, PYTHONPATH=str(SERVER)+os.pathsep+api.env["PYTHONPATH"]),
                    capture_output=True, text=True, encoding="utf-8", timeout=20)
                assert seeded.returncode == 0, seeded.stderr
                return f"/conversations/{cid}/tool-calls/{tool_id}:approve"

            def contend(method, suffix, body, *, conditional=True, proposal=None):
                """Pause one process after file replace; the other route must wait."""
                baseline = peer.call("GET", path)
                event_count = peer.call("GET", api.prefix+"/events")["total"]
                for marker in ("after-publish.ready", "after-publish.release"):
                    (api.root / marker).unlink(missing_ok=True)
                winner_text = baseline["content"]+f"\n// CASE_WRITE_BARRIER {uuid.uuid4().hex}\n"
                request_body = dict(body) if body is not None else None
                endpoint = path+suffix
                if conditional:
                    if request_body is None:
                        endpoint += f"?expected_revision={baseline['dict_revision']}"
                    else:
                        request_body["expected_revision"] = baseline["dict_revision"]
                if proposal:
                    args = {"path":"0-caseDict/caseDict", "baseline_sha256":baseline["content_sha256"]}
                    if proposal == "propose_file_update":
                        args["content"] = baseline["content"]+"\n// stale Agent replacement\n"
                    else:
                        args.update(old_text="legacy save", new_text="Agent edit")
                    endpoint = proposal_endpoint(proposal, args)
                    request_body = {}
                with ThreadPoolExecutor(max_workers=2) as pool:
                    writing = pool.submit(api.call, "PUT", path,
                        {"content":winner_text, "expected_revision":baseline["dict_revision"]})
                    for _ in range(1000):
                        if (api.root / "after-publish.ready").exists():
                            break
                        if writing.done():
                            raise AssertionError(writing.result())
                        time.sleep(0.01)
                    assert (api.root / "after-publish.ready").exists()
                    editing = pool.submit(peer.call, method, endpoint, request_body,
                        expect=409 if conditional and not proposal else 200, raw=bool(proposal))
                    time.sleep(0.2)
                    try:
                        assert not editing.done(), f"{suffix} bypassed case control lock"
                    finally:
                        (api.root / "after-publish.release").write_text("release", encoding="utf-8")
                    writing.result(timeout=15)
                    response = editing.result(timeout=15)
                actual = peer.call("GET", path)
                if conditional:
                    if proposal:
                        frames = [json.loads(line[5:]) for line in response.decode().splitlines() if line.startswith("data:")]
                        outcome = next(frame for frame in frames if frame["type"] == "tool.result")
                        assert not outcome["ok"] and "CaseWriteConflict" in outcome["result"], outcome
                    else:
                        assert response["detail"]["code"] == "revision_conflict", response
                    assert actual["content"] == winner_text
                else:
                    assert winner_text.splitlines()[-1] in actual["content"], "Merge discarded new editor content"
                    assert_receipt(response)
                delta = 1 if conditional else 2
                assert actual["dict_revision"] == baseline["dict_revision"]+delta
                assert peer.call("GET", api.prefix+"/events")["total"] == event_count+delta

            contend("PUT", "/block", {"block":"extensions", "entries":{"schemaVersion":1, "entries":{}}})
            checks.append("block_editor_waits_then_rejects_old_revision_without_writing")
            extension = {"version":"1.0", "enabled":True, "settings":{"count":2}}
            contend("PUT", "/extensions/acme.mesh", extension)
            checks.append("extension_editor_uses_same_revision_conflict_protocol")
            assert_receipt(peer.call("PUT", path+"/extensions/acme.mesh", extension))
            contend("DELETE", "/extensions/acme.mesh", None)
            checks.append("extension_delete_cannot_remove_newer_version")
            assert_receipt(peer.call("PUT", path+"/block", {"block":"errors", "entries":{"entries":{
                "mesh-1":{"title":"Invalid mesh", "stage":"mesh", "category":"meshQuality",
                          "severity":"error", "status":"open", "blocking":True, "message":"negative Jacobian"}}}}))
            contend("PATCH", "/errors/mesh-1", {"status":"investigating"})
            checks.append("error_lifecycle_update_cannot_overwrite_newer_dictionary")
            contend("POST", ":advance", {"state":"V", "confirmed":True})
            checks.append("stage_advance_rechecks_revision_after_lock_before_gate_or_write")
            contend("PUT", "/extensions/acme.other", extension, conditional=False)
            checks.append("legacy_partial_edit_reads_after_lock_and_preserves_concurrent_changes")
            baseline = peer.call("GET", path)
            current_revision = baseline["dict_revision"]
            for method, suffix, body in [
                ("PUT", "/block", {"block":"extensions", "entries":{"schemaVersion":1,"entries":{}}}),
                ("PUT", "/extensions/acme.mesh", extension),
                ("PATCH", "/errors/mesh-1", {"status":"investigating"}),
                ("DELETE", "/extensions/acme.mesh", None),
            ]:
                endpoint = path+suffix
                if body is None:
                    endpoint += f"?expected_revision={current_revision}&expected_sha256={'0'*64}"
                else:
                    body = dict(body, expected_revision=current_revision, expected_sha256="0"*64)
                conflict = peer.call(method, endpoint, body, expect=409)
                assert conflict["detail"]["code"] == "file_conflict"
                assert peer.call("GET", path) == baseline
            checks.append("all_partial_dictionary_routes_enforce_content_hash_preconditions")
            sealed = peer.call("PATCH", path+"/errors/mesh-1", {"status":"investigating",
                "expected_revision":current_revision, "expected_sha256":baseline["content_sha256"]})
            assert sealed["dict_revision"] == current_revision+1
            assert_receipt(sealed)
            assert_receipt(peer.call("DELETE", path+"/extensions/acme.mesh"))
            entry = peer.call("GET", path)["parsed"]["errors"]["entries"]["mesh-1"]
            assert "expected_revision" not in entry and "expected_sha256" not in entry
            checks.append("write_conditions_are_not_saved_as_error_business_fields")
            for name in ("propose_file_update", "propose_text_replacement"):
                contend("POST", "", {}, proposal=name)
                checks.append(name+"_approval_waits_then_rejects_changed_baseline")
            baseline = peer.call("GET", path)
            endpoint = proposal_endpoint("propose_file_update", {"path":"0-caseDict/caseDict",
                "content":baseline["content"]+"\n// fresh Agent change\n", "baseline_sha256":baseline["content_sha256"]})
            approved = peer.call("POST", endpoint, {}, raw=True).decode()
            outcomes = [json.loads(line[5:]) for line in approved.splitlines() if line.startswith("data:")]
            assert next(frame for frame in outcomes if frame["type"] == "tool.result")["ok"]
            assert peer.call("GET", path)["dict_revision"] == baseline["dict_revision"]+1
            checks.append("fresh_Agent_proposal_commits_dictionary_and_revision_once")

            regular = "D-post/input.txt"
            receipt = peer.call("PUT", api.prefix+"/file", {"path":regular, "content":"initial", "expected_sha256":None})
            assert receipt["content_sha256"] == hashlib.sha256(b"initial").hexdigest()
            peer.call("PUT", api.prefix+"/file", {"path":regular, "content":"bad", "expected_sha256":None}, expect=409)
            file_before = peer.call("GET", api.prefix+"/file?path="+regular)
            assert file_before["content_sha256"] == hashlib.sha256(b"initial").hexdigest()
            peer.call("PUT", api.prefix+"/file", {"path":regular, "content":"changed",
                "expected_sha256":file_before["content_sha256"]})
            peer.call("DELETE", api.prefix+"/file?path="+regular+"&expected_sha256="+file_before["content_sha256"], expect=409)
            assert peer.call("GET", api.prefix+"/file?path="+regular)["content"] == "changed"
            checks.append("ordinary_file_create_save_delete_enforce_hash_and_absence_conditions")

            import urllib.request
            def send_chunk(upload_id):
                request = urllib.request.Request(peer.base+"/api/v1"+api.prefix+f"/uploads/{upload_id}?offset=0",
                    data=b"upload", method="PUT", headers={"Authorization":"Bearer "+peer.token})
                with peer.opener.open(request, timeout=15) as response:
                    assert response.status == 200

            session = peer.call("POST", api.prefix+"/uploads", {"path":regular, "size":6, "overwrite":True})
            send_chunk(session["id"])
            peer.call("PUT", api.prefix+"/file", {"path":regular, "content":"changed after upload began"})
            conflict = peer.call("POST", api.prefix+f"/uploads/{session['id']}:complete", expect=409)
            assert conflict["detail"]["code"] == "file_conflict"
            assert peer.call("GET", api.prefix+"/file?path="+regular)["content"] == "changed after upload began"
            assert peer.call("GET", api.prefix+f"/uploads/{session['id']}")["offset"] == 6
            peer.call("DELETE", api.prefix+f"/uploads/{session['id']}")
            checks.append("chunked_upload_conflict_preserves_newer_target_and_staged_bytes")
            session = peer.call("POST", api.prefix+"/uploads", {"path":regular, "size":6, "overwrite":True})
            send_chunk(session["id"])
            peer.call("POST", api.prefix+f"/uploads/{session['id']}:complete")
            assert peer.call("GET", api.prefix+"/file?path="+regular)["content"] == "upload"
            checks.append("chunked_upload_publishes_only_with_unchanged_target_baseline")

            from urllib.parse import quote
            def upload_inline(target, payload, query="", expect=200):
                boundary = "vf-upload-"+uuid.uuid4().hex
                data = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="input.bin"\r\n'
                        'Content-Type: application/octet-stream\r\n\r\n').encode()+payload+f"\r\n--{boundary}--\r\n".encode()
                request = urllib.request.Request(peer.base+"/api/v1"+api.prefix+"/upload?path="+quote(target,safe="")+query,
                    data=data, method="POST", headers={"Authorization":"Bearer "+peer.token,
                    "Content-Type":"multipart/form-data; boundary="+boundary})
                try:
                    response = peer.opener.open(request, timeout=15)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    result = json.loads(response.read())
                    assert response.status == expect, (response.status,result)
                    return result

            target = "Ref/binary-中文🙂.bin"
            meta_path = api.prefix+"/file:version?path="+quote(target,safe="")
            empty = peer.call("GET", meta_path)
            assert not empty["exists"] and empty["content_sha256"] is None and empty["size"] == 0
            payload = bytes(range(256))
            uploaded = upload_inline(target,payload,"&expected_absent=true")
            binary = peer.call("GET",meta_path)
            assert binary["size"] == 256 and binary["content_sha256"] == hashlib.sha256(payload).hexdigest()
            assert uploaded["content_sha256"] == binary["content_sha256"]
            assert peer.call("GET",api.prefix+"/raw?path="+quote(target,safe=""),raw=True) == payload
            checks.append("binary_version_and_create_only_inline_upload_preserve_exact_bytes_and_hash")
            upload_inline(target,b"stale create","&expected_absent=true",409)
            upload_inline(target,b"invalid","&expected_absent=true&expected_sha256="+binary["content_sha256"],422)
            fresh = upload_inline(target,b"external change","&expected_sha256="+binary["content_sha256"])
            upload_inline(target,b"stale replacement","&expected_sha256="+binary["content_sha256"],409)
            peer.call("DELETE",api.prefix+"/file?path="+quote(target,safe="")+"&expected_sha256="+binary["content_sha256"],expect=409)
            assert peer.call("GET",meta_path)["content_sha256"] == fresh["content_sha256"]
            peer.call("DELETE",api.prefix+"/file?path="+quote(target,safe="")+"&expected_sha256="+fresh["content_sha256"])
            assert not peer.call("GET",meta_path)["exists"]
            checks.append("inline_absence_hash_and_delete_conflicts_reject_changed_target_without_side_effects")
            peer.call("PUT",api.prefix+"/file",{"path":target,"content":"exists"})
            for expected in (None,"0"*64):
                conflict = peer.call("POST",api.prefix+"/uploads",{"path":target,"size":6,"overwrite":True,
                    "expected_sha256":expected},expect=409)
                assert conflict["detail"]["code"] == "file_conflict"
            version = peer.call("GET",meta_path)
            session = peer.call("POST",api.prefix+"/uploads",{"path":target,"size":6,"overwrite":True,
                "expected_sha256":version["content_sha256"]})
            send_chunk(session["id"])
            peer.call("POST",api.prefix+f"/uploads/{session['id']}:complete")
            checks.append("chunk_session_creation_checks_user_reviewed_baseline_before_accepting_bytes")
            for invalid in ("../outside.bin", "C:/outside.bin", "Ref"):
                peer.call("GET",api.prefix+"/file:version?path="+quote(invalid,safe=""),expect=422)
            stranger = peer.call("POST","/auth/register",{"email":uuid.uuid4().hex+"@example.com",
                "name":"stranger","password":"isolated-stranger-123"})
            peer.call("GET",meta_path,expect=404,extra_headers={"Authorization":"Bearer "+stranger["token"]})
            checks.append("file_version_rejects_directories_traversal_and_cross_user_reads")
            staging_root = raw_path.parents[1] / ".vibeflow/uploads"
            before_dirs = set(staging_root.iterdir()) if staging_root.exists() else set()
            peer.call("POST",api.prefix+"/uploads",{"path":"Ref","size":6,"overwrite":True},expect=422)
            after_dirs = set(staging_root.iterdir()) if staging_root.exists() else set()
            assert before_dirs == after_dirs
            checks.append("invalid_chunk_target_rejected_before_creating_staging_directory")
            protected = ["0-caseDict/caseDict"]
            if os.name == "nt":
                protected += ["0-CASEDICT/CASEDICT", "0-caseDict/caseDict."]
            original = peer.call("GET",path)
            for alias in protected:
                peer.call("PUT",api.prefix+"/file",{"path":alias,"content":"bad"},expect=400)
                upload_inline(alias,b"bad",expect=400)
                peer.call("DELETE",api.prefix+"/file?path="+quote(alias,safe=""),expect=400)
                peer.call("POST",api.prefix+"/uploads",{"path":alias,"size":6,"overwrite":True},expect=400)
            assert peer.call("GET",path) == original
            checks.append("ordinary_write_upload_delete_and_chunk_creation_protect_dictionary_filesystem_aliases")
            endpoint = proposal_endpoint("propose_file_update", {"path":protected[-1],
                "content":original["content"].replace("workflowState A", "workflowState B"),
                "baseline_sha256":original["content_sha256"]})
            frames = peer.call("POST",endpoint,{},raw=True).decode()
            outcome = next(json.loads(line[5:]) for line in frames.splitlines()
                           if line.startswith("data:") and json.loads(line[5:])["type"] == "tool.result")
            assert not outcome["ok"] and "workflowState" in outcome["result"]
            assert peer.call("GET",path) == original
            checks.append("Agent_dictionary_alias_uses_stage_gated_mirror_writer")
            from case_copy_memory_checks import verify_memory_and_copy
            checks.extend(verify_memory_and_copy(api, peer, proposal_endpoint))
            from attachment_backup_checks import verify_attachments_and_backup
            checks.extend(verify_attachments_and_backup(api, peer))
            from trash_coordination_checks import verify_trash
            checks.extend(verify_trash(api, peer))
            from professional_write_checks import verify_professional
            checks.extend(verify_professional(api, peer))
            from job_admission_checks import verify_admissions
            checks.extend(verify_admissions(api, peer))
            from session_write_checks import verify_sessions
            checks.extend(verify_sessions(api, peer))
            result = {"passed":True, "count":len(checks), "checks":checks,
                "api_processes":2, "shared_database":True, "shared_storage":True, "evidence":str(api.root)}
            (api.root / "write-evidence.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
        finally:
            if peer.proc.poll() is None:
                peer.proc.terminate()
                try:
                    peer.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    peer.proc.kill()
                    peer.proc.wait(timeout=5)
            peer.log.close()
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--postgres", action="store_true")
    args = parser.parse_args()
    report = Path(tempfile.mkdtemp(prefix="case-write-evidence-", dir=SERVER.parents[1] / "tmp"))
    results = {"sqlite":await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        pg_url = make_url(Settings(_env_file=SERVER / ".env").database_url)
        assert pg_url.drivername == "postgresql+asyncpg" and pg_url.host in {"127.0.0.1", "localhost", "::1"}
        name = "tmp_vf_write_"+uuid.uuid4().hex
        assert re.fullmatch(r"tmp_vf_write_[0-9a-f]{32}", name)
        admin = await asyncpg.connect(host=pg_url.host, port=pg_url.port or 5432, user=pg_url.username,
            password=pg_url.password, database="postgres", timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"')
            created = True
            url = pg_url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic, url, report, "upgrade", "head")
            results["postgresql"] = await asyncio.to_thread(verify, url)
        finally:
            if created:
                assert await admin.fetchval("SELECT current_database()") == "postgres"
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results["postgresql"]["temporary_database_removed"] = True
    (report / "result.json").write_text(json.dumps(results, indent=2)+"\n", encoding="utf-8")
    print("CASE_WRITE_OK "+json.dumps({"databases":list(results), "evidence":str(report)}))


if __name__ == "__main__":
    asyncio.run(main())
