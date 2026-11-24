"""Real Linux Docker/S3 geometry-to-report, cancellation and serial checkpoint resume."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import secrets
import subprocess
import sys
import time
import uuid

import httpx

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'platform/server'))
from app.services.casedict import set_block
from cae_benchmarks import thermal_files
from cae_fixtures import cavity_2d
from registered_cae_benchmarks import thermal_errors

def sha(data):return hashlib.sha256(data).hexdigest()
def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n','utf8')

class Run:
    def __init__(self,review):
        self.root=ROOT/'tmp/linux-cae-20261118'/('acceptance-'+secrets.token_hex(5))
        (self.root/'private').mkdir(parents=True)
        self.result={'passed':False,'checks':[],'jobs':[],'flows':[],'real_model_calls':0,'mock_llm_calls':0,'review':review}
        self.client=httpx.Client(base_url='http://127.0.0.1:57700/api/v1',trust_env=False,timeout=120)
        self.save();print('LINUX_CAE_EVIDENCE '+str(self.root),flush=True)

    def save(self):write(self.root/'result.json',self.result)
    def check(self,name,**details):
        self.result['checks'].append({'name':name,**details});self.save();print('LINUX_CAE_PASS '+name,flush=True)

    def api(self,method,path,**kwargs):
        expected=kwargs.pop('expect',(200,201,202));until=time.monotonic()+30
        while True:
            response=self.client.request(method,path,**kwargs)
            detail=response.json().get('detail') if response.status_code==409 else None
            if not isinstance(detail,dict) or detail.get('code')!='storage_quota_busy':break
            assert time.monotonic()<until,'Storage admission stayed busy';time.sleep(.2)
        assert response.status_code in expected,(method,path,response.status_code,response.text[:1800])
        return response.json()

    def register(self):
        account={'email':'linux-cae-'+secrets.token_hex(6)+'@example.com','password':secrets.token_hex(24),'name':'Linux CAE acceptance'}
        write(self.root/'private/account.json',account)
        self.client.headers['Authorization']='Bearer '+self.api('POST','/auth/register',json=account)['token']
        self.workspace=self.api('GET','/workspaces')[0]['id']

    def put(self,path,content):return self.api('PUT',self.prefix+'/file',json={'path':path,'content':content})
    def raw(self,path):
        response=self.client.get(self.prefix+'/raw',params={'path':path});response.raise_for_status();return response.content

    def job(self,jid,expected='succeeded',timeout=240):
        until=time.monotonic()+timeout
        while time.monotonic()<until:
            job=self.api('GET','/jobs/'+str(jid))
            if job['status'] not in ['queued','running'] and ((job.get('attempt') or {}).get('process_stop') or {}).get('verified'):
                write(self.root/f'job-{jid}.json',job)
                if expected is not None:assert job['status']==expected,(jid,job['status'],job.get('log_backlog','')[-2000:])
                assert all(c['stop']['verified'] for c in job['attempt']['child_processes']['children'].values())
                if jid not in self.result['jobs']:self.result['jobs'].append(jid);self.save()
                return job
            time.sleep(.2)
        raise TimeoutError('Native job did not settle: '+str(jid))

    def setup(self,kind):
        case=self.api('POST',f'/workspaces/{self.workspace}/cases',json={'name':'Linux '+kind})
        self.prefix='/cases/'+str(case['id']);self.case_id=case['id']
        self.result.setdefault('cases',[]).append(case['id']);self.save()
        current=self.api('GET',self.prefix+'/caseDict');content=current['content']
        thermal=kind=='conduction';self.run_dir='C-run/conduction' if thermal else 'C-run/case-1'
        post={'runDir':self.run_dir}
        if thermal:
            post.update(fields={'T':{'unit':'K'}},dataExtraction={'volumeAverages':{'mean':{'field':'T','time':1}}},
                derivedQuantities={'referenceValues':{'linear':{'metric':'"mean.T@1"','value':350,'unit':'K',
                    'kind':'verification','source':'"analytic T(x)=300+100*x"','l2_max':1e-6}}})
        for key,value in {
            'caseMeta':{'discipline':'thermal' if thermal else 'fluid','analysisType':'steady' if thermal else 'transient',
                'dimensions':'3D' if thermal else '2D','units':{'length':'m','temperature':'K'}},
            'geometry':{'generator':'cadquery','parameters':{'lx':1,'ly':1,'lz':1}},
            'mesh':{'source':'blockMesh','qualityTargets':{'policy':'fail','maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100}},
            'numericalMethod':{'solver':'laplacianFoam' if thermal else 'icoFoam'},'postProcessing':post,
        }.items():content=set_block(content,key,value)
        self.api('PUT',self.prefix+'/caseDict',json={'content':content,'expected_revision':current['dict_revision']})
        inputs=thermal_files() if thermal else cavity_2d()
        for path,text in inputs.items():
            if thermal and path.endswith('/blockMeshDict'):text=text.replace('(20 2 2)','(10 4 2)')
            if not thermal and path.endswith('/controlDict'):
                text=text.replace('deltaT          0.005;','deltaT          0.00005;').replace('writeInterval   20;','writeInterval   100;')
            self.put(path,text)
        preview=self.put('A-geo/geometry.py',"import cadquery as cq\ndef build_geometry(ctx):\n return cq.Workplane('XY').box(1,1,1,centered=(False,False,False))\n")
        if preview.get('auto_preview_job'):self.job(preview['auto_preview_job'])

    def start(self,target):
        flow=self.api('POST',self.prefix+'/workflow-runs',json={'target':target,'run_dir':self.run_dir,
            'expected_revision':self.api('GET',self.prefix+'/caseDict')['dict_revision'],
            'budget':{'timeout_seconds':300,'max_log_bytes':16777216}},headers={'Idempotency-Key':str(uuid.uuid4())})
        self.result['flows'].append(flow['id']);self.save();return flow['id']

    def advance(self,fid,*,stop_at_solve=False):
        until=time.monotonic()+600;ready_since={}
        while time.monotonic()<until:
            flow=self.api('GET','/workflow-runs/'+fid)
            assert flow['status'] not in ['failed','cancelled','interrupted'],flow
            for step in flow['steps']:
                if stop_at_solve and step['key']=='solve' and step['status']=='running':return flow,step
                if step['status']=='awaiting_confirmation':
                    for producer in flow['steps']:
                        if producer['status']=='succeeded':
                            for attempt in producer['attempts']:self.job(attempt['job_id'])
                    self.api('POST',self.prefix+'/caseDict:advance',json={'state':step['confirmation_state'],'confirmed':True,
                        'expected_revision':step['confirmation_revision'],'workflow_step_id':step['id'],'expected_step_version':step['version']})
                    break
                if step['status']=='ready':
                    first=ready_since.setdefault(step['id'],time.monotonic())
                    if time.monotonic()-first>20:
                        response=self.client.post('/workflow-runs/'+fid+':dispatch',json={'expected_version':flow['version']})
                        if response.status_code!=200:
                            detail=response.json().get('detail',{})
                            assert isinstance(detail,dict) and detail.get('code')=='state_conflict',(response.status_code,response.text)
                        ready_since[step['id']]=time.monotonic()
            if flow['status']=='succeeded':return flow,None
            time.sleep(.15)
        raise TimeoutError('Workflow did not finish: '+fid)

    def conduction(self):
        self.setup('conduction');fid=self.start('report');flow,_=self.advance(fid)
        jobs={step['key']:self.job(step['attempts'][-1]['job_id']) for step in flow['steps'] if step['attempts']}
        assert set(jobs)=={'geometry','mesh','solve','postprocess','report'}
        for key in ['mesh','solve']:
            assert jobs[key]['attempt']['process_stop']['verified']
        toolchain=json.loads(self.raw('B-mesh/toolchain.json'))
        assert toolchain['distro']=='native-linux' and toolchain['openfoam']=='v2406'
        quality=json.loads(self.raw('B-mesh/quality.json'));assert quality['mesh_ok'] and quality['status']=='pass'
        solve=self.api('GET',self.prefix+'/solve/results');assert not solve['publication']['stale']
        assert solve['evidence']['parsed_log']['summary']['finished_cleanly']
        checkroot=self.root/'actual-thermal-fields'
        for name in ['constant/polyMesh/'+n for n in ['points','faces','owner','neighbour']]+['1/T']:
            path=checkroot/self.run_dir/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(self.raw(self.run_dir+'/'+name))
        l2,maximum=thermal_errors(checkroot,self.run_dir,80);assert l2<1e-6 and maximum<1e-6
        post=self.api('GET',self.prefix+'/postprocess/results');assert post['metrics']['readiness']['report_ready']
        assert post['metrics']['readiness']['can_claim_verification'] and not post['metrics']['readiness']['can_claim_validation']
        report=self.api('GET',self.prefix+'/report/results');assert not report['publication']['stale']
        for path,record in report['manifest']['artifacts'].items():
            response=self.client.get(self.prefix+'/report/artifact',params={'publication_id':report['publication']['id'],'path':path})
            response.raise_for_status();assert sha(response.content)==record['sha256'];(self.root/Path(path).name).write_bytes(response.content)
        write(self.root/'workflow.json',flow);write(self.root/'report.json',report)
        self.check('native_Linux_CadQuery_OpenFOAM_VTK_Pandoc_full_registered_S3_workflow',
            case_id=self.case_id,temperature_l2_K=l2,temperature_max_error_K=maximum)

    def cancellation_resume(self):
        self.setup('cavity-resume');fid=self.start('solve');flow,step=self.advance(fid,stop_at_solve=True)
        jid=step['attempts'][-1]['job_id'];job=self.api('GET','/jobs/'+str(jid));workspace=job['attempt']['workspace']
        checkpoint='/data/files/'+workspace['storage']['prefix']+'/'+workspace['run_path']+'/C-run/case-1/0.005'
        code=("from pathlib import Path;import time,sys;p=Path(sys.argv[1]);until=time.monotonic()+60\n"
              "while time.monotonic()<until:\n if all((p/n).is_file() for n in ['U','p']):print('WRITTEN');break\n time.sleep(.01)\n"
              "else:raise TimeoutError('Native checkpoint not written')")
        observed=subprocess.run(['docker','exec','vf-report-s3-20261117-arq-worker','python','-c',code,checkpoint],capture_output=True,text=True,timeout=70)
        assert observed.returncode==0 and 'WRITTEN' in observed.stdout,observed.stderr[:1000]
        current=self.api('GET','/jobs/'+str(jid));assert current['status']=='running','Actual solver completed before cancellation'
        flow=self.api('GET','/workflow-runs/'+fid)
        self.api('POST','/workflow-runs/'+fid+':cancel',json={'expected_version':flow['version']})
        self.resume(jid)

    def resume(self,jid):
        cancelled=self.job(jid,'cancelled');options=self.api('GET','/jobs/'+str(jid)+'/operations');assert options['resume']['available'],options
        self.check('actual_icoFoam_checkpoint_written_then_running_cancel_has_verified_stop',job_id=jid,checkpoint='0.005')
        payload={'action':'resume','source_attempt_id':options['source_attempt_id'],'source_version':options['source_version'],'parameters':{'time':'0.005'}}
        resumed=self.api('POST','/jobs/'+str(jid)+':repeat',json=payload,headers={'Idempotency-Key':str(uuid.uuid4())})
        done=self.job(resumed['id'],timeout=300)
        result=self.api('GET',self.prefix+'/solve/results');parsed=result['evidence']['parsed_log']
        assert parsed['summary']['finished_cleanly'] and math.isclose(parsed['summary']['last_time'],.5,abs_tol=1e-12)
        assert min(parsed['times'])>.005
        assert done['attempt']['process_stop']['verified']
        write(self.root/'resumed-solve.json',result)
        self.check('real_native_Linux_checkpoint_resume_finishes_remaining_solver_steps',source_job=jid,resumed_job=done['id'])

    def continue_cancelled(self,previous,jid):
        previous=(ROOT/previous).resolve();prior=json.loads((previous/'result.json').read_text('utf8'))
        account=json.loads((previous/'private/account.json').read_text('utf8'))
        write(self.root/'private/account.json',account)
        self.client.headers['Authorization']='Bearer '+self.api('POST','/auth/login',json={
            'email':account['email'],'password':account['password']})['token']
        job=self.api('GET','/jobs/'+str(jid))
        assert job['status']=='cancelled' and job['case_id'] in prior['cases']
        self.case_id=job['case_id'];self.prefix='/cases/'+str(self.case_id);self.run_dir='C-run/case-1'
        self.result['cases']=prior['cases'];self.result['flows']=prior['flows']
        self.result['previous_evidence']=previous.relative_to(ROOT).as_posix();self.save()
        self.resume(jid)

    def close(self):
        for fid in self.result['flows']:
            flow=self.api('GET','/workflow-runs/'+fid)
            if flow['status'] not in ['succeeded','failed','cancelled','interrupted']:
                self.api('POST','/workflow-runs/'+fid+':cancel',json={'expected_version':flow['version']})
        for cid in self.result.get('cases',[]):
            for job in self.api('GET','/cases/'+str(cid)+'/jobs'):
                if job['status'] in ['queued','running']:self.api('POST','/jobs/'+str(job['id'])+':cancel')
                self.job(job['id'],expected=None)
        self.client.close()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('review')
    parser.add_argument('--continue-from');parser.add_argument('--cancelled-job',type=int);args=parser.parse_args()
    assert bool(args.continue_from)==bool(args.cancelled_job)
    review=json.loads((ROOT/args.review).read_text('utf8'));assert review['classification']=='no_llm'
    for name,digest in review['sources'].items():assert sha((ROOT/name).read_bytes())==digest,name
    run=Run(args.review)
    try:
        if args.continue_from:run.continue_cancelled(args.continue_from,args.cancelled_job)
        else:
            run.register();capabilities=run.api('GET','/runtime/capabilities');write(run.root/'capabilities.json',capabilities)
            native=capabilities['wsl'];assert native['reachable'] and native['openfoam'] and native['wmake'] and native['distro']=='native-linux',native
            run.check('owned_native_Linux_OpenFOAM_and_wmake_capability_probe')
            run.conduction();run.cancellation_resume()
        run.result['functional_passed']=True
    except BaseException as error:
        run.result['failure']={'type':type(error).__name__,'message':str(error)[:2400]};raise
    finally:
        try:run.close();run.result['all_native_stops_verified']=True
        finally:
            run.result['passed']=bool(run.result.get('functional_passed') and run.result.get('all_native_stops_verified'));run.save()

if __name__=='__main__':main()
