"""SyncTeX 反向查询和快照输入路径到当前算例源文件的映射。"""
from __future__ import annotations

import gzip
import hashlib
import io
import zlib
import posixpath
import re
from pathlib import PurePosixPath

from ..storage import _fs_path


def parse_synctex_edit(output: str) -> dict:
    source = re.search(r"^Input:(.*)$", output, re.MULTILINE)
    line = re.search(r"^Line:(.*)$", output, re.MULTILINE)
    column = re.search(r"^Column:(.*)$", output, re.MULTILINE)
    if not source or not line:
        raise ValueError("该位置没有 SyncTeX 源映射")
    try:
        return {"input": source.group(1).strip().strip('"'),
                "line": int(float(line.group(1).strip())),
                "column": int(float(column.group(1).strip())) if column else -1}
    except ValueError as error:
        raise ValueError("SyncTeX 返回了无效源行号") from error


def map_synctex_input(raw_input: str, pdf_path: str) -> str:
    pdf = PurePosixPath(pdf_path)
    source_root = pdf.parent.parent if pdf.parent.name == "build" else pdf.parent
    raw = raw_input.strip().strip('"').replace("\\", "/")
    marker = ".vibeflow/latex-jobs/"
    if marker in raw:
        tail = raw.split(marker, 1)[1]
        parts = tail.split("/", 1)
        if len(parts) != 2:
            raise ValueError("SyncTeX 快照路径无效")
        raw = parts[1]
    elif raw.startswith("file://"):
        raw = raw[7:]
    root_text = source_root.as_posix()
    absolute_marker = f"/{root_text}/"
    if absolute_marker in raw:
        raw = raw.split(absolute_marker, 1)[1]
    elif raw.startswith(root_text + "/"):
        raw = raw[len(root_text) + 1:]
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ValueError("SyncTeX 输入不在当前算例")
    candidate = posixpath.normpath(posixpath.join(root_text, raw))
    if candidate == ".." or candidate.startswith("../") or not candidate.lower().endswith(".tex"):
        raise ValueError("SyncTeX 输入不是算例内 .tex")
    return candidate


def legacy_inputs(case_prefix,pdf_path):
    from .attempt_workspace import checked_path,digest_file
    from .synctex_native import MAX_PDF,MAX_SIDECAR
    root=_fs_path(case_prefix,'.')
    if not (root/pdf_path).is_file():raise FileNotFoundError('PDF 不存在')
    pdf = checked_path(root,pdf_path)
    sidecar = pdf.with_name(f"{pdf.stem}.synctex.gz")
    if not pdf.is_file():
        raise FileNotFoundError("PDF 不存在")
    if not sidecar.is_file():
        raise FileNotFoundError("该 PDF 没有 SyncTeX；请用当前 LaTeX 构建链重新编译")
    sidecar=checked_path(root,sidecar.relative_to(root).as_posix())
    if pdf.stat().st_size>MAX_PDF or sidecar.stat().st_size>MAX_SIDECAR:
        raise ValueError('SyncTeX snapshot exceeds its limit')
    pdf_bytes,sidecar_bytes=pdf.read_bytes(),sidecar.read_bytes()
    sources={}
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(sidecar_bytes)) as stream:
            total=0
            while True:
                line=stream.readline(16385);total+=len(line)
                if total>8*1024*1024 or len(line)>16384:raise ValueError('SyncTeX source header exceeds its limit')
                if not line or line.startswith(b'Content:'):break
                match=re.match(rb'^Input:[0-9]+:(.*)',line)
                if match:
                    try:path=map_synctex_input(match[1].decode('utf-8'),pdf_path)
                    except (ValueError,UnicodeError):continue
                    try:sources[path]=digest_file(checked_path(root,path))
                    except FileNotFoundError:sources[path]=None
                    if len(sources)>10000:raise ValueError('SyncTeX source count exceeds its limit')
    except (gzip.BadGzipFile,EOFError,zlib.error) as error:
        raise ValueError('SyncTeX gzip source header is invalid') from error
    files={pdf_path:hashlib.sha256(pdf_bytes).hexdigest(),sidecar.relative_to(root).as_posix():hashlib.sha256(sidecar_bytes).hexdigest()}
    return {'pdf_bytes':pdf_bytes,'sidecar_bytes':sidecar_bytes,'files':files,'sources':sources}
