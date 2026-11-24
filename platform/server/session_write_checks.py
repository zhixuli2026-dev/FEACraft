"""Derived inputs and native session admission; test-app-only file barriers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

from fault_fixture import FileBarrier
from testsupport import SERVER

STL = """solid triangle
facet normal 0 0 1
outer loop
vertex 0 0 0
vertex 1 0 0
vertex 0 1 0
endloop
endfacet
endsolid triangle
"""


def install_barriers(root):
    from app import storage
    from app.routers import jobs, terminal, viz
    from app.services import run_derive

    def match(name, cid):
        path = root / (name+".armed")
        return path.exists() and path.read_text() == str(cid)

    for module in (jobs, viz):
        original = module.get_owned_case

        async def authorized(cid, *args, _original=original, **kwargs):
            case = await _original(cid, *args, **kwargs)
            if match("session-auth", cid):
                (root / f"session-auth-{cid}-{uuid.uuid4().hex}.ready").touch()
            return case

        module.get_owned_case = authorized
    old_auth = terminal._authorize_terminal

    async def terminal_auth(db, cid, uid):
        case = await old_auth(db, cid, uid)
        if match("session-auth", cid):
            (root / f"session-auth-{cid}-{uuid.uuid4().hex}.ready").touch()
        return case

    terminal._authorize_terminal = terminal_auth
    old_inventory = run_derive.source_files

    def source_files(prefix, base):
        cid = prefix.rstrip("/").split("/")[-1]
        stages = storage._fs_path(prefix, ".vibeflow/derive")
        if match("derive-verify", cid) and stages.exists() and any(stages.iterdir()):
            failing = match("derive-fail", cid)
            FileBarrier(root, "derive-verify", timeout=20).wait()
            if failing:
                raise OSError("Injected failure after partial preparation")
        return old_inventory(prefix, base)

    run_derive.source_files = source_files

    def cancel_watcher(name, cid):
        if not match(name+"-cancel", cid):
            return None
        caller = asyncio.current_task()

        async def cancel():
            while not (root / f"{name}.ready").exists():
                await asyncio.sleep(0.01)
            caller.cancel()
            await asyncio.sleep(0.03)
            caller.cancel()
            (root / f"{name}-cancel.ready").touch()

        return asyncio.create_task(cancel())

    old_coordinate = jobs.coordinate

    @asynccontextmanager
    async def coordinate(db, case, **kwargs):
        async with old_coordinate(db, case, **kwargs) as snapshot:
            watcher = cancel_watcher("derive-verify", case.id)
            try:
                yield snapshot
            finally:
                if watcher is not None and not watcher.done():
                    watcher.cancel()

    jobs.coordinate = coordinate
    old_launch = viz.VizSession.start

    async def launch(self, *args, **kwargs):
        result = await old_launch(self, *args, **kwargs)
        (root / f"viz-created-{self.case_id}.json").write_text(json.dumps({
            "sid":self.id, "pid":self.owned.proc.pid, "port":self.port,
            "root":self.owned.record, "kernel":self.ready_birth}))
        if match("viz-start", self.case_id):
            if match("viz-start-cancel", self.case_id):
                asyncio.current_task().cancel()
            await FileBarrier(root, "viz-start", timeout=20).wait_async()
        return result

    viz.VizSession.start = launch
    from app.services.owned_terminal import OwnedTerminal
    from app.services.async_ops import finish_before_cancel
    old_start = terminal.TerminalSession.start

    async def starting(self, *args, **kwargs):
        self.transport.fixture_session = self
        return await old_start(self, *args, **kwargs)

    terminal.TerminalSession.start = starting
    old_bridge = OwnedTerminal.start_bridge

    async def bridge(self):
        result = await old_bridge(self)
        session = self.fixture_session
        (root / f"terminal-created-{session.case_id}.json").write_text(json.dumps({
            "sid":session.id, "pid":self.owned.proc.pid, "root":self.owned.record, "gate":self.gate_birth}))
        if match("terminal-spawn", session.case_id):
            await finish_before_cancel(asyncio.to_thread(FileBarrier(root, "terminal-spawn", timeout=20).wait))
        return result

    OwnedTerminal.start_bridge = bridge
    old_new = terminal._new_session

    async def new_session(cid, *args):
        watcher = cancel_watcher("terminal-spawn", cid)
        try:
            return await old_new(cid, *args)
        finally:
            if watcher is not None and not watcher.done():
                watcher.cancel()

    terminal._new_session = new_session
    old_unsubscribe = terminal.TerminalSession.unsubscribe

    def unsubscribe(self, queue):
        old_unsubscribe(self, queue)
        (root / f"terminal-unsubscribed-{self.id}.json").write_text(json.dumps({"subscribers": len(self._subscribers)}))

    terminal.TerminalSession.unsubscribe = unsubscribe


def verify_sessions(api, peer):
    import websockets
    checks = []
    ws_id = peer.call("POST", "/workspaces", {"name": "session-admission"})["id"]

    def pid_alive(pid):
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)

    def new_case(name):
        return peer.call("POST", f"/workspaces/{ws_id}/cases", {"name": name})["id"]

    def root(cid):
        return api.root / f"files/workspaces/1/cases/{cid}"

    def put(cid, path, text):
        return peer.call("PUT", f"/cases/{cid}/file", {"path": path, "content": text})

    def arm(name, cid):
        for suffix in ("ready", "release"):
            (api.root / f"{name}.{suffix}").unlink(missing_ok=True)
        (api.root / f"{name}.armed").write_text(str(cid))

    def release(name):
        (api.root / f"{name}.armed").unlink(missing_ok=True)
        (api.root / f"{name}.release").touch()

    def wait(name, future=None):
        for _ in range(1800):
            if (api.root / f"{name}.ready").exists():
                return
            if future is not None and future.done():
                raise AssertionError((name, future.result()))
            time.sleep(0.01)
        raise AssertionError("Missing barrier "+name)

    def derive_fixture(label):
        cid = new_case(label)
        for relative in ("system/controlDict", "0/U", "constant/transportProperties", "processorSettings/config",
                         "0.1/U", "1e-3/U", "-0.1/U", "processor0/0/U", "run.log", "result.foam"):
            put(cid, "C-run/base/"+relative, "ORIGINAL_INPUT")
        return cid

    cid = derive_fixture("derive-coordinated")
    arm("derive-verify", cid)
    endpoint = f"/cases/{cid}/runs:derive"
    body = {"base": "C-run/base", "name": "derived"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        deriving = pool.submit(api.call, "POST", endpoint, body)
        try:
            wait("derive-verify", deriving)
            assert not (root(cid) / "C-run/derived").exists()
            editing = pool.submit(put, cid, "C-run/base/0/U", "NEW_INPUT")
            duplicate = pool.submit(peer.call, "POST", endpoint, body, expect=409)
            reading = pool.submit(peer.call, "GET", f"/cases/{cid}/file:version?path=C-run/derived/0/U")
            time.sleep(0.2)
            assert not any(f.done() for f in (editing, duplicate, reading))
        finally:
            release("derive-verify")
        result = deriving.result(timeout=20)
        assert result["files"] == 4, result
        editing.result(timeout=20)
        duplicate.result(timeout=20)
        assert reading.result(timeout=20)["exists"]
    files = {p.relative_to(root(cid) / "C-run/derived").as_posix() for p in (root(cid) / "C-run/derived").rglob("*") if p.is_file()}
    assert files == {"system/controlDict", "0/U", "constant/transportProperties", "processorSettings/config"}
    assert (root(cid) / "C-run/derived/0/U").read_text() == "ORIGINAL_INPUT"
    checks.append("derive_prepares_private_complete_inputs_then_publishes_and_serializes_edit_duplicate_and_reader")

    for mode in ("changed", "collision", "failure", "cancel"):
        cid = derive_fixture("derive-"+mode)
        arm("derive-verify", cid)
        if mode == "failure":
            arm("derive-fail", cid)
        if mode == "cancel":
            arm("derive-verify-cancel", cid)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(api.call, "POST", f"/cases/{cid}/runs:derive", body,
                                  expect=500 if mode in {"failure", "cancel"} else 409, raw=True)
            try:
                wait("derive-verify", pending)
                if mode == "changed":
                    (root(cid) / "C-run/base/0/U").write_text("BYPASS_CHANGE")
                if mode == "collision":
                    (root(cid) / "C-run/derived").mkdir()
                    (root(cid) / "C-run/derived/KEEP").write_text("external target")
                if mode == "cancel":
                    wait("derive-verify-cancel", pending)
                    assert not pending.done()
            finally:
                release("derive-verify")
                release("derive-fail")
                release("derive-verify-cancel")
            pending.result(timeout=20)
        assert not list((root(cid) / ".vibeflow/derive").iterdir())
        if mode == "collision":
            assert (root(cid) / "C-run/derived/KEEP").read_text() == "external target"
        else:
            assert not (root(cid) / "C-run/derived").exists()
        checks.append("derive_"+mode+"_cleans_owned_stage_without_overwriting_source_or_existing_target")

    def socket_url(server, cid, token=None, session=None):
        return server.base.replace("http", "ws", 1)+f"/ws/terminal?case={cid}&token={token or server.token}"+(f"&session={session}" if session else "")

    async def rejected(cid, token=None, session=None, server=None, *, before_accept=False):
        try:
            async with websockets.connect(socket_url(server or peer, cid, token, session)) as sock:
                assert not before_accept, 'Disabled account passed WebSocket authentication'
                frames = []
                try:
                    while True:
                        frame = await asyncio.wait_for(sock.recv(), 5)
                        if isinstance(frame, str):
                            frames.append(json.loads(frame))
                except websockets.ConnectionClosed:
                    assert sock.close_code == 4403, (sock.close_code, frames)
                assert not any(f.get("type") == "ready" for f in frames)
        except websockets.exceptions.InvalidStatus as error:
            assert before_accept and error.response.status_code == 403, error

    cid = new_case("foreign-terminal")
    other = peer.call("POST", "/auth/register", {"email": uuid.uuid4().hex+"@example.com", "name": "foreign", "password": "terminal-other-123"})
    before = peer.call("GET", "/terminal/sessions")
    asyncio.run(rejected(cid, token=other["token"]))
    asyncio.run(rejected(2147483647))
    assert peer.call("GET", "/terminal/sessions") == before
    assert not (api.root / f"terminal-created-{cid}.json").exists()
    checks.append("terminal_foreign_or_missing_case_never_falls_back_to_server_cwd_or_spawns_process")

    cid = new_case("stale-session-starts")
    arm("trash-before-index", cid)
    (api.root / "trash.armed").write_text(str(cid))
    arm("session-auth", cid)
    with ThreadPoolExecutor(max_workers=4) as pool:
        deleting = pool.submit(api.call, "DELETE", f"/cases/{cid}")
        try:
            wait("trash-before-index", deleting)
            starts = [pool.submit(peer.call, "POST", f"/cases/{cid}/runs:derive", body, expect=409),
                      pool.submit(peer.call, "POST", f"/cases/{cid}/viz:sessions", {}, expect=409),
                      pool.submit(asyncio.run, rejected(cid))]
            for _ in range(1000):
                if len(list(api.root.glob(f"session-auth-{cid}-*.ready"))) == 3:
                    break
                if any(f.done() for f in starts):
                    raise AssertionError([f.result() for f in starts if f.done()])
                time.sleep(0.01)
            assert len(list(api.root.glob(f"session-auth-{cid}-*.ready"))) == 3
        finally:
            (api.root / "trash.armed").unlink(missing_ok=True)
            release("session-auth")
            release("trash-before-index")
        deleting.result(timeout=20)
        for f in starts:
            f.result(timeout=20)
    assert not root(cid).exists()
    assert not (api.root / f"terminal-created-{cid}.json").exists()
    assert not (api.root / f"viz-created-{cid}.json").exists()
    checks.append("derive_viz_and_terminal_start_recheck_deletion_after_authorization_in_second_API")

    # Earlier coordination scenarios deliberately leave cases in trash. Use a
    # clean workspace here so its rejection can only be due to this terminal.
    terminal_workspace = api.call('POST', '/workspaces', {'name':'Terminal activity boundary'})['id']
    cid = api.call('POST', f'/workspaces/{terminal_workspace}/cases', {'name':'real-terminal'})['id']

    async def real_terminal():
        sid = None
        try:
            async with websockets.connect(socket_url(api, cid)) as sock:
                ready = json.loads(await sock.recv())
                assert ready["type"] == "ready" and ready["alive"], ready
                sid = ready["session"]
                await sock.send(json.dumps({"type": "input", "data": "echo BOUND_TERMINAL>Ref/terminal.txt\r"}))
                for _ in range(300):
                    output = root(cid) / "Ref/terminal.txt"
                    if output.exists() and "BOUND_TERMINAL" in output.read_text():
                        break
                    await asyncio.sleep(0.01)
                assert "BOUND_TERMINAL" in (root(cid) / "Ref/terminal.txt").read_text()
            for _ in range(300):
                path = api.root / f"terminal-unsubscribed-{sid}.json"
                try:
                    if json.loads(path.read_text())["subscribers"] == 0:
                        break
                except (FileNotFoundError, json.JSONDecodeError):
                    pass
                await asyncio.sleep(0.01)
            assert path.exists() and json.loads(path.read_text())["subscribers"] == 0
            # Both instances must reject through durable activity checks. An OS
            # directory-sharing error is not evidence that the API saw the session.
            for server in (api, peer):
                for method, endpoint, payload in (
                    ('POST', f'/cases/{cid}:copy', {'name':'Active terminal copy'}),
                    ('POST', f'/workspaces/{terminal_workspace}:copy', {'name':'Active terminal workspace copy'}),
                    ('DELETE', f'/cases/{cid}', None),
                    ('DELETE', f'/workspaces/{terminal_workspace}', None)):
                    denied=server.call(method, endpoint, payload, expect=409)
                    assert '终端' in denied['detail'], (method, endpoint, denied)
            async with websockets.connect(socket_url(api, cid, session=sid)) as sock:
                ready = json.loads(await sock.recv())
                assert ready["session"] == sid
                await sock.send(json.dumps({"type": "input", "data": "echo RECONNECTED>Ref/reconnected.txt\r"}))
                for _ in range(300):
                    output = root(cid) / "Ref/reconnected.txt"
                    if output.exists() and "RECONNECTED" in output.read_text():
                        break
                    await asyncio.sleep(0.01)
                assert "RECONNECTED" in (root(cid) / "Ref/reconnected.txt").read_text()
        finally:
            if sid:
                api.call("DELETE", f"/terminal/sessions/{sid}")
        pid = json.loads((api.root / f"terminal-created-{cid}.json").read_text())["pid"]
        assert not pid_alive(pid)
        api.call("DELETE", f"/cases/{cid}")

    asyncio.run(real_terminal())
    checks.append("real_PTY_uses_case_cwd_disconnect_unsubscribes_reconnect_works_and_close_waits_for_exit")

    async def revoked_terminal():
        account = api.call("POST", "/auth/register", {"email": uuid.uuid4().hex+"@example.com",
            "name": "revoked terminal", "password": "terminal-revoke-123"})
        headers = {"Authorization": "Bearer "+account["token"]}
        workspace = api.call("POST", "/workspaces", {"name": "revocation"}, extra_headers=headers)["id"]
        owned = api.call("POST", f"/workspaces/{workspace}/cases", {"name": "revoked"}, extra_headers=headers)["id"]
        other_case = api.call("POST", f"/workspaces/{workspace}/cases", {"name": "wrong-binding"}, extra_headers=headers)["id"]
        script = api.root / "set-terminal-user-status.py"
        script.write_text('''import asyncio, sys
from app.db import SessionLocal, engine
from app.models import User
async def main():
    async with SessionLocal() as db:
        user = await db.get(User, int(sys.argv[1]))
        user.status = sys.argv[2]
        await db.commit()
    await engine.dispose()
asyncio.run(main())
''', encoding="utf-8")

        def status(value):
            result = subprocess.run([sys.executable, str(script), str(account["user"]["id"]), value], cwd=SERVER,
                env=dict(api.env, PYTHONPATH=str(SERVER)+os.pathsep+api.env["PYTHONPATH"]), capture_output=True, timeout=20)
            assert result.returncode == 0, result.stderr.decode("utf-8", "replace")

        sid = None
        try:
            async with websockets.connect(socket_url(api, owned, account["token"])) as sock:
                ready = json.loads(await sock.recv())
                assert ready["type"] == "ready", ready
                sid = ready["session"]
                async with websockets.connect(socket_url(api, other_case, account["token"], sid)) as wrong:
                    assert json.loads(await wrong.recv())["code"] == "terminal_unavailable"
                status("disabled")
                await sock.send(json.dumps({"type": "input", "data": "echo FORBIDDEN>Ref/forbidden.txt\r"}))
                errors = []
                try:
                    while True:
                        frame = await asyncio.wait_for(sock.recv(), 10)
                        if isinstance(frame, str):
                            errors.append(json.loads(frame))
                except websockets.ConnectionClosed:
                    assert sock.close_code == 4403, (sock.close_code, errors)
                assert any(f.get("code") == "terminal_unavailable" for f in errors)
            await rejected(owned, token=account["token"], session=sid, server=api, before_accept=True)
            await rejected(owned, token=account["token"], server=api, before_accept=True)
        finally:
            status("active")
            current = api.call("GET", "/terminal/sessions", extra_headers=headers)["sessions"]
            if any(item["id"] == sid for item in current):
                api.call("DELETE", f"/terminal/sessions/{sid}", extra_headers=headers)
        source = api.root / f"files/workspaces/{account['user']['id']}/cases/{owned}"
        assert not (source / "Ref/forbidden.txt").exists()
        captured = json.loads((api.root / f"terminal-created-{owned}.json").read_text())
        assert not pid_alive(captured["pid"])
        checks.append("terminal_reconnect_rechecks_case_binding_and_disabled_account_cannot_send_input_or_keep_process")

    asyncio.run(revoked_terminal())

    cid = new_case("cancelled-PTY-start")
    arm("terminal-spawn", cid)
    arm("terminal-spawn-cancel", cid)

    async def cancelled_terminal():
        try:
            async with websockets.connect(socket_url(api, cid)) as sock:
                while True:
                    frame = await asyncio.wait_for(sock.recv(), 25)
                    if isinstance(frame, str):
                        assert json.loads(frame).get("type") != "ready"
        except websockets.ConnectionClosed:
            pass

    with ThreadPoolExecutor(max_workers=2) as pool:
        starting = pool.submit(asyncio.run, cancelled_terminal())
        try:
            wait("terminal-spawn", starting)
            wait("terminal-spawn-cancel", starting)
            deleting = pool.submit(peer.call, "DELETE", f"/cases/{cid}", expect=409)
            denied=deleting.result(timeout=3)
            assert '终端' in denied['detail'], denied
            assert not starting.done()
        finally:
            release("terminal-spawn")
            release("terminal-spawn-cancel")
        starting.result(timeout=20)
        peer.call("DELETE", f"/cases/{cid}")
    pid = json.loads((api.root / f"terminal-created-{cid}.json").read_text())["pid"]
    assert not pid_alive(pid) and not root(cid).exists()
    assert not any(item["id"] == json.loads((api.root / f"terminal-created-{cid}.json").read_text())["sid"]
                   for item in api.call("GET", "/terminal/sessions")["sessions"])
    checks.append("PTY_start_cancellation_drains_thread_and_reaps_root_while_durable_session_prevents_case_delete")

    for cancel in (False, True):
        cid = new_case("viz-start-"+str(cancel))
        put(cid, "Ref/triangle.stl", STL)
        arm("viz-start", cid)
        if cancel:
            arm("viz-start-cancel", cid)
        with ThreadPoolExecutor(max_workers=3) as pool:
            pending = pool.submit(api.call, "POST", f"/cases/{cid}/viz:sessions", {"file_path": "Ref/triangle.stl"},
                                  expect=500 if cancel else 200, raw=cancel)
            try:
                wait("viz-start", pending)
                assert api.call("GET", "/api/health", api_prefix=False)["ok"]
                if not cancel:
                    deleting = pool.submit(api.call, "DELETE", f"/cases/{cid}", expect=409)
                    listing = pool.submit(api.call, "GET", f"/cases/{cid}/viz:sessions")
                    deleting.result(timeout=3)
                    visible = listing.result(timeout=3)
                    assert any(item['phase'] == 'active' and item['alive'] for item in visible), visible
            finally:
                release("viz-start")
                release("viz-start-cancel")
            result = pending.result(timeout=20)
            if not cancel:
                deleting.result(timeout=20)
                assert result["alive"]
                assert any(item["id"] == result["id"] and item["alive"] for item in listing.result(timeout=20))
                api.call("DELETE", f"/viz:sessions/{result['id']}")
        captured = json.loads((api.root / f"viz-created-{cid}.json").read_text())
        assert not pid_alive(captured["pid"])
        assert api.call("GET", f"/cases/{cid}/viz:sessions") == []
        history = api.call("GET", f"/viz:sessions/{captured['sid']}")
        assert history['cleanup_verified'] is True and history['phase'] in {'closed','failed'}, history
        api.call("DELETE", f"/cases/{cid}")
        checks.append("real_viz_"+("cancelled_start_reaps_process_and_registry" if cancel else "registered_startup_keeps_listing_available_and_durable_session_rejects_case_delete"))
    return checks
