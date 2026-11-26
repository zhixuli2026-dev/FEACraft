"""SQLAlchemy async 引擎与会话"""
import asyncio

from sqlalchemy import event, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Session

from .config import get_settings
from .services.async_ops import finish_before_cancel


class Base(DeclarativeBase):
    pass


_AFTER_TRANSACTION = 'after_root_transaction'
_TRANSACTION_TASKS = 'root_transaction_cleanup_tasks'


def defer_after_root_transaction(db, cleanup):
    """Register async cleanup for the current root, including rollback and close."""
    transaction = db.sync_session.get_transaction()
    if transaction is None:
        raise ValueError('No root database transaction for deferred cleanup')
    db.info.setdefault(_AFTER_TRANSACTION, {}).setdefault(transaction, []).append(cleanup)


@event.listens_for(Session, 'after_transaction_end')
def _start_transaction_cleanup(session, transaction):
    if transaction.parent is not None:
        return
    callbacks = session.info.get(_AFTER_TRANSACTION, {}).pop(transaction, ())
    if not callbacks:
        return
    tasks = session.info.setdefault(_TRANSACTION_TASKS, set())
    # This synchronous hook runs after the transaction releases its SQL
    # connection. The tasks themselves run on the surrounding event loop.
    for cleanup in callbacks:
        task = asyncio.create_task(cleanup())
        tasks.add(task)
        task.add_done_callback(tasks.discard)


class ApplicationSession(AsyncSession):
    async def _drain_transaction_cleanup(self):
        tasks = tuple(self.info.get(_TRANSACTION_TASKS, ()))
        if tasks:
            await finish_before_cancel(asyncio.gather(*tasks))

    async def commit(self):
        try:
            await super().commit()
        finally:
            await self._drain_transaction_cleanup()

    async def rollback(self):
        try:
            await super().rollback()
        finally:
            await self._drain_transaction_cleanup()

    async def close(self):
        try:
            await super().close()
        finally:
            await self._drain_transaction_cleanup()


settings = get_settings()
engine = create_async_engine(settings.database_url, echo=False, future=True,
                             connect_args={"timeout": 30} if settings.database_url.startswith("sqlite") else {})

if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_concurrency(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

SessionLocal = async_sessionmaker(engine, class_=ApplicationSession, expire_on_commit=False)

# Short conversation-control transactions need a connection even when case I/O
# is waiting with every business-pool connection checked out.
conversation_control_engine = (
    create_async_engine(engine.url, pool_size=2, max_overflow=0)
    if engine.dialect.name == "postgresql" else engine
)
ConversationControlSession = async_sessionmaker(
    conversation_control_engine, class_=ApplicationSession, expire_on_commit=False,
)


async def close_conversation_control_db():
    if conversation_control_engine is not engine:
        await conversation_control_engine.dispose()


async def get_db():
    async with SessionLocal() as session:
        yield session


async def init_db():
    """Only bootstrap empty development stores; never upgrade an existing schema."""
    from . import models  # noqa: F401  确保模型已注册
    async with engine.begin() as conn:
        await initialize_schema(conn)


async def initialize_schema(conn):
    """Protect existing stores from create_all silently bypassing migrations."""
    from . import models  # noqa: F401
    existing = set(await conn.run_sync(lambda sync: inspect(sync).get_table_names()))
    expected = set(Base.metadata.tables)
    if existing & expected:
        missing = expected - existing
        if missing:
            raise RuntimeError("Database requires a reviewed Alembic upgrade; missing tables: "
                               + ", ".join(sorted(missing)))
        if conn.dialect.name == "postgresql" and "alembic_version" not in existing:
            raise RuntimeError("Database requires a reviewed Alembic upgrade; existing PostgreSQL schema "
                               "has no Alembic revision. Verify and stamp the matching revision before upgrading")
        if "alembic_version" in existing:
            from pathlib import Path
            from alembic.config import Config
            from alembic.script import ScriptDirectory
            server = Path(__file__).resolve().parents[1]
            cfg = Config(str(server / "alembic.ini"))
            cfg.set_main_option("script_location", str(server / "migrations"))
            head = ScriptDirectory.from_config(cfg).get_current_head()
            versions = list((await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars())
            if versions != [head]:
                raise RuntimeError("Database revision differs from code; run a reviewed Alembic upgrade")
        return
    if conn.dialect.name != "sqlite":
        raise RuntimeError("Empty database requires an explicit Alembic upgrade; automatic bootstrap is SQLite-development only")
    await conn.run_sync(Base.metadata.create_all)
