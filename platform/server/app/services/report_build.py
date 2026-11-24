"""Immutable Markdown/image snapshots and guarded HTML/DOCX publication (local fs)."""
from __future__ import annotations

import hashlib
from html import escape
from html.parser import HTMLParser
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, unquote, urlsplit
import zipfile

from .. import storage
from .case_evidence import file_record, verify_records
from .casedict import CASE_DICT_PATH, parse_casedict
from .postprocess_gate import report_gate_reason


def report_work_dir(job_id: int) -> str:
    return f".vibeflow/report-jobs/job-{job_id}"


def capture_report_guard(prefix: str, *, validated_gate=False, root=None, load=None) -> dict:
    reason = None if validated_gate else report_gate_reason(SimpleNamespace(s3_prefix=prefix))
    if reason:
        raise ValueError(reason)
    def read(path):
        if load is not None and not load(path):
            raise FileNotFoundError(path)
        if root is None:return storage.get_bytes(prefix,path)
        from .attempt_workspace import checked_path
        return checked_path(root,path).read_bytes()
    def exists(path):
        if root is None:return storage.exists(prefix,path)
        try:read(path);return True
        except FileNotFoundError:return False
    parsed = parse_casedict(read(CASE_DICT_PATH).decode("utf-8"))
    for name in ("workflowState", "errors", "provenance"):
        parsed.pop(name, None)
    if isinstance(parsed.get("caseMeta"), dict):
        parsed["caseMeta"].pop("status", None)
    semantic = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    pointer = "D-post/data/latest-postprocess.json"
    evidence = {}
    if exists(pointer):
        latest = json.loads(read(pointer))
        if isinstance(latest, dict) and isinstance(latest.get("job_id"), int):
            for name in ("metrics.json", "artifact-manifest.json"):
                path = f"D-post/data/postprocess-{latest['job_id']}/{name}"
                evidence[path] = hashlib.sha256(read(path)).hexdigest()
    return {"case_semantic_sha256": hashlib.sha256(semantic.encode()).hexdigest(),
            "evidence": evidence,
            "postprocess_pointer_sha256": hashlib.sha256(read(pointer)).hexdigest()
            if exists(pointer) else None}


def validate_report_guard(prefix: str, expected: dict):
    if capture_report_guard(prefix) != expected:
        raise ValueError("报告构建期间工程参数或后处理证据已变化，未发布；请重新构建")


def _contained(root: Path, path: Path) -> Path:
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("报告资源越过算例目录")
    if not path.is_file():
        raise ValueError(f"报告资源不存在: {path.name}")
    return path


def metadata_path(value, name):
    """Pandoc YAML strings may be plain strings or parsed inline text."""
    if not isinstance(value, dict):
        raise ValueError(name + ' 必须是算例内文件路径')
    if value.get('t') == 'MetaString':
        result = value['c']
    elif value.get('t') == 'MetaInlines':
        parts = []
        for item in value['c']:
            if item.get('t') == 'Str': parts.append(item['c'])
            elif item.get('t') in {'Space', 'SoftBreak'}: parts.append(' ')
            else: raise ValueError(name + ' 必须是纯文本路径')
        result = ''.join(parts)
    else:
        raise ValueError(name + ' 必须是单一文件路径')
    if not isinstance(result, str) or not result.strip():
        raise ValueError(name + ' 不能为空')
    return result.strip()


