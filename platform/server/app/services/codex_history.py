"""Codex 原生分支边界、文字交接与 resume 历史对账。"""
from __future__ import annotations
import json

MAX_HANDOFF_CHARS = 180_000


def native_fork_allowed(boundary: dict, *, latest_native_turn: bool,
                        same_environment: bool) -> bool:
    """仅同环境、完整原生回合的最后回答可继承原生工具上下文。"""
    if boundary.get("type") != "message":
        return False
    payload = boundary.get("payload", {})
    return (latest_native_turn and same_environment and
            payload.get("role") == "assistant" and
            not payload.get("cancelled") and not payload.get("tool_calls") and
            payload.get("native_turn_complete") is True and
            bool(payload.get("native_turn_id")))


def build_text_handoff(events: list[dict], keep_chars: int = MAX_HANDOFF_CHARS, *,
                       include_images: bool = True) -> str:
    """保留最近 user/assistant 完成文字，工具执行和审批不随交接重放。"""
    if keep_chars < 1000 or keep_chars > MAX_HANDOFF_CHARS:
        raise ValueError("keep_chars 必须在 1000–180000 之间")
    blocks = []
    for event in events:
        if event.get('type') == 'system' and event.get('payload', {}).get('kind') in {'summary', 'handoff'}:
            text = event['payload'].get('text')
            if isinstance(text, str) and text.strip():
                blocks.append(text if event['payload'].get('handoff_mode') == 'bounded_text'
                              else 'Conversation summary:\n' + text)
            continue
        if event.get("type") != "message":
            continue
        payload = event.get("payload", {})
        role = payload.get("role")
        content = payload.get("content")
        if include_images and role == 'user' and isinstance(content, str) and isinstance(payload.get('images'), list):
            references = [{key: image[key] for key in ('name', 'path', 'sha256') if key in image}
                for image in payload['images'][:3] if isinstance(image, dict) and isinstance(image.get('path'), str)
                and image['path'].startswith('.vibeflow/attachments/')]
            if references:
                content += '\nImage file references in the case (visual content is not embedded): ' + json.dumps(references, ensure_ascii=False)
        if (role not in {"user", "assistant"} or not isinstance(content, str) or not content.strip()
                or any(payload.get(key) for key in ('cancelled', 'interrupted', 'failed', 'tool_calls'))
                or role == 'assistant' and payload.get('status') not in (None, 'completed')):
            continue
        blocks.append(f"{'User' if role == 'user' else 'Assistant'}:\n{content}")
    text = "\n\n".join(blocks)
    if len(text) <= keep_chars:
        return text
    marker = "[Earlier history truncated; only recent text is handed off.]\n"
    return marker + text[-(keep_chars - len(marker)):]


def reconcile_native_history(local_events: list[dict], native_entries: list[dict]) -> dict:
    """对账 clientUserMessageId；只返回差异，不自动重发未确认用户请求。"""
    confirmed = {entry.get("clientId") for entry in native_entries
                 if entry.get("type") == "userMessage" and entry.get("clientId")}
    local_users = {event["payload"].get("client_user_message_id"): event
                   for event in local_events
                   if event.get("type") == "message" and
                   event.get("payload", {}).get("role") == "user" and
                   event.get("payload", {}).get("client_user_message_id")}
    existing_turns = {event["payload"].get("native_turn_id") for event in local_events
                      if event.get("type") == "message" and
                      event.get("payload", {}).get("native_turn_id")}
    unconfirmed = [client_id for client_id in local_users if client_id not in confirmed]
    missing = []
    orphans = []
    for entry in native_entries:
        if entry.get("type") != "assistantMessage":
            continue
        turn_id = entry.get("turnId")
        client_id = entry.get("clientUserMessageId")
        if not turn_id or turn_id in existing_turns:
            continue
        user = local_users.get(client_id)
        answer = {"native_turn_id": turn_id, "client_user_message_id": client_id,
                  "content": str(entry.get("text", ""))}
        if user is None:
            orphans.append(answer)
        else:
            missing.append({**answer, "insert_after_event_id": user["id"]})
    return {"confirmed_client_ids": sorted(confirmed),
            "unconfirmed_client_ids": sorted(unconfirmed),
            "missing_answers": missing, "orphan_answers": orphans,
            "auto_resend": False}
