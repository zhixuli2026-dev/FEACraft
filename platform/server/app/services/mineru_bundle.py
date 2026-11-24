"""Materialize one MinerU Markdown document and its local raster resources."""

import hashlib
import io
import json
from pathlib import PurePosixPath
import posixpath
import re
from urllib.parse import unquote, urlsplit
import zipfile

from .attempt_workspace import canonical_path
from .case_import import _entry_path, CaseImportError


MAX_ARCHIVE = 50 * 1024 * 1024
MAX_EXPANSION = 96 * 1024 * 1024
MAX_MEMBER = 10 * 1024 * 1024
MAX_MARKDOWN = 4 * 1024 * 1024
IMAGE_TYPES = {'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.gif':'image/gif','.webp':'image/webp'}
INLINE_IMAGE = re.compile(r'(!\[(?:\\.|[^\]])*\]\(\s*)(<[^<>\r\n]+>|[^\s()]+)(?=\s|\))')
HTML_IMAGE = re.compile(r'(<img\b[^>]*?\bsrc\s*=\s*)(["\'])([^"\'<>]+)\2', re.IGNORECASE)
REFERENCE = re.compile(r'(?m)^(\s*\[[^\]\r\n]+\]:[ \t]*)(<[^<>\r\n]+>|[^\s()]+)')


class BundleError(ValueError):
    def __init__(self,message,status=422):
        super().__init__(message)
        self.status = status


def _inventory(archive):
    members = {}
    seen = set()
    total = 0
    if not 1 <= len(archive.infolist()) <= 1024:
        raise BundleError('ZIP 条目数量超出限制',413)
    for info in archive.infolist():
        if '\0' in info.orig_filename:
            raise BundleError('ZIP 路径包含无效字符')
        name = _entry_path(info)
        if canonical_path(name)!=name or name.casefold() in seen:
            raise BundleError('ZIP 文件名重复或包含不规范路径')
        seen.add(name.casefold())
        if info.is_dir():
            continue
        if info.file_size>MAX_MEMBER or (info.file_size>1024**2 and info.file_size > 200*info.compress_size):
            raise BundleError('ZIP 文件大小或压缩比超出限制',413)
        total += info.file_size
        if total>MAX_EXPANSION:
            raise BundleError('ZIP 解包大小超出限制',413)
        members[name] = info
    files = {name.casefold() for name in members}
    for name in files:
        if any(parent.as_posix() in files for parent in PurePosixPath(name).parents):
            raise BundleError('ZIP 文件与目录名称冲突')
    return members


def _read(archive,info):
    with archive.open(info) as file:
        value = file.read(info.file_size+1)
    if len(value)!=info.file_size:
        raise BundleError('ZIP 文件大小与目录不一致')
    return value


def _matches_image(data,extension):
    if extension=='.webp':
        return data.startswith(b'RIFF') and data[8:12]==b'WEBP'
    signatures = {'.png':(b'\x89PNG\r\n\x1a\n',),'.jpg':(b'\xff\xd8\xff',),'.jpeg':(b'\xff\xd8\xff',),
                  '.gif':(b'GIF87a',b'GIF89a')}
    return data.startswith(signatures.get(extension,()))


def unpack(data, selected=None):
    if not 0<len(data)<=MAX_ARCHIVE:
        raise BundleError('ZIP 大小超出限制',413)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = _inventory(archive)
            documents = sorted(name for name in members if name.lower().endswith('.md'))
            if selected is None and len(documents)==1:
                selected = documents[0]
            if selected not in documents:
                raise BundleError('请指定 ZIP 中的 Markdown 文件' if documents else 'ZIP 中没有 Markdown 文件')
            if members[selected].file_size>MAX_MARKDOWN:
                raise BundleError('Markdown 文件超过 4 MiB',413)
            markdown = _read(archive,members[selected]).decode('utf-8-sig')
            return _materialize(archive,members,markdown,selected,hashlib.sha256(data).hexdigest())
    except BundleError:
        raise
    except CaseImportError as error:
        raise BundleError(str(error)) from error
    except (zipfile.BadZipFile,UnicodeError,NotImplementedError,ValueError) as error:
        raise BundleError('ZIP 或 Markdown 数据无效') from error


def _materialize(archive,members,markdown,selected,archive_hash):
    output = {}
    assets = {}
    base = posixpath.dirname(selected)

    def image(raw, required):
        value = raw[1:-1] if raw.startswith('<') and raw.endswith('>') else raw
        url = urlsplit(value)
        if url.scheme in ('http','https','data') or url.netloc or not url.path:
            return raw
        path = unquote(url.path)
        extension = PurePosixPath(path).suffix.casefold()
        if not required and extension not in IMAGE_TYPES:
            return raw
        name = posixpath.normpath(posixpath.join(base,path))
        if url.scheme or path.startswith('/') or '\\' in path or name not in members or extension not in IMAGE_TYPES:
            raise BundleError(f'缺少有效的本地图像 {value[:160]}')
        if name not in assets:
            content = _read(archive,members[name])
            if not _matches_image(content,extension):
                raise BundleError(f'图像格式与文件不一致 {name[:160]}')
            digest = hashlib.sha256(content).hexdigest()
            target = f'assets/{digest}{extension}'
            assets[name] = {'path':target,'size':len(content),'sha256':digest,'media_type':IMAGE_TYPES[extension]}
            output[target] = content
        if len(output)>128:
            raise BundleError('Markdown 的本地图像超过 128 个',413)
        return assets[name]['path']

    markdown = INLINE_IMAGE.sub(lambda item:item[1]+image(item[2],True),markdown)
    markdown = HTML_IMAGE.sub(lambda item:item[1]+item[2]+image(item[3],True)+item[2],markdown)
    markdown = REFERENCE.sub(lambda item:item[1]+image(item[2],False),markdown)
    output['document.md'] = markdown.encode('utf-8')
    manifest = {'schema':'vibeflow.mineru-import.v1','source_sha256':archive_hash,'markdown_member':selected,
                'markdown_sha256':hashlib.sha256(output['document.md']).hexdigest(),'assets':assets}
    output['import.json'] = (json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode('utf-8')
    identifier = hashlib.sha256((archive_hash+'\0'+selected).encode('utf-8')).hexdigest()[:24]
    return f'E-report/mineru/{identifier}',output,manifest