class _FrozenHTML(HTMLParser):
    def __init__(self, resource, css=None, srcset=None):
        super().__init__(convert_charrefs=False)
        self.resource, self.parts = resource, []
        self.css, self.in_style = css, False
        self.srcset = srcset

    def _start(self, tag, attrs, close):
        if tag == 'link' and self.css and 'stylesheet' in (dict(attrs).get('rel') or '').lower().split():
            values=dict(attrs)
            href=self.css.stylesheet(values.get('href') or '')
            media=' media="'+escape(values['media'],quote=True)+'"' if values.get('media') else ''
            self.parts.append('<link rel="stylesheet" href="'+escape(href,quote=True)+'"'+media+'>')
            return
        if tag in ("base", "link"):
            raise ValueError("快照报告暂不支持外部样式表或 base 标签，请使用报告内样式")
        rendered = []
        for name, value in attrs:
            if name == 'style' and value and self.css:
                value=self.css.inline(value,declarations=True)
            if name == 'srcset':
                if not self.srcset:raise ValueError('srcset 资源冻结入口不可用')
                value=self.srcset(value or '')
            if name == "style" and value and not self.css and re.search(r"url\s*\(", value, re.I):
                raise ValueError("快照报告 CSS 资源冻结入口不可用")
            if name in ("src", "poster") and value is not None:
                value = self.resource(value)
            if tag in ("image", "use") and name in ("href", "xlink:href") and value and not value.startswith('#'):
                value = self.resource(value)
            rendered.append(name if value is None else f'{name}="{escape(value, quote=True)}"')
        self.parts.append("<" + tag + (" " if rendered else "") + " ".join(rendered) + close)
        if tag=='style':self.in_style=True

    def handle_starttag(self, tag, attrs):
        self._start(tag, attrs, ">")

    def handle_startendtag(self, tag, attrs):
        self._start(tag, attrs, "/>")

    def handle_endtag(self, tag):
        self.parts.append(f"</{tag}>")
        if tag=='style':self.in_style=False

    def handle_data(self, data):
        if self.in_style and self.css:
            self.parts.append(self.css.inline(data))
            return
        if re.search(r"(?:url\s*\(|@import)", data, re.I):
            raise ValueError("快照报告暂不支持 CSS url/@import，请使用报告内图片")
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        self.parts.append("<!--" + data + "-->")


def _resource_loader(root, source, records, original_root, selections, load=None):
    def available(path):
        if load is None:
            return path.is_file()
        candidate = Path(os.path.abspath(path))
        return candidate.is_relative_to(root) and load(candidate.relative_to(root).as_posix())

    def local_resource(url: str, *, image=False, base=None) -> str:
        if image and url.lower().startswith("data:image/"):
            return url
        parsed = urlsplit(url)
        if parsed.scheme or parsed.netloc:
            raise ValueError("请先将远程或绝对 URI 图片保存到算例内，再构建报告")
        raw = unquote(parsed.path).replace("\\", "/")
        if Path(raw).is_absolute() and original_root is not None:
            # Map an already selected absolute case resource into the sealed
            # working copy. Never read the live path during compilation.
            raw = (root / Path(raw).relative_to(original_root)).as_posix()
        base=root/base if base is not None else source.parent
        candidates = [base / raw, root / raw] if not Path(raw).is_absolute() else [Path(raw)]
        candidate = next((path for path in candidates if available(path)), candidates[0])
        _contained(root, candidate)
        # Normalize '..' without losing the original symlink name for later revalidation.
        candidate = Path(os.path.abspath(candidate))
        relative = candidate.relative_to(root).as_posix()
        if selections is not None:
            choices=[]
            for choice in candidates:
                choice=Path(os.path.abspath(choice))
                if not choice.is_relative_to(root):raise ValueError('Report resource candidate escaped its root')
                choices.append(choice.relative_to(root).as_posix())
                if choice==candidate:break
            selections.append({'candidates':choices,'selected':relative})
        if relative not in records:
            content = candidate.read_bytes()
            records[relative] = content
        return relative + ("#" + parsed.fragment if parsed.fragment else "")

    return local_resource


def _reference_document(requested, local_resource, records):
    if urlsplit(requested).query or urlsplit(requested).fragment:
        raise ValueError('DOCX 模板路径不能包含查询参数或片段')
    selected = local_resource(requested)
    if Path(selected).suffix.lower() != '.docx':
        raise ValueError('reference-doc 必须是 DOCX 文件')
    # Read and validate the selected archive before native compilation.
    from .report_artifacts import canonical_docx
    canonical_docx(records[selected])
    return selected


