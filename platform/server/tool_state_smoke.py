"""F07：孤儿工具调用启动恢复；仅使用本次新建的临时 SQLite。"""
import asyncio
import os
from pathlib import Path
import shutil
import tempfile

ROOT = Path(tempfile.mkdtemp(prefix="vibeflow-tool-state-"))
os.environ["VIBEFLOW_DATABASE_URL"] = f"sqlite+aiosqlite:///{(ROOT / 'state.db').as_posix()}"
os.environ["VIBEFLOW_STORAGE_BACKEND"] = "fs"
os.environ["VIBEFLOW_STORAGE_FS_ROOT"] = str(ROOT / "files")

from app.config import get_settings
from app.db import SessionLocal, engine, init_db
from app.main import app, startup
from app.models import ToolCall
from sqlalchemy import select


async def main():
    assert get_settings().database_url.startswith("sqlite+aiosqlite:///"), get_settings().database_url
    task = None
    try:
        await init_db()
        async with SessionLocal() as db:
            for status in ("queued", "running", "pending", "executed"):
                db.add(ToolCall(id=f"1:{status}", conversation_id=1, tool="list_files",
                                args="{}", status=status))
            await db.commit()
        await startup()
        task = app.state.viz_reaper_task
        async with SessionLocal() as db:
            rows = (await db.scalars(select(ToolCall))).all()
            states = {row.id: row.status for row in rows}
        assert states["1:queued"] == "interrupted", states
        assert states["1:running"] == "interrupted", states
        assert states["1:pending"] == "pending", states
        assert states["1:executed"] == "executed", states
        print("1. queued/running 孤儿工具调用标记 interrupted，pending/executed 不变 [OK]")
        print("\n=== 工具状态启动恢复冒烟通过（临时 SQLite）===")
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await engine.dispose()


try:
    asyncio.run(main())
finally:
    shutil.rmtree(ROOT, ignore_errors=True)
