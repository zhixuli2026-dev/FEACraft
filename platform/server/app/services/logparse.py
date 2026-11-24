"""求解器日志解析服务（对标桌面版 VFLogService，重写实现）

解析 OpenFOAM 风格求解器日志为结构化数据：
  - 迭代残差：smoothSolver/GAMG "Solving for Xx, Initial residual = r"
  - 时间步：Time = t
  - Courant 数：Courant Number mean/max
  - 执行时间：ExecutionTime
输出 chart-ready 的时间序列 + 摘要。
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
from contextlib import closing

from .native_openfoam_residuals import residual_samples

RE_TIME = re.compile(r"^Time\s*=\s*(\S+)", re.MULTILINE)
# 第二时刻格式（Flotherm/STAR 风格："TimeStep   12: Time = 0.5"）——桌面版同款兼容
RE_TIME2 = re.compile(r"^\s*TimeStep\s+\d+\s*:\s*Time\s*=?\s*(\S+)", re.MULTILINE)
RE_COURANT = re.compile(r"Courant Number mean:\s*(\S+)\s*max:\s*(\S+)")
RE_EXEC = re.compile(r"ExecutionTime\s*=\s*([\d.eE+-]+)\s*s")
RE_END = re.compile(r"^End\s*$|^Finalising|^writeCPUtime", re.MULTILINE)
# 差异化增强（桌面版没有）：错误/警告行计数
RE_FATAL = re.compile(r"FOAM FATAL|-->\s*FOAM FATAL", re.IGNORECASE)
RE_WARN = re.compile(r"^\s*-*\s*Warning", re.MULTILINE | re.IGNORECASE)


def parse_solver_log(text: str) -> dict:
    series = _Series()
    series.add(text)
    return series.result()


def _finite(value):
    try:
        number = float(value)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


class _Series:
    def __init__(self):
        self.times = []
        self.fallback_times = []
        self.courant = []
        self.fields = {}
        self.last_execution = None
        self.ended = False
        self.fatal = 0
        self.warnings = 0

    def add(self, text):
        self.times.extend(number for match in RE_TIME.finditer(text) if (number := _finite(match[1])) is not None)
        if self.times:
            self.fallback_times.clear()
        else:
            self.fallback_times.extend(number for match in RE_TIME2.finditer(text) if (number := _finite(match[1])) is not None)
        for match in RE_COURANT.finditer(text):
            mean, maximum = _finite(match[1]), _finite(match[2])
            if mean is not None and maximum is not None:
                self.courant.append((mean, maximum))
        for match in RE_EXEC.finditer(text):
            number = _finite(match[1])
            if number is not None:
                self.last_execution = number
        for field,point in residual_samples(text):
            self.fields.setdefault(field, []).append(point)
        self.ended = self.ended or bool(RE_END.search(text))
        self.fatal += sum(1 for _ in RE_FATAL.finditer(text))
        self.warnings += sum(1 for _ in RE_WARN.finditer(text))

    def result(self):
        times = self.times or self.fallback_times
        return {
            "schema": "vibeflow.solver-log.v1",
            "times": times,
            "courant": self.courant,
            "residuals": {key: [point["final"] for point in values] for key, values in self.fields.items()},
            "residuals_detail": self.fields,
            "summary": {
                "time_points": len(times),
                "last_time": times[-1] if times else None,
                "fields": sorted(self.fields),
                "iterations_total": sum(len(values) for values in self.fields.values()),
                "courant_max_last": self.courant[-1][1] if self.courant else None,
                "execution_time_last_s": self.last_execution,
                "finished_cleanly": self.ended,
                "fatal_errors": self.fatal,
                "warnings": self.warnings,
            },
        }


    @property
    def time_source(self):
        if self.times:
            return 'time'
        return 'time_step' if self.fallback_times else None


class LogRewriteError(ValueError):
    """游标之前的字节已不再相同，不能把新内容当成追加。"""


class LogReadLimit(ValueError):
    """A single unterminated log record is larger than the parser can retain."""


MAX_LINE_BYTES = 4 * 1024 * 1024


def _prefix_digest(offset, fingerprint):
    if offset < 0:
        raise ValueError("offset 不能为负数")
    digest = hashlib.sha256()
    if offset == 0 and fingerprint is not None and fingerprint.lower() != digest.hexdigest():
        raise ValueError("零偏移游标需要空前缀的 fingerprint")
    if offset > 0 and (fingerprint is None or not re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint)):
        raise ValueError("续读必须提供 64 位十六进制 fingerprint")
    return digest


def _verify_prefix(digest, fingerprint):
    if not hmac.compare_digest(digest.hexdigest(), fingerprint.lower()):
        raise LogRewriteError("日志前缀 fingerprint 不匹配，文件可能已被重写")


def _suffix(blocks, offset, digest, fingerprint):
    """Consume and validate the old prefix without retaining those bytes."""
    if offset:
        for block in blocks:
            end = min(offset, len(block))
            digest.update(memoryview(block)[:end])
            offset -= end
            if offset:
                continue
            tail = block[end:]
            if block[end-1] != 10:
                if b'\n' not in tail and not any(b'\n' in rest for rest in blocks):
                    raise LogRewriteError("日志已截断，旧游标超出当前完整内容")
                _verify_prefix(digest, fingerprint)
                raise ValueError("offset 必须位于完整行之后")
            _verify_prefix(digest, fingerprint)
            if tail:
                yield tail
            break
        else:
            raise LogRewriteError("日志已截断，旧游标超出当前完整内容")
    yield from blocks


def parse_log_file(reader, incremental, offset, fingerprint, *, skip_oversized=False):
    """Scan one pinned file in complete blocks and retain only parsed points and its last line."""
    series = _Series()
    digest = _prefix_digest(offset, fingerprint)
    pending = b''
    complete = offset
    discarding = False
    with closing(reader.chunks()) as blocks:
        for block in _suffix(blocks, offset, digest, fingerprint):
            if discarding:
                newline = block.find(b'\n')
                if newline == -1:
                    continue
                block = block[newline+1:]
                discarding = False
            data = pending + block
            boundary = data.rfind(b'\n') + 1
            pending = data[boundary:]
            start = 0
            if boundary and data.find(b'\n') > MAX_LINE_BYTES:
                if incremental or not skip_oversized:
                    raise LogReadLimit("单行日志超过 4 MiB，请分段查看原始日志")
                start = data.find(b'\n') + 1
            if len(pending) > MAX_LINE_BYTES:
                if incremental or not skip_oversized:
                    raise LogReadLimit("单行日志超过 4 MiB，请分段查看原始日志")
                pending = b''
                discarding = True
            if boundary:
                chunk = memoryview(data)[start:boundary]
                if incremental:
                    digest.update(chunk)
                complete += boundary
                series.add(bytes(chunk).decode('utf-8', 'replace'))
    if pending and not incremental:
        series.add(pending.decode('utf-8', 'replace'))
    result = series.result()
    if incremental:
        result["cursor"] = {
            "previous_offset": offset, "offset": complete,
            "fingerprint": digest.hexdigest(), "unchanged": complete == offset,
            "delta_bytes": complete - offset, "pending_bytes": len(pending),
            "time_source": series.time_source,
        }
    return result


def parse_solver_log_incremental(data: bytes, offset: int = 0,
                                 fingerprint: str | None = None) -> dict:
    """按完整行边界解析追加日志，并用已消费前缀的 SHA-256 检测重写。

    游标始终指向换行后的字节；末尾半行不消费，下次连同追加内容一起解析。
    """
    digest = _prefix_digest(offset, fingerprint)

    complete_end = data.rfind(b"\n") + 1
    if offset > complete_end:
        raise LogRewriteError("日志已截断，旧游标超出当前完整内容")
    contents = memoryview(data)
    if offset:
        digest.update(contents[:offset])
        _verify_prefix(digest, fingerprint)
        if data[offset - 1] != 10:
            raise ValueError("offset 必须位于完整行之后")

    chunk = data[offset:complete_end]
    digest.update(contents[offset:complete_end])
    series = _Series()
    series.add(chunk.decode("utf-8", "replace"))
    result = series.result()
    result["cursor"] = {
        "previous_offset": offset,
        "offset": complete_end,
        "fingerprint": digest.hexdigest(),
        "unchanged": complete_end == offset,
        "delta_bytes": len(chunk),
        "pending_bytes": len(data) - complete_end,
        "time_source": series.time_source,
    }
    return result