def generated_report_resources(root, settings, figures, original_root, selections, *, load=None):
    records = {}
    local_resource = _resource_loader(root, root/'E-report/report.md', records, original_root, selections, load)
    from .report_css import FrozenCSS
    css = FrozenCSS(local_resource, records)
    for stylesheet in settings.get('css', []):
        css.stylesheet(stylesheet)
    if 'reference-doc' in settings:
        _reference_document(settings['reference-doc'], local_resource, records)
    for figure in figures:
        path = local_resource('../'+quote(figure['path'],safe='/'), image=True)
        if hashlib.sha256(records[path]).hexdigest()!=figure['sha256']:
            raise ValueError('后处理图件与发布摘要不一致: '+path)
        if figure.get('source')=='caseFile' and 'data_path' in figure:
            data_path = local_resource('../'+quote(figure['data_path'],safe='/'))
            if hashlib.sha256(records[data_path]).hexdigest()!=figure['data_sha256']:
                raise ValueError('报告图件的数据文件已变化: '+data_path)
    return records


def read_report_document(root: Path, source: Path, pandoc: str, *, markdown=None, original_root=None, selections=None,
                         record_source=True, load=None):
    markdown = source.read_bytes() if markdown is None else markdown
    try:
        from .pandoc_execution import run
        parsed = run('pandoc_parse',pandoc,root,markdown=markdown)
        parsed.check_returncode()
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or b"").decode("utf-8", "replace").strip()[:500]
        raise ValueError("报告文档无法解析：" + detail) from error
    document = json.loads(parsed.stdout)
    records = {source.relative_to(root).as_posix(): markdown} if record_source else {}
    local_resource = _resource_loader(root, source, records, original_root, selections, load)

    def image_resource(url: str) -> str:
        return local_resource(url, image=True)

    def embedded_image(url):
        import base64
        from urllib.parse import unquote_to_bytes
        from .report_css import TYPES, data_uri
        if url.lower().startswith('data:'):
            raw,separator,fragment=url.partition('#')
            header,comma,body=raw.partition(',')
            media=header[5:].split(';',1)[0].lower()
            if not comma or media not in set(TYPES.values()) or not media.startswith('image/'):
                raise ValueError('srcset 内嵌资源必须是图片')
            try:data=base64.b64decode(unquote_to_bytes(body),validate=True) if header.lower().endswith(';base64') else unquote_to_bytes(body)
            except ValueError as error:raise ValueError('srcset 内嵌图片编码无效') from error
        else:
            selected=local_resource(url)
            path,separator,fragment=selected.partition('#')
            media=TYPES.get(Path(path).suffix.lower())
            if not media or not media.startswith('image/'):raise ValueError('srcset 资源必须是受支持的图片格式')
            data=records[path]
        return data_uri(media,data)+(separator+fragment if separator else '')

    from .report_srcset import rewrite
    def frozen_srcset(value):return rewrite(value,embedded_image)

    from .report_css import FrozenCSS
    css=FrozenCSS(local_resource,records)
    css_metadata=document.get('meta',{}).get('css')
    if css_metadata is not None:
        values=css_metadata['c'] if css_metadata.get('t')=='MetaList' else [css_metadata]
        document['meta']['css']={'t':'MetaList','c':[
            {'t':'MetaString','c':css.stylesheet(metadata_path(item,'css'))} for item in values]}

    if 'reference-doc' in document.get('meta', {}):
        requested = metadata_path(document['meta']['reference-doc'], 'reference-doc')
        selected = _reference_document(requested, local_resource, records)
        document['meta']['reference-doc'] = {'t':'MetaString', 'c':selected}

    def visit(node):
        if isinstance(node, list):
            # Pandoc promotes HTML Div/Span/table attributes into Attr tuples,
            # so those styles no longer pass through the RawHTML parser.
            if (len(node)==3 and isinstance(node[0],str) and isinstance(node[1],list)
                    and all(isinstance(value,str) for value in node[1]) and isinstance(node[2],list)
                    and all(isinstance(pair,list) and len(pair)==2 and all(isinstance(value,str) for value in pair)
                            for pair in node[2])):
                for pair in node[2]:
                    if pair[0].lower()=='style':pair[1]=css.inline(pair[1],declarations=True)
                    elif pair[0].lower()=='srcset':pair[1]=frozen_srcset(pair[1])
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            if node.get("t") == "Image":
                node["c"][2][0] = image_resource(node["c"][2][0])
            elif node.get("t") in ("RawInline", "RawBlock") and node["c"][0] == "html":
                parser = _FrozenHTML(image_resource,css,frozen_srcset)
                parser.feed(node["c"][1])
                parser.close()
                node["c"][1] = "".join(parser.parts)
            for item in node.values():
                visit(item)
    visit(document)
    return document, records


