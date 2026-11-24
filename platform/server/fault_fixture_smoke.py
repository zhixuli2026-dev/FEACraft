"""Verify test-only barriers preserve real operations and bound failure waits."""
import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace

from fault_fixture import FileBarrier, intercept


async def main():
    with tempfile.TemporaryDirectory(prefix="vf-fault-fixture-") as temp:
        root = Path(temp)
        worker = root / "worker.py"
        worker.write_text("from pathlib import Path\nimport sys\nfrom fault_fixture import FileBarrier\n"
            "root=Path(sys.argv[1])\nFileBarrier(root,'spawn').wait()\n"
            "(root/'output').write_text('real subprocess output')\n", encoding="utf-8")
        import os
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parent))
        proc = subprocess.Popen([sys.executable, str(worker), str(root)], env=env)
        try:
            barrier = FileBarrier(root, "spawn")
            for _ in range(300):
                if barrier.ready.exists():
                    break
                await asyncio.sleep(0.01)
            assert barrier.ready.exists() and proc.poll() is None
            assert not (root / "output").exists()
            barrier.release()
            assert await asyncio.to_thread(proc.wait, 10) == 0
            assert (root / "output").read_text() == "real subprocess output"
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
        calls = []
        async def operation():
            calls.append("committed")
            return 42
        target = SimpleNamespace(commit=operation)
        for name, after in (("before_commit", False), ("after_commit", True)):
            gate = FileBarrier(root, name)
            with intercept(target, "commit", gate, after=after):
                calls.clear()
                task = asyncio.create_task(target.commit())
                for _ in range(100):
                    if gate.ready.exists():
                        break
                    await asyncio.sleep(0.01)
                assert gate.ready.exists() and not task.done()
                assert calls == (["committed"] if after else [])
                gate.release()
                assert await task == 42 and calls == ["committed"]
            assert target.commit is operation
        # Native file publication and event delivery can be paused independently.
        # Synchronous boundaries run in a test-owned thread while the controller
        # releases their barrier; a separate test process can do the same by file.
        import os
        from app.services import eventbus
        for name, target, method, args, observe in (
            ("publish", os, "replace", (root / "private", root / "public"),
             lambda: (root / "public").exists()),
            ("event", eventbus, "publish", (-999, {"type": "test.fixture"}), lambda: False)):
            (root / "private").write_text("real file publication")
            gate = FileBarrier(root, name)
            with intercept(target, method, gate, asynchronous=False):
                task = asyncio.create_task(asyncio.to_thread(getattr(target, method), *args))
                for _ in range(100):
                    if gate.ready.exists():
                        break
                    await asyncio.sleep(0.01)
                assert gate.ready.exists() and not task.done() and not observe()
                gate.release()
                await task
            if name == "publish":
                assert (root / "public").read_text() == "real file publication"
        try:
            FileBarrier(root, "timeout", timeout=0.02).wait()
            raise AssertionError("Missing hard timeout")
        except TimeoutError:
            pass
    print("FAULT_FIXTURE_OK real_spawn before_commit after_commit native_publish event timeout restoration")


if __name__ == "__main__":
    asyncio.run(main())
