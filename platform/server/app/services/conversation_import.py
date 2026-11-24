"""Agent JSONL 导入解析；只产生历史事件，不恢复执行权限或活动任务。"""
from __future__ import annotations

import json
import time

MAX_IMPORT_BYTES = 16 * 1024 * 1024
MAX_IMPORT_EVENTS = 6000
MAX_IMPORT_LINE_BYTES = 2 * 1024 * 1024
CURRENT_SESSION_SCHEMA = "vibeflow.agent.session.v1"
CURRENT_EVENT_SCHEMA = "vibeflow.agent.event.v1"
HISTORY_EVENT_TYPES = {
    "message", "system", "tool", "tool_state", "tool_result", "request", "request_state",
    "run_state", "run_checkpoint", "goal",
}


class ConversationImportError(ValueError):
    pass


def _reject_json_constant(value: str):
    raise ConversationImportError(f"JSON 不允许非有限数值 {value}")


def _object(value, description: str) -> dict:
    if not isinstance(value, dict):
        raise ConversationImportError(f"{description} 必须是对象")
    return value


def _normalize_event(line: dict) -> dict:
    schema = line.get("schema")
    if schema is not None and schema != CURRENT_EVENT_SCHEMA:
        raise ConversationImportError(f"不支持的事件 schema: {schema}")
    event_type = line.get("type")
    if event_type not in HISTORY_EVENT_TYPES:
        raise ConversationImportError(f"不支持的历史事件类型: {event_type}")
    payload = dict(_object(line.get("payload", {}), "事件 payload"))
    if event_type == "message":
        if payload.get("role") not in {"user", "assistant"}:
            raise ConversationImportError("message 仅支持 user/assistant 历史")
        if not isinstance(payload.get("content", ""), str):
            raise ConversationImportError("message.content 必须是字符串")
    if event_type == "tool":
        # 旧版单条 tool 记录投影为只读工具结果，不创建 ToolCall 审批记录。
        event_type = "tool_result"
        payload = {
            "tool_call_id": str(payload.get("tool_call_id") or payload.get("id") or "legacy-tool"),
            "tool": payload.get("tool") or payload.get("name") or "legacy_tool",
            "args": payload.get("args") if isinstance(payload.get("args"), dict) else {},
            "result": str(payload.get("result", "")),
            "ok": bool(payload.get("ok", True)), "status": "completed",
        }
    if event_type == "run_state" and not isinstance(payload.get("run_id"), str):
        raise ConversationImportError("run_state 缺少 run_id")
    if event_type == "run_checkpoint" and not isinstance(payload.get("run_id"), str):
        raise ConversationImportError("run_checkpoint 缺少 run_id")
    if event_type == "request_state" and (
            not isinstance(payload.get("run_id"), str) or
            not isinstance(payload.get("request_id"), str)):
        raise ConversationImportError("request_state 缺少 run_id/request_id")
    if event_type == "request_state":
        payload.pop("reported_usage", None)  # Imported text cannot attest a provider report.
        payload.pop("input_selection", None)
    if event_type == "goal" and payload.get("status") == "active":
        payload["imported_status"] = "active"
        payload["status"] = "cancelled"
        payload["stop_reason"] = "导入历史不会自动恢复 Goal 执行"
    if event_type in {"tool_state", "tool_result"} and payload.get("status") in {
            "pending", "queued", "running", "awaiting_approval"}:
        payload["imported_status"] = payload["status"]
        payload["status"] = "interrupted"
    return {"type": event_type, "payload": payload,
            "source_id": line.get("id") if isinstance(line.get("id"), int) and
            not isinstance(line.get("id"), bool) else None}


def parse_conversation_jsonl(data: bytes) -> tuple[dict, list[dict]]:
    if len(data) > MAX_IMPORT_BYTES:
        raise ConversationImportError("导入文件超过 16 MiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConversationImportError("JSONL 必须使用 UTF-8") from error
    metadata: dict = {}
    session_seen = False
    events: list[dict] = []
    for line_number, raw in enumerate(text.split('\n'), 1):
        if not raw.strip():
            continue
        if len(raw.encode("utf-8")) > MAX_IMPORT_LINE_BYTES:
            raise ConversationImportError(f"第 {line_number} 行超过 2 MiB")
        try:
            line = _object(json.loads(raw, parse_constant=_reject_json_constant), f"第 {line_number} 行")
        except json.JSONDecodeError as error:
            raise ConversationImportError(f"第 {line_number} 行不是有效 JSON") from error
        if not events and not session_seen and line.get("schema") == CURRENT_SESSION_SCHEMA:
            session = _object(line.get("session", {}), "session")
            session_seen = True
            metadata = {
                "title": str(session.get("title", ""))[:200],
                "model": str(session.get("model", ""))[:200],
            }
            # tool_calls/session 中的工作目录、profile、权限和密钥均不导入。
            continue
        if len(events) >= MAX_IMPORT_EVENTS:
            raise ConversationImportError("导入事件超过 6000 条")
        events.append(_normalize_event(line))
    if not events and not session_seen:
        raise ConversationImportError("导入文件没有历史事件")

    # 活动状态仅作为历史保留；文件末尾仍未结束的 run 追加 interrupted。
    latest: dict[str, str] = {}
    started_at: dict[str, float] = {}
    latest_requests: dict[tuple[str, str], dict] = {}
    for event in events:
        payload = event["payload"]
        if event["type"] == "run_state":
            run_id = payload["run_id"]
            latest[run_id] = str(payload.get("status", ""))
            started = payload.get("started_at")
            if isinstance(started, (int, float)) and not isinstance(started, bool):
                started_at[run_id] = started
        elif event["type"] == "request_state":
            latest_requests[payload["run_id"], payload["request_id"]] = payload
    ended_at = time.time()
    for run_id, status in latest.items():
        if status in {"started", "awaiting_approval", "queued", "running"}:
            interrupted = {"run_id": run_id, "status": "interrupted",
                           "imported_status": status}
            started = started_at.get(run_id)
            if started is not None:
                interrupted.update({"started_at": started, "ended_at": ended_at,
                                    "duration_ms": max(0, round((ended_at - started) * 1000))})
            events.append({"type": "run_state", "payload": interrupted})
    for (run_id, request_id), payload in latest_requests.items():
        if payload.get("status") == "started":
            interrupted = {"run_id": run_id, "request_id": request_id,
                           "request_index": payload.get("request_index"),
                           "status": "interrupted", "imported_status": "started"}
            started = payload.get("started_at")
            if isinstance(started, (int, float)) and not isinstance(started, bool):
                interrupted.update({"started_at": started, "ended_at": ended_at,
                                    "duration_ms": max(0, round((ended_at - started) * 1000))})
            events.append({"type": "request_state", "payload": interrupted})
    if len(events) > MAX_IMPORT_EVENTS:
        raise ConversationImportError("恢复后的导入事件超过 6000 条")
    return metadata, events
