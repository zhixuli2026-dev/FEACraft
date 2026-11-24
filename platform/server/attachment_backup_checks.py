"""Real two-API attachment/backup/restore failure and coherence checks."""
import base64
from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import io
import json
import struct
import time
import urllib.error
import urllib.request
import uuid
import zipfile

from fault_fixture import FileBarrier

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z3XcAAAAASUVORK5CYII=")


def install_barriers(root):
    from fastapi import HTTPException
    from app import storage
    from app.routers import agent
    from app.services import backup, quota, case_provision
    old_put, old_record, old_event = storage.put_bytes, backup._file_record, agent._append_event
    old_llm, old_storage = quota.check_llm_quota, quota.check_storage_quota
    old_creation_write = case_provision.write_file

    def put(prefix, path, data, *args, **kwargs):
        result = old_put(prefix, path, data, *args, **kwargs)
        if path.startswith(".vibeflow/attachments/"):
            if (root / "attachment-fail.armed").exists() and path.endswith("/broken.png"):
                raise OSError("injected failure after attachment publication")
            if (root / "attachment.armed").exists():
                FileBarrier(root, "attachment-published", timeout=20).wait()
        return result

    def creation_write(plan, path, data):
        result = old_creation_write(plan, path, data)
        if plan['kind'] == 'backup.restore' and path == "Ref/restore-fail.txt" and (root / "restore-fail.armed").exists():
            raise OSError("injected failure after restore publication")
        return result

    def record(case, path, data):
        result = old_record(case, path, data)
        armed = root / "backup.armed"
        if armed.exists() and str(case.id) == armed.read_text(encoding="utf-8") and path == "0-caseDict/caseDict":
            FileBarrier(root, "backup-read", timeout=20).wait()
        return result

    async def event(db, cid, kind, payload):
        result = await old_event(db, cid, kind, payload)
        if payload.get("content") == "INJECT_ATTACHMENT_EVENT_FAILURE":
            raise ValueError("injected event failure after flush")
        return result

    async def llm(db, user):
        if (root / "llm-quota-block.armed").exists():
            raise HTTPException(429, "injected LLM quota exhaustion")
        return await old_llm(db, user)

    async def storage_quota(db, user, incoming):
        if (root / "storage-quota-block.armed").exists():
            raise HTTPException(429, "injected storage quota exhaustion")
        return await old_storage(db, user, incoming)

    storage.put_bytes, backup._file_record, agent._append_event = put, record, event
    from app.services import file_transactions
    old_apply = file_transactions._apply

    def applying(case, directory, index, entry, side, expected):
        result = old_apply(case, directory, index, entry, side, expected)
        path = entry['path']
        if side == 'after' and path.startswith('.vibeflow/attachments/'):
            if (root/'attachment-fail.armed').exists() and path.endswith('/broken.png'):
                raise OSError('injected failure after journaled attachment publication')
            if (root/'attachment.armed').exists():
                FileBarrier(root, 'attachment-published', timeout=20).wait()
        return result

    file_transactions._apply = applying
    case_provision.write_file = creation_write
    quota.check_llm_quota, quota.check_storage_quota = llm, storage_quota


