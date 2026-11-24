"""Freeze CSS URLs/imports as self-contained data, preserving source records."""
import base64
import json
from pathlib import PurePosixPath
from urllib.parse import unquote_to_bytes, urlsplit

import tinycss2

TYPES={'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.gif':'image/gif',
       '.webp':'image/webp','.svg':'image/svg+xml','.avif':'image/avif','.woff':'font/woff',
       '.woff2':'font/woff2','.ttf':'font/ttf','.otf':'font/otf','.eot':'application/vnd.ms-fontobject'}

def data_uri(kind, data):
    return 'data:'+kind+';base64,'+base64.b64encode(data).decode('ascii')

class FrozenCSS:
    def __init__(self, select, records):
        self.select, self.records = select, records
        self.active=[]
        self.cache={}

    def _asset(self, url, parent):
        if url.startswith('#'):return url
        if url.lower().startswith('data:'):
            media=url[5:].split(';',1)[0].split(',',1)[0].lower()
            if media.startswith(('image/','font/')) or media in {'application/font-woff','application/vnd.ms-fontobject'}:
                return url
            raise ValueError('CSS 内嵌资源只支持图片或字体')
        selected=self.select(url, base=parent)
        path,separator,fragment=selected.partition('#')
        result=data_uri(TYPES.get(PurePosixPath(path).suffix.lower(),'application/octet-stream'),self.records[path])
        return result+(separator+fragment if separator else '')

    @staticmethod
    def _url(token):
        if token.type in {'url','string'}:return token.value
        if token.type=='function' and token.lower_name=='url':
            args=[t for t in token.arguments if t.type not in {'whitespace','comment'}]
            if len(args)==1 and args[0].type=='string':return args[0].value
        raise ValueError('CSS URL 必须是固定文件路径')

    @staticmethod
    def _token(url):
        return tinycss2.parse_component_value_list('url('+json.dumps(url)+')')[0]

    def _walk(self,tokens,parent):
        for index,token in enumerate(tokens):
            if token.type=='error':raise ValueError('CSS 语法无法解析：'+token.message)
            if token.type=='url' or (token.type=='function' and token.lower_name=='url'):
                tokens[index]=self._token(self._asset(self._url(token),parent))
            elif token.type=='function':
                if token.lower_name in {'image-set','-webkit-image-set'}:
                    # CSS image-set also permits bare string URLs.
                    for i,arg in enumerate(token.arguments):
                        if arg.type=='string':token.arguments[i]=self._token(self._asset(arg.value,parent))
                self._walk(token.arguments,parent)
            elif token.type=='at-keyword' and token.lower_value=='import':
                raise ValueError('CSS @import 必须位于样式表顶层')
            elif hasattr(token,'content') and token.content is not None:
                self._walk(token.content,parent)

    def _rules(self,rules,parent,encoding=None):
        result=[]
        for rule in rules:
            if rule.type=='error':raise ValueError('CSS 语法无法解析：'+rule.message)
            if rule.type=='at-rule' and rule.lower_at_keyword=='charset':continue
            if rule.type=='at-rule' and rule.lower_at_keyword=='import':
                meaningful=[(i,t) for i,t in enumerate(rule.prelude) if t.type not in {'whitespace','comment'}]
                if not meaningful:raise ValueError('CSS @import 缺少路径')
                index,token=meaningful[0]
                rule.prelude[index]=self._token(self.stylesheet(self._url(token),parent,encoding))
                # Media, layer and supports qualifiers remain in source order.
                tail=rule.prelude[index+1:]
                self._walk(tail,parent)
                rule.prelude[index+1:]=tail
            else:
                if hasattr(rule,'prelude'):self._walk(rule.prelude,parent)
                if getattr(rule,'content',None) is not None:self._walk(rule.content,parent)
            result.append(rule)
        return tinycss2.serialize(result)

    def stylesheet(self,url,parent=None,encoding=None):
        if url.lower().startswith('data:'):
            header,separator,body=url.partition(',')
            if not separator or header[5:].split(';',1)[0].lower()!='text/css':
                raise ValueError('内嵌样式表必须使用 text/css')
            try:data=base64.b64decode(body,validate=True) if header.lower().endswith(';base64') else unquote_to_bytes(body)
            except ValueError as error:raise ValueError('内嵌 CSS 编码无效') from error
            key=url
        else:
            if urlsplit(url).fragment:raise ValueError('样式表路径不支持片段')
            key=self.select(url,base=parent)
            data=self.records[key]
            parent=PurePosixPath(key).parent.as_posix()
        cache_key=(key,parent,getattr(encoding,'name',None))
        if cache_key in self.cache:return self.cache[cache_key]
        if key in self.active or len(self.active)>=32:
            raise ValueError('CSS 引用循环或超过32层')
        self.active.append(key)
        try:
            rules,used_encoding=tinycss2.parse_stylesheet_bytes(data,environment_encoding=encoding)
            rendered=self._rules(rules,parent,used_encoding)
            result=data_uri('text/css;charset=utf-8',rendered.encode('utf-8'))
            self.cache[cache_key]=result
            return result
        finally:self.active.pop()

    def inline(self,text,*,declarations=False):
        if declarations:
            tokens=tinycss2.parse_component_value_list(text)
            self._walk(tokens,None)
            return tinycss2.serialize(tokens)
        return self._rules(tinycss2.parse_stylesheet(text),None)
