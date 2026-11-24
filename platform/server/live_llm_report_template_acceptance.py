"""Actual frozen DOCX templates, version changes and authorized DeepSeek readback."""
import argparse
import asyncio
import hashlib
import io
import json
from pathlib import Path
import secrets
import time
import xml.etree.ElementTree as ET
import zipfile
import httpx
from live_llm_acceptance import source

ROOT=Path(__file__).resolve().parents[2]
W='http://schemas.openxmlformats.org/wordprocessingml/2006/main'
def digest(value): return hashlib.sha256(value).hexdigest()

def reference_docx(original, color, margin):
    ET.register_namespace('w', W)
    data={}
    with zipfile.ZipFile(io.BytesIO(original)) as archive:
        data={name:archive.read(name) for name in archive.namelist()}
    styles=ET.fromstring(data['word/styles.xml'])
    heading=next(e for e in styles.findall('{'+W+'}style') if e.get('{'+W+'}styleId')=='Heading1')
    props=heading.find('{'+W+'}rPr')
    if props is None:props=ET.SubElement(heading,'{'+W+'}rPr')
    element=props.find('{'+W+'}color')
    if element is None:element=ET.SubElement(props,'{'+W+'}color')
    element.clear();element.set('{'+W+'}val',color)
    document=ET.fromstring(data['word/document.xml'])
    section=document.find('.//{'+W+'}sectPr');assert section is not None
    margins=section.find('{'+W+'}pgMar')
    if margins is None:
        margins=ET.SubElement(section,'{'+W+'}pgMar',{'{'+W+'}'+key:str(value) for key,value in
            {'top':1440,'right':1440,'bottom':1440,'left':1440,'header':720,'footer':720,'gutter':0}.items()})
    margins.set('{'+W+'}left',str(margin))
    data['word/styles.xml']=ET.tostring(styles,encoding='utf-8',xml_declaration=True)
    data['word/document.xml']=ET.tostring(document,encoding='utf-8',xml_declaration=True)
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,value in data.items():archive.writestr(name,value)
    return output.getvalue()

def inspect_docx(data,color,margin,marker):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        styles=ET.fromstring(archive.read('word/styles.xml'))
        heading=next(e for e in styles.findall('{'+W+'}style') if e.get('{'+W+'}styleId')=='Heading1')
        assert heading.find('{'+W+'}rPr/{'+W+'}color').get('{'+W+'}val')==color
        document=ET.fromstring(archive.read('word/document.xml'))
        assert document.find('.//{'+W+'}sectPr/{'+W+'}pgMar').get('{'+W+'}left')==str(margin)
        assert marker in ''.join(document.itertext())

