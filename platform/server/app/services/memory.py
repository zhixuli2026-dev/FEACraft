"""case 长期经验记忆：受控追加与有界遗忘。"""
from __future__ import annotations

MEMORY_PATH = "0-caseDict/memory.md"
MAX_MEMORY_BYTES = 64 * 1024
MAX_MEMORY_APPEND_BYTES = 16 * 1024
TRUNCATION_MARKER = "# Earlier memory truncated\n\n"


def compact_memory(text: str, max_bytes: int = MAX_MEMORY_BYTES) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    if max_bytes <= len(TRUNCATION_MARKER.encode("utf-8")) + 1:
        raise ValueError("记忆上限过小")
    marker_bytes = TRUNCATION_MARKER.encode("utf-8")
    return _truncated_tail(data[-(max_bytes - len(marker_bytes)):])


def _truncated_tail(data: bytes) -> str:
    tail = data.decode("utf-8", "ignore")
    # 丢弃边界处的半行，保留最近完整经验条目。
    first_newline = tail.find("\n")
    if first_newline >= 0:
        tail = tail[first_newline + 1:]
    return TRUNCATION_MARKER + tail.lstrip("\r\n")


def append_memory(current: str, addition: str) -> str:
    addition = _append_suffix(addition)
    combined = (current.rstrip() + "\n\n" if current.strip() else "") + addition
    return compact_memory(combined)


def _append_suffix(addition):
    if not isinstance(addition, str) or not addition.strip():
        raise ValueError("追加记忆不能为空")
    if "\x00" in addition:
        raise ValueError("记忆不能包含 NUL")
    if len(addition.encode("utf-8")) > MAX_MEMORY_APPEND_BYTES:
        raise ValueError("单次追加记忆不能超过 16 KiB")
    return addition.strip() + "\n"


def read_memory(prefix: str):
    """Read authoritative, version-pinned bytes and the derivation precondition."""
    import hashlib
    from .file_read import FileRead
    from .file_transactions import MAX_BYTES
    try:
        selected = FileRead(prefix, MEMORY_PATH)
    except FileNotFoundError:
        return '', None
    if selected.size > MAX_BYTES - MAX_MEMORY_BYTES:
        raise ValueError('Memory exceeds file transaction capacity')
    data = selected.read()
    return data.decode('utf8'), {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def read_prompt_memory(prefix: str) -> str:
    """Only admit the retained, version-pinned part of memory to the prompt."""
    from .file_read import FileRead

    try:
        selected = FileRead(prefix, MEMORY_PATH)
    except FileNotFoundError:
        return ''
    if not selected.size:
        return ''
    if selected.size <= MAX_MEMORY_BYTES:
        return selected.read().decode('utf8')
    retained_bytes = MAX_MEMORY_BYTES - len(TRUNCATION_MARKER.encode('utf8'))
    tail = selected.read(selected.size - retained_bytes, selected.size - 1)
    return _truncated_tail(tail)


def prepare_memory_edit(prefix, action='append', content='', *, check=lambda: None):
    """Derive the new body and its SHA-256 precondition with bounded memory."""
    import codecs
    import hashlib
    from contextlib import closing
    from .file_read import FileRead
    from .file_transactions import MAX_BYTES

    if action not in {'append', 'compact'}:
        raise ValueError('记忆操作只能是 append/compact')
    suffix = _append_suffix(content).encode('utf8') if action == 'append' else b''
    check()
    try:
        selected = FileRead(prefix, MEMORY_PATH)
    except FileNotFoundError:
        return suffix, None
    if selected.size > MAX_BYTES - MAX_MEMORY_BYTES:
        raise ValueError('Memory exceeds file transaction capacity')

    # Track both the verbatim end and the end with trailing whitespace removed.
    # A legacy file can contain more than 64 KiB of trailing whitespace.
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder('utf8')()
    recent = trimmed = b''
    size = trimmed_size = 0
    if selected.size:
        with closing(selected.chunks()) as blocks:
            while True:
                check()
                block = next(blocks, None)
                if block is None:
                    break
                digest.update(block)
                decoded = decoder.decode(block)
                encoded = decoded.encode('utf8')
                if suffix and (kept := decoded.rstrip()):
                    kept_bytes = kept.encode('utf8')
                    trimmed = (recent + kept_bytes)[-MAX_MEMORY_BYTES:]
                    trimmed_size = size + len(kept_bytes)
                recent = (recent + encoded)[-MAX_MEMORY_BYTES:]
                size += len(encoded)
            decoder.decode(b'', final=True)
    if suffix:
        suffix = (b'\n\n' if trimmed_size else b'') + suffix
        recent, size = trimmed + suffix, trimmed_size + len(suffix)
    if size > MAX_MEMORY_BYTES:
        retained = MAX_MEMORY_BYTES - len(TRUNCATION_MARKER.encode('utf8'))
        recent = _truncated_tail(recent[-retained:]).encode('utf8')
    check()
    return recent, digest.hexdigest()


def update_result(action, size):
    return {'ok': True, 'result': f"长期记忆已{('追加' if action == 'append' else '压缩')}：{size} 字节"}


async def write_memory(db, case, action='append', content=''):
    """Caller holds the case coordinator and commits with the Agent tool event."""
    import asyncio
    from .async_ops import finish_before_cancel
    from .file_transactions import write
    if action not in {'append', 'compact'}:
        raise ValueError('记忆操作只能是 append/compact')
    current, expected = await finish_before_cancel(asyncio.to_thread(read_memory, case.s3_prefix))
    updated = append_memory(current, content) if action == 'append' else compact_memory(current)
    await write(db, case, {MEMORY_PATH: updated.encode('utf8')},
                expected_files={MEMORY_PATH: expected},
                content_types={MEMORY_PATH: 'text/plain; charset=utf-8'})
    return update_result(action, len(updated.encode('utf8')))
