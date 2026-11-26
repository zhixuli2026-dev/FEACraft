"""Owner storage credit for Agent file mutations, excluding native job launches."""
import asyncio
from contextlib import asynccontextmanager
from threading import Event
from .async_ops import finish_before_cancel
from .owner_storage_budget import hold_case

FILE_TOOLS = frozenset({'propose_file_update', 'propose_text_replacement', 'update_memory'})


async def read_case(case_id, prefix, user_id, function, *args, cancel=None, wait_for_writer=False,
                    code='agent_file_read', label='Agent 文件读取', file_path=None):
    """Read a bounded file under the case lock, or verify a tree-wide snapshot."""
    from .. import storage
    from ..db import SessionLocal
    from ..models import User
    from ..routers.files import _case_for_file_request, _read_file_tree
    from ..routers.workspaces import get_owned_case
    from .agent_file_reads import stop_signal
    from .case_read_capacity import file_read_deadline, reading
    from .case_write import CaseWriteConflict, bounded_case_read
    from .file_read import FileRead

    stop = Event()

    def execute(selected, *arguments):
        token = stop_signal.set(stop)
        try:
            if selected != prefix:
                raise ValueError('Case storage location changed')
            return function(*arguments)
        finally:
            stop_signal.reset(token)

    def execute_file(selected):
        try:
            reader = FileRead(selected, file_path)
        except FileNotFoundError:
            reader = None
        return execute(selected, reader, *args)

    async def read():
        async with file_read_deadline(code, label), SessionLocal() as db:
            while True:
                try:
                    user = await db.get(User, user_id)
                    if user is None or user.status != 'active':
                        raise ValueError('文件读取用户已不可用')
                    selected = await _case_for_file_request(case_id, user, db, write=False)
                    if selected.s3_prefix != prefix:
                        raise ValueError('Case storage location changed')
                    if file_path is None:
                        result, _ = await _read_file_tree(db, selected, user, execute, *args, code=code, label=label)
                        return result
                    async with reading(db, selected, defer_journal=True):
                        actor = await db.get(User, user_id, populate_existing=True)
                        if actor is None or actor.status != 'active':
                            raise ValueError('文件读取用户已不可用')
                        checked = await get_owned_case(case_id, actor, db, write=False)
                        if checked.s3_prefix != prefix:
                            raise ValueError('Case storage location changed')
                        result = await bounded_case_read(execute_file, prefix, code=code, label=label,
                                                         wait_for_capacity=not storage._use_s3())
                    await finish_before_cancel(db.rollback())
                    return result
                except CaseWriteConflict as error:
                    if not wait_for_writer or error.code != 'file_sync_busy':
                        raise
                    await finish_before_cancel(db.rollback())
                    await asyncio.sleep(.2)

    cancelled = {'ok':False,'status':'cancelled','result':'文件读取已取消'}
    if cancel is not None and cancel.is_set():
        return cancelled
    operation = asyncio.create_task(read())
    signal = asyncio.create_task(cancel.wait()) if cancel is not None else None
    try:
        if signal is None:
            return await asyncio.shield(operation)
        done, _ = await asyncio.wait((operation, signal), return_when=asyncio.FIRST_COMPLETED)
        return cancelled if signal in done else operation.result()
    finally:
        stop.set()
        if not operation.done():
            operation.cancel()
        if signal is not None:
            signal.cancel()
        await finish_before_cancel(asyncio.gather(operation, *([signal] if signal is not None else []),
                                                  return_exceptions=True))


@asynccontextmanager
async def file_tool(db, case, name):
    # Call before changing tool state; retain credit through the caller's SQL
    # commit and committed file-journal cleanup, then release before inference.
    if name in FILE_TOOLS:
        async with hold_case(db, case):
            yield
    else:
        yield
