"""F41/F44：Codex 原生分支边界、文字交接与 clientUserMessageId 对账。"""
from app.services.codex_history import (build_text_handoff, native_fork_allowed,
                                        reconcile_native_history)

boundary = {"type": "message", "payload": {"role": "assistant", "native_turn_id": "turn-1",
                                           "native_turn_complete": True}}
assert native_fork_allowed(boundary, latest_native_turn=True, same_environment=True)
assert not native_fork_allowed(boundary, latest_native_turn=False, same_environment=True)
assert not native_fork_allowed(boundary, latest_native_turn=True, same_environment=False)
for invalid in (
    {"type": "message", "payload": {"role": "assistant", "native_turn_id": "turn-1"}},
    {"type": "message", "payload": {"role": "assistant", "native_turn_id": "turn-1",
                                    "native_turn_complete": True, "tool_calls": [{}]}},
    {"type": "message", "payload": {"role": "assistant", "native_turn_id": "turn-1",
                                    "native_turn_complete": True, "cancelled": True}},
):
    assert not native_fork_allowed(invalid, latest_native_turn=True, same_environment=True)
print("1. 只有同环境完整原生回合最后回答允许 native fork [OK]")

local = [
    {"id": 1, "type": "message", "payload": {"role": "user", "content": "first", "client_user_message_id": "c1"}},
    {"id": 2, "type": "message", "payload": {"role": "assistant", "content": "answer1", "native_turn_id": "turn-1"}},
    {"id": 3, "type": "message", "payload": {"role": "user", "content": "second", "client_user_message_id": "c2"}},
    {"id": 4, "type": "message", "payload": {"role": "user", "content": "unconfirmed", "client_user_message_id": "c3"}},
]
native = [
    {"type": "userMessage", "clientId": "c1"},
    {"type": "assistantMessage", "clientUserMessageId": "c1", "turnId": "turn-1", "text": "answer1"},
    {"type": "userMessage", "clientId": "c2"},
    {"type": "assistantMessage", "clientUserMessageId": "c2", "turnId": "turn-2", "text": "missing answer"},
    {"type": "assistantMessage", "clientUserMessageId": "other", "turnId": "turn-x", "text": "orphan"},
]
result = reconcile_native_history(local, native)
assert result["unconfirmed_client_ids"] == ["c3"] and result["auto_resend"] is False, result
assert result["missing_answers"] == [{"native_turn_id": "turn-2", "client_user_message_id": "c2",
                                      "content": "missing answer", "insert_after_event_id": 3}], result
assert len(result["orphan_answers"]) == 1, result
print("2. resume 对账未确认消息不自动重发，缺失回答定位且不重插/不串会话 [OK]")

short = build_text_handoff(local[:3])
assert short.index("first") < short.index("answer1") < short.index("second")
long_events = local + [
    {"id": 5, "type": "tool_result", "payload": {"result": "DO_NOT_HANDOFF"}},
    {"id": 6, "type": "message", "payload": {"role": "assistant", "content": "CANCELLED", "cancelled": True}},
    {"id": 7, "type": "message", "payload": {"role": "assistant", "content": "TOOL", "tool_calls": [{}]}},
    {"id": 8, "type": "message", "payload": {"role": "assistant", "content": "Z" * 2000}},
]
bounded = build_text_handoff(long_events, keep_chars=1000)
assert len(bounded) <= 1000 and bounded.startswith("[Earlier history truncated")
assert bounded.endswith("Z" * 900) and "DO_NOT_HANDOFF" not in bounded and "CANCELLED" not in bounded
try:
    build_text_handoff([], 999)
    raise AssertionError("过小交接窗口应拒绝")
except ValueError:
    pass
print("3. 文字交接保留最近 180K 上限内文本，不复制工具/取消/审批执行 [OK]")
summary = build_text_handoff([
    {'type':'system','payload':{'kind':'summary','text':'SUMMARY_CONTEXT'}},
    {'type':'system','payload':{'kind':'handoff','text':'HANDOFF_CONTEXT'}},
    {'type':'message','payload':{'role':'assistant','content':'INTERRUPTED','interrupted':True}},
])
assert 'SUMMARY_CONTEXT' in summary and 'HANDOFF_CONTEXT' in summary and 'INTERRUPTED' not in summary
print('4. 压缩/交接摘要保留，中断回答排除 [OK]')
images = build_text_handoff([{'type':'message','payload':{'role':'user','content':'','images':[
    {'name':'image.png','path':'.vibeflow/attachments/id/image.png','data':'DO_NOT_EMBED_IMAGE','sha256':'0'*64}]}}])
assert '.vibeflow/attachments/id/image.png' in images and 'visual content is not embedded' in images
assert 'DO_NOT_EMBED_IMAGE' not in images
print('5. 图片空文字消息保留文件位置，不把内联图片或原生事件塞入文字交接 [OK]')
print("\n=== Codex 原生分支/文字交接/恢复对账冒烟通过 ===")
