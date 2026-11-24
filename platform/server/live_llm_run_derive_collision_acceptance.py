"""Race two run directories in one case for a single derived directory."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import Barrier
from time import monotonic, sleep
from urllib.parse import quote

from live_llm_job_repeat_acceptance import main as live_main, put
from testsupport import IsolatedServer


def scenario(api, *, store=None, **_):
    seeds = ("left", "right")
    inputs = ("system/controlDict", "system/log.config", "0/U", "constant/material")
    outputs = ("log.icoFoam", "postProcessing/probes/0/U", "processors2/0/U", "processors4_0-3/0/U")
    target = "C-run/collision"
    for seed in seeds:
        for path in (*inputs, *outputs):
            put(api, f"C-run/{seed}/{path}", f"{seed} {path}\n")

    events = api.prefix + "/workflow-events"
    watermark = api.call("GET", events)["watermark"]
    jobs_before = api.call("GET", api.prefix + "/jobs")
    ready = Barrier(2)
    peer = IsolatedServer('derive-collision-peer',app='derive_peer:app',database_url=api.database_url)
    overrides = {key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_JWT_SECRET',
                                              'VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_INDEX_ROOT')}
    if store:
        overrides.update(store.settings())
        overrides['VIBEFLOW_STORAGE_FS_ROOT'] = str(peer.root/'s3-cache')
    (peer.root/'derive_peer.py').write_text('import os\nos.environ.update('+repr(overrides)+
                                          ')\nfrom app.main import app\n',encoding='utf8')
    peer.token = api.token

    def compete(seed):
        worker = api if seed == 'left' else peer
        ready.wait(timeout=10)
        deadline = monotonic() + 30
        while True:
            result = worker.call("POST", api.prefix + "/runs:derive", {"base": f"C-run/{seed}", "name": "collision"},
                                 expect=(200, 409))
            detail = result.get("detail")
            if not isinstance(detail, dict) or detail.get("code") not in {
                    "storage_quota_busy", "validation_busy", "file_sync_busy"}:
                return result
            if monotonic() >= deadline:
                raise TimeoutError(f"{seed} could not acquire the storage lock")
            sleep(0.1)

    with peer, ThreadPoolExecutor(max_workers=2) as clients:
        assert api.proc.pid != peer.proc.pid and api.port != peer.port
        replies = dict(zip(seeds, clients.map(compete, seeds)))
    assert peer.proc.poll() is not None
    winner, = [seed for seed, reply in replies.items() if reply.get("created") == target]
    loser, = [seed for seed in seeds if seed != winner]
    assert replies[winner]["files"] == len(inputs)
    assert "目标工况已存在" in str(replies[loser]["detail"]) or "目标目录已存在" in str(replies[loser]["detail"]), replies
    def read(path, expect=200):
        return api.call("GET", api.prefix + "/raw?path=" + quote(path), expect=expect, raw=True)

    digests = {}
    for path in inputs:
        content = read(f"{target}/{path}")
        assert content == f"{winner} {path}\n".encode()
        digests[path] = hashlib.sha256(content).hexdigest()
    for path in outputs:
        read(f"{target}/{path}", expect=404)
    for seed in seeds:
        for path in (*inputs, *outputs):
            assert read(f"C-run/{seed}/{path}") == f"{seed} {path}\n".encode()
    page = api.call("GET", events + f"?after={watermark}")
    assert not page["has_more"] and not page["blocked"]
    change, = [event for event in page["events"] if event["type"] == "files.changed" and
               any(path == target or path.startswith(target+'/') for path in event['payload']['paths'])]
    hints = change["payload"]["paths"]
    assert change["payload"]["rescan"] and (hints == [target] or set(hints) == {f"{target}/{p}" for p in inputs})
    assert not any(event["type"].startswith("artifact.") for event in page["events"])
    assert api.call("GET", api.prefix + "/jobs") == jobs_before
    proof = {"backend": "s3" if store else "fs", "replies": replies, "winner": winner,
             "copied_sha256": digests, "excluded": outputs, "both_sources_preserved": True,
             "file_change_event": change["seq"], "paths": hints, "jobs": len(jobs_before),
             "api_pids": [api.proc.pid,peer.proc.pid], "peer_receipt": str(peer.root/'result.json')}
    (api.root / "derive-collision.json").write_text(json.dumps(proof, indent=2), encoding="utf8")
    return proof


if __name__ == "__main__":
    asyncio.run(live_main(selected_override=scenario))
