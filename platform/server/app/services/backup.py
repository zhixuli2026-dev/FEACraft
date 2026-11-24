"""workspace 级真实备份/恢复（不含密钥、活动任务或数据库升级）。"""
from __future__ import annotations

import base64
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
from typing import BinaryIO
import hashlib
import io
import json
import time
import uuid
import zipfile

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import storage
from ..models import AgentEvent, Case, CaseEventCounter, Conversation, Job, MaterialLibraryRevision, ToolCall, User, Workspace
from .case_admin import assert_no_case_activity
from .case_write import CaseWriteConflict, coordinate
from .workspace_write import coordinate_workspaces
from .case_source import SourceChanged
from .async_ops import finish_before_cancel
from .case_provision import io as drained_io
from .request_deadline import current as current_deadline
from .owner_storage_budget import hold as owner_capacity
from .backup_manifest import (SCHEMA, MAX_BACKUP_BYTES, MAX_SOURCE_BYTES, MAX_MANIFEST_BYTES,
    MAX_FILES, MAX_RECORDS, BackupError, BackupTooLarge, validate_structure, validate_case_dicts)

EXCLUDED = (".vibeflow/uploads/", ".vibeflow/attempts/", '.vibeflow/attempt-provisions/', '.vibeflow/publications/', '.vibeflow/file-transactions/', '.vibeflow/live-store/')


def _iso(value):
    return value.isoformat() if value else None


def _dt(value):
    return datetime.fromisoformat(value) if value else None


@asynccontextmanager
async def _backup_cases(db, workspace, workspace_id, owner_id):
    from .trash import is_case_deleted

    # Recovery inside the coordinators can write; obtain capacity first.
    async with owner_capacity(db, owner_id):
        async with AsyncExitStack() as locks:
            await locks.enter_async_context(coordinate_workspaces(db, [workspace_id]))
            await db.refresh(workspace)
            if workspace.user_id != owner_id:
                raise BackupError("源工作区权限已变化，请刷新后重试")
            cases = (await db.scalars(select(Case).where(Case.workspace_id == workspace_id)
                                      .order_by(Case.id).execution_options(populate_existing=True))).all()
            cases = [case for case in cases if not is_case_deleted(case.id)]
            for case in cases:
                await locks.enter_async_context(coordinate(db, case, content_write=False))
                if case.workspace_id != workspace_id or is_case_deleted(case.id):
                    raise BackupError("源工作区算例归属已变化，请刷新后重试")
            await assert_no_case_activity(db, [case.id for case in cases])
            yield cases
        await finish_before_cancel(db.commit())


async def _case_sequences(db, cases):
    if not cases:
        return []
    rows = await db.execute(select(CaseEventCounter.case_id, CaseEventCounter.next_seq)
                            .where(CaseEventCounter.case_id.in_([case.id for case in cases]))
                            .order_by(CaseEventCounter.case_id))
    return rows.all()


async def create_backup(db: AsyncSession, workspace: Workspace, output=None):
    workspace_id, owner_id = workspace.id, workspace.user_id
    created_at = datetime.now().isoformat()
    try:
        async with _backup_cases(db, workspace, workspace_id, owner_id) as cases:
            references = [(case.id, case.s3_prefix) for case in cases]
            sequences = await _case_sequences(db, cases)
            manifest = await _capture_metadata(db, workspace, cases, created_at)

        # These operations only read the source and write the node-admitted
        # response spool. Neither business SQL nor an owner write grant is held.
        manifests, _ = await drained_io(_source_manifests, references)
        data = await drained_io(_pack_backup, dict(manifest), references, manifests, output)
        observed, versions = await drained_io(_source_manifests, references)
        if observed != manifests:
            raise SourceChanged("备份期间源文件清单已变化，请重试")

        async with _backup_cases(db, workspace, workspace_id, owner_id) as cases:
            if ([(case.id, case.s3_prefix) for case in cases] != references
                    or await _case_sequences(db, cases) != sequences
                    or await _capture_metadata(db, workspace, cases, created_at) != manifest):
                raise SourceChanged("备份期间源工作区已变化，请重试")
            if await drained_io(_source_versions, references) != versions:
                raise SourceChanged("备份期间源文件清单已变化，请重试")
        return data
    except BaseException as error:
        await finish_before_cancel(db.rollback())
        if isinstance(error, SourceChanged):
            raise BackupError(str(error)) from error
        raise


