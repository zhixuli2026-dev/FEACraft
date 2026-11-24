"""错误事件脱敏与 UTF-8 有界预览。"""
from __future__ import annotations

import json
import re
from urllib.parse import quote

ERROR_PREVIEW_BYTES = 16 * 1024
_SECRET_PATTERNS = [
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r'''(?i)(\b(?:api[_-]?key|(?:(?:access|refresh|id)[_-]?)?token|(?:client[_-]?)?secret)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;&}\]]+)'''),
    re.compile(r"\bsk-[A-Za-z0-9_-]{6,}"),
]


def redact_text(value: str, secrets=(), limit: int = ERROR_PREVIEW_BYTES) -> str:
    text = value
    for secret in secrets:
        if secret:
            plain = str(secret)
            for encoded in {plain, json.dumps(plain)[1:-1], quote(plain, safe='')}:
                text = text.replace(encoded, "***")
    # Bearer/sk-* 整体遮蔽；键值形式保留键名和分隔符。
    text = _SECRET_PATTERNS[0].sub("Bearer ***", text)
    text = _SECRET_PATTERNS[1].sub(r"\1***", text)
    text = _SECRET_PATTERNS[2].sub("***", text)
    return text.encode("utf-8")[:max(0, limit)].decode("utf-8", "ignore")
