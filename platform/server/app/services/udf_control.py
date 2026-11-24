"""Insert one literal functionObject without reserializing a native controlDict."""
import re

# Comments, strings and OpenFOAM verbatim code blocks cannot supply delimiters.
_TOKENS = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:[^"\\]|\\.)*"|#\{.*?#\}|#[A-Za-z][^\n]*|[{};]|[^\s{};]+', re.DOTALL)
_LIB_TOKENS = re.compile(r'//[^\n]*|/\*.*?\*/|"(?:[^"\\]|\\.)*"|#\{.*?#\}|#[A-Za-z][^\n]*|[{}();]|[^\s{}();]+',re.DOTALL)


def insert_function(control, name, *, reuse_identical=False):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", name):
        raise ValueError("非法 UDF 名称")
    tokens = [m for m in _TOKENS.finditer(control) if not m.group().startswith(("//", "/*"))]
    depth, blocks, opening = 0, [], None
    for index, token in enumerate(tokens):
        value = token.group()
        entry_start = index == 0 or tokens[index-1].group() in {";", "}"} or tokens[index-1].group().startswith("#")
        if depth == 0 and entry_start and value.strip('"') == "functions":
            if index+1 == len(tokens) or tokens[index+1].group() != "{":
                raise ValueError("functions 必须是显式字典块")
            opening = index+1
        if value == "{":
            depth += 1
        elif value == "}":
            depth -= 1
            if depth < 0:
                raise ValueError("controlDict 括号不匹配")
            if depth == 0 and opening is not None:
                blocks.append((opening, index))
                opening = None
    if depth or len(blocks) > 1:
        raise ValueError("controlDict 括号不匹配或重复 functions 块")
    entry = (f'    {name}\n    {{\n        type myFunctionObject;\n'
             '        libs ("libmyFunctionObject.so");\n    }\n')
    if not blocks:
        return control.rstrip()+"\nfunctions\n{\n"+entry+"}\n"
    first, last = blocks[0]
    depth = 0
    candidates,types = [],[]
    for index in range(first+1, last):
        value = tokens[index].group().strip('"')
        following = tokens[index+1].group().strip('"')
        if depth == 0 and value == name and following == "{":
            nested=0;end=None
            for position in range(index+1,last):
                if tokens[position].group()=='{':nested+=1
                elif tokens[position].group()=='}':
                    nested-=1
                    if nested==0:end=position;break
            if end is None:raise ValueError('functionObject括号不匹配')
            candidates.append((index,end))
        if value == "type" and following == "myFunctionObject":
            types.append(index)
        if tokens[index].group() == "{":
            depth += 1
        elif tokens[index].group() == "}":
            depth -= 1
    if candidates:
        if reuse_identical and len(candidates)==1 and len(types)==1:
            start,end=candidates[0]
            expected=[m.group() for m in _TOKENS.finditer(entry) if not m.group().startswith(('//','/*'))]
            if start<types[0]<end and [m.group() for m in tokens[start:end+1]]==expected:
                return control
        raise FileExistsError("该 controlDict 已注册同名或不兼容的 functionObject")
    if types:raise FileExistsError("该 controlDict 已注册 myFunctionObject")
    position = tokens[last].start()
    return control[:position]+"\n"+entry+control[position:]


def insert_library(control, library):
    if not re.fullmatch(r'lib[A-Za-z][A-Za-z0-9_]*\.so',library):
        raise ValueError('Invalid native UDF library name')
    tokens = [match for match in _LIB_TOKENS.finditer(control) if not match.group().startswith(('//','/*'))]
    depth = 0
    lists = []
    for index,token in enumerate(tokens):
        word = token.group()
        previous = tokens[index-1].group() if index else ';'
        if depth == 0 and word.strip('"')=='libs' and (previous in (';','}') or previous.startswith('#')):
            if index+1>=len(tokens) or tokens[index+1].group()!='(':
                raise ValueError('controlDict 需要显式的顶层 libs 列表')
            close = next((position for position in range(index+2,len(tokens))
                          if tokens[position].group() in {'(',')','{','}',';'}),None)
            if close is None or close+1>=len(tokens) or tokens[close].group()!=')' or tokens[close+1].group()!=';':
                raise ValueError('controlDict 的顶层 libs 列表无效')
            lists.append((index+2,close))
        depth += (word=='{')-(word=='}')
        if depth<0:
            raise ValueError('controlDict 括号不匹配')
    if depth or len(lists)>1:
        raise ValueError('controlDict 括号不匹配或重复定义顶层 libs')
    if not lists:
        return control.rstrip()+f'\nlibs ("{library}");\n'
    first,last = lists[0]
    count = sum(tokens[index].group().strip('"')==library for index in range(first,last))
    if count>1:
        raise ValueError('controlDict 重复注册了同一个 UDF 库')
    if count==1:
        return control
    position = tokens[last].start()
    return control[:position]+f' "{library}"'+control[position:]
