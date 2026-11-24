"""Read the FoamFile header and contiguous list format used by OpenFOAM v2406."""
import re
import struct


_SPACE = re.compile(rb'(?:\s+|//[^\r\n]*|/\*.*?\*/)*', re.S)
_HEADER_TOKEN = re.compile(rb'//[^\r\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|[{}]', re.S)
_COMMENTS = re.compile(r'/\*.*?\*/|//[^\r\n]*', re.S)
_FIELD = rb'\b%s\s+([^;\r\n]+)\s*;'


def _header(data):
    start = re.compile(rb'FoamFile\s*\{').match(data, _SPACE.match(data).end())
    if start is None:
        raise ValueError('OpenFOAM 文件头缺少 FoamFile')
    depth = 1
    for token in _HEADER_TOKEN.finditer(data, start.end(), min(len(data), 4096)):
        if token[0] == b'{':
            depth += 1
        elif token[0] == b'}':
            depth -= 1
            if depth == 0:
                return data[start.end():token.start()], token.end()
    raise ValueError('OpenFOAM 文件头无效')


class FoamFile:
    def __init__(self, data, path):
        self.data, self.path = data, path
        header, self.body = _header(data)
        header = _HEADER_TOKEN.sub(lambda token: b' ' if token[0].startswith(b'/') else token[0], header)
        selected_format = self._field(header, b'format')
        if selected_format not in (b'ascii', b'binary'):
            raise ValueError(f'OpenFOAM 文件格式无效：{path}')
        self.binary = selected_format == b'binary'
        self.class_name = self._field(header, b'class')
        self.object_name = self._field(header, b'object')
        if self.binary:
            declarations = re.findall(rb'\barch\s+"([^"]*)"\s*;', header)
            arch = re.fullmatch(rb'(LSB|MSB);label=(32|64);scalar=(32|64)', declarations[0]) if len(declarations) == 1 else None
            if arch is None:
                raise ValueError(f'OpenFOAM 二进制架构声明无效：{path}')
            byte_order, label, scalar = arch.groups()
            self.endian = '<' if byte_order == b'LSB' else '>'
            self.label = 'i' if label == b'32' else 'q'
            self.scalar = 'f' if scalar == b'32' else 'd'

    def _field(self, header, name):
        values = re.findall(_FIELD % name, header)
        if len(values) != 1:
            raise ValueError(f'OpenFOAM 文件头缺少唯一字段 {name.decode()}：{self.path}')
        return values[0].strip()

    def match(self, pattern, offset=None):
        """Match at the next textual token. Callers advance over binary lists explicitly."""
        cursor = self.body if offset is None else offset
        return re.compile(pattern).match(self.data, _SPACE.match(self.data, cursor).end())

    def text(self, offset=None):
        try:
            return _COMMENTS.sub(' ', self.data[self.body if offset is None else offset:].decode('utf8')).strip()
        except UnicodeDecodeError as error:
            raise ValueError(f'OpenFOAM 字典包含意外的二进制内容：{self.path}') from error

    def raw_list(self, code, offset=None):
        count = self.match(rb'([0-9]{1,9})\s*\(', offset)
        if count is None:
            raise ValueError(f'OpenFOAM 二进制列表缺少有效数量：{self.path}')
        layout = struct.Struct(self.endian+code)
        start = count.end()
        end = start+int(count[1])*layout.size
        if self.data[end:end+1] != b')':
            raise ValueError(f'OpenFOAM 二进制列表长度不符或被截断：{self.path}')
        payload = memoryview(self.data)[start:end]
        return list(layout.iter_unpack(payload)), end+1

    def labels(self, offset=None):
        records, end = self.raw_list(self.label, offset)
        values = [value for (value,) in records]
        if any(value < 0 for value in values):
            raise ValueError(f'OpenFOAM 网格标签不得为负：{self.path}')
        return values, end
