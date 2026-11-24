"""LLM 网关：provider 注册表 + 双协议 SSE 中继 + 工具调用

事件模型（继承桌面版语义，重写实现）：
  text.delta / reasoning.delta / tool.delta / approval.requested(由 agent runner 发) /
  message.completed / turn.completed(带 tool_calls/finish_reason) / stream.done / stream.error / request.*
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet

from .sanitizer import ProtocolSanitizer
from .redaction import ERROR_PREVIEW_BYTES, redact_text
from . import llm_usage, provider_kms, provider_vault

MAX_MODEL_STREAM_BYTES = 64 * 1024 * 1024
MAX_TEXT_REASONING_BYTES = 8 * 1024 * 1024
MAX_SSE_EVENT_BYTES = 8 * 1024 * 1024
MAX_TOOL_CALL_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BODY_BYTES = 10 * 1024 * 1024

_VAULT_KEY_PATH = Path(__file__).parent.parent / ".vault_key"


def _vault() -> Fernet:
    from ..config import get_settings
    configured = get_settings().vault_key_path
    if configured:
        # Explicit deployment keys are provisioned once and mounted read-only.
        # Never replace a missing/corrupt key with one that loses old credentials.
        try:
            return provider_vault.read(Path(configured).expanduser())
        except (OSError, ValueError):
            raise RuntimeError(
                "Configured provider vault is missing, unreadable or invalid; "
                "restore the original key file before starting the service"
            ) from None
    return provider_vault.local(_VAULT_KEY_PATH)


def validate_vault_configuration():
    from ..config import get_settings
    if get_settings().vault_key_path:
        _vault()


def encrypt_key(plain: str) -> str:
    return _vault().encrypt(plain.encode()).decode()


def decrypt_key(cipher: str) -> str:
    return _vault().decrypt(cipher.encode()).decode()


async def unseal_profile_key(cipher: str, identifier: int, org_id: int | None, provider: str, base_url: str) -> str:
    if org_id is None:
        return decrypt_key(cipher)
    return await provider_kms.unseal(org_id, identifier, provider, base_url, cipher)


# 已知 provider 注册表
# api: openai = /chat/completions 协议；anthropic = /messages 协议
PROVIDERS: dict[str, dict] = {
    "openai":    {"api": "openai", "base_url": "https://api.openai.com/v1", "chat_path": "/chat/completions"},
    "openrouter": {"api": "openai", "base_url": "https://openrouter.ai/api/v1", "chat_path": "/chat/completions"},
    "deepseek":  {"api": "openai", "base_url": "https://api.deepseek.com/v1", "chat_path": "/chat/completions"},
    "moonshot":  {"api": "openai", "base_url": "https://api.moonshot.cn/v1", "chat_path": "/chat/completions"},
    "zhipu":     {"api": "openai", "base_url": "https://open.bigmodel.cn/api/paas/v4", "chat_path": "/chat/completions"},
    "dashscope": {"api": "openai", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "chat_path": "/chat/completions"},
    "ollama":    {"api": "openai", "base_url": "http://localhost:11434/v1", "chat_path": "/chat/completions"},
    "anthropic": {"api": "anthropic", "base_url": "https://api.anthropic.com", "chat_path": "/v1/messages"},
    "kimi-coding": {"api": "anthropic", "base_url": "https://api.kimi.com/coding", "chat_path": "/v1/messages"},
    "custom":    {"api": "openai", "base_url": "", "chat_path": "/chat/completions"},
}


def provider_api(provider: str) -> str:
    return PROVIDERS.get(provider, PROVIDERS["custom"])["api"]


def resolve_endpoint(provider: str, base_url: str | None) -> str:
    reg = PROVIDERS.get(provider, PROVIDERS["custom"])
    base = (base_url or reg["base_url"]).rstrip("/")
    path = reg["chat_path"]
    if path.startswith("/v1"):
        return base + path
    if base.endswith("/v1"):
        return base + path
    return base + "/v1" + path


def use_env_proxy(endpoint: str) -> bool:
    """回环 mock/Ollama 直连；公网 provider 继续支持系统代理。"""
    return urlsplit(endpoint).hostname not in {"localhost", "127.0.0.1", "::1"}


def _identity_encoding(response):
    if response.headers.get('content-encoding', 'identity').lower() != 'identity':
        raise ValueError('上游响应使用了不受支持的压缩编码')


class _BoundedSse(httpx.AsyncByteStream):
    """Enforce wire and line limits before the HTTP client's line decoder buffers data."""

    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        total = line = 0
        async for chunk in self.stream:
            total += len(chunk)
            if total > MAX_MODEL_STREAM_BYTES:
                raise ValueError('模型流超过允许的字节上限')
            parts = chunk.replace(b'\r', b'\n').split(b'\n')
            for index, part in enumerate(parts):
                line = (line if index == 0 else 0) + len(part)
                if line > MAX_SSE_EVENT_BYTES:
                    raise ValueError('单个模型流事件超过允许的字节上限')
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


