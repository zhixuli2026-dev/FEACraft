"""Actual report API, Pandoc DOCX and LibreOffice/PDF rendering. No LLM."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
import httpx
from pypdf import PdfReader

ROOT=Path(__file__).resolve().parents[2]
W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKG='http://schemas.openxmlformats.org/package/2006/relationships'
CT='http://schemas.openxmlformats.org/package/2006/content-types'
ET.register_namespace('w',W);ET.register_namespace('r',REL)
def w(name):return '{'+W+'}'+name
def digest(data):return hashlib.sha256(data).hexdigest()
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf8')
def template():
    original=ROOT/'tmp/docker-deployment-20261117/docx-math-064c29af78/actual-report.docx'
    with zipfile.ZipFile(original) as z:parts={n:z.read(n) for n in z.namelist()}
    document=ET.fromstring(parts['word/document.xml']);section=document.find('.//'+w('sectPr'));assert section is not None
    margins=section.find(w('pgMar'))
    if margins is None:margins=ET.SubElement(section,w('pgMar'))
    for k,v in {'top':'1440','bottom':'1440','left':'1440','right':'1440','header':'720','footer':'720','gutter':'0'}.items():margins.set(w(k),v)
    styles=ET.fromstring(parts['word/styles.xml'])
    for style in styles.findall(w('style')):
        if style.get(w('styleId'))=='Heading1':
            properties=style.find(w('rPr'))
            if properties is None:properties=ET.SubElement(style,w('rPr'))
            color=properties.find(w('color'))
            if color is None:color=ET.SubElement(properties,w('color'))
            color.set(w('val'),'8C245B')
    rels=ET.fromstring(parts['word/_rels/document.xml.rels']);types=ET.fromstring(parts['[Content_Types].xml'])
    for kind,root_tag,label in [('header','hdr','VF-LAYOUT-V1 · 排版核验'),('footer','ftr','VF-PAGE ')]:
        rid='vfOffice'+kind
        ET.SubElement(rels,'{'+PKG+'}Relationship',{'Id':rid,'Type':REL+'/'+kind,'Target':kind+'1.xml'})
        ET.SubElement(section,w(kind+'Reference'),{w('type'):'default','{'+REL+'}id':rid})
        ET.SubElement(types,'{'+CT+'}Override',{'PartName':'/word/'+kind+'1.xml','ContentType':'application/vnd.openxmlformats-officedocument.wordprocessingml.'+kind+'+xml'})
        part=ET.Element(w(root_tag));paragraph=ET.SubElement(part,w('p'));run=ET.SubElement(paragraph,w('r'));text=ET.SubElement(run,w('t'));text.text=label;text.set('{http://www.w3.org/XML/1998/namespace}space','preserve')
        if kind=='footer':
            field=ET.SubElement(paragraph,w('fldSimple'),{w('instr'):' PAGE '});ET.SubElement(ET.SubElement(field,w('r')),w('t')).text='1'
        parts['word/'+kind+'1.xml']=ET.tostring(part,encoding='utf-8',xml_declaration=True)
    for name,node in [('word/document.xml',document),('word/styles.xml',styles),('word/_rels/document.xml.rels',rels),('[Content_Types].xml',types)]:
        # Pandoc 3.1 copies reference relationship elements without adding
        # arbitrary prefixes to its output root. Use normal OOXML defaults.
        if node is rels:ET.register_namespace('',PKG)
        elif node is types:ET.register_namespace('',CT)
        parts[name]=ET.tostring(node,encoding='utf-8',xml_declaration=True)
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as z:
        for name,data in parts.items():z.writestr(name,data)
    return output.getvalue()

def prepare(directory,result):
    account={'email':'office-'+secrets.token_hex(6)+'@example.com','password':secrets.token_hex(24),'name':'Office layout acceptance'}
    (directory/'private').mkdir();write(directory/'private/account.json',account)
    with httpx.Client(base_url='http://127.0.0.1:62883/api/v1',trust_env=False,timeout=180) as client:
        def api(method,path,**kwargs):
            response=client.request(method,path,**kwargs);response.raise_for_status();return response.json()
        token=api('POST','/auth/register',json=account)['token'];client.headers['Authorization']='Bearer '+token
        workspace=api('GET','/workspaces')[0]['id'];case=api('POST',f'/workspaces/{workspace}/cases',json={'name':'Office layout '+secrets.token_hex(4)})
        result['case_id']=case['id'];result['case_name']=case['name'];prefix=f"/cases/{case['id']}"
        reference=template();(directory/'reference.docx').write_bytes(reference)
        api('POST',prefix+'/upload',params={'path':'E-report/templates/office.docx'},files={'file':('office.docx',reference,'application/vnd.openxmlformats-officedocument.wordprocessingml.document')})
        svg='<svg xmlns="http://www.w3.org/2000/svg" width="400" height="160"><rect x="5" y="5" width="390" height="150" fill="#e9f0f6" stroke="#135c72"/><path d="M25 135L120 40L240 80L375 25" stroke="#8C245B" fill="none" stroke-width="5"/></svg>'
        api('PUT',prefix+'/file',json={'path':'E-report/plot.svg','content':svg})
        markdown='''---
title: "复杂文档实际排版"
reference-doc: templates/office.docx
---

# 排版内容 {#overview}

中文与 Latin 文本、**加粗**、*斜体*、行内公式 $x_i^2$。本文件为排版算例，数值不是实际工程结论。

1. 第一级有序列表
   - 嵌套项目甲
   - 嵌套项目乙
2. 第二项

> 引用段落保留语义。

```python
temperature = 300
print(temperature)
```

正文含脚注[^note]及[结果章节](#results)引用。

[^note]: FOOTNOTE-ORIGINAL 中文脚注；公式 $E=mc^2$。

![FIGURE-ORIGINAL 示意图](plot.svg)

$$T(x)=T_0+\\frac{qL}{k}\\left(1-\\frac{x}{L}\\right)$$

# 连续表格

| 编号 | 数值 | 说明 |
|:---|---:|:---|
'''
        for i in range(1,101):markdown+=f'| ROW-{i:03d} | {i*7} | 中文表格内容 {i} |\n'
        markdown+='\n# 结果章节 {#results}\n\nEND-OF-REPORT 内容完整。\n'
        (directory/'report.md').write_text(markdown,'utf8')
        api('PUT',prefix+'/file',json={'path':'E-report/report.md','content':markdown})
        submitted=api('POST',prefix+'/report:build',json={});result['job_id']=submitted['job_id'];write(directory/'result.json',result)
        deadline=time.monotonic()+180
        while True:
            job=api('GET','/jobs/'+str(result['job_id']))
            if job['status'] not in ['queued','running']:break
            assert time.monotonic()<deadline,'Report timed out';time.sleep(.3)
        write(directory/'job.json',job);assert job['status']=='succeeded' and job['attempt']['process_stop']['verified']
        publication=api('GET',prefix+'/report/results');write(directory/'publication.json',publication)
        assert publication['manifest']['inputs']['E-report/templates/office.docx']['sha256']==digest(reference)
        result['publication_id']=publication['publication']['id'];result['artifacts']={}
        for ext in ['html','docx']:
            response=client.get(prefix+'/report/artifact',params={'publication_id':result['publication_id'],'path':'E-report/build/report.'+ext});response.raise_for_status()
            (directory/('report.'+ext)).write_bytes(response.content);result['artifacts'][ext]=digest(response.content)
        with zipfile.ZipFile(directory/'report.docx') as z:
            for name in z.namelist():
                if name.endswith(('.xml','.rels')):ET.fromstring(z.read(name))
            assert any(name.startswith('word/header') for name in z.namelist())
            assert any(name.startswith('word/footer') for name in z.namelist())
        result['prepared']=True;result['checks'].append('actual_Pandoc_DOCX_with_template_header_footer_table_100_rows_math_SVG_footnote_and_frozen_reference')

def render(directory,result):
    assert result['prepared']
    output=directory/'office';output.mkdir()
    command=['docker','run','--rm','--network','none','--mount',f'type=bind,source={directory.resolve()},target=/input,readonly','--mount',f'type=bind,source={output.resolve()},target=/output','vibeflow/office-renderer:20261117','libreoffice','-env:UserInstallation=file:///tmp/office-profile','--headless','--convert-to','pdf:writer_pdf_Export','--outdir','/output','/input/report.docx']
    process=subprocess.run(command,capture_output=True,text=True,encoding='utf8',timeout=180)
    (directory/'office-render.log').write_text(process.stdout+'\n'+process.stderr,'utf8');assert process.returncode==0
    pdf=output/'report.pdf';assert pdf.exists()
    document=PdfReader(pdf);texts=[p.extract_text() for p in document.pages];all_text='\n'.join(texts)
    write(directory/'pdf-text.json',texts);assert len(texts)>=3
    for i in range(1,101):assert f'ROW-{i:03d}' in all_text,f'Missing row {i}'
    for text in ['FOOTNOTE-ORIGINAL','FIGURE-ORIGINAL','END-OF-REPORT','复杂文档实际排版','中文表格内容']:assert text in all_text,text
    for i,text in enumerate(texts,1):
        assert 'VF-LAYOUT-V1' in text,f'Missing header {i}'
        assert f'VF-PAGE {i}' in text,f'Missing page number {i}'
    assert digest((directory/'report.docx').read_bytes())==result['artifacts']['docx']
    sys.path.insert(0,str(ROOT/'tmp/office-render-tools'))
    import pypdfium2 as pdfium
    rendered=pdfium.PdfDocument(str(pdf))
    for i in sorted({0,1,len(rendered)-1}):
        page=rendered[i];bitmap=page.render(scale=1.3);bitmap.to_pil().save(output/f'page-{i+1}.png');bitmap.close();page.close()
    rendered.close()
    result['office']={'pages':len(texts),'pdf_sha256':digest(pdf.read_bytes()),'version':subprocess.check_output(['docker','run','--rm','--network','none','vibeflow/office-renderer:20261117','libreoffice','--version'],text=True).strip()}
    result['checks'].append('actual_LibreOffice_PDF_pages_all_100_rows_Chinese_footnote_caption_repeated_headers_page_numbers_and_original_bytes')
    result['passed']=True

def main():
    parser=argparse.ArgumentParser();parser.add_argument('review');parser.add_argument('--render');args=parser.parse_args()
    review=json.loads((ROOT/args.review).read_text('utf8'));assert review['classification']=='no_llm'
    for name,h in review['sources'].items():assert digest((ROOT/name).read_bytes())==h,name
    directory=ROOT/args.render if args.render else ROOT/'tmp/docker-deployment-20261117'/('office-layout-'+secrets.token_hex(5))
    if args.render:result=json.loads((directory/'result.json').read_text('utf8'))
    else:directory.mkdir();result={'passed':False,'prepared':False,'checks':[],'real_model_calls':0,'mock_llm_calls':0}
    try:
        if args.render:render(directory,result)
        else:prepare(directory,result)
    except Exception as error:result['error']=repr(error);raise
    finally:write(directory/'result.json',result);print('OFFICE_EVIDENCE',directory.relative_to(ROOT))
    print(json.dumps({k:result.get(k) for k in ['passed','prepared','checks','case_id','job_id','office']},ensure_ascii=False))
if __name__=='__main__':main()
