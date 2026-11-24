"""Offline restore-package bounds, reference graph and path collision checks."""
import base64
import hashlib
import io
import json
import os
import struct
from tempfile import SpooledTemporaryFile
from unittest.mock import patch
import zipfile

from app.services import backup_manifest as bm


def file(path, data=b"workflowState A;\n"):
    return {"case_id": 1, "path": path, "data": base64.b64encode(data).decode(),
            "sha256": hashlib.sha256(data).hexdigest()}


def fixture():
    return {"schema": bm.SCHEMA, "workspace": {"name": "source"},
        "cases": [{"id": 1, "name": "source", "workflow_state": "A", "dict_revision": 1,
            "case_meta": "{}", "parse_status": "ok"}],
        "jobs": [], "conversations": [
            {"id": 1, "case_id": 1, "title": "parent", "model": "mock", "permission_mode": "plan"},
            {"id": 2, "case_id": 1, "title": "child", "model": "mock", "permission_mode": "plan",
             "forked_from_id": 1, "branch_head_event_id": 1}],
        "events": [{"id": 1, "conversation_id": 1, "seq": 1, "event_id": "old-event", "type": "message", "payload": "{}"}],
        "tool_calls": [], "files": [file("0-caseDict/caseDict")]}


def pack(manifest):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
    return output.getvalue()


def main():
    checks = []
    source = fixture()
    value = bm.load_manifest(pack(source))
    assert value["files"][0]["_bytes"] == b"workflowState A;\n"
    assert value["conversations"][1]["branch_head_event_id"] == 1
    bm.validate_manifest(source, keep_bytes=False)
    assert "_bytes" not in source["files"][0]
    checks.append("valid_parent_branch_and_file_hash_decode_without_mutating_export_bytes")

    archive = pack(source)
    for spool_limit in (16, len(archive) + 1):
        with SpooledTemporaryFile(max_size=spool_limit) as upload:
            upload.write(archive)
            decoded = bm.load_manifest(upload, retain_encoded=False)
            assert decoded["files"][0]["_bytes"] == value["files"][0]["_bytes"]
            assert "data" not in decoded["files"][0] and "data" in value["files"][0]
            with patch.object(bm, "MAX_BACKUP_BYTES", len(archive) - 1):
                try:
                    bm.load_manifest(upload)
                except bm.BackupTooLarge:
                    pass
                else:
                    raise AssertionError("Oversized multipart spool accepted")
            assert not upload.closed
    checks.append("memory_and_disk_spools_support_validation_size_checks_and_caller_owned_lifetime")

    def reject(name, manifest):
        try:
            bm.load_manifest(pack(manifest))
        except bm.BackupError:
            checks.append(name)
        else:
            raise AssertionError(name+" accepted")

    changed = fixture()
    changed["files"].append(file("0-caseDict"))
    reject("file_and_parent_directory_collision", changed)
    if os.name == "nt":
        for path in ("Ref/NUL.txt", "Ref/bad?.txt", "0-CASEDICT/CASEDICT"):
            changed = fixture()
            changed["files"].append(file(path))
            reject("Windows_device_invalid_character_or_case_alias_"+path, changed)
    changed = fixture()
    changed["events"][0]["seq"] = 2147483648
    reject("event_sequence_DB_integer_overflow", changed)
    changed = fixture()
    changed["cases"][0]["dict_revision"] = True
    reject("boolean_revision_is_not_an_integer", changed)
    changed = fixture()
    changed["cases"][0]["created_at"] = "not-a-date"
    reject("invalid_date_before_DB_work", changed)
    changed = fixture()
    changed["conversations"].append({"id": 3, "case_id": 1, "title": "unrelated", "model": "mock", "permission_mode": "plan"})
    changed["events"][0]["conversation_id"] = 3
    reject("branch_head_must_belong_to_ancestor_not_other_conversation", changed)
    changed = fixture()
    changed["tool_calls"] = [{"conversation_id": 1, "tool": "run_shell_command", "args": "{}", "status": "pending", "result": ""}]
    reject("pending_tool_cannot_be_restored_as_live_work", changed)
    changed = fixture()
    changed["jobs"] = [{"case_id": 1, "type": "shell", "command": "", "status": "running", "log_s3_key": "", "parsed_summary": "{}"}]
    reject("running_job_cannot_be_restored_as_live_work", changed)
    with patch.object(bm, "MAX_SOURCE_BYTES", 4):
        reject("decoded_file_bytes_enforce_limit_before_storage", fixture())
    changed = fixture()
    changed["files"] = [file("0-caseDict/caseDict", b"ABC"), file("Ref/extra", b"DEFG")]
    with patch.object(bm, "MAX_SOURCE_BYTES", 6):
        reject("combined_file_bytes_enforce_total_limit", changed)
    changed = fixture()
    changed["cases"][0]["workflow_state"] = "E"
    reject("restored_dictionary_stage_must_match_DB_mirror", changed)
    changed = fixture()
    changed["cases"][0]["case_meta"] = '{"injected":"value"}'
    reject("restored_dictionary_metadata_must_match_DB_mirror", changed)
    with patch.object(bm, "MAX_MANIFEST_BYTES", 64):
        reject("compressed_archive_is_limited_by_expanded_manifest_size", fixture())
    with patch.object(bm, "MAX_RECORDS", 1):
        reject("record_count_limit", fixture())
    encrypted = bytearray(pack(fixture()))
    central = encrypted.index(b"PK\x01\x02")
    struct.pack_into("<H", encrypted, central+8, 1)
    try:
        bm.load_manifest(bytes(encrypted))
    except bm.BackupError:
        checks.append("encrypted_manifest_rejected_before_read")
    else:
        raise AssertionError("Encrypted archive accepted")
    corrupt = bytearray(pack(fixture()))
    name_size, extra_size = struct.unpack_from("<HH", corrupt, 26)
    corrupt[30+name_size+extra_size] = 255
    try:
        bm.load_manifest(bytes(corrupt))
    except bm.BackupError:
        checks.append("corrupt_deflate_stream_returns_validation_error")
    else:
        raise AssertionError("Corrupt compressed stream accepted")
    changed = fixture()
    for n in range(3, 2003):
        changed["conversations"].append({"id": n, "case_id": 1, "title": "deep", "model": "mock", "permission_mode": "plan",
            "forked_from_id": n-1, "branch_head_event_id": 1})
    assert len(bm.load_manifest(pack(changed))["conversations"]) == 2002
    checks.append("deep_valid_branch_graph_does_not_depend_on_Python_recursion_limit")
    result = {"passed": True, "count": len(checks), "checks": checks}
    print("BACKUP_MANIFEST_OK "+json.dumps(result))


if __name__ == "__main__":
    main()
