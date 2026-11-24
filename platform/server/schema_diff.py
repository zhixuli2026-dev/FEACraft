"""只读输出当前数据库与 SQLAlchemy 模型的结构差异；不执行迁移。"""
import asyncio
import json
import os
from pathlib import Path


def _load_database_url():
    """从脚本所在目录加载配置，避免绝对路径调用时误用 cwd 下的默认 SQLite。"""
    if "VIBEFLOW_DATABASE_URL" in os.environ:
        return
    env_file = Path(__file__).resolve().with_name(".env")
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("VIBEFLOW_DATABASE_URL="):
            os.environ["VIBEFLOW_DATABASE_URL"] = line.split("=", 1)[1].strip().strip('"').strip("'")
            return


_load_database_url()

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.engine import make_url

from app import models  # noqa: F401
from app.config import get_settings
from app.db import Base, engine


def describe(operation):
    kind = operation[0]
    if kind in ("add_table", "remove_table"):
        return {"op": kind, "table": operation[1].name}
    if kind in ("add_column", "remove_column"):
        column = operation[3]
        return {"op": kind, "table": operation[2], "column": column.name,
                "type": str(column.type)}
    if kind in ("add_index", "remove_index"):
        index = operation[1]
        return {"op": kind, "table": index.table.name, "index": index.name,
                "columns": [c.name for c in index.columns], "unique": index.unique}
    if kind in ("add_constraint", "remove_constraint"):
        constraint = operation[1]
        return {"op": kind, "table": getattr(constraint, "table", None) and constraint.table.name,
                "constraint": constraint.name, "detail": str(constraint)}
    return {"op": kind, "detail": [str(item) for item in operation[1:]]}


async def main():
    url = make_url(get_settings().database_url)
    print(f"DATABASE_DRIVER={url.drivername}; HOST={url.host}; DATABASE={url.database}")
    async with engine.connect() as connection:
        differences = await connection.run_sync(_compare)
    await engine.dispose()
    print(json.dumps([describe(op) for op in differences], ensure_ascii=False, indent=2))
    print(f"SCHEMA_DIFF_COUNT={len(differences)}")


def _compare(connection):
    context = MigrationContext.configure(connection, opts={"compare_type": True})
    return compare_metadata(context, Base.metadata)


if __name__ == "__main__":
    asyncio.run(main())
