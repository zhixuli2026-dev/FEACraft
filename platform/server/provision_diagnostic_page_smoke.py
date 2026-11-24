"""Read the account storage diagnosis through public HTTP across bounded pages."""
import asyncio
from collections import Counter, defaultdict
from contextlib import nullcontext
import json
from pathlib import Path
import sys
import tempfile

import httpx

from case_copy_source_lock_smoke import RUNTIME
from s3testsupport import ObjectStore
from seal_maintenance_page_smoke import database
from testsupport import IsolatedServer, SERVER


PATH = "/api/v1/case-provisions:maintenance"
PAGE = 7
ENTRY_BUDGET = 64
KINDS = {"pending": "pending_records", "history": "history_records", "staging": "staging_files",
         "claims": "reservation_records"}
FIXTURE = r"""
from app.main import app
"""


def server(label, configuration, database_url):
    api = IsolatedServer("provision-diag-" + label, app="provision_diag_fixture:app", database_url=database_url)
    native_temp = api.root / "native-temp"
    native_temp.mkdir()
    env = {**configuration, "TEMP": str(native_temp), "TMP": str(native_temp), "TMPDIR": str(native_temp),
           "VIBEFLOW_STORAGE_MEASUREMENT_MAX_ENTRIES": str(ENTRY_BUDGET),
           "VIBEFLOW_STORAGE_MAINTENANCE_ENABLED": "false"}
    module = api.root / "provision_diag_fixture.py"
    module.write_text("import os\nos.environ.update(" + repr(env) + ")\n" + FIXTURE, encoding="utf8")
    return api


def put_storage(root, store, key, body):
    if store is not None:
        store.raw_client.put_object(Bucket=store.bucket, Key=key, Body=body, IfNoneMatch="*")
        return
    path = root / key
    assert path.resolve().is_relative_to(root.resolve())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(body)


def observed_objects(store, prefix):
    listing = store.raw_client.list_objects_v2(Bucket=store.bucket, Prefix=prefix + "/", MaxKeys=1000)
    assert not listing.get("IsTruncated")
    objects = [row for row in listing.get("Contents", []) if not row["Key"].endswith("/")]
    assert all(row["Key"].startswith(prefix + "/") for row in objects)
    return len(objects), sum(row["Size"] for row in objects)


