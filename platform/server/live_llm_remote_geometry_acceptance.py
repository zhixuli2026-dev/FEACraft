"""Actual CadQuery preview/STEP export and FloTHERM conversion on Linux."""
import asyncio,hashlib,json,shlex,time
from pathlib import Path
import asyncpg
import cadquery as cq
from app.services.flotherm_ir import build_ir
import live_llm_remote_worker_acceptance as baseline

PYTHON='/opt/vibeflow-worker-venv/bin/python3'


class ConfiguredWorker(baseline.LinuxWorker):
    def fixture_code(self,root):
        # Actual deployment configuration, consumed by production runtime_config.
        return "runtime=Path(os.environ['VIBEFLOW_RUNTIME_CONFIG'])\nruntime.parent.mkdir(parents=True,exist_ok=True)\nruntime.write_text(json.dumps({'python':"+repr(PYTHON)+"}))\n"


async def scenario(api,worker,selected):
    result={'passed':False,'mock_llm_calls':0,'jobs':[],'checks':[],'environments':[]}
    def save():(api.root/'remote-geometry.json').write_text(json.dumps(result,indent=2)+'\n','utf8')
    def record(text):result['checks'].append(text);save();print('REMOTE_GEOMETRY_PASS '+text,flush=True)
    async def call(method,path,**kwargs):
        deadline=time.monotonic()+45
        while True:
            try:return await asyncio.to_thread(api.call,method,path,**kwargs)
            except AssertionError as error:
                if not any(c in str(error) for c in ('storage_quota_busy','validation_busy')) or time.monotonic()>deadline:raise
                await asyncio.sleep(.2)
    async def wait(identifier):
        deadline=time.monotonic()+180
        while True:
            job=await call('GET','/jobs/'+str(identifier))
            if job['status']=='succeeded' and job['attempt']['outputs'].get('completion',{}).get('state')=='settled':return job
            if job['status'] in {'failed','cancelled','interrupted'}:
                result['failed_job']=job;save();raise AssertionError('Remote professional job failed')
            assert time.monotonic()<deadline,'Remote professional job did not settle'
            await asyncio.sleep(.3)
    connection=await asyncpg.connect(host=selected.host,port=selected.port or 5432,user=selected.username,
        password=selected.password,database=selected.database,timeout=10)
    async def submit(kind,endpoint,parameters,identifier=None):
        if identifier is None:
            response=await call('POST',api.prefix+endpoint,body=parameters);identifier=response['job_id']
        original=await connection.fetchval('SELECT input_manifest_json FROM job_attempts WHERE job_id=$1',identifier)
        job=await wait(identifier);result['jobs'].append(job)
        assert original==await connection.fetchval('SELECT input_manifest_json FROM job_attempts WHERE job_id=$1',identifier)
        document=json.loads(original);assert document['handler']['key']==kind
        rows=await connection.fetch('SELECT payload_json FROM durable_events WHERE attempt_id=$1 AND type=$2',job['attempt']['id'],'attempt.execution_environment')
        assert len(rows)==1;environment=json.loads(rows[0]['payload_json'])
        assert environment['declaration']==document['handler']['environment']
        assert environment['host_id']==worker.observed['host_id'] and environment['host_id']!=environment['declaration']['origin_host_id']
        configured=kind.startswith('geometry.')
        assert environment['declaration']['interpreter']==('configured_python' if configured else 'server_python')
        assert environment['resolved_argv'][0]==(PYTHON if configured else baseline.PYTHON)
        assert environment['resolved_argv'][2:]==document['execution_spec']['argv'][2:]
        assert job['attempt']['process_identity']['host']['platform']=='linux' and job['attempt']['process_stop']['verified']
        result['environments'].append(environment);save();return job
    try:
        geometry="import cadquery as cq\ndef build_geometry(ctx):\n return {'box':cq.Workplane('XY').box(2,3,4)}\n"
        saved=await call('PUT',api.prefix+'/file',body={'path':'A-geo/geometry.py','content':geometry})
        assert saved['auto_preview_job'] is not None
        for mode in ('preview','export'):
            await submit('geometry.'+mode,'/geometry:'+mode,{},saved['auto_preview_job'] if mode=='preview' else None)
            scene=json.loads(await call('GET',api.prefix+'/raw?path=A-geo/scene.json',raw=True))
            assert len(scene['objects'])==1
            obj=scene['objects'][0];assert obj['vertices'] and obj['faces']
            assert all(abs(b-a-size)<1e-6 for a,b,size in zip(obj['bbox']['min'],obj['bbox']['max'],[2,3,4]))
            toolchain=json.loads(await call('GET',api.prefix+'/raw?path=A-geo/toolchain.json',raw=True))
            assert toolchain['cadquery']=='2.8.0' and toolchain['mode']==mode
            result.setdefault('toolchains',[]).append(toolchain)
            if mode=='export':
                manifest=json.loads(await call('GET',api.prefix+'/raw?path=A-geo/geometry_manifest.json',raw=True))
                assert len(manifest['artifacts'])==1
                for extension in ('step','stl'):
                    path='A-geo/exports/'+manifest['artifacts'][0][extension]
                    data=await call('GET',api.prefix+'/raw?path='+path,raw=True)
                    dest=api.root/('box.'+extension);dest.write_bytes(data)
                    assert len(data)>100
                    result.setdefault('artifacts',[]).append({'path':path,'sha256':hashlib.sha256(data).hexdigest(),'file':dest.name})
                shape=await asyncio.to_thread(lambda:cq.importers.importStep(str(api.root/'box.step')).val())
                assert abs(shape.Volume()-24)<1e-7
                result['imported_step_volume']=shape.Volume()
            record('actual CadQuery '+mode+' uses execution-host configured Python and preserves geometry dimensions')
        ir=build_ir('source.ecxml',b'chip',[{'id':'chip','type':'box','material':'silicon','mapping':'converted','power_w':5,
            'shape':{'kind':'box','origin':[0,0,0],'dimensions':[1,1,1]}}],{'silicon':{'conductivity_w_mk':130,'rho_cp':1000}})
        await call('PUT',api.prefix+'/file',body={'path':'Ref/ir.json','content':json.dumps(ir)})
        await call('PUT',api.prefix+'/file',body={'path':'Ref/settings.json','content':json.dumps({'power':{'chip':{'value':5,'unit':'W'}}})})
        parameters={'ir_path':'Ref/ir.json','settings_path':'Ref/settings.json','output_dir':'C-run/thermal-remote'}
        await submit('flotherm.convert','/flotherm:convert',parameters)
        coverage_path=parameters['output_dir']+'/conversion-coverage.json'
        coverage=await call('GET',api.prefix+'/raw?path='+coverage_path,raw=True)
        assert json.loads(coverage)['summary']['input_power_w']==5
        (api.root/'conversion-coverage.json').write_bytes(coverage)
        result['artifacts'].append({'path':coverage_path,'sha256':hashlib.sha256(coverage).hexdigest(),'file':'conversion-coverage.json'})
        record('actual Linux FloTHERM IR converter preserves the configured five-watt source')
        paths=[a['path'] for a in result['artifacts']]
        code="from pathlib import Path;import hashlib;paths="+repr(paths)+";data=b''.join(Path(p).read_bytes() for p in paths);Path('Ref/proof.txt').write_text('RETIREMENT_'+hashlib.sha256(data).hexdigest()+'\\n')"
        proof=await call('POST',api.prefix+'/jobs',body={'type':'convert','runtime':'windows','timeout_seconds':120,'command':'python3 -c '+shlex.quote(code)})
        result['jobs'].append(await wait(proof['id']))
        result['passed']=True;save()
    except BaseException as error:result['failure_type']=type(error).__name__;save();raise
    finally:await connection.close();save()


async def verify(configuration,selected,admin,observed):
    return await baseline.verify(configuration,selected,admin,observed,worker_factory=ConfiguredWorker,after_publish=scenario)
if __name__=='__main__':asyncio.run(baseline.main(entry='platform/server/live_llm_remote_geometry_acceptance.py',verifier=verify))
