"""Explicit engineering gate and journaled dictionary transitions."""
from contextlib import asynccontextmanager, nullcontext
import hashlib
import json

from sqlalchemy import select

from .. import storage
from ..models import Job
from .casedict import (CASE_DICT_PATH, WorkflowGateError, WorkflowTransition,
                       get_workflow_state, parse_casedict, set_workflow_state, validate_mesh_transition, write_case_dict)
from .case_write import CaseWriteConflict, bounded_case_read, coordinate
from .file_read import FileRead, FileReadConflict


class StageAdvanceError(ValueError):
    def __init__(self, status_code, detail):
        self.status_code, self.detail = status_code, detail
        super().__init__(detail)


def _read_dictionary(prefix):
    return FileRead(prefix, CASE_DICT_PATH).read()


def _legacy_postprocess_job_id(prefix):
    pointer = json.loads(FileRead(prefix, "D-post/data/latest-postprocess.json").read())
    job_id = pointer.get("job_id") if isinstance(pointer, dict) else None
    return int(job_id) if type(job_id) in (int, str) else None


async def evidence_reason(db, case, target, *, original_dictionary=None, freshness=None):
    if target == "B":
        from .geometry_publication import gate_reason
        return await gate_reason(db, case, original_dictionary=original_dictionary)
    if target == "C":
        from .mesh_publication import gate_reason
        return await gate_reason(db, case)
    if target == "D":
        from .solve_publication import gate_reason
        reason, handled = await gate_reason(db, case, freshness=freshness)
        if handled:
            return reason
        from .case_evidence import solve_gate_reason
        parsed = parse_casedict(storage.get_bytes(case.s3_prefix, CASE_DICT_PATH).decode("utf-8"))
        run_dir = (parsed.get("postProcessing") or {}).get("runDir")
        solved = (await db.scalars(select(Job.id).where(Job.case_id == case.id,
            Job.type == "solve", Job.status == "succeeded").order_by(Job.id.desc()).limit(20))).all()
        if not solved or not any(solve_gate_reason(case.s3_prefix, job_id, run_dir) is None for job_id in solved):
            return "没有与当前输入和求解场一致的成功求解证据，请指定 run_dir 重新求解"
    if target == "E":
        from .postprocess_publication import gate_reason
        reason, handled = await gate_reason(db, case, freshness=freshness)
        if handled:
            return reason
        from .postprocess_gate import report_gate_reason
        try:
            job_id = await bounded_case_read(_legacy_postprocess_job_id, case.s3_prefix,
                                            code='postprocess_evidence', label='后处理证据读取')
        except (OSError, ValueError, FileReadConflict):
            job_id = None
        max_job_id = 2**31 - 1 if db.get_bind().dialect.name == 'postgresql' else 2**63 - 1
        evidence = await db.get(Job, job_id) if job_id is not None and 0 < job_id <= max_job_id else None
        if (evidence is None or evidence.case_id != case.id or evidence.type != "postprocess" or
                evidence.status != "succeeded"):
            return "没有当前算例的成功后处理证据，不能进入报告阶段"
        return report_gate_reason(case)
    return None


def _target(target, confirmed):
    target = target.upper()
    if target not in ("A", "B", "C", "D", "E", "V"):
        raise StageAdvanceError(422, "非法目标阶段")
    if not confirmed:
        raise StageAdvanceError(409, "阶段推进必须由用户显式确认")
    return target


def _check_state(text, current, target, revision):
    if get_workflow_state(text) != current:
        raise CaseWriteConflict('case_dict_state_conflict', 'caseDict 文件阶段与已提交记录不一致，请核对源文件后重试',
                                path=CASE_DICT_PATH, current_revision=revision)
    order = {"A":0, "B":1, "C":2, "D":3, "E":4}
    if target != current and target not in ("A", "V") and (current not in order or order[target] != order[current]+1):
        raise StageAdvanceError(409, "阶段不可跳级；V 开发态和 A 回退除外")