def verify():
    directory=ROOT/'tmp/docker-deployment-20261117'/('reference-doc-'+secrets.token_hex(5))
    (directory/'private').mkdir(parents=True)
    result={'passed':False,'checks':[],'jobs':[],'real_model_application_requests':0,'mock_llm_calls':0,'claude_calls':0}
    secret_values=[]
    def write(name,data):
        text=json.dumps(data,ensure_ascii=False,indent=2)
        assert all(secret not in text for secret in secret_values)
        (directory/name).write_text(text,'utf8')
    def save():write('result.json',result)
    def check(name):result['checks'].append(name);save();print('REFERENCE_DOC_PASS '+name,flush=True)
    print('REFERENCE_DOC_EVIDENCE '+str(directory),flush=True)
    try:
        with httpx.Client(base_url='http://127.0.0.1:62883/api/v1',trust_env=False,timeout=180) as client:
            def call(method,path,body=None):
                response=client.request(method,path,json=body)
                assert response.status_code==200,path+' HTTP '+str(response.status_code)+' '+response.text[:400]
                return response.json()
            account={'email':'reference-'+secrets.token_hex(8)+'@example.com','password':secrets.token_hex(24),'name':'Reference template acceptance'}
            secret_values.append(account['password']);(directory/'private/account.json').write_text(json.dumps(account),'utf8')
            auth=call('POST','/auth/register',account);secret_values.append(auth['token']);client.headers['Authorization']='Bearer '+auth['token']
            ws=call('GET','/workspaces')[0]['id']
            case=call('POST',f'/workspaces/{ws}/cases',{'name':'Actual DOCX templates'})
            result['case_id']=case['id'];prefix=f'/cases/{case["id"]}'
            template='E-report/templates/brand reference.docx'
            marker='TEMPLATE_'+secrets.token_hex(12)
            markdown='---\nreference-doc: "templates/brand reference.docx"\n---\n\n# 品牌样式报告\n\n'+marker+'\n\n## 方法\n\n模板读取及发布版本验收。\n'
            def put(path,content):return call('PUT',prefix+'/file',{'path':path,'content':content})
            def upload(data):
                response=client.post(prefix+'/upload',params={'path':template},files={'file':('reference.docx',data,'application/vnd.openxmlformats-officedocument.wordprocessingml.document')})
                assert response.status_code==200,response.text[:300]
            def artifact(publication,ext):
                response=client.get(prefix+'/report/artifact',params={'path':'E-report/build/report.'+ext,'publication_id':publication})
                assert response.status_code==200,response.text[:300];return response.content
            def build():
                submitted=call('POST',prefix+'/report:build',{});job_id=submitted['job_id'];result['jobs'].append(job_id);save()
                deadline=time.monotonic()+180
                while True:
                    job=call('GET',f'/jobs/{job_id}')
                    if job['status'] not in {'queued','running'}:break
                    if time.monotonic()>deadline:raise TimeoutError('Actual report still running')
                    time.sleep(.3)
                write('job-'+str(job_id)+'.json',job)
                assert job['status']=='succeeded',job['status']
                assert job['attempt']['process_stop']['verified']
                return call('GET',prefix+'/report/results')
            put('E-report/report.md',markdown)
            missing=client.post(prefix+'/report:build',json={})
            assert missing.status_code in {400,404},str(missing.status_code)
            upload(b'not a zip archive')
            malformed=client.post(prefix+'/report:build',json={})
            assert malformed.status_code==400,str(malformed.status_code)
            assert call('GET',prefix+'/jobs')==[]
            result['negative_statuses']=[missing.status_code,malformed.status_code]
            check('missing_and_malformed_template_rejected_before_job_creation')
            original=(ROOT/'tmp/docker-deployment-20261117/docx-math-064c29af78/actual-report.docx').read_bytes()
            first_reference=reference_docx(original,'8C245B',1877)
            malformed_zip=io.BytesIO()
            with zipfile.ZipFile(io.BytesIO(first_reference)) as source_zip, zipfile.ZipFile(malformed_zip,'w',zipfile.ZIP_DEFLATED) as output_zip:
                for name in source_zip.namelist():
                    output_zip.writestr(name,b'<broken:styles/>' if name=='word/styles.xml' else source_zip.read(name))
            upload(malformed_zip.getvalue())
            bad_style=client.post(prefix+'/report:build',json={});assert bad_style.status_code==400
            assert call('GET',prefix+'/jobs')==[]
            check('malformed_style_XML_rejected_before_job_creation')
            upload(first_reference)
            first=build();first_id=first['publication']['id'];first_docx=artifact(first_id,'docx')
            inspect_docx(first_docx,'8C245B',1877,marker)
            assert first['manifest']['inputs'][template]['sha256']==digest(first_reference)
            (directory/'first.docx').write_bytes(first_docx);write('first-publication.json',first)
            check('actual_DOCX_heading_color_page_margin_and_template_frozen_in_manifest')
            second_reference=reference_docx(original,'17634B',2333);upload(second_reference)
            stale=call('GET',prefix+'/report/results');assert stale['publication']['stale']
            assert artifact(first_id,'docx')==first_docx
            second=build();second_id=second['publication']['id'];second_docx=artifact(second_id,'docx')
            assert second_id!=first_id;inspect_docx(second_docx,'17634B',2333,marker)
            assert second['manifest']['inputs'][template]['sha256']==digest(second_reference)
            assert artifact(first_id,'docx')==first_docx
            (directory/'second.docx').write_bytes(second_docx);write('second-publication.json',second)
            result['publications']=[first_id,second_id]
            result['artifact_sha256']=[digest(first_docx),digest(second_docx)]
            check('changed_template_marks_old_source_stale_new_version_uses_new_style_old_bytes_retained')
            put('E-report/report.md',markdown.replace('templates/brand reference.docx','https://example.com/remote.docx'))
            remote=client.post(prefix+'/report:build',json={});assert remote.status_code==400
            assert len(call('GET',prefix+'/jobs'))==2
            assert artifact(second_id,'docx')==second_docx
            put('E-report/report.md',markdown)
            check('remote_template_rejected_and_successful_publication_preserved')
            config=asyncio.run(source(26));secret_values.append(config['key'])
            profile=call('POST','/provider-profiles',{'provider':'deepseek','name':'Report template verification','base_url':config['base_url'],
                'default_model':config['model'],'api_key':config['key']})
            conversation=call('POST','/conversations',{'case_id':case['id'],'title':'Read template report','provider_profile_id':profile['id'],
                'model':config['model'],'permission_mode':'plan'})
            result['model']={'provider':'deepseek','host':'api.deepseek.com','model':config['model'],'upstream_HTTP_count':'not instrumented'}
            events=[];result['real_model_application_requests']+=1;save()
            with client.stream('POST',f'/conversations/{conversation["id"]}/messages',json={
                'content':'请调用 read_text_file 读取 E-report/build/report.html，找到以 TEMPLATE_ 开头的验证字符串并原样回复。不要猜测，不要修改文件。',
                'provider_profile_id':profile['id'],'model':config['model']}) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if line.startswith('data:'):events.append(json.loads(line[5:]))
            write('live-model-events.json',events)
            answer=''.join(e.get('text','') for e in events if e.get('type')=='text.delta')
            assert marker in answer and any(e.get('type')=='tool.result' for e in events)
            assert any(e.get('type')=='run.completed' for e in events)
            assert not any(e.get('type') in {'stream.error','request.failed','request.timed_out'} for e in events)
            saved=call('GET',f'/conversations/{conversation["id"]}')
            assert any(e['type']=='tool_result' and marker in str(e['payload']) for e in saved['events'])
            result['model']['answer']=answer
            check('actual_DeepSeek_tool_reads_generated_report_and_persists_result')
            result['passed']=True
    except BaseException as error:
        detail=str(error)
        for secret in secret_values:detail=detail.replace(secret,'<redacted>')
        result['failure']={'type':type(error).__name__,'detail':detail[:1500]};save()
        print('REFERENCE_DOC_FAILED '+json.dumps(result['failure'],ensure_ascii=False),flush=True)
        return 1
    finally:save()
    print(json.dumps({'passed':True,'evidence':str(directory),'checks':len(result['checks'])}),flush=True)
    return 0

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--review',type=Path,required=True);args=parser.parse_args()
    review=json.loads(args.review.read_text('utf8'));assert review['classification']=='real_llm'
    assert review['entry']=='platform/server/live_llm_report_template_acceptance.py'
    for path,value in review['sources'].items():assert digest((ROOT/path).read_bytes())==value,path
    raise SystemExit(verify())