async def read_response(response, limit, *, preview=False):
    _identity_encoding(response)
    body = bytearray()
    async for chunk in response.aiter_raw(65536):
        if len(body) + len(chunk) > limit:
            if not preview:
                raise ValueError('上游响应正文超过允许的字节上限')
            body.extend(chunk[:limit - len(body)])
            break
        body.extend(chunk)
    return body


# ======================= OpenAI 协议 =======================
async def stream_chat_openai(
    *,
    endpoint: str,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    max_tokens: int | None = None,
    reasoning_effort: str = "off",
) -> AsyncIterator[dict]:
    """OpenAI 兼容端点流式中继。tool_calls 跨 delta 分片累积，turn.completed 收尾。"""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Accept-Encoding": "identity",
        "HTTP-Referer": "https://vibeflow.local",
        "X-Title": "VibeFlow Platform",
    }
    payload: dict = {"model": model, "messages": messages, "stream": True}
    if max_tokens is not None:
        payload['max_tokens'] = max_tokens
    if urlsplit(endpoint).hostname == 'api.openai.com':
        payload['stream_options'] = {'include_usage': True}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if reasoning_effort != "off":
        payload["reasoning_effort"] = reasoning_effort

    # tool_calls 累积器：index -> {id, name, arguments}
    tc_acc: dict[int, dict] = {}
    text_filter = ProtocolSanitizer()
    reasoning_filter = ProtocolSanitizer()
    text_reasoning_bytes = 0
    finish_reason = None
    received_done = False
    usage = None
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=20.0),
                                     trust_env=use_env_proxy(endpoint)) as client:
            async with client.stream("POST", endpoint, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    raw = await read_response(resp, ERROR_PREVIEW_BYTES, preview=True)
                    detail = redact_text(raw.decode('utf-8', 'replace'), (api_key,), limit=ERROR_PREVIEW_BYTES)
                    yield {"type": "request.failed", "code": resp.status_code, "detail": detail}
                    return
                _identity_encoding(resp)
                resp.stream = _BoundedSse(resp.stream)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if len(data.encode("utf-8")) > MAX_SSE_EVENT_BYTES:
                        yield {"type": "stream.error", "message": "单个 SSE event 超过 8 MiB 上限"}
                        return
                    if data == "[DONE]":
                        received_done = True
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(chunk.get('usage'), dict):
                        usage = llm_usage.chat_completion(chunk['usage'])
                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    text_reasoning_bytes += len((delta.get("reasoning_content") or "").encode("utf-8"))
                    text_reasoning_bytes += len((delta.get("content") or "").encode("utf-8"))
                    if text_reasoning_bytes > MAX_TEXT_REASONING_BYTES:
                        yield {"type": "stream.error", "message": "正文与 reasoning 合计超过 8 MiB 上限"}
                        return
                    if delta.get("reasoning_content"):
                        safe = reasoning_filter.feed(delta["reasoning_content"])
                        if safe:
                            yield {"type": "reasoning.delta", "text": safe}
                    if delta.get("content"):
                        safe = text_filter.feed(delta["content"])
                        if safe:
                            yield {"type": "text.delta", "text": safe}
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        acc = tc_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            acc["name"] = fn["name"]
                        if fn.get("arguments"):
                            acc["arguments"] += fn["arguments"]
                            if len(acc["arguments"].encode("utf-8")) > MAX_TOOL_CALL_BYTES:
                                yield {"type": "stream.error", "message": "单个 tool call 超过 2 MiB 上限"}
                                return
                        yield {"type": "tool.delta", "name": acc["name"] or None}
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]
                        yield {"type": "message.completed", "finish_reason": finish_reason}
        safe_reasoning = reasoning_filter.finish()
        if safe_reasoning:
            yield {"type": "reasoning.delta", "text": safe_reasoning}
        safe_text = text_filter.finish()
        if safe_text:
            yield {"type": "text.delta", "text": safe_text}
        if not received_done and not finish_reason:
            yield {"type": "stream.error", "error_type": "upstream_incomplete",
                   "message": "模型连接已结束，未收到回合完成标记。"}
            return
        tool_calls = []
        for idx in sorted(tc_acc):
            acc = tc_acc[idx]
            try:
                args = json.loads(acc["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {"_raw": acc["arguments"]}
            tool_calls.append({"id": acc["id"] or f"call_{idx}", "name": acc["name"], "args": args})
        yield {"type": "turn.completed", "finish_reason": finish_reason or "stop",
               "tool_calls": tool_calls, "usage": usage}
        yield {"type": "stream.done"}
    except httpx.TimeoutException:
        yield {"type": "request.timed_out"}
    except Exception as e:
        yield {"type": "stream.error", "message": redact_text(str(e) or type(e).__name__, (api_key,), limit=ERROR_PREVIEW_BYTES),
               "error_type": type(e).__name__, "cause_type": type(e.__cause__).__name__ if e.__cause__ else None}


# ======================= Anthropic 协议 =======================
async def stream_chat_anthropic(
    *,
    endpoint: str,
    api_key: str,
    model: str,
    messages: list[dict],
    tools: list[dict] | None = None,
    reasoning_effort: str = "off",
) -> AsyncIterator[dict]:
    """Anthropic Messages API 流式中继。tool_use block 的 input_json_delta 累积。"""
    system = ""
    msgs = []
    for m in messages:
        if m["role"] == "system":
            system += m["content"] + "\n"
        else:
            msgs.append(m)
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "Accept-Encoding": "identity",
    }
    payload: dict = {"model": model, "max_tokens": 8192, "messages": msgs, "stream": True}
    if system.strip():
        payload["system"] = system.strip()
    if tools:
        payload["tools"] = tools
    if reasoning_effort != "off":
        budget = {"low": 1024, "medium": 2048, "high": 4096}[reasoning_effort]
        payload["thinking"] = {"type": "enabled", "budget_tokens": budget}

    blocks: dict[int, dict] = {}   # index -> {type, id, name, text/json_acc}
    text_filter = ProtocolSanitizer()
    reasoning_filter = ProtocolSanitizer()
    text_reasoning_bytes = 0
    finish_reason = None
    usage = {}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=20.0),
                                     trust_env=use_env_proxy(endpoint)) as client:
            async with client.stream("POST", endpoint, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    raw = await read_response(resp, ERROR_PREVIEW_BYTES, preview=True)
                    detail = redact_text(raw.decode('utf-8', 'replace'), (api_key,), limit=ERROR_PREVIEW_BYTES)
                    yield {"type": "request.failed", "code": resp.status_code, "detail": detail}
                    return
                _identity_encoding(resp)
                resp.stream = _BoundedSse(resp.stream)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if len(data.encode("utf-8")) > MAX_SSE_EVENT_BYTES:
                        yield {"type": "stream.error", "message": "单个 SSE event 超过 8 MiB 上限"}
                        return
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    t = chunk.get("type")
                    if t == 'message_start':
                        message = chunk.get('message') or {}
                        if isinstance(message.get('usage'), dict):
                            usage.update(message['usage'])
                    elif t == "content_block_start":
                        idx = chunk.get("index", 0)
                        cb = chunk.get("content_block", {})
                        blocks[idx] = {"type": cb.get("type"), "id": cb.get("id"),
                                       "name": cb.get("name"), "text": "", "json_acc": ""}
                    elif t == "content_block_delta":
                        idx = chunk.get("index", 0)
                        d = chunk.get("delta", {})
                        if d.get("type") == "text_delta":
                            text_reasoning_bytes += len((d.get("text") or "").encode("utf-8"))
                        elif d.get("type") == "thinking_delta":
                            text_reasoning_bytes += len((d.get("thinking") or "").encode("utf-8"))
                        if text_reasoning_bytes > MAX_TEXT_REASONING_BYTES:
                            yield {"type": "stream.error", "message": "正文与 reasoning 合计超过 8 MiB 上限"}
                            return
                        blk = blocks.get(idx)
                        if blk is None:
                            # 容错：未收到 content_block_start 的块按文本块处理
                            blk = {"type": "text", "id": None, "name": None,
                                   "text": "", "json_acc": ""}
                            blocks[idx] = blk
                        if d.get("type") == "text_delta" and d.get("text"):
                            blk["text"] += d["text"]
                            safe = text_filter.feed(d["text"])
                            if safe:
                                yield {"type": "text.delta", "text": safe}
                        elif d.get("type") == "thinking_delta" and d.get("thinking"):
                            safe = reasoning_filter.feed(d["thinking"])
                            if safe:
                                yield {"type": "reasoning.delta", "text": safe}
                        elif d.get("type") == "input_json_delta":
                            blk["json_acc"] += d.get("partial_json", "")
                            if len(blk["json_acc"].encode("utf-8")) > MAX_TOOL_CALL_BYTES:
                                yield {"type": "stream.error", "message": "单个 tool call 超过 2 MiB 上限"}
                                return
                            yield {"type": "tool.delta", "name": blk.get("name")}
                    elif t == "message_delta":
                        if isinstance(chunk.get('usage'), dict):
                            usage.update(chunk['usage'])
                        sr = chunk.get("delta", {}).get("stop_reason")
                        if sr:
                            finish_reason = sr
                            yield {"type": "message.completed", "finish_reason": sr}
                    elif t == "error":
                        yield {"type": "stream.error",
                               "message": redact_text(json.dumps(chunk.get("error", {}), ensure_ascii=False),
                                                      (api_key,), limit=ERROR_PREVIEW_BYTES)}
        safe_reasoning = reasoning_filter.finish()
        if safe_reasoning:
            yield {"type": "reasoning.delta", "text": safe_reasoning}
        safe_text = text_filter.finish()
        if safe_text:
            yield {"type": "text.delta", "text": safe_text}
        tool_calls = []
        for idx in sorted(blocks):
            b = blocks[idx]
            if b["type"] == "tool_use":
                try:
                    args = json.loads(b["json_acc"] or "{}")
                except json.JSONDecodeError:
                    args = {"_raw": b["json_acc"]}
                tool_calls.append({"id": b["id"] or f"toolu_{idx}", "name": b["name"], "args": args})
        yield {"type": "turn.completed",
               "finish_reason": finish_reason or "end_turn",
               "tool_calls": tool_calls, "usage": llm_usage.messages(usage) if finish_reason else None}
        yield {"type": "stream.done"}
    except httpx.TimeoutException:
        yield {"type": "request.timed_out"}
    except Exception as e:
        yield {"type": "stream.error", "message": redact_text(str(e) or type(e).__name__, (api_key,), limit=ERROR_PREVIEW_BYTES),
               "error_type": type(e).__name__, "cause_type": type(e.__cause__).__name__ if e.__cause__ else None}


def stream_chat_auto(*, provider: str, endpoint: str, api_key: str,
                     model: str, messages: list[dict],
                     tools: list[dict] | None = None,
                     max_tokens: int | None = None,
                     reasoning_effort: str = "off") -> AsyncIterator[dict]:
    """按 provider 的 API 类型分发"""
    if provider_api(provider) == "anthropic":
        return stream_chat_anthropic(endpoint=endpoint, api_key=api_key,
                                     model=model, messages=messages, tools=tools,
                                     reasoning_effort=reasoning_effort)
    return stream_chat_openai(endpoint=endpoint, api_key=api_key,
                              model=model, messages=messages, tools=tools,
                              max_tokens=max_tokens,
                              reasoning_effort=reasoning_effort)


# ======================= 非流式补全（压缩/翻译等内部调用） =======================
async def complete(*, provider: str, endpoint: str, api_key: str, model: str,
                   messages: list[dict], max_tokens: int = 2048) -> str:
    """非流式一问一答（双协议）。供会话压缩、划词翻译等服务端内部调用。"""
    headers = {'Content-Type': 'application/json', 'Accept-Encoding': 'identity'}
    anthropic = provider_api(provider) == 'anthropic'
    if anthropic:
        system = '\n'.join(m['content'] for m in messages if m['role'] == 'system').strip()
        payload = {'model': model, 'max_tokens': max_tokens,
                   'messages': [m for m in messages if m['role'] != 'system'], 'stream': False}
        if system:
            payload['system'] = system
        headers.update({'x-api-key': api_key, 'anthropic-version': '2023-06-01'})
    else:
        payload = {'model': model, 'messages': messages, 'stream': False}
        headers['Authorization'] = 'Bearer ' + api_key
    async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=20), trust_env=use_env_proxy(endpoint)) as client:
        async with client.stream('POST', endpoint, json=payload, headers=headers) as response:
            if response.status_code != 200:
                preview = await read_response(response, ERROR_PREVIEW_BYTES, preview=True)
                clean = redact_text(preview.decode('utf-8', 'replace'), (api_key,), limit=ERROR_PREVIEW_BYTES)
                raise RuntimeError(f'LLM {response.status_code}: {clean}')
            data = json.loads(await read_response(response, MAX_RESPONSE_BODY_BYTES))
    if anthropic:
        return ''.join(block.get('text', '') for block in data.get('content', []) if block.get('type') == 'text')
    return data['choices'][0]['message']['content'] or ''


