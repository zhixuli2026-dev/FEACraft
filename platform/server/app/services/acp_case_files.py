"""Explicitly selected case inputs and approved draft publication for Docker ACP."""
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import PurePosixPath
from uuid import UUID

from fastapi import HTTPException

from .agent_file_reads import _bytes
from .attempt_workspace import canonical_path
from .case_write import CaseWriteConflict
from .cli_private_paths import root as private_root
from .file_read import FileRead
from .file_transactions import _path
from .fs_directory import remove_owned


MAX_FILES = 12
MAX_BYTES = 256 * 1024
MOUNT = '/workspace/case'
PROTECTED_RECORDS = {'0-casedict/case-events.jsonl', '0-casedict/provenance.jsonl'}


def selection(paths):
    from ..storage_paths import is_file_stage

    names = set()
    aliases = set()
    for raw in paths:
        name = canonical_path(raw)
        parts = PurePosixPath(name).parts
        if len(name) > 384 or len(parts) > 20 or any(c in name for c in '<>"|?*\r\n') or any(
                part.casefold() == '.vibeflow' or is_file_stage(part) for part in parts):
            raise ValueError('请选择算例内的普通文本文件')
        if name.casefold() in aliases:
            raise ValueError('算例文件选择含重复路径')
        names.add(name)
        aliases.add(name.casefold())
    if any(str(parent).casefold() in aliases for name in names for parent in PurePosixPath(name).parents):
        raise ValueError('文件选择含重叠路径')
    if len(names) > MAX_FILES:
        raise ValueError('本回合选择的算例文件过多')
    return sorted(names)


def capture(prefix, names):
    selected = {}
    remaining = MAX_BYTES
    for name in names:
        try:
            file = FileRead(prefix, name)
            if file.size > remaining:
                raise HTTPException(413, '所选算例文本合计不能超过 256 KiB')
            data = b'' if file.remote and file.size == 0 else _bytes(file)
            data.decode('utf8')
        except FileNotFoundError as error:
            raise HTTPException(404, '所选算例文件不存在') from error
        except (UnicodeError, OSError, ValueError) as error:
            raise HTTPException(422, '所选算例文件须为 UTF-8 普通文件') from error
        if b'\0' in data:
            raise HTTPException(422, '所选算例文件须为 UTF-8 普通文件')
        remaining -= len(data)
        selected[name] = data
    return selected


def directory(settings, user_id, thread_id, turn_id):
    root = private_root(settings)
    parent = root/'users'/str(int(user_id))/'docker'/'acp'/str(UUID(thread_id))
    target = parent/'case-inputs'/str(UUID(turn_id))
    if target.resolve() != target:
        raise ValueError('ACP input directory was redirected')
    return target


def stage(target, files):
    target.mkdir(parents=True, exist_ok=False)
    target.chmod(0o755)
    for name, data in files.items():
        path = _path(target, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        parent = path.parent
        while parent != target:
            parent.chmod(0o755)
            parent = parent.parent
        with path.open('xb') as handle:
            handle.write(data)
        path.chmod(0o644)


def clean(target):
    remove_owned(target, root=target.parent)


def receipt(files):
    return [{'path':name, 'size':len(data), 'sha256':sha256(data).hexdigest()} for name, data in files.items()]


class Published(Exception):
    def __init__(self, result):
        self.result = result


async def publish(db, identifier, draft_path, target, user):
    from ..routers.agent import _append_event
    from ..routers.files import _case_for_file_request, _locked_recovery_case, _ordinary_write_path
    from . import acp_approval, acp_execution, audit, ordinary_file_edits

    turn, thread = await acp_execution.lookup(db, identifier, user)
    if not thread.host_id.startswith('docker:') or turn.phase != 'completed' or not acp_execution.verified_cleanup(turn):
        raise HTTPException(409, '只能发布已完成且已核验退出的 Docker 草稿')
    try:
        target, = selection([target])
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    cid, user_id = turn.conversation_id, user.id
    source = await acp_approval.item(db, cid, identifier, draft_path, 'draft', key='path')
    if source is None:
        raise HTTPException(404, '已批准的草稿不存在')
    inputs = await acp_approval.item(db, cid, identifier, None, 'case_inputs')
    baseline = next((file['sha256'] for file in inputs['files'] if file['path'] == target), None) if inputs else None
    if baseline is None:
        raise HTTPException(422, '发布目标须是本回合已选择的算例文件')
    data = source['text'].encode('utf8')
    result = {'type':'draft_published', 'turn_id':identifier, 'draft':draft_path, 'path':target,
              'content_sha256':sha256(data).hexdigest(), 'baseline_sha256':baseline}
    case = await acp_execution.owned_case(db, cid, user)
    case = await _case_for_file_request(case.id, user, db)
    case_id, prefix = case.id, case.s3_prefix
    if target.casefold() in PROTECTED_RECORDS:
        raise HTTPException(422, '算例历史记录不能由草稿替换')
    _ordinary_write_path(prefix, target)

    async def ensure_unpublished(context):
        prior = await acp_approval.item(context, cid, identifier, target, 'draft_published', key='path')
        if prior is not None:
            if prior != result:
                raise HTTPException(409, '该目标已由本回合的其他草稿发布')
            raise Published(prior)

    async def checked(context):
        current = await _locked_recovery_case(context, case_id=case_id, user_id=user_id, write=True, prefix=prefix)
        await ensure_unpublished(context)
        return current

    @asynccontextmanager
    async def publish_receipt(context, current):
        await ensure_unpublished(context)
        yield
        await _append_event(context, cid, 'acp', result)
        await audit.case_event(context, current, user_id, 'acp.draft.published', 'acp_turn', identifier,
                               {key:result[key] for key in ('draft','path','content_sha256','baseline_sha256')})

    try:
        await ordinary_file_edits.save(db, case, checked, target, lambda _prefix: data,
            revision=None, conditions={target:baseline}, content_type='text/plain; charset=utf-8',
            publication=publish_receipt)
    except Published as existing:
        return existing.result
    except CaseWriteConflict as error:
        raise HTTPException(409, error.detail()) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return result
