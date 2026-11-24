"""Actual default local CAD and IR conversion, with real model proof readback."""
import argparse,asyncio,hashlib,json,time
from pathlib import Path
import cadquery as cq
from app.services.flotherm_ir import build_ir
from live_llm_acceptance import FIXTURE,source
from live_llm_sandbox_recovery_acceptance import model_read
from testsupport import IsolatedServer,SERVER


def verify(configuration):
    api=IsolatedServer('local-geometry',app='local_geometry_fixture:app')
    (api.root/'local_geometry_fixture.py').write_text("import os\nos.environ.update(VIBEFLOW_DISTRIBUTED_JOBS='false',VIBEFLOW_JOB_EXECUTION_HOSTS='{}')\n"+FIXTURE,'utf8')
    result={'passed':False,'mock_llm_calls':0,'jobs':[],'artifacts':[]}
    def submit(kind,endpoint,parameters,identifier=None):
        if identifier is None:identifier=api.call('POST',api.prefix+endpoint,parameters)['job_id']
        job=api.wait_job(identifier,timeout=180)
        deadline=time.monotonic()+45
        while job['attempt']['outputs'].get('completion',{}).get('state')!='settled':
            assert time.monotonic()<deadline,'Geometry publication did not settle'
            time.sleep(.2);job=api.call('GET','/jobs/'+str(identifier))
        assert job['attempt']['handler']['key']==kind and 'environment' not in job['attempt']['handler']
        assert job['attempt']['process_identity']['host']['platform']=='windows' and job['attempt']['process_stop']['verified']
        result['jobs'].append(job);print('LOCAL_GEOMETRY_PASS '+kind,flush=True)
    try:
        with api:
            api.create_case('Actual local CAD')
            saved=api.put('A-geo/geometry.py',"import cadquery as cq\ndef build_geometry(ctx):\n return {'box':cq.Workplane('XY').box(2,3,4)}\n")
            assert saved['auto_preview_job'] is not None
            for mode in ('preview','export'):
                submit('geometry.'+mode,'/geometry:'+mode,{},saved['auto_preview_job'] if mode=='preview' else None)
                scene=json.loads(api.call('GET',api.prefix+'/raw?path=A-geo/scene.json',raw=True))
                assert len(scene['objects'])==1
                obj=scene['objects'][0]
                assert obj['vertices'] and obj['faces'] and all(abs(b-a-d)<1e-6 for a,b,d in zip(obj['bbox']['min'],obj['bbox']['max'],[2,3,4]))
            manifest=json.loads(api.call('GET',api.prefix+'/raw?path=A-geo/geometry_manifest.json',raw=True))
            step='A-geo/exports/'+manifest['artifacts'][0]['step']
            data=api.call('GET',api.prefix+'/raw?path='+step,raw=True);(api.root/'box.step').write_bytes(data)
            volume=cq.importers.importStep(str(api.root/'box.step')).val().Volume();assert abs(volume-24)<1e-7
            result['imported_step_volume']=volume;result['artifacts'].append({'path':step,'file':'box.step','sha256':hashlib.sha256(data).hexdigest()})
            ir=build_ir('source.ecxml',b'chip',[{'id':'chip','type':'box','material':'silicon','mapping':'converted','power_w':5,
                'shape':{'kind':'box','origin':[0,0,0],'dimensions':[1,1,1]}}],{'silicon':{'conductivity_w_mk':130,'rho_cp':1000}})
            api.put('Ref/ir.json',json.dumps(ir));api.put('Ref/settings.json',json.dumps({'power':{'chip':{'value':5,'unit':'W'}}}))
            submit('flotherm.convert','/flotherm:convert',{'ir_path':'Ref/ir.json','settings_path':'Ref/settings.json','output_dir':'C-run/thermal-local'})
            coverage_path='C-run/thermal-local/conversion-coverage.json'
            coverage=api.call('GET',api.prefix+'/raw?path='+coverage_path,raw=True)
            assert json.loads(coverage)['summary']['input_power_w']==5
            (api.root/'conversion-coverage.json').write_bytes(coverage)
            result['artifacts'].append({'path':coverage_path,'file':'conversion-coverage.json','sha256':hashlib.sha256(coverage).hexdigest()})
            paths=[a['path'] for a in result['artifacts']]
            api.put('Ref/geometry-proof.py',"from pathlib import Path\nimport hashlib\npaths="+repr(paths)+"\ndata=b''.join(Path(p).read_bytes() for p in paths)\nPath('Ref/proof.txt').write_bytes(('RETIREMENT_'+hashlib.sha256(data).hexdigest()+'\\n').encode())\n")
            job=api.call('POST',api.prefix+'/jobs',{'type':'convert','runtime':'windows','command':'python Ref/geometry-proof.py'})
            result['jobs'].append(api.wait_job(job['id']))
            content=api.call('GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True)
            result['real_model_calls']=model_read(api,'','',content.decode().strip(),configuration,
                already_restored=True,path='Ref/proof.txt',expected_content=content)
            result['passed']=True
    except BaseException as error:result['failure_type']=type(error).__name__;raise
    finally:(api.root/'local-geometry.json').write_text(json.dumps(result,indent=2)+'\n','utf8')


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True);parser.add_argument('--review',type=Path,required=True)
    args=parser.parse_args();review=json.loads(args.review.read_text('utf8'));assert review['entry']=='platform/server/live_llm_local_geometry_acceptance.py'
    for name,digest in review['sources'].items():assert hashlib.sha256((SERVER.parents[1]/name).read_bytes()).hexdigest()==digest,name
    await asyncio.to_thread(verify,await source(args.source_profile_id))
if __name__=='__main__':asyncio.run(main())