def _source_entries(references):
    """Check the whole workspace size before hashing any file contents."""
    entries = {}
    count = total = 0
    for case_id, prefix in references:
        selected = [row for row in storage.list_tree(prefix) if not row['path'].startswith(EXCLUDED)]
        count += len(selected)
        total += sum(row['size'] for row in selected)
        if count > MAX_FILES or total > MAX_SOURCE_BYTES:
            raise BackupError("备份超过10000文件或100MiB源文件上限")
        entries[case_id] = selected
    return entries


def _selected_file(prefix, row):
    from .file_read import FileRead, FileReadConflict

    deadline = current_deadline()
    if deadline is not None:
        deadline.check()
    try:
        source = FileRead(prefix, row['path'])
    except (FileReadConflict, FileNotFoundError, NotADirectoryError, ValueError) as error:
        raise SourceChanged("备份期间源文件已变化，请重试") from error
    if source.size != row['size']:
        raise SourceChanged("备份期间源文件大小已变化，请重试")
    if source.remote and row.get('etag') and source.etag.strip('"') != row['etag']:
        raise SourceChanged("备份期间源文件版本已变化，请重试")
    return source


def _source_versions(references):
    entries = _source_entries(references)
    return {case_id: {row['path']: _selected_file(prefix, row).version for row in entries[case_id]}
            for case_id, prefix in references}


def _source_manifests(references):
    from .file_transactions import _capture
    from .s3_streams import snapshot_from_head

    entries = _source_entries(references)
    manifests, versions = {}, {}
    for case_id, prefix in references:
        records, selected = {}, {}
        for row in entries[case_id]:
            path = row['path']
            source = _selected_file(prefix, row)
            try:
                if source.remote:
                    record, _, _ = snapshot_from_head(prefix, path,
                                                     {'ContentLength': source.size, 'ETag': source.etag})
                else:
                    record = _capture(source.path, limit=source.size, deadline=current_deadline())
                    if _selected_file(prefix, row).version != source.version:
                        raise SourceChanged("备份期间源文件版本已变化，请重试")
            except CaseWriteConflict as error:
                if error.code == 'input_stale':
                    raise SourceChanged("备份期间源文件已变化，请重试") from error
                raise
            except (FileNotFoundError, NotADirectoryError, ValueError) as error:
                raise SourceChanged("备份期间源文件大小或内容已变化，请重试") from error
            if record is None or record['size'] != source.size:
                raise SourceChanged("备份期间源文件已变化，请重试")
            records[path] = (record['size'], record['sha256'])
            selected[path] = source.version
        manifests[case_id], versions[case_id] = records, selected
    return manifests, versions