def verify(api, store, configuration, database_url, baseline, report):
    with api:
        api.create_case("Bounded account storage diagnosis")
        account = api.call("GET", "/auth/me")
        known = api.call("GET", api.prefix)["storage_prefix"]
        root = Path(api.env["VIBEFLOW_STORAGE_FS_ROOT"])
        base = f"workspaces/{account['id']}/cases"
        prefix = f"workspaces/{account['id']}/.case-provisions"
        assert known.startswith(base + "/")
        headers = {"Authorization": "Bearer " + api.token}
        with httpx.Client(base_url=api.base, timeout=30, trust_env=False) as client:
            initial = client.get(PATH, headers=headers)
            assert initial.status_code == 200, initial.text
            small = initial.json()
            if not baseline:
                assert small["complete"] and small["next_cursor"] is None
            assert not small["unreferenced_inventory"]

            count_fields = (*KINDS.values(), "history_bytes", "staging_bytes")
            expected_counts = Counter({field:small[field] for field in count_fields})
            for category, field in KINDS.items():
                for number in range(3):
                    body = bytes([number + 1]) * (number + 5)
                    put_storage(root, store, f"{prefix}/{category}/test-{number}.bin", body)
                    expected_counts[field] += 1
                    if category in {"history", "staging"}:
                        expected_counts[category + "_bytes"] += len(body)

            expected_inventory = {}
            for group in range(9):
                candidate = f"{base}/{900000 + group}" if group % 2 == 0 else f"{base}/{group:032x}/{900000 + group}"
                sizes = [index + group + 1 for index in range(9)]
                if group % 2:
                    sizes.append(12)
                    put_storage(root, store, candidate + '/alt/range.txt', b'0123456789AB')
                for index, size in enumerate(sizes[:9]):
                    put_storage(root, store, candidate + f"/Ref/data-{index:02d}.bin", b"x" * size)
                expected_inventory[candidate] = (len(sizes), sum(sizes))
                if store is not None and group == 3:
                    store.client.put_object(Bucket=store.bucket, Key=candidate + "/Ref/managed.bin",
                                            Body=b"managed evidence", IfNoneMatch="*")
                    physical = observed_objects(store, candidate)
                    assert physical[0] > len(sizes) and physical[1] >= sum(sizes) + len(b"managed evidence")
                    expected_inventory[candidate] = physical
                    report["cataloged_physical_inventory"] = physical
            assert len(expected_inventory) == 9 and sum(items[0] for items in expected_inventory.values()) > ENTRY_BUDGET
            put_storage(root, store, known + "/Ref/diagnostic-known.bin", b"registered case")
            foreign = api.call("POST", "/auth/register", {"email": "other-diagnosis@example.com",
                "name": "Other diagnostic account", "password": "isolated-test-123"})
            other = {"Authorization": "Bearer " + foreign["token"]}
            foreign_prefix = f"workspaces/{foreign['user']['id']}/cases/910000/Ref/outside.bin"
            foreign_case = foreign_prefix.split("/Ref/")[0]
            if store is None:
                put_storage(root, store, foreign_prefix, b"another account")
                foreign_files, foreign_bytes = 1, len(b"another account")
            else:
                store.client.put_object(Bucket=store.bucket, Key=foreign_prefix, Body=b"another account", IfNoneMatch="*")
                foreign_files, foreign_bytes = observed_objects(store, foreign_case)
                assert foreign_files > 1 and foreign_bytes > len(b"another account")
            first = client.get(PATH, headers=headers, params={"limit": PAGE})
            report["first_http"] = first.status_code
            if baseline:
                assert first.status_code == 409 and first.json()["detail"]["code"] == "storage_listing_limit", first.text
                return
            assert first.status_code == 200, first.text
            first_page = first.json()
            assert not first_page["complete"] and first_page["next_cursor"], first_page
            assert client.get(PATH, headers=other, params={"cursor": first_page["next_cursor"]}).status_code == 422
            assert client.get(PATH, headers=headers, params={"cursor": "not-a-cursor!"}).status_code == 422
            assert client.get(PATH, headers=headers, params={"limit": 501}).status_code == 422
            foreign_page = client.get(PATH, headers=other).raise_for_status().json()
            report["foreign_inventory"] = foreign_page
            assert foreign_page["unreferenced_inventory"] == [{"prefix": foreign_case,
                 "bytes": foreign_bytes, "files": foreign_files}], foreign_page
    report["first_api_exit"] = api.proc.poll()
    assert report["first_api_exit"] is not None
    assert not baseline

    peer = server("resumed", {**configuration, "VIBEFLOW_DATABASE_URL":api.env["VIBEFLOW_DATABASE_URL"],
                  "VIBEFLOW_JWT_SECRET":api.env["VIBEFLOW_JWT_SECRET"],
                  "VIBEFLOW_STORAGE_FS_ROOT": str(root)}, database_url)
    pages = [first_page]
    seen = set()
    with peer, httpx.Client(base_url=peer.base, timeout=30, trust_env=False) as client:
        while pages[-1]["next_cursor"]:
            cursor = pages[-1]["next_cursor"]
            assert cursor not in seen and len(pages) < 128
            seen.add(cursor)
            response = client.get(PATH, headers=headers, params={"cursor":cursor, "limit":PAGE})
            assert response.status_code == 200, response.text
            pages.append(response.json())
        chunks = defaultdict(int)
        counted = defaultdict(lambda: [0, 0])
        for page in pages:
            assert not page["complete"] and page["inventory"] == "physical"
            assert page["unreferenced_action"] == "retained"
            for key in expected_counts:
                chunks[key] += page[key]
            for item in page["unreferenced_inventory"]:
                counted[item["prefix"]][0] += item["files"]
                counted[item["prefix"]][1] += item["bytes"]
        assert chunks == expected_counts, chunks
        assert {key:tuple(value) for key,value in counted.items()} == expected_inventory, counted
        target = next(iter(expected_inventory)) + "/Ref/data-00.bin"
        if store is None:
            assert (root / target).read_bytes() == b"x"
        else:
            result = store.client.get_object(Bucket=store.bucket, Key=target)
            with result["Body"] as stream:
                assert stream.read() == b"x"
    report["resumed_api_exit"] = peer.proc.poll()
    assert report["resumed_api_exit"] is not None
    report["port_pair"] = [api.port, peer.port]
    report["pages"] = len(pages)
    report["counts"] = dict(chunks)
    report["orphan_prefixes"] = len(counted)
    report["unaltered_file"] = True


async def run(backend, database_name, baseline):
    evidence = Path(tempfile.mkdtemp(prefix=f"provision-diagnosis-{database_name}-{backend}-",
                                     dir=SERVER.parents[1] / "tmp"))
    report = {"passed": False, "phase": "baseline" if baseline else "fixed", "backend": backend,
              "database": database_name, "models": 0}
    context = ObjectStore(executable=RUNTIME, volume_max=24, authenticated=True) if backend == "s3" else nullcontext()
    print("PROVISION_DIAGNOSTIC_EVIDENCE", evidence, flush=True)
    try:
        async with database(database_name, evidence) as database_url:
            with context as store:
                configuration = store.settings() if store else {}
                api = server("first", configuration, database_url)
                report["first_api"] = {"port": api.port, "evidence": str(api.root)}
                await asyncio.to_thread(verify, api, store, configuration, database_url, baseline, report)
            if store is not None:
                report["store_exit"] = store.proc.poll()
                assert report["store_exit"] == 0
        report["passed"] = True
    except BaseException as error:
        report["failure"] = {"type": type(error).__name__, "message": str(error)[:1000]}
        raise
    finally:
        (evidence / "diagnosis-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


async def main(baseline):
    await run("fs", "sqlite", baseline)
    if not baseline:
        await run("s3", "sqlite", False)
        await run("fs", "postgresql", False)
        await run("s3", "postgresql", False)


if __name__ == "__main__":
    import argparse

    sys.path.insert(0, str(SERVER.parent / "tools"))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", action="store_true")
    parser.add_argument("--backend", choices=("fs", "s3"))
    parser.add_argument("--database", choices=("sqlite", "postgresql"), default="sqlite")
    args = parser.parse_args()
    if args.backend:
        asyncio.run(run(args.backend, args.database, args.baseline))
    else:
        asyncio.run(main(args.baseline))
