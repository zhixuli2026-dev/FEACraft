"""Actual S3 report resources with separate API/worker volumes; no model calls."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import secrets
import struct
import subprocess
import time
import zipfile
import zlib
import httpx

ROOT=Path(__file__).resolve().parents[2]
DEPLOY=ROOT/'tmp/report-s3-20261117'
def sha(data):return hashlib.sha256(data).hexdigest()
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf8')
def command(*args):
    p=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,encoding='utf8',timeout=120)
    assert p.returncode==0,'Command failed: '+' '.join(args[:4])
    return p.stdout.strip()
def png(width,height,color):
    def chunk(kind,data):return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
    pixels=(b'\0'+bytes(color)*width)*height
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(pixels))+chunk(b'IEND',b'')

def main():
    parser=argparse.ArgumentParser();parser.add_argument('review');args=parser.parse_args()
    review=json.loads((ROOT/args.review).read_text('utf8'));assert review['classification']=='no_llm'
    for name,digest in review['sources'].items():assert sha((ROOT/name).read_bytes())==digest,name
    deployment=json.loads((DEPLOY/'deployment.json').read_text('utf8'));assert deployment['status']=='healthy'
    base=deployment['base_url'];project=deployment['project'];assert project=='vf-report-s3-20261117'
    directory=DEPLOY/('resources-'+secrets.token_hex(5));(directory/'private').mkdir(parents=True)
    result={'passed':False,'checks':[],'jobs':[],'base_url':base,'real_model_calls':0,'mock_llm_calls':0,'review':args.review}
    def save():write(directory/'result.json',result)
    def check(label):result['checks'].append(label);save();print('S3_REPORT_PASS '+label,flush=True)
    worker_stopped=False
    try:
        with httpx.Client(base_url=base+'/api/v1',trust_env=False,timeout=180) as client:
            def api(method,path,**kwargs):
                deadline=time.monotonic()+15
                while True:
                    r=client.request(method,path,**kwargs)
                    detail=r.json().get('detail') if r.status_code==409 else None
                    if not isinstance(detail,dict) or detail.get('code')!='storage_quota_busy':break
                    result.setdefault('admission_retries',[]).append({'method':method,'path':path,'code':'storage_quota_busy'})
                    save()
                    assert time.monotonic()<deadline,'Owner storage admission did not settle'
                    time.sleep(.2)
                if r.status_code!=200:
                    result['last_failure']={'path':path,'status':r.status_code,'body':r.text[:1500]};save()
                assert r.status_code==200,f'{method} {path}: {r.status_code}'
                return r.json()
            account={'email':'s3-report-'+secrets.token_hex(6)+'@example.com','password':secrets.token_hex(24),'name':'S3 report acceptance'}
            write(directory/'private/account.json',account)
            client.headers['Authorization']='Bearer '+api('POST','/auth/register',json=account)['token']
            ws=api('GET','/workspaces')[0]['id'];case=api('POST',f'/workspaces/{ws}/cases',json={'name':'S3 resource combination'})
            result.update(case_id=case['id'],case_name=case['name']);prefix=f"/cases/{case['id']}"
            # Mount inspection is selective: never expose environment or command secrets.
            mounts={}
            for service in ['api','arq-worker']:
                mounts[service]=json.loads(command('docker','inspect',project+'-'+service,'--format','{{json .Mounts}}'))
            roots=[next(v['Name'] for v in mounts[s] if v['Destination']=='/data/files') for s in mounts]
            assert roots[0]!=roots[1];result['cache_volumes']=roots
            contents={}
            def put(path,text):
                api('PUT',prefix+'/file',json={'path':path,'content':text});contents[path]=text.encode()
            def upload(path,data):
                api('POST',prefix+'/upload',params={'path':path},files={'file':(Path(path).name,data,'application/octet-stream')});contents[path]=data
            reference=(ROOT/'tmp/docker-deployment-20261117/office-layout-f59d586df8/reference.docx').read_bytes()
            upload('E-report/templates/reference.docx',reference)
            upload('E-report/assets/font.woff2',(ROOT/'platform/web/node_modules/katex/dist/fonts/KaTeX_Main-Regular.woff2').read_bytes())
            upload('E-report/assets/small.png',png(160,80,[190,30,40]));upload('E-report/assets/large.png',png(320,160,[20,70,190]))
            put('E-report/styles/main.css','@import "nested/theme.css";\n@font-face {font-family: FrozenReport;src:url("../assets/font.woff2")}\n.frozen-font{font-family:FrozenReport}.plot{width:160px;height:80px;background-image:url("../assets/large.png")}')
            put('E-report/styles/nested/theme.css','h1{color:#135c72}')
            markdown='''---
title: S3 frozen resource report
reference-doc: templates/reference.docx
css: styles/main.css
---

# S3 resource combination {#resources}

Actual report resource check, not an engineering result. **Bold**, *emphasis*, math $x_i^2$.

<p class="frozen-font">FROZEN-S3-FONT</p>

<div class="plot"></div>

<img id="responsive" src="assets/small.png" srcset="assets/small.png 1x, assets/large.png 2x" width="160" alt="responsive">

| Item | Value |
|:---|---:|
| S3-ROW | 42 |

1. Parent
   - Nested

```python
value = 42
print(value)
```

[Resources](#resources)
'''
            put('E-report/report.md',markdown)
            def settle(jid,expected='succeeded'):
                deadline=time.monotonic()+240
                while True:
                    job=api('GET',f'/jobs/{jid}')
                    if job['status'] not in ['queued','running']:break
                    assert time.monotonic()<deadline,'Report job did not settle';time.sleep(.4)
                write(directory/f'job-{jid}.json',job);assert job['status']==expected,job.get('completion_error') or job.get('parsed_summary')
                if expected=='succeeded':
                    assert job['attempt']['process_stop']['verified']
                    children=job['attempt']['child_processes']['children'].values()
                    assert all(c['stop']['verified'] for c in children)
                    assert job['attempt']['workspace']['storage']['backend']=='s3'
                return job
            def submit():
                created=api('POST',prefix+'/report:build',json={});result['jobs'].append(created['job_id']);save();return created['job_id']
            def build():
                jid=submit();settle(jid);return api('GET',prefix+'/report/results')
            def download(publication,ext):
                r=client.get(prefix+'/report/artifact',params={'publication_id':publication,'path':'E-report/build/report.'+ext});assert r.status_code==200;return r.content
            first=build();write(directory/'first-publication.json',first);first_id=first['publication']['id'];result['first_publication']=first_id
            for path,data in contents.items():assert first['manifest']['inputs'][path]['sha256']==sha(data),path
            first_bytes={ext:download(first_id,ext) for ext in ['html','docx']}
            for ext,data in first_bytes.items():(directory/('first.'+ext)).write_bytes(data)
            result['first_sha256']={ext:sha(data) for ext,data in first_bytes.items()}
            worker_host=command('docker','exec',project+'-arq-worker','hostname')
            owner=command('docker','exec',project+'-postgres','psql','-U','vibeflow','-d','vibeflow','-tAc',
                f"SELECT worker_id FROM job_attempts WHERE job_id={int(result['jobs'][0])}")
            assert owner.startswith(worker_host+':');result['first_worker_host']=worker_host
            with zipfile.ZipFile(io.BytesIO(first_bytes['docx'])) as z:
                assert b'8C245B' in z.read('word/styles.xml')
                assert b'VF-LAYOUT-V1' in z.read('word/header1.xml')
                assert b'S3-ROW' in z.read('word/document.xml')
                assert any(n.startswith('word/media/') for n in z.namelist())
            check('real_S3_resources_frozen_Pandoc_HTML_DOCX_on_independent_worker_with_separate_cache_volumes')
            # A separate owner must be denied by real API authorization.
            peer={'email':'s3-peer-'+secrets.token_hex(6)+'@example.com','password':secrets.token_hex(24),'name':'Other owner'}
            peer_token=api('POST','/auth/register',json=peer)['token'];denied=[]
            for method,url,kwargs in [
                ('GET',prefix+'/report/results',{}),
                ('GET',prefix+'/report/artifact',{'params':{'publication_id':first_id,'path':'E-report/build/report.html'}}),
                ('GET',prefix+'/raw',{'params':{'path':'E-report/templates/reference.docx'}}),
                ('POST',prefix+'/report:build',{'json':{}}),
                ('GET',f"/jobs/{result['jobs'][0]}",{})]:
                r=client.request(method,url,headers={'Authorization':'Bearer '+peer_token},**kwargs)
                assert r.status_code in (403,404);denied.append({'path':url,'status':r.status_code})
            result['owner_denials']=denied;check('second_owner_denied_report_build_results_artifact_template_and_job')
            put('E-report/report.md',markdown.replace('assets/large.png 2x','assets/missing.png 2x'))
            r=client.post(prefix+'/report:build',json={});assert r.status_code==400
            assert len(api('GET',prefix+'/jobs'))==1
            for ext,data in first_bytes.items():assert download(first_id,ext)==data
            put('E-report/report.md',markdown)
            check('S3_missing_candidate_refused_before_job_creation_previous_publication_preserved')
            # Pause consumption at the actual worker; cancel the real queued report.
            command('docker','stop','-t','40',project+'-arq-worker');worker_stopped=True
            cancel_id=submit();queued=api('GET',f'/jobs/{cancel_id}');assert queued['status']=='queued'
            api('POST',f'/jobs/{cancel_id}:cancel',json={});settle(cancel_id,'cancelled')
            command('docker','start',project+'-arq-worker');worker_stopped=False
            for ext,data in first_bytes.items():assert download(first_id,ext)==data
            check('actual_worker_stopped_queued_S3_report_cancelled_worker_restarted_previous_artifacts_preserved')
            upload('E-report/assets/large.png',png(320,160,[230,120,20]))
            put('E-report/styles/nested/theme.css','h1{color:#8c245b}')
            with zipfile.ZipFile(io.BytesIO(reference)) as source:
                changed=io.BytesIO()
                with zipfile.ZipFile(changed,'w',zipfile.ZIP_DEFLATED) as target:
                    for name in source.namelist():
                        data=source.read(name)
                        if name=='word/styles.xml':data=data.replace(b'8C245B',b'247A49')
                        target.writestr(name,data)
            upload('E-report/templates/reference.docx',changed.getvalue())
            assert api('GET',prefix+'/report/results')['publication']['stale']
            second=build();write(directory/'second-publication.json',second);second_id=second['publication']['id'];assert second_id!=first_id
            result['second_publication']=second_id;second_bytes={ext:download(second_id,ext) for ext in ['html','docx']}
            for ext,data in second_bytes.items():(directory/('second.'+ext)).write_bytes(data)
            with zipfile.ZipFile(io.BytesIO(second_bytes['docx'])) as z:assert b'247A49' in z.read('word/styles.xml')
            for ext,data in first_bytes.items():assert download(first_id,ext)==data
            result['second_sha256']={ext:sha(data) for ext,data in second_bytes.items()}
            check('S3_CSS_srcset_reference_changes_mark_stale_new_publication_changes_old_versions_keep_exact_bytes')
            # Recreate only API/worker, replacing their process-local caches; retain S3/SQL.
            compose=['docker','compose','-p',project,'-f',str(ROOT/deployment['compose'])]
            command(*compose,'up','-d','--force-recreate','--no-deps','api','arq-worker')
            deadline=time.monotonic()+100
            while True:
                try:
                    health=client.get(base+'/api/health')
                    if health.status_code==200 and health.json()['ok']:break
                except (httpx.HTTPError,ValueError):pass
                assert time.monotonic()<deadline,'Restarted API unavailable';time.sleep(1)
            latest=api('GET',prefix+'/report/results');assert latest['publication']['id']==second_id and not latest['publication']['stale']
            for ident,values in [(first_id,first_bytes),(second_id,second_bytes)]:
                for ext,data in values.items():assert download(ident,ext)==data
            new_worker=command('docker','exec',project+'-arq-worker','hostname');assert new_worker!=worker_host
            result['recreated_worker_host']=new_worker
            check('recreated_API_worker_read_current_and_historical_S3_HTML_DOCX_exact_bytes')
            # Verify restarted worker also executes, rather than only serving old data.
            third=build();result['third_publication']=third['publication']['id']
            owner=command('docker','exec',project+'-postgres','psql','-U','vibeflow','-d','vibeflow','-tAc',
                f"SELECT worker_id FROM job_attempts WHERE job_id={int(result['jobs'][-1])}")
            assert owner.startswith(new_worker+':')
            check('recreated_independent_worker_builds_new_S3_report_after_cancel_and_restart')
            result['passed']=True;save()
    except BaseException as error:
        result['error']={'type':type(error).__name__,'message':str(error)[:1000]};save();raise
    finally:
        if worker_stopped:command('docker','start',project+'-arq-worker')
        print('S3_REPORT_EVIDENCE '+str(directory.relative_to(ROOT)),flush=True)

if __name__=='__main__':main()
