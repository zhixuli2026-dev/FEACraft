"""Agent 与通用 Jobs 共用的命令安全底线；不能替代执行沙箱。"""
import re

_BLOCKED_PATTERNS = (
    r"rm\s+-rf\s+[~/]",
    r"mkfs",
    r"format\s+[a-z]:",
    r"del\s+/[fs]",
    r"rd\s+/s",
)


def command_block_reason(command: str) -> str | None:
    for pattern in _BLOCKED_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return f"命中高危命令模式 {pattern!r}"
    return None
