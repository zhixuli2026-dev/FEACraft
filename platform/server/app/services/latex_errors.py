"""LaTeX 日志错误/警告结构化，保留源文件行号和上下文。"""
from __future__ import annotations

import re

_FILE_LINE = re.compile(r"^(?P<file>.+?\.tex):(?P<line>[1-9]\d*):\s*(?P<message>.+)$")


def parse_latex_errors(log: str) -> list[dict]:
    errors = []
    seen = set()

    def add(item: dict):
        key = (item["severity"], item["file"], item["line"], item["message"])
        if key not in seen:
            seen.add(key)
            errors.append(item)

    lines = log.splitlines()
    for index, line in enumerate(lines):
        file_line = _FILE_LINE.match(line)
        if file_line:
            message = file_line.group("message").strip()
            severity = "warning" if "Warning:" in message else "error"
            add({"severity": severity, "message": message,
                 "file": file_line.group("file"), "line": int(file_line.group("line")),
                 "context": "\n".join(lines[index:index + 3])})
        elif line.startswith("! "):
            item = {"severity": "error", "message": line[2:].strip(), "file": None,
                    "line": None, "context": line}
            context = [line]
            for following in lines[index + 1:index + 7]:
                context.append(following)
                if item["line"] is None:
                    match = re.search(r"\bl\.(\d+)\b", following)
                    if match:
                        item["line"] = int(match.group(1))
                if item["file"] is None:
                    match = re.search(r"([\w./\\-]+\.tex)\b", following)
                    if match:
                        item["file"] = match.group(1)
                if item["line"] is not None and item["file"] is not None:
                    break
            item["context"] = "\n".join(context)
            add(item)
        elif "LaTeX Warning:" in line:
            match = re.search(r"on input line (\d+)", line)
            add({"severity": "warning", "message": line.split("LaTeX Warning:", 1)[1].strip(),
                 "file": None, "line": int(match.group(1)) if match else None,
                 "context": line})
    return errors
