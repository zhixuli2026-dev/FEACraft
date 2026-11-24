"""LaTeX 每次编译的独立源快照、受限命令和原子产物发布。"""
from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path

from ..storage import _fs_path
from .case_evidence import file_record, verify_records

LATEX_TIMEOUT_SECONDS = 120


def latex_work_dir(job_id: int) -> str:
    return f".vibeflow/latex-jobs/job-{job_id}"


def prepare_latex_snapshot(case_prefix: str, tex_dir: str, job_id: int):
    source = _fs_path(case_prefix, tex_dir)
    target = _fs_path(case_prefix, latex_work_dir(job_id))
    target.parent.mkdir(parents=True, exist_ok=True)
    # 工作目录在源目录之外，copytree 不会递归包含自身；唯一 job-id 禁止覆盖旧目录。
    records = _source_records(case_prefix, tex_dir)
    shutil.copytree(source, target, symlinks=False,
                    ignore=shutil.ignore_patterns(".vibeflow", "build", "out"))
    for record in records:
        relative = Path(record["path"]).relative_to(tex_dir)
        frozen = target / relative
        if not frozen.is_file() or file_record(target, frozen)["sha256"] != record["sha256"]:
            raise ValueError("LaTeX 复制期间源文件已变化，未启动编译")
    validate_latex_snapshot(case_prefix, tex_dir, records)
    return records


def _source_records(case_prefix: str, tex_dir: str) -> list[dict]:
    root = _fs_path(case_prefix, ".")
    source = _fs_path(case_prefix, tex_dir)
    records = []
    for path in source.rglob("*"):
        if any(part in ("build", "out", ".vibeflow") for part in path.relative_to(source).parts):
            continue
        if path.is_symlink():
            raise ValueError("LaTeX 快照源不支持符号链接，请使用算例内普通文件")
        if path.is_file():
            records.append(file_record(root, path))
    return sorted(records, key=lambda item: item["path"])


def validate_latex_snapshot(case_prefix: str, tex_dir: str, records: list[dict]):
    if _source_records(case_prefix, tex_dir) != records:
        raise ValueError("LaTeX 构建期间源文件已变化，未发布；请重新构建")
    reason = verify_records(case_prefix, records, label="LaTeX 源文件", required=True)
    if reason:
        raise ValueError(reason)


def latex_build_command(work_dir: str, main_name: str, recipe: str) -> str:
    return (
        f"cd {shlex.quote(work_dir)} && mkdir out && "
        f"timeout --signal=TERM --kill-after=5s {LATEX_TIMEOUT_SECONDS}s "
        f"latexmk -{recipe} -no-shell-escape -norc -interaction=nonstopmode "
        f"-synctex=1 -file-line-error -outdir=out {shlex.quote(main_name)}"
    )


def publish_latex_artifacts(case_prefix: str, tex_dir: str, work_dir: str,
                            main_name: str, job_id: int):
    """先完整暂存，再替换生成物；失败构建不会调用此函数。"""
    output = _fs_path(case_prefix, work_dir) / "out"
    build = _fs_path(case_prefix, f"{tex_dir}/build")
    stem = Path(main_name).stem
    pdf_source = output / f"{stem}.pdf"
    if not pdf_source.is_file():
        raise RuntimeError("LaTeX 作业结束但没有生成 PDF")
    build.mkdir(parents=True, exist_ok=True)
    staged = []
    try:
        for suffix, required in (("pdf", True), ("bbl", False), ("synctex.gz", True)):
            source = output / f"{stem}.{suffix}"
            if not source.is_file():
                if required:
                    raise RuntimeError(f"缺少 LaTeX 产物 {source.name}")
                continue
            temporary = build / f".{stem}.{job_id}.{suffix}.tmp"
            shutil.copyfile(source, temporary)
            staged.append((temporary, build / f"{stem}.{suffix}"))
        # 辅助产物先发布，主 PDF 最后原子替换，阅读器不会看到半写入 PDF。
        for temporary, destination in reversed(staged):
            os.replace(temporary, destination)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def cleanup_latex_snapshot(case_prefix: str, job_id: int):
    from .fs_directory import remove_owned
    target = _fs_path(case_prefix, latex_work_dir(job_id))
    remove_owned(target,root=_fs_path(case_prefix,'.'))
