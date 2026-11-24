"""Cancel real running Linux MPI ranks/UDF solver and verify the owned PID namespace is empty."""
import argparse,importlib.util,json,subprocess,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('professional_acceptance',Path(__file__).with_name('linux-mpi-udf-acceptance.py'))
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
SCAN="""import json,os,sys
from pathlib import Path
rows=[]
for p in Path('/proc').iterdir():
 if not p.name.isdigit():continue
 try:
  if os.readlink(p/'ns/pid')!='pid:['+sys.argv[1]+']':continue
  rows.append({'pid':int(p.name),'name':(p/'comm').read_text().strip()})
 except (FileNotFoundError,ProcessLookupError,PermissionError):pass
print(json.dumps(rows))
"""
def processes(namespace):
    assert namespace.isdigit()
    r=subprocess.run(['docker','exec','vf-report-s3-20261117-arq-worker','python','-c',SCAN,namespace],capture_output=True,text=True,timeout=15)
    assert r.returncode==0,r.stderr[:500]
    return json.loads(r.stdout)

def scenario(run,mode):
    run.setup(mode)
    path=run.run_dir+'/system/controlDict'
    # A real long integration gives the public cancel request time to arrive.
    # No fake process, delayed solver or synthetic output replaces OpenFOAM.
    run.put(path,run.inputs[path].replace('deltaT          0.005;','deltaT          0.000001;').replace('writeInterval   20;','writeInterval   100000;'))
    parameters={'solve_mode':mode,**({'subdomains':2} if mode=='mpi' else {'udf_name':'myFunctionObject'})}
    flow=run.api('POST',run.prefix+'/workflow-runs',json={'target':'solve','run_dir':run.run_dir,
        'expected_revision':run.api('GET',run.prefix+'/caseDict')['dict_revision'],'parameters':parameters,
        'budget':{'timeout_seconds':240,'max_log_bytes':16777216}},headers={'Idempotency-Key':str(uuid.uuid4())})
    run.result['flows'].append(flow['id']);run.save()
    _,step=run.advance(flow['id'],stop_at_solve=True);jid=step['attempts'][-1]['job_id']
    until=time.monotonic()+90;namespace=None;observed=[]
    while time.monotonic()<until:
        job=run.api('GET','/jobs/'+str(jid));assert job['status'] in ['queued','running'],job
        children=(job.get('attempt') or {}).get('child_processes') or {}
        for child in children.get('children',{}).values():
            if child['phase']=='running' and child['purpose']==mode+'_run_linux':
                namespace=child['identity']['namespace_init']['pid_namespace'];observed=processes(namespace)
        if sum(row['name']=='icoFoam' for row in observed)==(2 if mode=='mpi' else 1):break
        time.sleep(.1)
    else:raise TimeoutError('Actual solver ranks did not become visible in their owned namespace')
    before={'job_id':jid,'namespace':namespace,'processes':observed,'observed_running_job':job['status']}
    base.write(run.root/(mode+'-before-cancel.json'),before)
    flow=run.api('GET','/workflow-runs/'+flow['id'])
    run.api('POST','/workflow-runs/'+flow['id']+':cancel',json={'expected_version':flow['version']})
    stopped=run.job(jid,'cancelled');after=processes(namespace);assert not after,after
    assert stopped['attempt']['process_stop']['verified']
    base.write(run.root/(mode+'-after-cancel.json'),{'namespace':namespace,'processes':after})
    run.check('actual_'+mode+'_running_cancel_empties_owned_Linux_namespace',job_id=jid,
        actual_icoFoam_processes=sum(row['name']=='icoFoam' for row in observed),namespace=namespace)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('review')
    parser.add_argument('--modes',nargs='+',choices=['mpi','udf'],default=['mpi','udf']);args=parser.parse_args()
    review=json.loads((ROOT/args.review).read_text('utf8'));assert review['classification']=='no_llm'
    for name,digest in review['sources'].items():assert base.sha((ROOT/name).read_bytes())==digest,name
    run=base.Run(args.review,'http://127.0.0.1:57700',False);run.result['scope']='real Linux running cancellation'
    try:
        run.register()
        for mode in args.modes:scenario(run,mode)
        run.result['functional_passed']=True
    except BaseException as error:
        run.result['failure']={'type':type(error).__name__,'message':str(error)[:3000]};raise
    finally:
        try:run.close();run.result['all_native_stops_verified']=True
        finally:
            run.result['passed']=bool(run.result.get('functional_passed') and run.result.get('all_native_stops_verified'));run.save()

if __name__=='__main__':main()
