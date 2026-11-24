"""Check public multipart restore using a v1 archive with a disk-backed binary payload."""
import asyncio
import base64
import hashlib
import io
import json
import os
import zipfile

import httpx

from live_llm_job_repeat_acceptance import main


def scenario(api, *, store=None, **_):
    workspace = api.call("GET", "/workspaces")[0]["id"]
    archive = api.call("GET", f"/workspaces/{workspace}/backup", raw=True)
    with zipfile.ZipFile(io.BytesIO(archive)) as source:
        manifest = json.loads(source.read("manifest.json"))
    payload = os.urandom(12 * 1024 * 1024)
    digest = hashlib.sha256(payload).hexdigest()
    path = "Ref/restore-source.bin"
    row = {"case_id": api.case_id, "path": path, "data": base64.b64encode(payload).decode("ascii"), "sha256": digest}
    manifest["files"].append(row)
    target = api.root / "restore-v1.zip"
    def pack():
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as output:
            output.writestr("manifest.json", json.dumps(manifest))
    pack()
    original_size = target.stat().st_size
    headers = {"Authorization": "Bearer " + api.token}
    with httpx.Client(base_url=api.base + "/api/v1", headers=headers, trust_env=False, timeout=180) as client:
        def restore():
            with target.open("rb") as source:
                return client.post("/workspaces:restore", files={"file": ("workspace.zip", source, "application/zip")})
        response = restore()
        assert response.status_code == 200, response.text[:800]
        restored = response.json()
        cases = api.call("GET", f'/workspaces/{restored["workspace_id"]}/cases')
        clone, = cases
        observed = api.call("GET", f'/cases/{clone["id"]}/raw?path={path}', raw=True)
        assert observed == payload and restored["files"] == len(manifest["files"])
        workspaces = api.call("GET", "/workspaces")
        row["sha256"] = "0" * 64
        pack()
        corrupt = restore()
        assert corrupt.status_code == 400 and "hash" in corrupt.json()["detail"]
        from app.services.backup_manifest import MAX_BACKUP_BYTES
        with target.open("ab") as bigger:
            bigger.truncate(MAX_BACKUP_BYTES + 1)
        oversized = restore()
        assert oversized.status_code == 413
        assert api.call("GET", "/workspaces") == workspaces
    row["sha256"] = digest
    pack()
    proof = {"backend": "s3" if store else "fs", "archive_bytes": original_size, "restored": restored,
             "binary_bytes": len(observed), "sha256": digest, "bad_digest": corrupt.status_code,
             "over_limit": oversized.status_code, "rejections_preserved_workspaces": True}
    (api.root / "workspace-restore.json").write_text(json.dumps(proof, indent=2), encoding="utf8")
    return proof


if __name__ == "__main__":
    asyncio.run(main(selected_override=scenario, store_volume_max=6))
