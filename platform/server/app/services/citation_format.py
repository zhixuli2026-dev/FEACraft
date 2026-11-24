"""Render an isolated BibTeX selection with Pandoc's local CSL processor."""
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from ..storage import _fs_root
from .native_execution_client import ChildExecutionError
from .native_request_execution import scope as native_scope
from .pandoc_execution import run
from .pandoc_native import CITATION_INPUTS
from .request_deadline import scope as deadline_scope
from .runtime_config import get_runtime_setting

STYLES = Path(__file__).with_name("citation_styles")
PRESETS = {"apa": "apa.csl", "ieee": "ieee.csl", "chicago": "chicago-author-date.csl"}
CSL = "{http://purl.org/net/xbiblio/csl}"


class CitationInputError(ValueError):
    pass


class CitationUnavailable(RuntimeError):
    pass


def _style(source):
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)", source, re.I):
        raise CitationInputError("CSL 文件包含不受支持的 XML 声明。")
    try:
        style = ET.fromstring(source)
    except (ET.ParseError, UnicodeError) as error:
        raise CitationInputError("无法读取 CSL XML，请检查样式文件。") from error
    if style.tag != CSL + "style":
        raise CitationInputError("请选择有效的 CSL 样式文件。")
    return style


def _parent_id(style):
    link = style.find(f"{CSL}info/{CSL}link[@rel='independent-parent']")
    if link is None:
        return None
    href = link.get("href")
    if not href:
        raise CitationInputError("出版商样式没有提供父样式标识。")
    return href


def _bounded(value, name):
    try:
        encoded = value.encode("utf-8")
    except UnicodeError as error:
        raise CitationInputError("请使用 UTF-8 文件。") from error
    if len(encoded) > CITATION_INPUTS[name]:
        raise CitationInputError("文献库或 CSL 样式超过预览大小限制。")
    return encoded


def _select_style(preset, source, parent):
    if preset != "custom":
        if source is not None or parent is not None:
            raise CitationInputError("上传样式前，请选择自定义 CSL。")
        return (STYLES / PRESETS[preset]).read_bytes()
    if not source:
        raise CitationInputError("请选择 CSL 样式文件。")
    _bounded(source, "style.csl")
    style = _style(source)
    parent_id = _parent_id(style)
    if parent_id is None:
        if parent is not None:
            raise CitationInputError("当前样式可独立使用，移除父样式文件即可。")
        if style.find(CSL + "citation") is None:
            raise CitationInputError("CSL 样式缺少正文引文格式。")
        return source.encode("utf-8")
    if not parent:
        raise CitationInputError("这个出版商样式依赖父样式，请一并选择对应的 .csl 文件。")
    _bounded(parent, "style.csl")
    independent = _style(parent)
    independent_id = independent.findtext(f"{CSL}info/{CSL}id")
    if independent_id != parent_id or _parent_id(independent) is not None or independent.find(CSL + "citation") is None:
        raise CitationInputError("父样式与出版商样式要求的独立 CSL 不匹配。")
    locale = style.get("default-locale")
    if not locale:
        return parent.encode("utf-8")
    independent.set("default-locale", locale)
    return _bounded(ET.tostring(independent, encoding="unicode"), "style.csl")


async def render(case_id, user_id, *, bibtex, keys, preset, style, parent):
    try:
        files = {
            "input.md": _bounded("\n\n".join(f"[@{key}]" for key in keys) + "\n", "input.md"),
            "refs.bib": _bounded(bibtex, "refs.bib"),
            "style.csl": _select_style(preset, style, parent),
        }
        configured = get_runtime_setting("pandoc", "")
        if not configured or not Path(configured).is_file():
            raise CitationUnavailable("引文预览需要已安装的 Pandoc，请在运行时设置中配置。")
        working_root = _fs_root().resolve() / ".citation-previews" / "run"
        working_root.mkdir(parents=True, exist_ok=True)
        async with deadline_scope(35):
            profile = {"pandoc": configured, "purpose": "pandoc_cite"}
            async with native_scope(case_id, user_id, working_root, "report.prepare", profile) as executor:
                result = await executor.run_sync(run, "pandoc_cite", configured, working_root,
                                                 citation_inputs=files, timeout=34)
    except (ChildExecutionError, OSError) as error:
        raise CitationUnavailable("引文预览暂时不可用，请重试。") from error
    diagnostics = result.stderr.decode("utf-8", "replace")
    if result.returncode or re.search(r"Citeproc: citation .+ not found", diagnostics):
        raise CitationInputError("无法格式化引文，请检查 BibTeX 引用键和 CSL 样式。")
    return result.stdout.decode("utf-8").strip()