async def advance_staged(db, case, checked, *, target, confirmed, expected_revision, confirmation=None,
                         expected_sha256=None, defer_journal=False, on_commit=None, actor_id=None):
    from . import case_dict_edits

    target = _target(target, confirmed)
    transition = WorkflowTransition(case.workflow_state, target, expected_revision, confirmed)

    def edit(text, _prefix):
        _check_state(text, transition.previous_state, target, expected_revision)
        return (text if target == transition.previous_state else set_workflow_state(text, target)), {}

    @asynccontextmanager
    async def publication(session, current, draft):
        # S3 applies the conditional journal before this commit. Legacy geometry
        # evidence was generated against the original dictionary's raw digest.
        async with confirmation(session, current, draft) if confirmation is not None else nullcontext():
            if draft.prepared is not None and confirmation is None:
                reason = await evidence_reason(session, current, target, original_dictionary=draft.original_text)
                if reason:
                    raise StageAdvanceError(409, reason)
            if draft.prepared is not None:
                validate_mesh_transition(current, draft.prepared.parsed, target)
            yield

    try:
        receipt = await case_dict_edits.save(db, case, checked, edit, revision=expected_revision,
            expected_sha256=expected_sha256, event_source='workflow-advance', allow_noop=True,
            transition=transition, publication=publication, defer_journal=defer_journal, on_commit=on_commit,
            actor_id=actor_id)
    except (FileNotFoundError, FileReadConflict) as error:
        conflict = CaseWriteConflict('file_conflict', 'caseDict 不存在或读取期间已变化，请核对源文件后重试',
                                     path=CASE_DICT_PATH, current_revision=expected_revision)
        raise StageAdvanceError(409, conflict.detail()) from error
    except CaseWriteConflict as error:
        raise StageAdvanceError(409, error.detail()) from error
    except WorkflowGateError as error:
        raise StageAdvanceError(409, str(error)) from error
    except ValueError as error:
        if isinstance(error, StageAdvanceError):
            raise
        raise StageAdvanceError(422, str(error)) from error
    return {key:receipt[key] for key in ('ok','workflow_state','dict_revision')}


async def advance(db, case, *, target, confirmed, expected_revision, actor_id=None):
    try:
        async with coordinate(db, case, expected_revision=expected_revision):
            return await _advance_locked(db, case, target=target, confirmed=confirmed,
                                         expected_revision=expected_revision, actor_id=actor_id)
    except CaseWriteConflict as error:
        raise StageAdvanceError(409, error.detail()) from error
    except ValueError as error:
        if isinstance(error, StageAdvanceError):
            raise
        raise StageAdvanceError(422, str(error)) from error


async def _advance_locked(db, case, *, target, confirmed, expected_revision, actor_id=None):
    target = _target(target, confirmed)
    if expected_revision != case.dict_revision:
        raise StageAdvanceError(409, "caseDict 已被其他操作修改，请刷新后重试")
    current = case.workflow_state
    try:
        raw = await bounded_case_read(_read_dictionary, case.s3_prefix, code='editor_read', label='caseDict 读取')
    except (FileNotFoundError, FileReadConflict) as error:
        raise CaseWriteConflict('file_conflict', 'caseDict 不存在或读取期间已变化，请核对源文件后重试',
                                path=CASE_DICT_PATH, current_revision=case.dict_revision) from error
    text = raw.decode('utf-8')
    _check_state(text, current, target, case.dict_revision)
    if target == current:
        return {"ok":True, "workflow_state":current, "dict_revision":case.dict_revision}
    reason = await evidence_reason(db, case, target)
    if reason:
        raise StageAdvanceError(409, reason)
    try:
        await write_case_dict(db, case, set_workflow_state(text, target), source="workflow-advance",
            transition=WorkflowTransition(current, target, expected_revision, confirmed),
            expected_files={CASE_DICT_PATH: hashlib.sha256(raw).hexdigest()}, actor_id=actor_id)
    except WorkflowGateError as error:
        raise StageAdvanceError(409, str(error)) from error
    return {"ok":True, "workflow_state":case.workflow_state, "dict_revision":case.dict_revision}
