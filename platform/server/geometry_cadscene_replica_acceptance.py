"""Verify versioned CAD scenes across two actual APIs, CadQuery and an Edge browser."""
import argparse
import asyncio
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys


CASE = "Replicated CAD workspace"
NATIVE_NAME = "Native rounded block"
REMOTE_NAME = "Live scene from peer"
RESTORED_NAME = "Live after reconnect"
GEOMETRY = """import cadquery as cq

def build_geometry(ctx):
    shape = cq.Workplane('XY').box(5, 7, 3).edges().fillet(0.35)
    ctx.show('Native rounded block', shape, color='#dc832d')
"""


def write_result(root, name, value):
    (root / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


def byte_hash(content):
    return hashlib.sha256(content).hexdigest()


def publication_hash(document):
    content = {key: value for key, value in document.items() if key != "publication"}
    return byte_hash(json.dumps(content, sort_keys=True).encode())


async def browser(settings):
    import aiohttp
    from playwright.async_api import async_playwright, expect

    root, base, peer, case_id = Path(settings["root"]), settings["base"], settings["peer"], settings["case_id"]
    path = f"/api/v1/cases/{case_id}/geometry/scene"
    expect.set_options(timeout=25000)
    report = {"passed": False, "checks": {}, "versions": [], "page_errors": [], "model_routes": []}
    checks = report["checks"]
    links = []
    forwarded = []
    pending_reconnect = False
    snapshots = []

    async def close_route(route):
        result = route.close(code=1012, reason="Isolated temporary connection loss")
        if inspect.isawaitable(result):
            await result

    async def websocket_route(page_side):
        if pending_reconnect:
            await close_route(page_side)
            return
        server_side = page_side.connect_to_server()
        links.append((page_side, server_side))

        def forward(raw):
            if isinstance(raw, str):
                current = json.loads(raw)
                forwarded.append(current["type"])
                if current["type"] == "scene.snapshot":
                    snapshots.append({"source": current["source"], "version": current["version"]})
            page_side.send(raw)

        server_side.on_message(forward)

    async def expect_snapshot(version, source):
        async with asyncio.timeout(30):
            while not any(item["version"] == version and item["source"] == source for item in snapshots):
                await asyncio.sleep(0.05)

    try:
        async with aiohttp.ClientSession(trust_env=False, timeout=aiohttp.ClientTimeout(total=20)) as client, \
                async_playwright() as driver:
            primary_args = {"case_id": case_id, "token": settings["token"]}
            peer_args = {"case_id": case_id, "token": settings["peer_token"], "snapshot": "true"}
            async with client.ws_connect(base + "/ws/cadscene", params=primary_args) as primary_socket, \
                    client.ws_connect(peer + "/ws/cadscene", params=peer_args) as peer_socket:
                for socket in (primary_socket, peer_socket):
                    assert await socket.receive_json(timeout=12) == {"type": "ready", "backend": "cadscene", "case_id": case_id}
                initial = await peer_socket.receive_json(timeout=12)
                assert initial["type"] == "scene.snapshot" and initial["version"] == 0
                assert initial["source"] == "case" and initial["scene_url"] == path
                assert "objects" not in initial
                checks["ready_and_native_snapshot_reference"] = True

                edge = await driver.chromium.launch(channel=settings["channel"], args=["--enable-unsafe-swiftshader"])
                context = await edge.new_context(viewport={"width": 1500, "height": 1080}, locale="zh-CN")
                await context.add_init_script("if (location.origin === " + json.dumps(base)
                                              + ") localStorage.setItem('vf_token', " + json.dumps(settings["token"]) + ");")
                page = await context.new_page()
                page.set_default_timeout(30000)
                page.on("pageerror", lambda exc: report["page_errors"].append(str(exc)))
                scene_reads = []

                def request_started(request):
                    if request.method == "GET" and request.url == base + path:
                        scene_reads.append(request.url)
                    if request.method == "POST" and re.search(r"/(?:messages|goal:run|turns)(?:[?]|$)", request.url):
                        report["model_routes"].append(request.url)

                page.on("request", request_started)
                await page.route_web_socket("**/ws/cadscene?**", websocket_route)
                await page.goto(base)
                row = page.locator("[data-case-name=" + json.dumps(CASE) + "]")
                if await row.get_attribute("aria-pressed") != "true":
                    await row.click()
                await expect(row).to_have_attribute("aria-pressed", "true")
                host = page.get_by_test_id("geo-canvas-host")
                if not await host.is_visible():
                    await page.locator(".dv-tab:visible").filter(has_text=re.compile("^几何预览$")).click()
                tree = page.get_by_test_id("cad-tree")
                if not await tree.is_visible():
                    await page.get_by_role("button", name="对象与设置", exact=True).click()
                await expect(tree.get_by_role("button", name=NATIVE_NAME, exact=True)).to_be_visible()
                await expect_snapshot(0, "case")

                first = {**settings["native_object"], "name": REMOTE_NAME}
                await peer_socket.send_json({"type": "scene.replace", "objects": [first], "expected_version": 0,
                                             "expected_sha256": settings["native_sha256"],
                                             "expected_revision": initial["dict_revision"]})
                remote = await peer_socket.receive_json(timeout=12)
                on_primary = await primary_socket.receive_json(timeout=12)
                assert remote == on_primary
                assert remote["type"] == "scene.replace" and remote["objects"][0]["name"] == REMOTE_NAME
                version = remote["version"]
                assert type(version) is int and version > 0
                report["versions"].append(version)
                await expect(tree.get_by_role("button", name=REMOTE_NAME, exact=True)).to_be_visible()
                await expect(tree.get_by_role("button", name=NATIVE_NAME, exact=True)).to_have_count(0)
                await page.screenshot(path=root / "peer-scene-live.png")
                checks["legacy_and_versioned_fanout_from_peer"] = True

                await primary_socket.send_json({"type": "scene.clear", "expected_version": 0})
                outdated = await primary_socket.receive_json(timeout=12)
                assert outdated["type"] == "error" and outdated["code"] == "scene_version_conflict"
                assert outdated["current_version"] == version
                await peer_socket.send_json({"type": "scene.clear", "expected_sha256": settings["native_sha256"]})
                bad_hash = await peer_socket.receive_json(timeout=12)
                assert bad_hash["type"] == "error" and bad_hash["code"] == "file_conflict"
                await primary_socket.send_json({"type": "scene.clear", "expected_revision": initial["dict_revision"] + 1})
                bad_revision = await primary_socket.receive_json(timeout=12)
                assert bad_revision["type"] == "error" and bad_revision["code"] == "revision_conflict"
                bad_geometry = {**first, "faces": [[0, 1, len(first["vertices"])]]}
                await peer_socket.send_json({"type": "scene.replace", "objects": [bad_geometry]})
                invalid = await peer_socket.receive_json(timeout=12)
                assert invalid["type"] == "error" and "面索引越界" in invalid["detail"]
                await expect(tree.get_by_role("button", name=REMOTE_NAME, exact=True)).to_be_visible()
                checks["cross_replica_version_hash_revision_and_invalid_payload"] = True

                pending_reconnect = True
                # Playwright forwards closure to the connected server side by default.
                await close_route(links[-1][0])
                await primary_socket.send_json({"type": "scene.clear", "expected_version": version,
                                               "expected_sha256": remote["content_sha256"]})
                cleared = await primary_socket.receive_json(timeout=12)
                assert cleared == await peer_socket.receive_json(timeout=12)
                assert cleared["type"] == "scene.clear" and cleared["version"] > version
                report["versions"].append(cleared["version"])
                await expect(tree.get_by_role("button", name=REMOTE_NAME, exact=True)).to_be_visible()
                pending_reconnect = False
                await expect_snapshot(cleared["version"], "live")
                assert len(links) >= 2
                await expect(tree.get_by_role("button", name=REMOTE_NAME, exact=True)).to_have_count(0)
                await expect(page.get_by_test_id("geo-obj-info")).to_have_text("")
                await page.screenshot(path=root / "clear-restored-from-snapshot.png")
                checks["missed_clear_restored_on_real_reconnect"] = True

                before_stale = len(scene_reads)
                # Replay the real earlier notification through the active browser channel.
                links[-1][0].send(json.dumps(remote))
                await page.wait_for_timeout(700)
                assert len(scene_reads) == before_stale and await page.get_by_test_id("geo-obj-info").inner_text() == ""
                checks["authenticated_prior_version_ignored_without_fetch"] = True

                last = {**settings["native_object"], "name": RESTORED_NAME}
                await peer_socket.send_json({"type": "scene.replace", "objects": [last], "expected_version": cleared["version"]})
                recovered = await peer_socket.receive_json(timeout=12)
                assert recovered == await primary_socket.receive_json(timeout=12)
                assert recovered["version"] > cleared["version"] and recovered["objects"][0]["name"] == RESTORED_NAME
                report["versions"].append(recovered["version"])
                await expect(tree.get_by_role("button", name=RESTORED_NAME, exact=True)).to_be_visible()
                await page.screenshot(path=root / "recovered-scene.png")
                report["snapshots"] = snapshots
                assert not report["page_errors"] and not report["model_routes"], report
                checks["live_traffic_after_reconnect"] = True
                await edge.close()

            outsider = {"case_id": case_id, "token": settings["outsider_token"], "snapshot": "true"}
            try:
                async with client.ws_connect(peer + "/ws/cadscene", params=outsider):
                    raise AssertionError("Other account opened the CAD scene")
            except aiohttp.WSServerHandshakeError as exc:
                assert exc.status == 403, exc.status
                checks["other_account_handshake_denied"] = exc.status
        report["passed"] = True
    finally:
        write_result(root, "cadscene-replica-browser.json", report)


def durable_receipts(root, case_id):
    uri = (root / "test.sqlite").resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as db:
        rows = db.execute("SELECT seq,payload_json FROM durable_events WHERE case_id=? AND type=? ORDER BY seq",
                          (case_id, "cadscene.updated")).fetchall()
    return [{"version": seq, **json.loads(payload)} for seq, payload in rows]


def main(args):
    from testsupport import IsolatedServer

    api = IsolatedServer("geometry-cadscene-replica", app="cadscene_replica_fixture:app", access_log=True)
    peer = IsolatedServer("geometry-cadscene-peer", app="cadscene_replica_fixture:app", access_log=True)
    root = api.root
    print("GEOMETRY_CADSCENE_REPLICA_EVIDENCE", root, flush=True)
    dist = args.dist.resolve(strict=True)
    assert (dist / "index.html").is_file()
    mount = ("from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n"
             + f"app.mount('/', StaticFiles(directory={str(dist)!r}, html=True))\n")
    (root / "cadscene_replica_fixture.py").write_text(mount, encoding="utf8")
    browser_tmp = root / "browser-tmp"
    browser_tmp.mkdir()
    report = {"passed": False, "real_model_requests": 0, "mock_llm_calls": 0, "peer_root": str(peer.root)}
    try:
        primitives = subprocess.run(["node", str(Path(__file__).resolve().parents[1] / "tools/cad-scene-feed-smoke.mjs")],
                                    capture_output=True, text=True, encoding="utf8", timeout=30)
        (root / "cadscene-feed-primitives.log").write_text(primitives.stdout + primitives.stderr, encoding="utf8")
        primitives.check_returncode()
        with api:
            case = api.create_case(CASE)
            case_id = case["id"]
            route = f"/cases/{case_id}"
            owner = api.call("GET", "/auth/me")
            owner_token = api.token
            api.create_case("Unrelated CAD owner")
            outsider_token = api.token
            api.token = owner_token
            source = api.call("PUT", route + "/file", {"path": "A-geo/geometry.py", "content": GEOMETRY})
            job = api.wait_job(source["auto_preview_job"])
            assert job["exit_code"] == 0 and job["attempt"]["process_stop"]["verified"], job
            original = api.call("GET", route + "/geometry/scene")
            assert original["ocp_status"] == {"status": "ready"} and original["objects"][0]["name"] == NATIVE_NAME
            native = original["publication"]["id"]
            exact_native = publication_hash(original)
            file_route = route + "/raw?path=A-geo%2Fscene.json"
            initial_sha = byte_hash(api.call("GET", file_route, raw=True))
            shared = {key: api.env[key] for key in ("VIBEFLOW_DATABASE_URL", "VIBEFLOW_STORAGE_FS_ROOT", "VIBEFLOW_JWT_SECRET")}
            (peer.root / "cadscene_replica_fixture.py").write_text(
                "import os\nos.environ.update(" + repr(shared) + ")\n" + mount, encoding="utf8")
            with peer:
                token = peer.call("POST", "/auth/login", {"email": owner["email"], "password": "isolated-test-123"})["token"]
                peer.token = token
                report.update(primary_pid=api.proc.pid, peer_pid=peer.proc.pid, primary_port=api.port, peer_port=peer.port)
                settings = {"root": str(root), "base": api.base, "peer": peer.base, "token": owner_token,
                            "peer_token": token, "outsider_token": outsider_token, "case_id": case_id,
                            "native_object": original["objects"][0], "native_sha256": initial_sha, "channel": args.channel}
                subprocess.run([str(args.browser_python.resolve(strict=True)), "-B", "-X", "utf8",
                                str(Path(__file__)), "--browser"], input=json.dumps(settings), text=True,
                               check=True, timeout=240, env=dict(os.environ, TEMP=str(browser_tmp), TMP=str(browser_tmp)))
                browser_receipt = json.loads((root / "cadscene-replica-browser.json").read_text(encoding="utf8"))
                accepted = durable_receipts(root, case_id)
                assert [record["version"] for record in accepted] == browser_receipt["versions"]
                assert [record["action"] for record in accepted] == ["scene.replace", "scene.clear", "scene.replace"]
                assert all("objects" not in record and "content_sha256" in record for record in accepted)
                latest_bytes = api.call("GET", file_route, raw=True)
                assert latest_bytes == peer.call("GET", file_route, raw=True)
                assert accepted[-1]["content_sha256"] == byte_hash(latest_bytes)
                assert json.loads(latest_bytes)["objects"][0]["name"] == RESTORED_NAME
                for server in (api, peer):
                    readback = server.call("GET", route + "/geometry/scene")
                    assert readback["objects"][0]["name"] == RESTORED_NAME
                    assert readback["publication"]["status"] == "draft"
                    previous = server.call("GET", route + f"/geometry/scene?publication_id={native}")
                    assert publication_hash(previous) == exact_native
                report["native"] = {"job": job["id"], "publication": native, "sha256": exact_native}
                report["durable_commits"] = accepted
                report["both_replicas_and_immutable_history"] = True
            assert peer.proc.poll() is not None
        assert api.proc.poll() is not None
        report["passed"] = True
    finally:
        write_result(root, "cadscene-replica-result.json", report)


if __name__ == "__main__":
    if sys.argv[1:] == ["--browser"]:
        asyncio.run(browser(json.load(sys.stdin)))
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
        from regression_llm_policy import enforce

        enforce([Path(__file__)])
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--dist", type=Path, required=True)
        parser.add_argument("--browser-python", type=Path, required=True)
        parser.add_argument("--channel", default="msedge")
        main(parser.parse_args())