async def _capture_metadata(db, workspace, cases, created_at):
    if cases:
        job_query = select(Job).where(Job.case_id.in_([case.id for case in cases])).order_by(Job.id)
        jobs = (await db.scalars(job_query.limit(MAX_RECORDS+1))).all()
        conversation_query = select(Conversation).where(
            Conversation.case_id.in_([case.id for case in cases])).order_by(Conversation.id)
        conversations = (await db.scalars(conversation_query.limit(MAX_RECORDS+1))).all()
    else:
        jobs = []
        conversations = []
    conversation_ids = [row.id for row in conversations]
    if conversation_ids:
        event_query = select(AgentEvent).where(AgentEvent.conversation_id.in_(conversation_ids)).order_by(AgentEvent.id)
        events = (await db.scalars(event_query.limit(MAX_RECORDS+1))).all()
        tool_query = select(ToolCall).where(ToolCall.conversation_id.in_(conversation_ids)).order_by(ToolCall.id)
        tools = (await db.scalars(tool_query.limit(MAX_RECORDS+1))).all()
    else:
        events = []
        tools = []
    manifest = {
        "schema": SCHEMA,
        "created_at": created_at,
        "workspace": {"name": workspace.name, "created_at": _iso(workspace.created_at)},
        "cases": [{"id": row.id, "name": row.name, "workflow_state": row.workflow_state,
                   "dict_revision": row.dict_revision, "case_meta": row.case_meta,
                   "parse_status": row.parse_status, "created_at": _iso(row.created_at),
                   "updated_at": _iso(row.updated_at)} for row in cases],
        "jobs": [{"case_id": row.case_id, "type": row.type, "command": row.command,
                  "status": row.status, "exit_code": row.exit_code,
                  "log_s3_key": row.log_s3_key, "parsed_summary": row.parsed_summary,
                  "created_at": _iso(row.created_at), "ended_at": _iso(row.ended_at)} for row in jobs],
        "conversations": [{"id": row.id, "case_id": row.case_id, "title": row.title,
                           "forked_from_id": row.forked_from_id,
                           "branch_head_event_id": row.branch_head_event_id,
                           "model": row.model, "permission_mode": row.permission_mode,
                           "created_at": _iso(row.created_at)} for row in conversations],
        "events": [{"id": row.id, "conversation_id": row.conversation_id, "seq": row.seq,
                    "event_id": row.event_id, "type": row.type, "payload": row.payload,
                    "created_at": _iso(row.created_at)} for row in events],
        "tool_calls": [{"conversation_id": row.conversation_id, "tool": row.tool,
                        "args": row.args, "status": row.status, "result": row.result,
                        "created_at": _iso(row.created_at)} for row in tools],
        "files": [],
    }
    from .material_library import serialize
    query = (select(MaterialLibraryRevision).where(MaterialLibraryRevision.workspace_id == workspace.id)
             .order_by(MaterialLibraryRevision.key, MaterialLibraryRevision.revision).limit(MAX_RECORDS+1)
             .execution_options(yield_per=100))
    rows = await db.stream_scalars(query)
    materials, total = [], 0
    try:
        async for row in rows:
            record = serialize(row, full=True)
            total += len(json.dumps(record, ensure_ascii=False).encode('utf8'))
            if total > MAX_MANIFEST_BYTES or len(materials) >= MAX_RECORDS:
                raise BackupTooLarge('材料库历史超过备份清单上限')
            materials.append(record)
    finally:
        await rows.close()
    if materials:
        manifest['material_revisions'] = materials
    return manifest


