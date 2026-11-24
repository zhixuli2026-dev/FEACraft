"""One real mixed-host workflow through confirmed solve, Linux extraction and Windows Pandoc."""
import asyncio,hashlib,io,json,re,shlex,time,zipfile
from pathlib import Path
from urllib.parse import quote
import asyncpg
from app.services.casedict import set_block
from cae_benchmarks import thermal_files
from app.services.process_identity_native import host_id
from workflow_batch_redis_support import Workers
from live_llm_remote_post_report_acceptance import ConfiguredWorker,PYTHON,RUN
import live_llm_remote_worker_acceptance as baseline


async def scenario(api,worker,selected):
    result={'passed':False,'mock_llm_calls':0,'jobs':[],'confirmations':[],'artifacts':[],'checks':[]}
    api.env['VIBEFLOW_WORKER_HOST_QUEUE']='true'
    windows=Workers(api,worker.broker,worker.store)
    original={}
    def save():(api.root/'confirmed-postprocess.json').write_text(json.dumps(result,indent=2)+'\n','utf8')
    def record(message):result['checks'].append(message);save();print('CONFIRMED_POST_PASS '+message,flush=True)
    async def call(method,path,*,server=api,**kwargs):
        deadline=time.monotonic()+45
        while True:
            try:return await asyncio.to_thread(server.call,method,path,**kwargs)
            except AssertionError as error:
                if not any(c in str(error) for c in ('storage_quota_busy','validation_busy')) or time.monotonic()>deadline:raise
                await asyncio.sleep(.2)
    async def raw(path):return await call('GET',api.prefix+'/raw?path='+quote(path,safe=''),raw=True)
    async def artifact(path,name):
        data=await raw(path);(api.root/name).write_bytes(data)
        result['artifacts'].append({'path':path,'file':name,'sha256':hashlib.sha256(data).hexdigest()});save();return data
    connection=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,
        password=selected.password,database=selected.database,timeout=10)
    async def sealed(identifier):
        row=await connection.fetchrow('SELECT input_manifest_json,publication_json FROM job_attempts WHERE job_id=$1',identifier)
        assert row is not None
        value=dict(row)
        if identifier in original:assert original[identifier]==value,'Original sealed request changed'
        else:original[identifier]=value
        return value
    async def settled(identifier):
        await sealed(identifier);deadline=time.monotonic()+300
        while True:
            job=await call('GET','/jobs/'+str(identifier))
            if job['status']=='succeeded' and job['attempt']['outputs'].get('completion',{}).get('state')=='settled':
                assert job['attempt']['process_stop']['verified'];await sealed(identifier)
                if not any(j['id']==identifier for j in result['jobs']):result['jobs'].append(job);save()
                return job
            if job['status'] in {'failed','cancelled','interrupted'}:
                result['failed_job']=job;save();raise AssertionError('Actual confirmed workflow job failed')
            assert time.monotonic()<deadline,'Actual confirmed job did not settle'
            await asyncio.sleep(.3)
    async def awaiting(identifier,key):
        deadline=time.monotonic()+300
        while True:
            flow=await call('GET','/workflow-runs/'+identifier)
            for step in flow['steps']:
                for attempt in step['attempts']:
                    if attempt.get('job_id'):await sealed(attempt['job_id'])
            if flow['status'] in {'failed','cancelled','interrupted'}:
                result['failed_workflow']=flow;save();raise AssertionError('Actual workflow did not reach confirmation')
            step=next(s for s in flow['steps'] if s['key']==key)
            if step['status']=='awaiting_confirmation':return flow,step
            assert time.monotonic()<deadline,'Actual workflow confirmation timed out: '+key
            await asyncio.sleep(.3)
    async def confirm(identifier,key):
        flow,step=await awaiting(identifier,key)
        for producer in flow['steps']:
            if producer['status']=='succeeded' and producer['attempts']:
                await settled(producer['attempts'][-1]['job_id'])
        body={'state':step['confirmation_state'],'confirmed':True,'expected_revision':step['confirmation_revision'],
              'workflow_step_id':step['id'],'expected_step_version':step['version']}
        await call('POST',api.prefix+'/caseDict:advance',body=body)
        result['confirmations'].append({'step':key,'request':body});save()
        return flow
    try:
        await asyncio.to_thread(windows.start)
        current=await call('GET',api.prefix+'/caseDict');content=current['content']
        for key,value in {
            'caseMeta':{'discipline':'thermal','analysisType':'steady','dimensions':'3D','units':{'length':'m','temperature':'K'}},
            'geometry':{'generator':'cadquery','parameters':{'lx':1,'ly':1,'lz':1}},
            'mesh':{'source':'blockMesh','qualityTargets':{'policy':'fail','maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100}},
            'numericalMethod':{'solver':'laplacianFoam'},
            'postProcessing':{'runDir':RUN,'fields':{'T':{'unit':'K'}},'dataExtraction':{
                'volumeAverages':{'mean':{'field':'T','time':1}},
                'lineSamples':{'profile':{'fields':['T'],'start':[.05,.5,.5],'end':[.95,.5,.5],'nPoints':19,'times':[1]}}},
                'derivedQuantities':{'referenceValues':{'analytic_mean':{'metric':'"mean.T@1"','value':350,
                    'unit':'K','kind':'verification','source':'"analytic T(x)=300+100*x"','l2_max':1e-6}}},
                'artifacts':{'plots':{'temperature':{'source':'profile','field':'T','time':1,'formats':['png'],'title':'"Temperature"'}}}}
        }.items():content=set_block(content,key,value)
        await call('PUT',api.prefix+'/caseDict',body={'content':content,'expected_revision':current['dict_revision']})
        for path,body in thermal_files().items():
            if path.endswith('/blockMeshDict'):body=body.replace('(20 2 2)','(10 4 2)')
            await call('PUT',api.prefix+'/file',body={'path':path,'content':body})
        seeded=await call('PUT',api.prefix+'/file',body={'path':'A-geo/geometry.py','content':
            'import cadquery as cq\ndef build_geometry(ctx):return {"box":cq.Workplane("XY").box(1,1,1,centered=(False,False,False))}\n'})
        if seeded.get('auto_preview_job'):await settled(seeded['auto_preview_job'])
        revision=(await call('GET',api.prefix+'/caseDict'))['dict_revision']
        flow=await call('POST',api.prefix+'/workflow-runs',body={'target':'report','run_dir':RUN,'expected_revision':revision,
            'parameters':{'geometry_mode':'export'},'budget':{'timeout_seconds':180,'max_log_bytes':16777216}})
        identifier=flow['id'];result['workflow_id']=identifier;save()
        await confirm(identifier,'confirm_B');await confirm(identifier,'confirm_C')
        flow,step=await awaiting(identifier,'confirm_D')
        solve_step=next(s for s in flow['steps'] if s['key']=='solve')
        solve=await settled(solve_step['attempts'][-1]['job_id'])
        assert solve['attempt']['process_identity']['host']['platform']=='windows'
        solve_publication=solve['attempt']['outputs']['publication']['id']
        assert not next(s for s in flow['steps'] if s['key']=='postprocess')['attempts']
        denied=await call('POST',api.prefix+'/caseDict:advance',body={'state':'D','confirmed':False,
            'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],
            'expected_step_version':step['version']},expect=409)
        assert denied['detail']['code']=='confirmation_required'
        field=await artifact(RUN+'/1/T','confirmed-temperature.T')
        match=re.search(r'internalField\s+nonuniform\s+List<scalar>\s+(\d+)\s*\((.*?)\)\s*;',field.decode(),re.S)
        assert match and int(match.group(1))==80
        values=list(map(float,match.group(2).split()));error=max(abs(v-(300+100*((i%10)+.5)/10)) for i,v in enumerate(values))
        assert len(values)==80 and error<1e-6;result['maximum_temperature_error_k']=error
        record('real registered geometry, 80-cell mesh and solve complete; postprocess waits for explicit D')
        await confirm(identifier,'confirm_D')
        flow,_=await awaiting(identifier,'confirm_E')
        post_step=next(s for s in flow['steps'] if s['key']=='postprocess')
        post=await settled(post_step['attempts'][-1]['job_id']);post_id=post['id']
        assert post['attempt']['process_identity']['host']['platform']=='linux'
        assert post['attempt']['process_identity']['host']['uid']==65534
        document=json.loads(original[post_id]['input_manifest_json']);descriptor=json.loads(original[post_id]['publication_json'])['descriptor']
        assert document['handler']['key']=='postprocess.run' and descriptor['handler']=='postprocess.v1'
        assert descriptor['input_publications'][0]['publication_id']==solve_publication
        rows=await connection.fetch('SELECT payload_json FROM durable_events WHERE attempt_id=$1 AND type=$2',post['attempt']['id'],'attempt.execution_environment')
        assert len(rows)==1;environment=json.loads(rows[0]['payload_json']);result['environment']=environment
        assert environment['declaration']==document['handler']['environment']
        assert environment['host_id']==worker.observed['host_id']!=environment['declaration']['origin_host_id']
        assert environment['resolved_argv'][0]==PYTHON and environment['resolved_argv'][2:]==document['execution_spec']['argv'][2:]
        metrics=json.loads(await artifact('D-post/data/postprocess-'+str(post_id)+'/metrics.json','confirmed-metrics.json'))
        mean=next(m['value'] for m in metrics['metrics'] if m['name']=='mean.T@1')
        assert abs(mean-350)<1e-6 and metrics['readiness']['report_ready'] and metrics['readiness']['can_claim_verification']
        assert not metrics['readiness']['can_claim_validation'];result['mean_temperature_k']=mean
        result['solve_publication_id']=solve_publication;result['postprocess_publication_id']=post['attempt']['outputs']['publication']['id']
        await confirm(identifier,'confirm_E')
        deadline=time.monotonic()+300
        while True:
            flow=await call('GET','/workflow-runs/'+identifier)
            report_step=next(s for s in flow['steps'] if s['key']=='report')
            if report_step['attempts']:
                report=await settled(report_step['attempts'][-1]['job_id'])
            if flow['status']=='succeeded':break
            if flow['status'] in {'failed','cancelled','interrupted'}:
                result['failed_workflow']=flow;save();raise AssertionError('Canonical report workflow failed')
            assert time.monotonic()<deadline,'Canonical report workflow did not finish'
            await asyncio.sleep(.3)
        assert report['attempt']['process_identity']['host']['platform']=='windows'
        result['workflow']=flow;save()
        record('Linux extraction pins confirmed solve; explicit E dispatches Windows Pandoc and completes one workflow')
        for output,name in [('E-report/report.md','confirmed-report.md'),('E-report/report.generated.md','confirmed-report.generated.md')]:
            created=await call('POST',api.prefix+'/report:compose?wait=false',body={'output':output,'overwrite':True})
            job=await settled(created['job_id']);assert job['attempt']['process_identity']['host']['platform']=='linux'
            text=(await artifact(output,name)).decode();row=next(line for line in text.splitlines() if line.startswith('| mean.T@1 | volumeAverages |'))
            assert float(row.split('|')[4].strip())==mean
        record('both actual Linux report drafts retain the confirmed extraction metric')
        report_descriptor=json.loads(original[report['id']]['publication_json'])['descriptor']
        assert report_descriptor['registered_postprocess'] is True
        assert report_descriptor['input_publications'][0]['publication_id']==result['postprocess_publication_id']
        result['report_publication_id']=report['attempt']['outputs']['publication']['id']
        document=await call('GET',api.prefix+'/report/results')
        assert document['publication']['id']==result['report_publication_id'] and not document['publication']['stale']
        formats=[]
        for path,expected in document['manifest']['artifacts'].items():
            data=await artifact(path,'confirmed-'+Path(path).name)
            assert hashlib.sha256(data).hexdigest()==expected['sha256']
            if path.endswith('.html'):assert b'<html' in data.lower();formats.append('html')
            if path.endswith('.docx'):
                with zipfile.ZipFile(io.BytesIO(data)) as z:assert b'<w:document' in z.read('word/document.xml')
                formats.append('docx')
        assert set(formats)=={'html','docx'}
        record('canonical Windows Pandoc HTML/DOCX pin the Linux postprocess publication without another submission API')
        paths=[a['path'] for a in result['artifacts']]
        code="from pathlib import Path\nimport hashlib\npaths="+repr(paths)+"\nPath('Ref/proof.txt').write_bytes(('RETIREMENT_'+hashlib.sha256(b''.join(Path(p).read_bytes() for p in paths)).hexdigest()+'\\n').encode())\n"
        await call('PUT',api.prefix+'/file',body={'path':'Ref/confirmed-proof.py','content':code})
        created=await call('POST',api.prefix+'/jobs',body={'type':'convert','runtime':'windows','command':'python3 Ref/confirmed-proof.py'})
        await settled(created['id'])
        result['delivery_hosts']=[]
        for job in result['jobs']:
            await sealed(job['id'])
            rows=await connection.fetch('SELECT payload_json FROM durable_events WHERE attempt_id=$1 AND type=$2',job['attempt']['id'],'attempt.delivery_requested')
            assert len(rows)==1;delivery=json.loads(rows[0]['payload_json'])
            birth=job['attempt']['process_identity']['host']
            assert delivery['schema']=='vibeflow.job-delivery.v2' and delivery['host_id']==birth['host_id']
            assert delivery['target_platform']==birth['platform']
            result['delivery_hosts'].append({'job_id':job['id'],'delivery':delivery,'actual_host':birth})
        result['original_requests']=original;result['passed']=True;save()
    except BaseException as error:result['failure_type']=type(error).__name__;save();raise
    finally:
        await asyncio.to_thread(windows.stop);result['windows_workers']=windows.receipts
        await connection.close();save()


async def verify(configuration,selected,admin,observed):
    windows={'host_id':host_id(),'platform':'windows'}
    environment={'VIBEFLOW_JOB_EXECUTION_HOSTS':json.dumps({'windows':{'host_id':observed['host_id'],'platform':'linux'},'wsl':windows}),
                 'VIBEFLOW_JOB_HANDLER_EXECUTION_HOSTS':json.dumps({'report.build':windows})}
    return await baseline.verify(configuration,selected,admin,observed,worker_factory=ConfiguredWorker,
        after_publish=scenario,environment_overrides=environment)

if __name__=='__main__':asyncio.run(baseline.main(entry='platform/server/live_llm_remote_confirmed_postprocess_acceptance.py',verifier=verify))
