"""Actual S3/worker postprocessing and template export from verified solver files."""
import argparse
import base64
import hashlib
import io
import json
import math
from pathlib import Path
import secrets
import re
import sqlite3
import subprocess
import sys
import time
import zipfile
import xml.etree.ElementTree as ET

import httpx

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'platform/server'))
from app.services.casedict import set_block

def sha(data):return hashlib.sha256(data).hexdigest()
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf8')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('review');parser.add_argument('engineering_evidence');args=parser.parse_args()
    review=json.loads((ROOT/args.review).read_text('utf8'));assert review['classification']=='no_llm'
    for path,digest in review['sources'].items():assert sha((ROOT/path).read_bytes())==digest,path
    origin=(ROOT/args.engineering_evidence).resolve();assert origin.is_relative_to(ROOT/'tmp')
    source_result=json.loads((origin/'benchmark.json').read_text('utf8'))
    assert source_result['passed'] and source_result['original_numerical_thresholds_preserved']
    solve=source_result['lineage']['solve']
    with sqlite3.connect('file:'+(origin/'test.sqlite').as_posix()+'?mode=ro',uri=True) as db:
        frozen=json.loads(db.execute('select input_manifest_json from job_attempts where id=?',(solve['attempt_id'],)).fetchone()[0])['workspace']
    case_root=next((origin/'files/workspaces').glob('*/cases/1'))
    source=(case_root/frozen['run_path']/'C-run/conduction').resolve()
    assert source.is_relative_to(case_root) and (source/'1/T').is_file()
    directory=ROOT/'tmp'/('engineering-s3-'+secrets.token_hex(5));(directory/'private').mkdir(parents=True)
    result={'passed':False,'checks':[],'jobs':[],'real_model_calls':0,'mock_llm_calls':0,
        'origin':args.engineering_evidence,'origin_benchmark_sha256':sha((origin/'benchmark.json').read_bytes()),
        'original_solve':solve,'numerical':source_result['numerical'],'review':args.review}
    def save():write(directory/'result.json',result)
    def check(name):result['checks'].append(name);save();print('ENGINEERING_S3_PASS '+name,flush=True)
    print('ENGINEERING_S3_EVIDENCE '+str(directory),flush=True)
    try:
        with httpx.Client(base_url='http://127.0.0.1:57700/api/v1',trust_env=False,timeout=120) as client:
            def api(method,path,**kwargs):
                until=time.monotonic()+30
                while True:
                    response=client.request(method,path,**kwargs)
                    detail=response.json().get('detail') if response.status_code==409 else None
                    if not isinstance(detail,dict) or detail.get('code')!='storage_quota_busy':break
                    assert time.monotonic()<until,'Storage admission stayed busy';time.sleep(.2)
                assert response.status_code==200,(path,response.status_code,response.text[:1200])
                return response.json()
            account={'email':'engineering-s3-'+secrets.token_hex(5)+'@example.com','password':secrets.token_hex(24),'name':'Engineering S3 acceptance'}
            write(directory/'private/account.json',account)
            client.headers['Authorization']='Bearer '+api('POST','/auth/register',json=account)['token']
            ws=api('GET','/workspaces')[0]['id'];case=api('POST',f'/workspaces/{ws}/cases',json={'name':'Actual thermal report templates'})
            result['case_id']=case['id'];prefix='/cases/'+str(case['id']);save()
            current=api('GET',prefix+'/caseDict');content=current['content']
            for block,value in {
                'caseMeta':{'discipline':'thermal','analysisType':'steady','dimensions':'3D','units':{'length':'m','temperature':'K'}},
                'postProcessing':{'runDir':'C-run/conduction','fields':{'T':{'unit':'K'}},
                    'dataExtraction':{'volumeAverages':{'mean':{'field':'T','time':1}}},
                    'derivedQuantities':{'referenceValues':{'linear_solution':{'metric':'"mean.T@1"',
                        'value':350,'unit':'K','kind':'verification','source':'"analytic T(x)=300+100*x"','l2_max':1e-6}}}},
                'report':{'referenceDoc':'"templates/reference.docx"','css':['"styles/main.css"','"styles/extra.css"']},
            }.items():content=set_block(content,block,value)
            api('PUT',prefix+'/caseDict',json={'content':content,'expected_revision':current['dict_revision']})
            inputs={}
            def upload(path,data):
                api('POST',prefix+'/upload',params={'path':path},files={'file':(Path(path).name,data,'application/octet-stream')})
                inputs[path]=sha(data)
            for path in sorted(source.rglob('*')):
                if path.is_file():upload('C-run/conduction/'+path.relative_to(source).as_posix(),path.read_bytes())
            reference=(ROOT/'tmp/docker-deployment-20261117/office-layout-f59d586df8/reference.docx').read_bytes()
            upload('E-report/templates/reference.docx',reference)
            for path,text in {'E-report/styles/main.css':'@import "nested/theme.css";',
                'E-report/styles/nested/theme.css':'h1 { color: #135c72; }',
                'E-report/styles/extra.css':'table { border-collapse: collapse; }'}.items():
                api('PUT',prefix+'/file',json={'path':path,'content':text});inputs[path]=sha(text.encode())
            result['uploaded_sha256']=inputs;save()
            def wait_job(jid):
                result['jobs'].append(jid);save();until=time.monotonic()+240
                while time.monotonic()<until:
                    job=api('GET','/jobs/'+str(jid))
                    if job['status'] not in ['queued','running']:
                        write(directory/f'job-{jid}.json',job)
                        assert job['status']=='succeeded',(jid,job.get('completion_error'),job.get('log_backlog','')[-1200:])
                        assert job['attempt']['process_stop']['verified']
                        assert all(c['stop']['verified'] for c in job['attempt']['child_processes']['children'].values())
                        return job
                    time.sleep(.3)
                raise TimeoutError('Actual worker job did not finish: '+str(jid))
            post=wait_job(api('POST',prefix+'/postprocess:run',json={'run_dir':'C-run/conduction'})['job_id'])
            extracted=api('GET',prefix+'/postprocess/results')
            assert extracted['metrics']['readiness']['report_ready'] and extracted['metrics']['readiness']['can_claim_verification']
            assert not extracted['metrics']['readiness']['can_claim_validation']
            write(directory/'postprocess.json',extracted)
            check('real_solver_files_uploaded_to_S3_independent_worker_VTK_extracts_verified_temperature')
            draft=api('POST',prefix+'/report:compose',params={'wait':'false'},json={'overwrite':True})
            wait_job(draft['job_id'])
            composed=api('GET',prefix+'/report/compose-result',params={'publication_id':draft['publication_id']})
            assert composed['content'].startswith('---\nreference-doc: "templates/reference.docx"\n')
            assert 'css: ["styles/main.css", "styles/extra.css"]' in composed['content'] and '| mean.T@1 | volumeAverages |' in composed['content']
            report=wait_job(api('POST',prefix+'/report:build',json={})['job_id'])
            publication=api('GET',prefix+'/report/results');assert not publication['publication']['stale']
            result['publication_id']=publication['publication']['id'];write(directory/'publication.json',publication)
            for path,digest in inputs.items():
                if path.startswith('E-report/'):
                    assert publication['manifest']['inputs'][path]['sha256']==digest,path
            for ext in ['html','docx']:
                response=client.get(prefix+'/report/artifact',params={'publication_id':result['publication_id'],'path':'E-report/build/report.'+ext})
                assert response.status_code==200;data=response.content;(directory/('report.'+ext)).write_bytes(data)
                if ext=='html':
                    text=data.decode();styles=text
                    for _ in range(4):
                        styles+='\n'+'\n'.join(base64.b64decode(v).decode() for v in re.findall(r'data:text/css;charset=utf-8;base64,([A-Za-z0-9+/=]+)',styles))
                    assert '#135c72' in styles
                    row=re.search(r'<td>mean.T@1</td>\s*<td>volumeAverages</td>\s*<td>[^<]+</td>\s*<td>([^<]+)</td>',text)
                    assert row and math.isclose(float(row[1]),350,rel_tol=0,abs_tol=1e-6)
                else:
                    with zipfile.ZipFile(io.BytesIO(data)) as z:
                        assert b'8C245B' in z.read('word/styles.xml')
                        ns={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
                        rows=[[''.join(c.itertext()) for c in row.findall('w:tc',ns)] for row in ET.fromstring(z.read('word/document.xml')).findall('.//w:tr',ns)]
                        metric=next(row for row in rows if row[:2]==['mean.T@1','volumeAverages'])
                        assert math.isclose(float(metric[3]),350,rel_tol=0,abs_tol=1e-6)
            host=subprocess.run(['docker','exec','vf-report-s3-20261117-arq-worker','hostname'],capture_output=True,text=True,check=True).stdout.strip()
            owners=subprocess.run(['docker','exec','vf-report-s3-20261117-postgres','psql','-U','vibeflow','-d','vibeflow','-tAc',
                'SELECT worker_id FROM job_attempts WHERE job_id IN ('+','.join(map(str,result['jobs']))+')'],capture_output=True,text=True,check=True).stdout.splitlines()
            assert len(owners)==3 and all(value.startswith(host+':') for value in owners)
            result['worker_host']=host
            check('generated_engineering_draft_and_HTML_DOCX_templates_frozen_on_independent_S3_worker')
            result['passed']=True;save()
    except BaseException as error:
        result['failure']={'type':type(error).__name__,'message':str(error)[:1800]};save();raise

if __name__=='__main__':main()
