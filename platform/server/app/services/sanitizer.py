"""模型正文/reasoning 的伪工具 XML/DSML/sentinel 增量清洗。"""
from __future__ import annotations

import re

_NAMES = r"(?:tool_calls?|function_calls?|dsml|update_plan)"
_XML_NAMES = (rf"(?:{_NAMES}|｜DSML｜(?:tool_calls|function_calls|invoke|parameter)"
              r"|｜｜DSML｜｜\s+(?:calls|invoke|parameter))")
_XML_ATTRIBUTES = r'''(?:\s(?:[^"'<>]|"[^"]*"|'[^']*')*)?'''
_START = re.compile(
    rf"<\|(?P<sentinel>{_NAMES})\|>|<｜(?P<legacy>tool▁calls?)▁begin｜>"
    rf"|<(?P<xml>{_XML_NAMES}){_XML_ATTRIBUTES}/?>"
    rf"|(?P<orphan></\s*{_XML_NAMES}\s*>|<｜tool▁calls?▁end｜>)",
    re.IGNORECASE,
)
_OPEN_NAME = re.compile(rf"<{_XML_NAMES}(?=\s|/)", re.IGNORECASE)
_SENTINEL_CLOSE = re.compile(r"<\|(?:end|tool_calls?_end|function_calls?_end|dsml_end|update_plan_end)\|>", re.IGNORECASE)


class ProtocolSanitizer:
    """保留普通文本，丢弃伪协议块；start/end 可跨任意 delta 分片。"""

    def __init__(self):
        self._buffer = ""
        self._closing: re.Pattern[str] | None = None

    def feed(self, text: str, final: bool = False) -> str:
        self._buffer += text
        output = []
        while self._buffer:
            if self._closing is not None:
                close = self._closing.search(self._buffer)
                if close is None:
                    hold = None if final else self._partial_tag(self._buffer)
                    self._buffer = "" if hold is None else self._buffer[hold:]
                    break
                self._buffer = self._buffer[close.end():]
                self._closing = None
                continue
            match = _START.search(self._buffer)
            if match is None:
                hold = self._partial_tag(self._buffer, opening=True)
                if final:
                    # Drop a recognized opening tag whose attributes ended mid-stream.
                    incomplete = hold is not None and _OPEN_NAME.match(self._buffer, hold)
                    output.append(self._buffer[:hold] if incomplete else self._buffer)
                    self._buffer = ""
                else:
                    if hold is None:
                        output.append(self._buffer)
                        self._buffer = ""
                    else:
                        output.append(self._buffer[:hold])
                        self._buffer = self._buffer[hold:]
                break
            output.append(self._buffer[:match.start()])
            self._buffer = self._buffer[match.end():]
            if match.group("orphan") or (match.group("xml") and match.group(0).endswith("/>")):
                continue
            if match.group("sentinel"):
                self._closing = _SENTINEL_CLOSE
            elif legacy := match.group("legacy"):
                self._closing = re.compile(rf"<｜{legacy}▁end｜>", re.IGNORECASE)
            else:
                name = r"\s+".join(re.escape(part) for part in match.group("xml").split())
                self._closing = re.compile(rf"</\s*{name}\s*>", re.IGNORECASE)
        if final:
            self._closing = None
        return "".join(output)

    def finish(self) -> str:
        return self.feed("", final=True)

    @staticmethod
    def _partial_tag(buffer: str, *, opening: bool = False) -> int | None:
        start = buffer.rfind("<")
        if start < 0:
            return None
        suffix = buffer[start:]
        if opening and _OPEN_NAME.match(suffix):
            return start
        return start if len(suffix) <= 256 and ">" not in suffix else None


def sanitize_text(text: str) -> str:
    sanitizer = ProtocolSanitizer()
    return sanitizer.feed(text, final=True)
