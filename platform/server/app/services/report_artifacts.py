"""Frozen report sources, deterministic Pandoc comparison and publication paths."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
from types import SimpleNamespace
import xml.etree.ElementTree as ET
import zipfile

from .. import storage
from . import attempt_workspace as workspace, report_builder
from .casedict import CASE_DICT_PATH, parse_casedict
from .postprocess_artifacts import context
from .report_build import capture_report_guard, generated_report_resources, read_report_document

MANIFEST='E-report/build/report-manifest.json'
TOOLCHAIN='E-report/build/report-toolchain.json'
OUTPUTS=('E-report/build/report.html','E-report/build/report.docx')


def encoded(value):
    return (json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)+'\n').encode('utf-8')


def source_path(job_id):
    return f'E-report/report.source-{job_id}.md'


def validate_resources(files):
    for path,data in files.items():
        if path.lower().endswith('.svg') or (not path.lower().endswith('.md') and
                (data.lstrip().startswith(b'<svg') or (data.lstrip().startswith(b'<?xml') and b'<svg' in data[:2048]))):
            try:
                svg=ET.fromstring(data)
            except ET.ParseError as error:
                raise ValueError('Invalid SVG report resource: '+path) from error
            for node in svg.iter():
                for key,value in node.attrib.items():
                    if key.rsplit('}',1)[-1]=='href' and not value.startswith(('#','data:image/')):
                        raise ValueError('SVG resources must embed their external dependencies: '+path)
                values=[*node.attrib.values(),node.text or '']
                for value in values:
                    if '@import' in value.lower() or any(not target.strip(' \"\'').startswith('#')
                            for target in re.findall(r'url\s*\((.*?)\)',value,re.I)):
                        raise ValueError('SVG styles have unfrozen external dependencies: '+path)


def generation_time(argument):
    if not isinstance(argument,str) or not argument.startswith('--generated-at='):
        raise ValueError('Missing frozen document generation time')
    value=argument.removeprefix('--generated-at=')
    moment=datetime.fromisoformat(value)
    if moment.tzinfo!=timezone.utc or moment.isoformat()!=value:
        raise ValueError('Document generation time must be canonical UTC')
    return value


@contextmanager
def source_view(prefix):
    from .case_read_view import selected_view

    if not storage._use_s3():
        yield storage._fs_path(prefix,'.'), None, None
        return
    with selected_view(prefix) as selected:
        yield selected.root, selected.load, selected.versions


def inspect_case_source(prefix, *args, **kwargs):
    with source_view(prefix) as (root, load, _):
        return source_snapshot(root,*args,gate_prefix=prefix,load=load,**kwargs)


def source_snapshot(root,mode,pandoc,*,validated_gate=False,standalone=False,known_files=None,
                    markdown=None,track_resolution=False,known_resolution=None,gate_prefix=None,load=None):
    if load is not None:
        load(CASE_DICT_PATH)
        if mode=='markdown':
            load('E-report/report.md')
        if standalone and mode=='compose':
            load('B-mesh/quality.json')
    parsed=parse_casedict((root/CASE_DICT_PATH).read_text(encoding='utf-8'))
    for key in ('workflowState','provenance'):
        parsed.pop(key,None)
    if isinstance(parsed.get('caseMeta'),dict):
        parsed['caseMeta'].pop('status',None)
    files={}
    selections=known_resolution if known_resolution is not None else ([] if track_resolution else None)
    if mode=='markdown' or (mode=='compose' and (pandoc or track_resolution)):
        if known_files is None:
            original=root.parents[3] if root.name=='run' and (root.parent/'manifest.json').is_file() else None
            if mode=='markdown':
                source=workspace.checked_path(root,'E-report/report.md')
                _,data=read_report_document(root,source,pandoc,
                    original_root=original,markdown=markdown,selections=selections,load=load)
            else:
                from .report_templates import metadata
                figures=report_builder.published_figures(SimpleNamespace(s3_prefix=gate_prefix),root,parsed,load=load)
                data=generated_report_resources(root,metadata(parsed),figures,original,selections,load=load)
            validate_resources(data)
            files=workspace.inventory(root,list(data)) if data else {}
            if any(files[path]['sha256']!=hashlib.sha256(value).hexdigest() for path,value in data.items()):
                from .case_write import CaseWriteConflict
                raise CaseWriteConflict('input_stale','Report resources changed while selecting inputs')
        else:
            # The registered descriptor was parsed and independently rebuilt
            # before publication. Under the commit lock compare every selected
            # byte (including Markdown); do not start Pandoc inside that lock.
            if not isinstance(known_files,dict) or (mode=='markdown' and 'E-report/report.md' not in known_files):
                raise ValueError('Known report resources omit the source document')
            if load is not None:
                for path in known_files:
                    load(path)
            files=workspace.inventory(root,list(known_files)) if known_files else {}
            if known_resolution is not None:
                for choice in known_resolution:
                    if load is not None:
                        for path in choice['candidates']:
                            if load(path):
                                break
                    candidates=[root/workspace.canonical_path(path) for path in choice['candidates']]
                    selected=next((path for path in candidates if path.is_file()),None)
                    if selected is None or selected.relative_to(root).as_posix()!=choice['selected']:
                        from .case_write import CaseWriteConflict
                        raise CaseWriteConflict('input_stale','Report resource resolution changed during preparation')
                selections=known_resolution
        if any(path.split('/')[0].casefold()=='.vibeflow' for path in files):
            raise ValueError('Report resources cannot select execution metadata')
    elif mode!='compose':
        raise ValueError('Unsupported report source mode')
    prefix=gate_prefix if gate_prefix is not None else root.relative_to(storage._fs_root().resolve()).as_posix()
    guard=capture_report_guard(prefix,validated_gate=validated_gate,root=root,load=load)
    if standalone:
        # Without publication references these are ordinary frozen inputs too.
        # Their presence/absence is compared again before publishing.
        extra=list(guard['evidence'])
        if guard['postprocess_pointer_sha256'] is not None:
            extra.append('D-post/data/latest-postprocess.json')
        if mode=='compose' and (root/'B-mesh/quality.json').exists():
            extra.append('B-mesh/quality.json')
        if extra:
            files.update(workspace.inventory(root,extra))
    result={'case_semantic_sha256':hashlib.sha256(encoded(parsed)).hexdigest(),'mode':mode,'files':files,
        'postprocess_guard':guard}
    if selections is not None:result['resource_resolution']=selections
    return result


def document(root,mode,pandoc,generated_at=None):
    identity=context(root)
    case_id,job_id=(identity['source'][key] for key in ('case_id','job_id'))
    engineering=None
    if mode=='compose':
        parsed=parse_casedict((root/'0-caseDict/caseDict').read_text(encoding='utf-8'))
        prefix=root.relative_to(storage._fs_root().resolve()).as_posix()
        case=SimpleNamespace(s3_prefix=prefix,id=case_id,name=(parsed.get('caseMeta') or {}).get('title') or f'case-{case_id}',
            workflow_state=parsed.get('workflowState','E'))
        composed=report_builder.compose_report(case,validated_gate=True,generated_at=generated_at,root=root)
        if not composed['evidence']['report_ready']:
            raise ValueError('Frozen postprocessing is not report-ready')
        markdown=composed['content'].encode('utf-8')
        engineering={key:composed[key] for key in ('chapter_schema_version','chapter_schema','template')}
        source=root/source_path(job_id)
    else:
        source=workspace.checked_path(root,'E-report/report.md')
        markdown=source.read_bytes()
    parsed,files=read_report_document(root,source,pandoc,markdown=markdown,original_root=root.parents[3])
    validate_resources(files)
    return markdown,parsed,files,engineering


@contextmanager
def compilation(root):
    # Separate from frozen inputs and run/output inventories; only this owned
    # temporary directory is removed. Existing attempt files are never cleaned.
    folder=Path(tempfile.mkdtemp(prefix='.report-compile-',dir=root.parent))
    try:
        yield folder
    finally:
        from .fs_directory import remove_owned
        remove_owned(folder,root=root.parent)


def compile_document(root,document,pandoc,folder):
    ast=folder/'document.json'
    reference = None
    if 'reference-doc' in document.get('meta', {}):
        from .report_build import metadata_path
        reference = workspace.checked_path(root, metadata_path(document['meta']['reference-doc'], 'reference-doc'))
    for extension in ('html','docx'):
        from .report_srcset import docx_image_fallbacks
        ast.write_bytes(encoded(docx_image_fallbacks(document) if extension=='docx' else document))
        try:
            from .pandoc_execution import run
            completed=run('pandoc_compile',pandoc,root,ast=ast,output=folder/('report.'+extension),extension=extension,
                reference_doc=reference if extension=='docx' else None)
        except (OSError,subprocess.TimeoutExpired) as error:
            raise ValueError('Pandoc compilation did not finish') from error
        if completed.returncode:
            raise ValueError('Pandoc compilation failed: '+completed.stderr.decode('utf-8','replace')[-1000:])
    return {path:(folder/Path(path).name).read_bytes() for path in OUTPUTS}


def canonical_docx(data):
    import io
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names=archive.namelist()
            if len(names)!=len(set(names)) or 'word/document.xml' not in names or len(names)>10000:
                raise ValueError('DOCX has no unique document part')
            if sum(info.file_size for info in archive.infolist())>256*1024*1024:
                raise ValueError('Expanded DOCX exceeds the 256 MiB validation limit')
            result={name:archive.read(name) for name in names}
        for name,part in result.items():
            if name.lower().endswith(('.xml','.rels')):
                ET.fromstring(part)
        # ZIP timestamps and these two generated core-property timestamps are
        # incidental. All document XML, relationships, media and other metadata
        # must remain byte-identical to an independent native compilation.
        if 'docProps/core.xml' in result:
            core=ET.fromstring(result['docProps/core.xml'])
            for item in list(core):
                if item.tag in {'{http://purl.org/dc/terms/}created','{http://purl.org/dc/terms/}modified'}:
                    core.remove(item)
            result['docProps/core.xml']=ET.tostring(core)
        ET.fromstring(result['word/document.xml'])
        return result
    except (zipfile.BadZipFile,ET.ParseError,KeyError,RuntimeError,NotImplementedError) as error:
        raise ValueError('Invalid DOCX output') from error


def managed(path,job_id):
    return path in {*OUTPUTS,MANIFEST,TOOLCHAIN,source_path(job_id)}
