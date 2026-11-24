"""Bounded reads of the OpenFOAM format declaration for the file editor."""
from pathlib import PurePosixPath
import re


_HEADER_BYTES = 8192
_TIME = re.compile(r"(?:[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|0\.orig)", re.I)
_HEADER = re.compile(rb"FoamFile\s*\{([^{}]*)\}")
_COMMENT = re.compile(rb"//[^\r\n]*|/\*.*?\*/", re.S)
_DECLARATION = re.compile(rb"(?:^|;)\s*(format|class)\s+([A-Za-z][A-Za-z0-9_]*)\s*(?=;)")


def wants_header(path, ordinary_text):
    file = PurePosixPath(path.replace("\\", "/"))
    if ordinary_text:
        return not file.suffix and not file.name.startswith(".")
    # Dotted fields sit directly in a time directory, or one level deeper in
    # its region. Keep ordinary binary assets on their existing download path.
    return any(_TIME.fullmatch(parent) for parent in file.parts[-3:-1])


def stream_format(reader):
    if not reader.size:
        return None
    data = reader.read(0, min(reader.size, _HEADER_BYTES) - 1)
    return declared_format(data)


def declared_format(data):
    data = data[:_HEADER_BYTES].removeprefix(b"\xef\xbb\xbf").lstrip()
    while data.startswith((b"//", b"/*")):
        comment = _COMMENT.match(data)
        if comment is None:
            return None
        data = data[comment.end():].lstrip()
    header = _HEADER.match(data)
    if not header:
        return None
    fields = dict(_DECLARATION.findall(_COMMENT.sub(b" ", header[1])))
    declared = fields.get(b"format")
    if b"class" in fields and declared in {b"ascii", b"binary"}:
        return declared.decode("ascii")
    return None
