"""Read SCNET file and ZIP replies with bounded memory and disk admission."""
from base64 import b64decode
from contextlib import contextmanager
import io
import json
import zipfile

import httpx

from .backup_download import SpooledDownload
from .backup_restore import _JSONReader
from .case_write import CaseWriteConflict
from .request_deadline import current
from .scnet import MAX_REDIRECTS, REDIRECT_STATUSES, parse_error, safe_redirect


DOWNLOAD_PATH = '/openapi/v2/files:download'
MAX_BODY = 100 * 1024 * 1024
MAX_PREVIEW = 1024 * 1024
MAX_METADATA = 16 * 1024


class DownloadLimit(ValueError):
    pass


class DownloadProtocol(ValueError):
    pass


class DownloadSpool(SpooledDownload):
    @property
    def maximum(self):
        return MAX_BODY

    def write(self, data):
        if self.tell() + len(data) > self.maximum:
            raise DownloadLimit('SCNET 下载超过字节上限')
        return super().write(data)


class _Body(io.RawIOBase):
    def __init__(self, response, maximum, check):
        self.blocks = response.iter_raw()
        self.left = memoryview(b'')
        self.maximum = maximum
        self.size = 0
        self.check = check

    def readable(self):
        return True

    def readinto(self, buffer):
        self.check()
        if not self.left:
            block = next(self.blocks, b'')
            self.check()
            self.size += len(block)
            if self.size > self.maximum:
                raise DownloadLimit('SCNET 响应超过字节上限')
            self.left = memoryview(block)
        size = min(len(self.left), len(buffer))
        buffer[:size] = self.left[:size]
        self.left = self.left[size:]
        return size


@contextmanager
def _response(client, path, maximum, check):
    # Import here so legacy client construction stays independent of this parser.
    from .scnet_client import ScnetError

    deadline = current()
    try:
        with httpx.Client(follow_redirects=False, trust_env=False) as connection:
            url = client.base_url + DOWNLOAD_PATH
            params = {'path': path}
            headers = {**client.headers(), 'Accept-Encoding': 'identity'}
            for hop in range(MAX_REDIRECTS + 1):
                check()
                timeout = min(10, deadline.remaining()) if deadline else 10
                with connection.stream('GET', url, params=params, headers=headers, timeout=timeout) as response:
                    if response.status_code in REDIRECT_STATUSES:
                        try:
                            if hop == MAX_REDIRECTS:
                                raise ValueError('重定向次数超限')
                            url = safe_redirect(str(response.url), response.headers.get('Location', ''))
                        except ValueError as error:
                            raise ScnetError({'code':'scnet_redirect_refused',
                                              'message':'SCNET 下载只接受有效的同源重定向'}) from error
                        params = None
                        connection.cookies.clear()
                        continue
                    if not 200 <= response.status_code < 300:
                        # Diagnostic bytes are bounded independently of a successful reply.
                        with io.BufferedReader(_Body(response, MAX_METADATA * 4, check)) as body:
                            sample = body.read(MAX_METADATA)
                        raise ScnetError(parse_error(response.status_code, 'GET', response.url, sample,
                                                     [client.access_key, client.secret_key, client.token]))
                    if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                        raise DownloadProtocol('SCNET 下载使用了不支持的传输编码')
                    declared = response.headers.get('content-length')
                    if declared is not None and (not declared.isascii() or not declared.isdigit() or len(declared) > 12):
                        raise DownloadProtocol('SCNET 下载长度无效')
                    if declared is not None and int(declared) > maximum:
                        raise DownloadLimit('SCNET 响应超过字节上限')
                    with io.BufferedReader(_Body(response, maximum, check)) as body:
                        yield body
                    return
    except httpx.HTTPError as error:
        check()
        raise ScnetError({'message': f'SCNET 网络请求失败（{type(error).__name__}）'}) from error


def _write_zip(parts, output, check):
    tail, ended = '', False
    for part in parts:
        check()
        if ended and part:
            raise DownloadProtocol('SCNET 目录 Base64 无效')
        value = tail + part
        size = len(value) - len(value) % 4
        if size:
            output.write(b64decode(value[:size], validate=True))
            ended = '=' in value[:size]
        tail = value[size:]
        if ended and tail:
            raise DownloadProtocol('SCNET 目录 Base64 无效')
    if tail:
        raise DownloadProtocol('SCNET 目录 Base64 不完整')


def _write_text(parts, output, check):
    tail = ''
    for part in parts:
        check()
        text = tail + part
        tail = ''
        if text and '\ud800' <= text[-1] <= '\udbff':
            text, tail = text[:-1], text[-1]
        try:
            encoded = text.encode('utf8')
        except UnicodeEncodeError:
            encoded = text.encode('utf-16-le', 'surrogatepass').decode('utf-16-le').encode('utf8')
        output.write(encoded)
    if tail:
        raise DownloadProtocol('SCNET 文本包含不完整的字符')


def spool(client, path, output, check):
    zipped = path.endswith('/')
    received = False
    fields = {}
    # Base64 adds one third. JSON escapes and other fields share the bounded envelope.
    maximum = 4 * ((MAX_BODY + 2) // 3) + MAX_METADATA
    try:
        with _response(client, path, maximum, check) as body:
            with io.TextIOWrapper(body, encoding='utf-8-sig') as text:
                reader = _JSONReader(text)
                for _ in reader.entries('{', '}'):
                    key = reader.value(maximum=MAX_METADATA)
                    reader.require(':')
                    if (not isinstance(key, str) or key in fields or key == 'content' and received
                            or key != 'content' and len(fields) >= 32):
                        raise DownloadProtocol('SCNET 下载字段无效')
                    if key == 'content':
                        (_write_zip if zipped else _write_text)(reader.string_blocks(), output, check)
                        received = True
                    else:
                        fields[key] = reader.value(maximum=MAX_METADATA)
                if reader.peek() or not received or fields.get('format') not in (None, 'zip' if zipped else 'text'):
                    raise DownloadProtocol('SCNET 下载格式无效')
        check()
        size = output.tell()
        if zipped:
            output.seek(0)
            if output.read(4) not in {b'PK\x03\x04', b'PK\x05\x06'} or not zipfile.is_zipfile(output):
                raise DownloadProtocol('SCNET 返回的目录不是 ZIP')
        output.seek(0)
        return size, zipped
    except (DownloadLimit, CaseWriteConflict):
        raise
    except (UnicodeError, ValueError) as error:
        raise DownloadProtocol('SCNET 下载内容无效') from error


def preview(client, path):
    deadline = current()
    check = deadline.check if deadline else lambda: None
    try:
        with _response(client, path, MAX_PREVIEW, check) as body:
            data = json.loads(body.read())
        if not isinstance(data, dict) or not isinstance(data.get('content', ''), str):
            raise DownloadProtocol('SCNET 预览格式无效')
        return data.get('content', '')
    except DownloadLimit:
        raise
    except (UnicodeError, ValueError) as error:
        raise DownloadProtocol('SCNET 下载预览内容无效') from error