def verify_attachments_and_backup(api, peer):
    checks = []
    ws = peer.call("POST", "/workspaces", {"name": "attachment-backup"})["id"]
    first = peer.call("POST", f"/workspaces/{ws}/cases", {"name": "source-first"})
    second = peer.call("POST", f"/workspaces/{ws}/cases", {"name": "source-second"})
    cid = first["id"]
    route = f"/cases/{cid}"
    raw_root = next((api.root / "files/workspaces").glob(f"*/cases/{cid}"))
    profile = peer.call("POST", "/provider-profiles", {"provider": "custom", "name": "unused-attachments",
        "base_url": "http://127.0.0.1:1", "default_model": "unused", "api_key": "test-no-model"})
    conv = peer.call("POST", "/conversations", {"case_id": cid, "title": "attachments",
        "provider_profile_id": profile["id"], "model": "unused"})["id"]
    message = f"/conversations/{conv}/messages"
    picture = {"name": "pixel.png", "mime": "image/png", "data": base64.b64encode(PNG).decode()}
    body = {"content": "picture", "provider_profile_id": profile["id"], "images": [picture]}

    def files():
        return {p.relative_to(api.root / "files").as_posix() for p in (api.root / "files").rglob("*")
                if p.is_file() and not {'.case-provisions','file-transactions'} & set(p.relative_to(api.root / "files").parts)}

    def event_count():
        return len(peer.call("GET", f"/conversations/{conv}")["events"])

    def reset(name):
        for suffix in ("ready", "release"):
            (api.root / f"{name}.{suffix}").unlink(missing_ok=True)

    def wait(name, future):
        for _ in range(1500):
            if (api.root / f"{name}.ready").exists():
                return
            if future.done():
                raise AssertionError((name, future.result()))
            time.sleep(0.01)
        raise AssertionError(name+" did not reach barrier")

    def release(name):
        (api.root / f"{name}.release").write_text("release", encoding="utf-8")

    def unpack(data):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return json.loads(z.read("manifest.json"))

    def pack(manifest):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("manifest.json", json.dumps(manifest))
        return output.getvalue()

    def restore(data, expect=200):
        boundary = uuid.uuid4().hex
        payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="backup.zip"\r\n'
            'Content-Type: application/zip\r\n\r\n').encode()+data+f"\r\n--{boundary}--\r\n".encode()
        request = urllib.request.Request(peer.base+"/api/v1/workspaces:restore", data=payload, method="POST",
            headers={"Authorization": "Bearer "+peer.token, "Content-Type": "multipart/form-data; boundary="+boundary})
        try:
            response = peer.opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            result = response.read()
            assert response.status == expect, (response.status, result[:1000])
            return json.loads(result) if expect != 500 else result

    baseline, count = files(), event_count()
    invalid = dict(picture, data="not-base64")
    peer.call("POST", message, dict(body, images=[picture, invalid]), expect=400)
    assert files() == baseline and event_count() == count
    checks.append("attachment_batch_validation_rejects_late_bad_image_before_any_file_or_event")
    for flag in ("llm-quota-block", "storage-quota-block"):
        armed = api.root / (flag+".armed")
        armed.write_text("armed", encoding="utf-8")
        try:
            peer.call("POST", message, body, expect=429)
        finally:
            armed.unlink()
        assert files() == baseline and event_count() == count
    checks.append("LLM_and_storage_quota_rejection_precede_attachment_file_publication")
    armed = api.root / "attachment-fail.armed"
    armed.write_text("armed", encoding="utf-8")
    try:
        peer.call("POST", message, dict(body, images=[picture, dict(picture, name="broken.png")]), expect=500, raw=True)
    finally:
        armed.unlink()
    assert (raw_root/'.vibeflow/file-transactions/pending').exists()
    peer.call('GET', route+'/tree')
    assert files() == baseline and event_count() == count
    checks.append("attachment_storage_failure_recovers_entire_journaled_batch_and_no_message")
    peer.call("POST", message, dict(body, content="INJECT_ATTACHMENT_EVENT_FAILURE"), expect=500, raw=True)
    assert (raw_root/'.vibeflow/file-transactions/pending').exists()
    peer.call('GET', route+'/tree')
    assert files() == baseline and event_count() == count
    checks.append("attachment_event_failure_after_flush_rolls_back_message_and_cleans_files")

    reset("attachment-published")
    (api.root / "attachment.armed").write_text("armed", encoding="utf-8")
    with ThreadPoolExecutor(max_workers=3) as pool:
        posting = pool.submit(api.call, "POST", message, body, raw=True)
        try:
            wait("attachment-published", posting)
            image = next(p for p in (raw_root / ".vibeflow/attachments").rglob("*") if p.is_file())
            metadata = pool.submit(peer.call, "GET", route+"/file:version?path="+image.relative_to(raw_root).as_posix())
            saving = pool.submit(peer.call, "GET", f"/workspaces/{ws}/backup", raw=True)
            time.sleep(0.25)
            assert not metadata.done() and not saving.done()
        finally:
            (api.root / "attachment.armed").unlink(missing_ok=True)
            release("attachment-published")
        posting.result(timeout=20)
        assert metadata.result(timeout=20)["content_sha256"] == hashlib.sha256(PNG).hexdigest()
        archive = saving.result(timeout=20)
    manifest = unpack(archive)
    user_event = next(json.loads(row["payload"]) for row in manifest["events"] if row["type"] == "message")
    attachment = user_event["images"][0]
    captured = next(row for row in manifest["files"] if row["case_id"] == cid and row["path"] == attachment["path"])
    assert base64.b64decode(captured["data"]) == PNG
    checks.append("metadata_and_backup_wait_for_attachment_and_message_commit_then_capture_matching_bytes")

    reset("backup-read")
    (api.root / "backup.armed").write_text(str(cid), encoding="utf-8")
    original = peer.call("GET", route+"/caseDict")
    other_route = f"/cases/{second['id']}"
    with ThreadPoolExecutor(max_workers=3) as pool:
        saving = pool.submit(api.call, "GET", f"/workspaces/{ws}/backup", raw=True)
        try:
            wait("backup-read", saving)
            editing = pool.submit(peer.call, "PUT", route+"/caseDict", {"content": original["content"]+"\n// after backup\n"})
            other_edit = pool.submit(peer.call, "PUT", other_route+"/file", {"path": "Ref/later.txt", "content": "later"})
            time.sleep(0.25)
            assert not editing.done() and not other_edit.done()
        finally:
            (api.root / "backup.armed").unlink(missing_ok=True)
            release("backup-read")
        saved = unpack(saving.result(timeout=20))
        editing.result(timeout=20)
        other_edit.result(timeout=20)
    dictionary = next(row for row in saved["files"] if row["case_id"] == cid and row["path"] == "0-caseDict/caseDict")
    case = next(row for row in saved["cases"] if row["id"] == cid)
    assert dictionary["sha256"] == original["content_sha256"] and case["dict_revision"] == original["dict_revision"]
    assert not any(row["path"] == "Ref/later.txt" for row in saved["files"])
    checks.append("backup_locks_all_sources_and_keeps_dictionary_bytes_revision_and_other_case_files_coherent")

    reset("backup-read")
    (api.root / "backup.armed").write_text(str(cid), encoding="utf-8")
    with ThreadPoolExecutor(max_workers=1) as pool:
        saving = pool.submit(api.call, "GET", f"/workspaces/{ws}/backup", expect=409)
        try:
            wait("backup-read", saving)
            dictionary = raw_root / "0-caseDict/caseDict"
            dictionary.write_text(dictionary.read_text(encoding="utf-8")+"\n// external during backup\n", encoding="utf-8")
        finally:
            (api.root / "backup.armed").unlink(missing_ok=True)
            release("backup-read")
        saving.result(timeout=20)
    checks.append("backup_rejects_observed_external_source_change_instead_of_returning_mixed_archive")

    def reject(changed, label):
        before, workspaces = files(), peer.call("GET", "/workspaces")
        restore(pack(changed), expect=400)
        assert files() == before and peer.call("GET", "/workspaces") == workspaces, label

    for replacement in ([], {"schema": "bad"}, dict(manifest, cases=[None])):
        reject(replacement, "invalid shape")
    checks.append("restore_rejects_invalid_manifest_root_and_records_before_allocating_targets")
    for bad_path in ("../outside.txt", "C:/outside.txt", "Ref//alias.txt", "Ref/file.", "Ref/bad\x00name"):
        changed = copy.deepcopy(manifest)
        changed["files"][0]["path"] = bad_path
        reject(changed, bad_path)
    changed = copy.deepcopy(manifest)
    changed["files"].append(copy.deepcopy(changed["files"][0]))
    reject(changed, "duplicate path")
    checks.append("restore_rejects_traversal_drive_NUL_normalization_aliases_and_duplicate_paths")
    changed = copy.deepcopy(manifest)
    changed["files"][0]["case_id"] = 999999
    reject(changed, "foreign file")
    changed = copy.deepcopy(manifest)
    changed["cases"].append(copy.deepcopy(changed["cases"][0]))
    reject(changed, "duplicate case")
    changed = copy.deepcopy(manifest)
    changed["conversations"][0]["forked_from_id"] = changed["conversations"][0]["id"]
    reject(changed, "fork cycle")
    changed = copy.deepcopy(manifest)
    changed["events"].append(copy.deepcopy(changed["events"][0]))
    reject(changed, "duplicate event")
    checks.append("restore_rejects_foreign_references_duplicate_IDs_event_sequences_and_fork_cycles")
    malicious = bytearray(archive)
    central = malicious.index(b"PK\x01\x02")
    struct.pack_into("<I", malicious, central+24, 160*1024*1024+1)
    restore(bytes(malicious), expect=400)
    checks.append("restore_rejects_oversized_uncompressed_manifest_from_ZIP_header_before_decompression")

    changed = copy.deepcopy(manifest)
    changed["files"].append({"case_id": cid, "path": "Ref/restore-fail.txt", "data": base64.b64encode(b"FAIL_AFTER_WRITE").decode(),
        "sha256": hashlib.sha256(b"FAIL_AFTER_WRITE").hexdigest()})
    before, workspaces = files(), peer.call("GET", "/workspaces")
    (api.root / "restore-fail.armed").write_text("armed", encoding="utf-8")
    try:
        restore(pack(changed), expect=500)
    finally:
        (api.root / "restore-fail.armed").unlink()
    assert peer.call("GET", "/workspaces") == workspaces
    recovery = peer.call("POST", "/case-provisions:recover")
    assert any(row['status'] == 'rolled_back' for row in recovery['results']), recovery
    assert all(row['status'] in {'committed', 'rolled_back'} for row in recovery['results']), recovery
    assert files() == before and peer.call("GET", "/workspaces") == workspaces
    checks.append("restore_storage_failure_after_publication_cleans_all_new_case_files_and_DB_rows")
    (api.root / "storage-quota-block.armed").write_text("armed", encoding="utf-8")
    try:
        restore(archive, expect=429)
    finally:
        (api.root / "storage-quota-block.armed").unlink()
    assert files() == before and peer.call("GET", "/workspaces") == workspaces
    checks.append("restore_checks_storage_quota_before_target_rows_or_files")

    remap = copy.deepcopy(manifest)
    remap["jobs"] = []
    for case in remap["cases"]:
        data = f"LOG_FOR_{case['name']}".encode()
        remap["jobs"].append({"case_id": case["id"], "type": "shell", "command": "", "status": "succeeded",
            "exit_code": 0, "log_s3_key": "logs/shared.log", "parsed_summary": "{}"})
        remap["files"].append({"case_id": case["id"], "path": "logs/shared.log", "data": base64.b64encode(data).decode(),
            "sha256": hashlib.sha256(data).hexdigest()})
    restored = restore(pack(remap))
    assert restored["jobs"] == 2 and restored["cases"] == 2
    for case in peer.call("GET", f"/workspaces/{restored['workspace_id']}/cases"):
        jobs = peer.call("GET", f"/cases/{case['id']}/jobs")
        assert len(jobs) == 1
        content = peer.call("GET", f"/cases/{case['id']}/raw?path="+jobs[0]["log_path"], raw=True)
        assert content == f"LOG_FOR_{case['name']}".encode()
    checks.append("restore_remaps_same_relative_job_log_independently_per_source_case")
    return checks
