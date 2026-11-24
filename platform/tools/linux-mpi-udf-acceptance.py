"""Actual serial/MPI/UDF engineering workflows, numerical comparison and report downloads; no LLM."""
import argparse,hashlib,importlib.util,json,math,re,secrets,sys,uuid
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'platform/server'))
from cae_fixtures import cavity_2d
from app.services.casedict import set_block
from testsupport import IsolatedServer
spec=importlib.util.spec_from_file_location('linux_cae_acceptance',Path(__file__).with_name('linux-cae-acceptance.py'))
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)
write=base.write;sha=base.sha

def vectors(raw):
    match=re.search(r'internalField\s+nonuniform\s+List<vector>\s+(\d+)\s*\((.*?)\)\s*;',raw.decode('utf8'),re.S)
    assert match,'Actual solver did not write a nonuniform vector field'
    values=[tuple(float(n) for n in row.split()) for row in re.findall(r'\(([^()]*)\)',match.group(2))]
    assert len(values)==int(match.group(1))==400
    assert all(len(v)==3 and all(math.isfinite(n) and abs(n)<5 for n in v) for v in values)
    return values

class Run(base.Run):
    def __init__(self,review,url,windows):
        self.root=ROOT/'tmp/linux-mpi-udf-20261118'/('windows-' if windows else 'linux-')
        self.root=self.root.with_name(self.root.name+secrets.token_hex(5));(self.root/'private').mkdir(parents=True)
        self.result={'passed':False,'checks':[],'jobs':[],'flows':[],'real_model_calls':0,'mock_llm_calls':0,'review':review,'windows':windows}
        self.client=httpx.Client(base_url=url+'/api/v1',trust_env=False,timeout=120)
        self.windows=windows;self.reference=None;self.save();print('MPI_UDF_EVIDENCE '+str(self.root),flush=True)

    def setup(self,kind):
        case=self.api('POST',f'/workspaces/{self.workspace}/cases',json={'name':'Actual '+kind+' engineering'})
        self.case_id=case['id'];self.prefix='/cases/'+str(self.case_id);self.run_dir='C-run/case-1'
        self.result.setdefault('cases',[]).append(self.case_id);self.save()
        current=self.api('GET',self.prefix+'/caseDict');content=current['content']
        for key,value in {
            'caseMeta':{'discipline':'fluid','analysisType':'transient','dimensions':'2D','units':{'length':'m'}},
            'geometry':{'generator':'cadquery','parameters':{'lx':1,'ly':1,'lz':1}},
            'mesh':{'source':'blockMesh','qualityTargets':{'policy':'fail','maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100}},
            'numericalMethod':{'solver':'icoFoam'},
            'postProcessing':{'runDir':self.run_dir,'fields':{'p':{'unit':'m2/s2'}},
                'dataExtraction':{'volumeAverages':{'meanPressure':{'field':'p','time':.5}}}},
        }.items():content=set_block(content,key,value)
        self.api('PUT',self.prefix+'/caseDict',json={'content':content,'expected_revision':current['dict_revision']})
        self.inputs=cavity_2d()
        for path,content in self.inputs.items():self.put(path,content)
        if kind=='udf':
            self.api('POST',self.prefix+'/udf/scaffold',json={'name':'myFunctionObject','objective':'Actual compile/load/solve with unique marker'})
            path='UDF/myFunctionObject/myFunctionObject.C';cpp=self.raw(path).decode('utf8');assert 'P0_UDF_OK' in cpp
            self.marker='NATIVE_UDF_'+secrets.token_hex(8);self.put(path,cpp.replace('P0_UDF_OK',self.marker))
        saved=self.put('A-geo/geometry.py',"import cadquery as cq\ndef build_geometry(ctx):\n return cq.Workplane('XY').box(1,1,1)\n")
        if saved.get('auto_preview_job'):self.job(saved['auto_preview_job'])

    def workflow(self,mode):
        self.setup(mode)
        parameters={'solve_mode':mode}
        if mode=='mpi':parameters['subdomains']=2
        if mode=='udf':parameters['udf_name']='myFunctionObject'
        flow=self.api('POST',self.prefix+'/workflow-runs',json={'target':'solve' if mode=='serial' else 'report',
            'run_dir':self.run_dir,'expected_revision':self.api('GET',self.prefix+'/caseDict')['dict_revision'],
            'parameters':parameters,'budget':{'timeout_seconds':360,'max_log_bytes':16777216}},
            headers={'Idempotency-Key':str(uuid.uuid4())})
        self.result['flows'].append(flow['id']);self.save()
        flow,_=self.advance(flow['id'])
        jobs={step['key']:self.job(step['attempts'][-1]['job_id'],timeout=420) for step in flow['steps'] if step['attempts']}
        solve=self.api('GET',self.prefix+'/solve/results');assert not solve['publication']['stale']
        summary=solve['evidence']['parsed_log']['summary'];assert summary['finished_cleanly'] and summary['last_time']==.5
        raw=self.raw(self.run_dir+'/0.5/U');values=vectors(raw);(self.root/(mode+'-U')).write_bytes(raw)
        write(self.root/(mode+'-workflow.json'),flow);write(self.root/(mode+'-solve.json'),solve)
        if mode=='serial':
            reference={'case_id':self.case_id,'job_id':jobs['solve']['id'],'sha256':sha(raw),'field':'serial-U',
                'inputs':{p:sha(v.encode()) for p,v in self.inputs.items()}}
            write(self.root/'serial-reference.json',reference);self.reference=values
            self.check('actual_serial_400_cell_velocity_reference',case_id=self.case_id,job_id=jobs['solve']['id']);return
        assert self.reference is not None
        error=max(abs(a-b) for left,right in zip(self.reference,values) for a,b in zip(left,right))/max(1,max(abs(x) for v in self.reference for x in v))
        assert error<=1e-5,(mode,error)
        native=solve['toolchain'];assert native['distro']==('vibeflow-linux' if self.windows else 'native-linux')
        if self.windows:assert native['uid']!=0
        assert all(s['returncode']==0 for s in native['steps'].values())
        if mode=='mpi':
            assert set(native['processors'])=={'processor0','processor1'}
            assert set(native['steps'])=={'decompose','solve','reconstruct'}
        else:
            assert set(native['steps'])=={'load','solve'} and 'udf-build' in jobs
            assert self.marker in self.raw(self.run_dir+'/udf-solve.log').decode('utf8')
            library=self.raw('UDF/myFunctionObject/libmyFunctionObject.so');assert library[:4]==b'\x7fELF'
            assert sha(library)==native['library']['sha256']
            assert b'UDF_LIBRARY_LOADED' in self.raw('UDF/myFunctionObject/udf-load.log')
            self.result['udf_library_sha256']=sha(library);self.result['udf_marker']=self.marker
        expected_purpose=mode+'_run'+('' if self.windows else '_linux')
        child=jobs['solve']['attempt']['child_processes']['children'];assert len(child)==1
        assert next(iter(child.values()))['purpose']==expected_purpose
        assert all(self.raw(p)==v.encode() for p,v in self.inputs.items()),'Live engineering inputs changed'
        post=self.api('GET',self.prefix+'/postprocess/results');assert post['metrics']['readiness']['report_ready']
        mean=next(row['value'] for row in post['metrics']['metrics'] if row['name']=='meanPressure.p@0.5')
        assert abs(mean-.022259352333744136)<1e-5,mean
        report=self.api('GET',self.prefix+'/report/results');assert not report['publication']['stale']
        for path,item in report['manifest']['artifacts'].items():
            r=self.client.get(self.prefix+'/report/artifact',params={'publication_id':report['publication']['id'],'path':path});r.raise_for_status()
            assert sha(r.content)==item['sha256'];(self.root/(mode+'-'+Path(path).name)).write_bytes(r.content)
        write(self.root/(mode+'-report.json'),report)
        self.check('actual_'+mode+'_native_workflow_to_report',case_id=self.case_id,job_id=jobs['solve']['id'],
            velocity_relative_error=error,mean_pressure=mean,uid=native['uid'],publication_id=report['publication']['id'])

    def reuse_serial(self,folder):
        root=(ROOT/folder).resolve();reference=json.loads((root/'serial-reference.json').read_text('utf8'))
        assert reference['inputs']=={p:sha(v.encode()) for p,v in cavity_2d().items()}
        raw=(root/'serial-U').read_bytes();assert sha(raw)==reference['sha256'];self.reference=vectors(raw)
        self.result['reused_actual_serial_reference']=root.relative_to(ROOT).as_posix();self.save()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('review');parser.add_argument('--windows',action='store_true')
    parser.add_argument('--modes',nargs='+',choices=['serial','mpi','udf'],default=['serial','mpi','udf'])
    parser.add_argument('--serial-reference');args=parser.parse_args()
    review=json.loads((ROOT/args.review).read_text('utf8'));assert review['classification']=='no_llm'
    for name,digest in review['sources'].items():assert sha((ROOT/name).read_bytes())==digest,name
    server=IsolatedServer('native-mpi-udf-windows') if args.windows else None
    run=None
    try:
        if server:server.__enter__()
        run=Run(args.review,server.base if server else 'http://127.0.0.1:57700',args.windows)
        if server:run.result['isolated_API']=str(server.root);run.save()
        run.register()
        if args.serial_reference:run.reuse_serial(args.serial_reference)
        for mode in args.modes:run.workflow(mode)
        run.result['functional_passed']=True
    except BaseException as error:
        if run:run.result['failure']={'type':type(error).__name__,'message':str(error)[:3000]}
        raise
    finally:
        try:
            if run:run.close();run.result['all_native_stops_verified']=True
        finally:
            if server and server.proc is not None:
                server.__exit__(*sys.exc_info())
                if run:run.result['isolated_API_exited']=server.proc.poll() is not None
            if run:
                run.result['passed']=bool(run.result.get('functional_passed') and run.result.get('all_native_stops_verified'));run.save()

if __name__=='__main__':main()
