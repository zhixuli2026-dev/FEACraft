"""Parse conforming HTML image candidates without splitting data URI commas."""
import math
import re

SPACE=' \t\n\r\f'
FLOAT=re.compile(r'(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z')

def candidates(value):
    result=[];position=0;kind=None;seen=set()
    while position<len(value):
        while position<len(value) and value[position] in SPACE:position+=1
        if position==len(value):break
        start=position
        while position<len(value) and value[position] not in SPACE:position+=1
        url=value[start:position]
        descriptor=''
        if url.endswith(','):
            url=url[:-1]
            if not url or url.endswith(','):raise ValueError('srcset 包含空候选图片')
        else:
            start=position
            while position<len(value) and value[position]!=',':position+=1
            descriptor=value[start:position].strip(SPACE)
            if position<len(value):position+=1
        if not url or url.startswith(','):raise ValueError('srcset 包含空候选图片')
        if not descriptor:current='x';number=1
        elif descriptor.endswith('w') and re.fullmatch(r'[0-9]+',descriptor[:-1]):
            current='w';number=int(descriptor[:-1])
        elif descriptor.endswith('x') and FLOAT.fullmatch(descriptor[:-1]):
            current='x';number=float(descriptor[:-1])
        else:raise ValueError('srcset 描述符必须是单一正数 x 或正整数 w')
        if number<=0 or (current=='x' and not math.isfinite(number)):
            raise ValueError('srcset 描述符必须是有限正数')
        if kind is not None and kind!=current:raise ValueError('srcset 不能混用 w 与 x 描述符')
        if number in seen:raise ValueError('srcset 候选描述符重复')
        seen.add(number);kind=current;result.append((url,descriptor))
    if not result:raise ValueError('srcset 不能为空')
    return result

def rewrite(value, resource):
    return ', '.join(resource(url)+((' '+descriptor) if descriptor else '') for url,descriptor in candidates(value))

def docx_image_fallbacks(document):
    """Pandoc ignores raw HTML in DOCX; retain each img's explicit fallback.

    Responsive candidates remain in HTML. A static document uses src, or the
    first candidate when src is absent; no screen geometry is invented.
    """
    from html.parser import HTMLParser
    class Images(HTMLParser):
        def __init__(self):super().__init__();self.images=[];self.picture_sources=[];self.in_picture=False
        def handle_starttag(self,tag,attrs):
            attrs=dict(attrs)
            if tag=='picture':self.in_picture=True;self.picture_sources=[]
            if tag=='source' and self.in_picture and attrs.get('srcset'):
                self.picture_sources.extend(candidates(attrs['srcset']))
            if tag!='img':return
            source=attrs.get('src')
            if not source and attrs.get('srcset'):source=candidates(attrs['srcset'])[0][0]
            if not source and self.picture_sources:source=self.picture_sources[0][0]
            if not source:raise ValueError('DOCX 图片缺少 src 或候选图片')
            attributes=[[key,attrs[key]] for key in ('width','height') if attrs.get(key)]
            self.images.append({'t':'Image','c':[[attrs.get('id') or '',[],attributes],
                [{'t':'Str','c':attrs.get('alt') or ''}],[source,attrs.get('title') or '']]})
        def handle_startendtag(self,tag,attrs):self.handle_starttag(tag,attrs)
        def handle_endtag(self,tag):
            if tag=='picture':self.in_picture=False;self.picture_sources=[]
    def visit(value):
        if isinstance(value,list):return [visit(item) for item in value]
        if not isinstance(value,dict):return value
        if value.get('t') in {'RawBlock','RawInline'} and value['c'][0]=='html':
            parser=Images();parser.feed(value['c'][1]);parser.close()
            if parser.images:
                return {'t':'Para','c':parser.images} if value['t']=='RawBlock' else {'t':'Span','c':[['',[],[]],parser.images]}
        return {key:visit(item) for key,item in value.items()}
    return visit(document)
