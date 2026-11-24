"""Actual OpenFOAM fields -> portable extraction -> two report drafts -> real LLM."""
import asyncio,hashlib,io,json,re,shlex,sqlite3,time
from pathlib import Path
from urllib.parse import quote
import xml.etree.ElementTree as ET
import asyncpg
from PIL import Image,ImageStat
from pypdf import PdfReader
from app.services.casedict import set_block
from cae_benchmarks import thermal_files,BASHRC
import live_llm_remote_worker_acceptance as baseline

RUN='C-run/conduction'
PYTHON='/opt/vibeflow-worker-venv/bin/python3'


class ConfiguredWorker(baseline.LinuxWorker):
    def fixture_code(self,root):
        return "runtime=Path(os.environ['VIBEFLOW_RUNTIME_CONFIG'])\nruntime.parent.mkdir(parents=True,exist_ok=True)\nruntime.write_text(json.dumps({'python':"+repr(PYTHON)+"}))\n"


async def scenario(api,worker=None,selected=None):
    remote=worker is not None
    result={'passed':False,'remote':remote,'mock_llm_calls':0,'jobs':[],'checks':[],'environments':[],'artifacts':[]}
    filename=('remote' if remote else 'local')+'-post-report.json'
    def save():(api.root/filename).write_text(json.dumps(result,indent=2)+'\n','utf8')
    def record(message):result['checks'].append(message);save();print('POST_REPORT_PASS '+message,flush=True)
    async def call(method,path,**kwargs):
        deadline=time.monotonic()+45
        while True:
            try:return await asyncio.to_thread(api.call,method,path,**kwargs)
            except AssertionError as error:
                if not any(c in str(error) for c in ('storage_quota_busy','validation_busy')) or time.monotonic()>deadline:raise
                await asyncio.sleep(.2)
    async def raw(path):return await call('GET',api.prefix+'/raw?path='+quote(path,safe=''),raw=True)
    async def wait(identifier):
        deadline=time.monotonic()+240
        while True:
            job=await call('GET','/jobs/'+str(identifier))
            if job['status']=='succeeded' and job['attempt']['outputs'].get('completion',{}).get('state')=='settled':
                assert job['attempt']['process_stop']['verified'];result['jobs'].append(job);save();return job
            if job['status'] in {'failed','cancelled','interrupted'}:
                result['failed_job']=job;save();raise AssertionError('Actual post/report job failed')
            assert time.monotonic()<deadline,'Actual post/report job did not settle'
            await asyncio.sleep(.3)
    async def artifact(path,name):
        data=await raw(path);(api.root/name).write_bytes(data)
        result['artifacts'].append({'path':path,'file':name,'sha256':hashlib.sha256(data).hexdigest()});save();return data
    connection=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,
        password=selected.password,database=selected.database,timeout=10) if selected is not None else None
    async def original(identifier):
        if connection:return await connection.fetchval('SELECT input_manifest_json FROM job_attempts WHERE job_id=$1',identifier)
        with sqlite3.connect('file:'+str(api.root/'test.sqlite')+'?mode=ro',uri=True) as db:
            return db.execute('SELECT input_manifest_json FROM job_attempts WHERE job_id=?',(identifier,)).fetchone()[0]
    async def submit(kind,endpoint,parameters):
        created=await call('POST',api.prefix+endpoint,body=parameters);identifier=created['job_id']
        sealed=await original(identifier);job=await wait(identifier);assert sealed==await original(identifier)
        document=json.loads(sealed);assert document['handler']['key']==kind
        assert job['attempt']['process_identity']['host']['platform']==('linux' if remote else 'windows')
        if remote:
            rows=await connection.fetch('SELECT payload_json FROM durable_events WHERE attempt_id=$1 AND type=$2',job['attempt']['id'],'attempt.execution_environment')
            assert len(rows)==1;environment=json.loads(rows[0]['payload_json'])
            assert environment['declaration']==document['handler']['environment']
            assert environment['host_id']==worker.observed['host_id']!=environment['declaration']['origin_host_id']
            assert environment['declaration']['interpreter']=='configured_python' and environment['resolved_argv'][0]==PYTHON
            assert environment['resolved_argv'][2:]==document['execution_spec']['argv'][2:]
            result['environments'].append(environment)
        else:assert 'environment' not in document['handler']
        save();return job
    try:
        saved=await call('GET',api.prefix+'/caseDict')
        post={'runDir':RUN,'fields':{'T':{'unit':'K'}},'dataExtraction':{
            'volumeAverages':{'mean':{'field':'T','time':1}},
            'lineSamples':{'profile':{'fields':['T'],'start':[.05,.5,.5],'end':[.95,.5,.5],'samples':19,'times':[1]}}},
            'derivedQuantities':{'referenceValues':{'analytic_mean':{'metric':'"mean.T@1"','value':350,
                'unit':'K','kind':'verification','source':'"analytic T(x)=300+100*x"','l2_max':1e-6}}},
            'artifacts':{'plots':{'temperature':{'source':'profile','field':'T','time':1,
                'formats':['pdf','svg','png'],'title':'"Actual temperature profile"'}}}}
        content=set_block(saved['content'],'caseMeta',{'discipline':'thermal','analysisType':'steady','dimensions':'3D','units':{'length':'m','temperature':'K'}})
        content=set_block(content,'postProcessing',post)
        await call('PUT',api.prefix+'/caseDict',body={'content':content,'expected_revision':saved['dict_revision']})
        for path,text in thermal_files().items():
            if path.endswith('/blockMeshDict'):text=text.replace('(20 2 2)','(10 4 2)')
            await call('PUT',api.prefix+'/file',body={'path':path,'content':text})
        script='source '+shlex.quote(BASHRC)+' || exit; set -e; '
        script+='; '.join(tool+' -case '+RUN+' > '+RUN+'/log.'+tool+' 2>&1' for tool in ['blockMesh','checkMesh','laplacianFoam'])
        created=await call('POST',api.prefix+'/jobs',body={'type':'convert','runtime':'windows' if remote else 'wsl',
            'command':'bash -c '+shlex.quote(script),'timeout_seconds':180})
        await wait(created['id'])
        field=await artifact(RUN+'/1/T','actual-temperature.T')
        match=re.search(r'internalField\s+nonuniform\s+List<scalar>\s+(\d+)\s*\((.*?)\)\s*;',field.decode(),re.S)
        assert match and int(match.group(1))==80
        values=[float(v) for v in match.group(2).split()]
        error=max(abs(value-(300+100*((i%10)+.5)/10)) for i,value in enumerate(values))
        assert len(values)==80 and error<1e-6;result['temperature_error_max']=error
        quality=await artifact(RUN+'/log.checkMesh','actual-checkMesh.log');assert b'Mesh OK' in quality
        record('actual 80-cell OpenFOAM mesh and laplacianFoam solution match analytic temperature')
        job=await submit('postprocess.case','/postprocess:run',{'run_dir':RUN})
        folder='D-post/data/postprocess-'+str(job['id'])
        metrics=json.loads(await artifact(folder+'/metrics.json','metrics.json'))
        mean=next(v['value'] for v in metrics['metrics'] if v['name']=='mean.T@1')
        assert abs(mean-350)<1e-6 and metrics['readiness']['report_ready'] and metrics['readiness']['can_claim_verification']
        assert not metrics['readiness']['can_claim_validation'] and all(v['status']=='pass' for v in metrics['comparisons'])
        series=next(v for v in metrics['extractions']['lineSamples'] if v['id']=='profile')['series']['T']['values']
        assert len(series)==19 and all(a[0]<b[0] for a,b in zip(series,series[1:]))
        pdf=await artifact(folder+'/temperature.pdf','temperature.pdf');assert len(PdfReader(io.BytesIO(pdf)).pages)==1
        svg=await artifact(folder+'/temperature.svg','temperature.svg');ET.fromstring(svg)
        png=await artifact(folder+'/temperature.png','temperature.png')
        with Image.open(io.BytesIO(png)) as picture:
            assert min(picture.size)>100 and max(ImageStat.Stat(picture.convert('RGB')).stddev)>5
        result['mean_temperature_k']=mean
        record('actual VTK extraction gives 350 K and generates real PDF/SVG/PNG plots')
        for output,kind,name in [('E-report/report.md','report.compose','report.md'),
                                 ('E-report/report.generated.md','report.compose-generated','report.generated.md')]:
            await submit(kind,'/report:compose?wait=false',{'output':output,'overwrite':True})
            document=await call('GET',api.prefix+'/report/compose-result?output='+quote(output,safe=''))
            assert document['evidence']['report_ready'] and document['publication']['applied'] and not document['publication']['stale']
            data=await artifact(output,name);assert data.decode()==document['content']
            metric_row=next(line for line in data.decode().splitlines() if line.startswith('| mean.T@1 | volumeAverages |'))
            reported_mean=float(metric_row.split('|')[4].strip())
            assert reported_mean==mean and abs(reported_mean-350)<1e-6
            record('actual report draft preserves extraction evidence: '+output)
        paths=[a['path'] for a in result['artifacts']]
        code="from pathlib import Path\nimport hashlib\npaths="+repr(paths)+"\ndata=b''.join(Path(p).read_bytes() for p in paths)\nPath('Ref/proof.txt').write_bytes(('RETIREMENT_'+hashlib.sha256(data).hexdigest()+'\\n').encode())\n"
        await call('PUT',api.prefix+'/file',body={'path':'Ref/post-report-proof.py','content':code})
        created=await call('POST',api.prefix+'/jobs',body={'type':'convert','runtime':'windows',
            'command':('python3' if remote else 'python')+' Ref/post-report-proof.py'})
        await wait(created['id']);result['passed']=True;save()
    except BaseException as error:result['failure_type']=type(error).__name__;save();raise
    finally:
        if connection:await connection.close()
        save()
    return result


async def verify(configuration,selected,admin,observed):
    return await baseline.verify(configuration,selected,admin,observed,worker_factory=ConfiguredWorker,after_publish=scenario)
if __name__=='__main__':asyncio.run(baseline.main(entry='platform/server/live_llm_remote_post_report_acceptance.py',verifier=verify))