def prepare_report_snapshot(prefix: str, job_id: int, pandoc: str, guard: dict) -> dict:
    if storage._use_s3():
        raise ValueError("Report snapshots require fs storage")
    root = storage._fs_path(prefix, ".")
    work = storage._fs_path(prefix, report_work_dir(job_id))
    work.mkdir(parents=True, exist_ok=False)
    source = _contained(root, root / "E-report/report.md")
    document, files = read_report_document(root, source, pandoc)
    records = []
    for relative, data in files.items():
        destination = work / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        records.append({"path":relative,"size":len(data),"sha256":hashlib.sha256(data).hexdigest()})
    (work / "document.json").write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    snapshot = {"schema":"vibeflow.report-snapshot.v1", "job_id":job_id,
                "guard":guard,"inputs":sorted(records,key=lambda item:item["path"])}
    validate_report_snapshot(prefix,snapshot)
    (work / "snapshot.json").write_text(json.dumps(snapshot,ensure_ascii=False),encoding="utf-8")
    return snapshot


def validate_report_snapshot(prefix: str, snapshot: dict):
    validate_report_guard(prefix, snapshot["guard"])
    reason = verify_records(prefix, snapshot["inputs"], label="报告源文件", required=True)
    if reason:
        raise ValueError(reason + "；未发布报告")


def publish_report_artifacts(prefix: str, snapshot: dict):
    work = storage._fs_path(prefix, report_work_dir(snapshot["job_id"]))
    paths = [work / "out/report.html", work / "out/report.docx"]
    # Validate the complete pair before replacing any successful public output.
    html, docx = paths
    if not html.is_file() or b"<html" not in html.read_bytes()[:4096].lower():
        raise ValueError("报告构建未生成有效 HTML，未发布")
    if not docx.is_file() or not zipfile.is_zipfile(docx):
        raise ValueError("报告构建未生成有效 DOCX，未发布")
    with zipfile.ZipFile(docx) as archive:
        if "word/document.xml" not in archive.namelist():
            raise ValueError("DOCX 缺少正文，未发布")
    manifest = {**snapshot, "schema": "vibeflow.report-build.v1", "artifacts": [
        {**file_record(work, path), "path": "E-report/build/" + path.name} for path in paths]}
    validate_report_snapshot(prefix, snapshot)
    for path in paths:
        storage.put_bytes(prefix, "E-report/build/" + path.name, path.read_bytes())
    # Commit record is last; each output uses storage's atomic replacement.
    storage.put_text(prefix, "E-report/build/report-manifest.json", json.dumps(manifest, ensure_ascii=False))


def cleanup_report_snapshot(prefix: str, job_id: int):
    from .fs_directory import remove_owned
    target = storage._fs_path(prefix, report_work_dir(job_id))
    remove_owned(target,root=storage._fs_path(prefix,'.'))