class _BoundedOutput(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_BACKUP_BYTES:
            raise BackupError("备份超过 100 MiB")
        return super().write(data)


def _pack_backup(manifest, references, manifests, output=None):
    """Stream original v1 Base64 fields into ZIP without retaining file payloads."""
    from .file_read import FileRead, FileReadConflict
    prefixes = dict(references)
    manifest['files'] = [
        {'case_id': case_id, 'path': path, 'sha256': expected[1]}
        for case_id, _ in references for path, expected in sorted(manifests[case_id].items())]
    cases, paths = validate_structure(manifest)

    def read_file(row, sink=None):
        prefix, path = prefixes[row['case_id']], row['path']
        expected = manifests[row['case_id']][path]
        deadline = current_deadline()
        if deadline is not None:
            deadline.check()
        try:
            if storage._use_s3():
                from .s3_streams import snapshot
                output = io.BytesIO() if sink is None else sink
                observed, _, _ = snapshot(prefix, path, output=output, limit=expected[0], rewind=False)
                if observed != {'size': expected[0], 'sha256': expected[1]}:
                    raise SourceChanged("备份期间源文件已变化，请重试")
                return output.getvalue() if sink is None else None
            source = FileRead(prefix, path)
            if source.size != expected[0]:
                raise SourceChanged("备份期间源文件大小已变化，请重试")
            digest = hashlib.sha256()
            output = io.BytesIO() if sink is None else sink
            # A pinned inclusive read verifies file identity on every block.
            if not source.size:
                source.read()
            for start in range(0, source.size, 1024 * 1024):
                if deadline is not None:
                    deadline.check()
                block = source.read(start, min(source.size, start + 1024 * 1024) - 1)
                digest.update(block)
                output.write(block)
            if digest.hexdigest() != expected[1]:
                raise SourceChanged("备份期间源文件内容已变化，请重试")
            return output.getvalue() if sink is None else None
        except CaseWriteConflict as error:
            if error.code == 'input_stale':
                raise SourceChanged("备份期间源文件大小或内容已变化，请重试") from error
            raise
        except BackupError:
            # The ZIP sink may refuse further output while a source is being read.
            raise
        except (FileReadConflict, FileNotFoundError, ValueError) as error:
            raise SourceChanged("备份期间源文件大小或内容已变化，请重试") from error

    # Semantic caseDict parsing still needs the canonical text. Other file
    # payloads are never collected into the manifest or decoded a second time.
    validate_case_dicts(cases, paths, read_file)
    total_source = sum(size for rows in manifests.values() for size, _ in rows.values())
    if total_source > MAX_SOURCE_BYTES:
        raise BackupError("备份文件总量超过100MiB")
    return_bytes = output is None
    if return_bytes:
        output = _BoundedOutput()
    encoder = json.JSONEncoder(ensure_ascii=False)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        with archive.open("manifest.json", "w") as destination:
            total = 0

            def write(data):
                nonlocal total
                total += len(data)
                if total > MAX_MANIFEST_BYTES:
                    raise BackupError("备份manifest超过160MiB")
                destination.write(data)

            def encode(value):
                for chunk in encoder.iterencode(value):
                    write(chunk.encode('utf8'))

            write(b'{')
            first = True
            for key, value in manifest.items():
                if key == 'files':
                    continue
                if not first:
                    write(b',')
                first = False
                encode(key); write(b':'); encode(value)
            if not first:
                write(b',')
            write(b'"files":[')
            for index, row in enumerate(manifest['files']):
                if index:
                    write(b',')
                write(b'{"case_id":'); encode(row['case_id'])
                write(b',"path":'); encode(row['path'])
                write(b',"sha256":'); encode(row['sha256'])
                write(b',"data":"')
                sink = _Base64Sink(write)
                read_file(row, sink)
                sink.finish()
                write(b'"}')
            write(b']}')
    if return_bytes:
        return output.getvalue()
    length = output.tell()
    output.seek(0)
    return length


class _Base64Sink:
    """Retain at most two input bytes across arbitrary storage read boundaries."""
    def __init__(self, write):
        self._write = write
        self._pending = b''

    def write(self, data):
        block = self._pending + data
        end = len(block) - len(block) % 3
        if end:
            self._write(base64.b64encode(block[:end]))
        self._pending = block[end:]
        return len(data)

    def flush(self):
        pass

    def finish(self):
        if self._pending:
            self._write(base64.b64encode(self._pending))
            self._pending = b''


async def restore_backup(db: AsyncSession, user: User, data: bytes | BinaryIO) -> dict:
    from . import backup_restore as restore, case_copy_local, case_provision
    from .quota import check_storage_quota
    from .storage_admission import lock_owner

    user_id = user.id
    async with AsyncExitStack() as resources, AsyncExitStack() as decoded:
        staged = restore.RestoreFiles()
        decoded.push_async_callback(drained_io, staged.close)
        manifest = await drained_io(staged.load, data)
        payloads = await drained_io(restore.case_payloads, manifest, staged)
        original = manifest.get("workspace", {})
        suffix = f"-restored-{time.time_ns()}-{uuid.uuid4().hex[:8]}"
        name = original["name"][:200-len(suffix)]+suffix
        try:
            # Release the authentication read transaction before materializing
            # anything. The outer archive admission still owns the I/O budget.
            await finish_before_cancel(db.commit())
            prepared = {}
            remote = storage._use_s3()
            if not remote and payloads:
                location = await drained_io(case_copy_local.staging_location, user_id, '.backup-restore-staging')
                prepared = await resources.enter_async_context(restore.stage_local(user_id, location, payloads))
                await decoded.aclose()
            await resources.enter_async_context(owner_capacity(db, user_id))
            await case_provision.recover_owner(db, user_id)
            incoming = sum(row["_size"] for row in manifest["files"])
            incoming -= sum(item.accounted_bytes(user_id) for item in prepared.values())
            await check_storage_quota(db, user, incoming)
            if remote:
                await finish_before_cancel(db.commit())
                prepared = await drained_io(restore.stage_remote, user_id, payloads)
                await decoded.aclose()
                await lock_owner(db, user_id, wait=True)

            if await db.scalar(select(Workspace.id).where(Workspace.user_id == user_id, Workspace.name == name)):
                raise BackupError("恢复目标工作区已存在")
            workspace = Workspace(user_id=user_id, name=name,
                                  created_at=_dt(original.get("created_at")))
            db.add(workspace)
            await db.flush()
            from .material_library import encode
            for record in manifest.get('material_revisions', []):
                db.add(MaterialLibraryRevision(workspace_id=workspace.id, key=record['key'], revision=record['revision'],
                    created_at=_dt(record['created_at']), material_json=None if record['archived'] else encode(record['key'], record['material'])))
            case_map = {}
            for record in manifest.get("cases", []):
                case = Case(workspace_id=workspace.id, name=record["name"],
                            s3_prefix="", workflow_state=record["workflow_state"],
                            dict_revision=record["dict_revision"], case_meta=record["case_meta"],
                            parse_status=record["parse_status"],
                            created_at=_dt(record.get("created_at")),
                            updated_at=_dt(record.get("updated_at")))
                db.add(case)
                await db.flush()
                case_map[record["id"]] = case
            job_logs = {case_id: {} for case_id in case_map}
            for record in manifest.get("jobs", []):
                case = case_map[record["case_id"]]
                job = Job(case_id=case.id, type=record["type"], command=record["command"],
                          status=record["status"], exit_code=record.get("exit_code"),
                          log_s3_key=record["log_s3_key"], parsed_summary=record["parsed_summary"],
                          created_at=_dt(record.get("created_at")), ended_at=_dt(record.get("ended_at")))
                db.add(job)
                await db.flush()
                job.log_s3_key = f".vibeflow/jobs/job-{job.id}.log"
                if record["log_s3_key"]:
                    job_logs[record["case_id"]][record["log_s3_key"]] = job.log_s3_key
            conversation_map = {}
            for record in manifest.get("conversations", []):
                conversation = Conversation(case_id=case_map[record["case_id"]].id,
                    title=record["title"], forked_from_id=None, branch_head_event_id=None,
                    provider_profile_id=None, model=record["model"],
                    permission_mode=record["permission_mode"], created_at=_dt(record.get("created_at")))
                db.add(conversation)
                await db.flush()
                conversation_map[record["id"]] = conversation
            for record in manifest.get("tool_calls", []):
                db.add(ToolCall(id=f"r-{uuid.uuid4().hex}",
                                conversation_id=conversation_map[record["conversation_id"]].id,
                                tool=record["tool"], args=record["args"], status=record["status"],
                                result=record["result"], created_at=_dt(record.get("created_at"))))
            event_map = {}
            for record in manifest.get("events", []):
                event = AgentEvent(conversation_id=conversation_map[record["conversation_id"]].id,
                    seq=record["seq"], event_id=uuid.uuid4().hex, type=record["type"],
                    payload=record["payload"], created_at=_dt(record.get("created_at")))
                db.add(event)
                await db.flush()
                event_map[record["id"]] = event
            for record in manifest.get("conversations", []):
                conversation = conversation_map[record["id"]]
                if record.get("forked_from_id") in conversation_map:
                    conversation.forked_from_id = conversation_map[record["forked_from_id"]].id
                if record.get("branch_head_event_id") in event_map:
                    conversation.branch_head_event_id = event_map[record["branch_head_event_id"]].id
            destinations = {case_id: payload.destinations(job_logs[case_id]) for case_id, payload in payloads.items()}
            for old_case_id, case in case_map.items():
                files, moves = destinations[old_case_id]
                await restore.publish_case(db, case, user_id, files, moves, prepared.get(old_case_id))
            await finish_before_cancel(db.commit())
            return {"workspace_id": workspace.id, "cases": len(case_map),
                    "jobs": len(manifest.get("jobs", [])), "conversations": len(conversation_map),
                    "events": len(event_map), "files": len(manifest["files"])}
        except BaseException as error:
            await finish_before_cancel(db.rollback())
            if isinstance(error, SourceChanged):
                raise CaseWriteConflict('backup_restore_stale', str(error)) from error
            raise
