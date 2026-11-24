"""为历史分页 E2E 在指定新会话中写入确定性用户事件。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from app.db import SessionLocal, engine
from app.routers.agent import _append_event


async def main():
    cid = int(sys.argv[1])
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 300
    try:
        async with SessionLocal() as db:
            for index in range(count):
                await _append_event(db, cid, "message", {"role": "user", "content": f"m{index}"})
            await db.commit()
    finally:
        await engine.dispose()


asyncio.run(main())