def _user_content(payload: dict, api_kind: str):
    images = payload.get("images") or []
    if not images:
        return payload.get("content", "")
    parts = []
    if payload.get("content"):
        parts.append({"type": "text", "text": payload["content"]})
    for image in images:
        if not image.get("data"):
            continue
        if api_kind == "anthropic":
            parts.append({"type": "image", "source": {"type": "base64",
                "media_type": image["mime"], "data": image["data"]}})
        else:
            parts.append({"type": "image_url", "image_url": {
                "url": f"data:{image['mime']};base64,{image['data']}"}})
    return parts if parts else payload.get("content", "")


# ======================= 消息格式转换（工具结果注入） =======================
def to_openai_messages(events: list[dict]) -> list[dict]:
    """事件历史 → OpenAI messages（含 tool 结果）。事件类型见 agent runner。"""
    out: list[dict] = []
    for e in events:
        t = e["type"]
        p = e["payload"]
        if t == "message" and p.get("role") in ("user", "assistant", "system"):
            msg = {"role": p["role"],
                   "content": _user_content(p, "openai") if p["role"] == "user" else p.get("content", "")}
            if p.get("tool_calls"):
                msg["tool_calls"] = [{"id": tc["id"], "type": "function",
                                      "function": {"name": tc["name"],
                                                   "arguments": json.dumps(tc["args"], ensure_ascii=False)}}
                                      for tc in p["tool_calls"]]
            out.append(msg)
        elif t == "tool_result":
            out.append({"role": "tool", "tool_call_id": p["tool_call_id"],
                        "content": p["result"]})
    return out


def to_anthropic_messages(events: list[dict]) -> list[dict]:
    """事件历史 → Anthropic messages（tool_use/tool_result 是 content block）。"""
    out: list[dict] = []
    for e in events:
        t = e["type"]
        p = e["payload"]
        if t == "message" and p.get("role") in ("user", "assistant"):
            if p.get("tool_calls"):
                out.append({"role": "assistant", "content": [
                    *([{"type": "text", "text": p["content"]}] if p.get("content") else []),
                    *[{"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["args"]}
                      for tc in p["tool_calls"]],
                ]})
            else:
                out.append({"role": p["role"],
                            "content": _user_content(p, "anthropic") if p["role"] == "user" else p.get("content", "")})
        elif t == "tool_result":
            # Anthropic 的 tool_result 必须包在 user 消息里
            block = {"type": "tool_result", "tool_use_id": p["tool_call_id"],
                     "content": p["result"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
    return out
