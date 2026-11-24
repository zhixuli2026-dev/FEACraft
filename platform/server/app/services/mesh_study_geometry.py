"""Fingerprint native blockMesh definitions with only cell counts removed."""

import hashlib
import re


_BLOCKS=re.compile(r'(?m)^\s*blocks\s*\(')
_COMMENT=re.compile(r'/\*.*?\*/|//[^\n]*', re.S)
_DIVISIONS=re.compile(r'(\bhex\s*\(\s*\d+(?:\s+\d+){7}\s*\)\s*(?:[A-Za-z_]\w*\s+)?)'
                      r'\(\s*[1-9]\d*\s+[1-9]\d*\s+[1-9]\d*\s*\)')


def geometry_digest(path):
    try:
        content=path.read_text(encoding='utf-8')
    except UnicodeError:
        return None
    content=_COMMENT.sub(' ',content)
    starts=list(_BLOCKS.finditer(content))
    if len(starts)!=1 or '#' in content or '$' in content:
        return None
    start=starts[0].end()
    depth=1
    for end in range(start,len(content)):
        if content[end]=='(':
            depth+=1
        elif content[end]==')':
            depth-=1
            if depth==0:
                break
    if depth:
        return None
    body=content[start:end]
    geometry,count=_DIVISIONS.subn(r'\1(__grid_divisions__)',body)
    if not count or count!=len(re.findall(r'\bhex\b',body)):
        return None
    normalized=content[:start]+geometry+content[end:]
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()
