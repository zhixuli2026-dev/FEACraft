"""Native S3 professional handlers, guarded outputs and empty-cache history."""
import argparse
from contextlib import nullcontext
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
from urllib.parse import quote

from app.services.casedict import set_block
from cae_fixtures import cavity_2d
from cae_benchmarks import thermal_files
from calculix_publication_smoke import DECK, displacement
from su2_publication_smoke import BASE, MESH, field, history
from paraview_publication_smoke import SCRIPT, sphere, image
from mpi_publication_smoke import compare, RUN
from flotherm_publication_smoke import input_ir
from flotherm_library_publication_smoke import seed as library_seed, LIB as FLO_LIB
from professional_launch_smoke import settled as wait_settled
from job_release_smoke import wait_file
from s3_core_workflow_smoke import workflow_api
from s3_document_workflows_smoke import new_case, raw, seed as remote_seed
from s3_typed_publication_smoke import RUNTIME
from s3testsupport import ObjectStore

GROUPS={'solvers':('calculix','su2','paraview'),
    'openfoam':('postprocess','mpi','udf-build','udf-integrate'),
    'flotherm':('flotherm-convert','flotherm-library')}
UDF='UDF/myFunctionObject';UDF_LIB=UDF+'/libmyFunctionObject.so'


def professional_api(store,peer=None):
    api=workflow_api(store,peer=peer)
    fixture=api.root/'s3_typed_fixture.py'
    with fixture.open('a',encoding='utf8') as stream:
        stream.write('''
from app.services.udf_handler import UdfHandler
_udf_source=UdfHandler.source
def diagnosed_source(self,*args,**kwargs):
 try:return _udf_source(self,*args,**kwargs)
 except Exception:
  import traceback
  traceback.print_exc()
  raise
UdfHandler.source=diagnosed_source
''')
    return api


def settled(api,job_id):
    # MPI publishes149 captured files in the real fixture. Native execution
    # finished before the earlier90s timeout; remote capture/publication did not.
    started=time.monotonic()
    try:return wait_settled(api,job_id,timeout=300)
    finally:
        with (api.root/'settlement-timings.jsonl').open('a',encoding='utf8') as stream:
            stream.write(json.dumps({'job_id':job_id,'seconds':round(time.monotonic()-started,3),'limit':300})+'\n')


def reference(api,publication,*,current=True,expect=200):
    created=api.call('POST',api.prefix+'/jobs',{'command':'echo PROFESSIONAL_REFERENCE',
        'input_publications':[{'publication_id':publication,'require_current':current}]},expect=expect)
    if expect==200:assert settled(api,created['id'])['status']=='succeeded'


def seed(store,case,path,data):
    if getattr(store,'backend','s3')=='fs':
        target=store.root/f"files/workspaces/{case['workspace_id']}/cases/{case['id']}"/path
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
    else:remote_seed(store,case,path,data)


def finish(api,created):
    job=settled(api,created.get('job_id',created.get('id')))
    assert job['status']=='succeeded',{'status':job['status'],'log':job.get('log_backlog','')[-3000:],'outputs':job['attempt']['outputs']}
    assert job['attempt']['outputs']['publication']['status']=='committed'
    return job


def live_files(store,case,directory):
    prefix=f"workspaces/{case['workspace_id']}/cases/{case['id']}/"
    if getattr(store,'backend','s3')=='fs':
        root=store.root/'files'/prefix
        return {p.relative_to(root).as_posix():p.read_bytes() for p in (root/directory).rglob('*') if p.is_file()}
    result={}
    for page in store.client.get_paginator('list_objects_v2').paginate(Bucket=store.bucket,Prefix=prefix+directory+'/'):
        for item in page.get('Contents',[]):
            key=item['Key']
            if not key.endswith('/'):
                result[key[len(prefix):]]=store.client.get_object(Bucket=store.bucket,Key=key)['Body'].read()
    return result


def openfoam_seed(api,store,kinds):
    donor={}
    if set(kinds)&{'mpi','udf-build','udf-integrate'}:
        case=new_case(api,'Native cavity donor')
        for path,text in cavity_2d().items():api.put(path,text)
        command='source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd '+RUN+' && '
        finish(api,api.call('POST',api.prefix+'/jobs',{'type':'mesh','runtime':'wsl','command':command+'blockMesh'}))
        donor['initial']=live_files(store,case,RUN)
        finish(api,api.call('POST',api.prefix+'/jobs',{'type':'solve','runtime':'wsl','run_dir':RUN,'command':command+'icoFoam'}))
        donor['serial']=raw(api,RUN+'/0.5/U')
        if set(kinds)&{'udf-build','udf-integrate'}:
            scaffold=api.call('POST',api.prefix+'/udf/scaffold',{})
            sources={UDF+'/'+path:raw(api,UDF+'/'+path) for path in scaffold['files']}
            finish(api,api.call('POST',api.prefix+'/udf:build',{}))
            library=raw(api,UDF_LIB);assert library.startswith(b'\x7fELF')
            donor['udf']={**sources,UDF_LIB:library}
    if 'postprocess' in kinds:
        case=new_case(api,'Native conduction donor')
        for path,text in thermal_files().items():api.put(path,text)
        finish(api,api.call('POST',api.prefix+'/jobs',{'type':'solve','runtime':'wsl',
            'command':'source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd C-run/conduction && blockMesh && laplacianFoam'}))
        donor['thermal']=live_files(store,case,'C-run/conduction')
        assert 'C-run/conduction/1/T' in donor['thermal']
    return donor


def prepare(api,store,kind,mode,donor):
    case=new_case(api,kind+' remote '+mode)
    api.put('unrelated.txt','retain user bytes')
    if kind=='calculix':
        api.put('Ref/model.inc',DECK);api.put('Ref/nested.inc','*INCLUDE, INPUT=Ref/model.inc\n')
        path='C-run/space folder/beam.inp';api.put(path,'*INCLUDE, INPUT="Ref/nested.inc"\n')
        endpoint='/calculix:solve';body={'input_path':path};source='Ref/model.inc';target=path[:-4]+'.frd'
    elif kind=='su2':
        api.put('Ref/naca0012.su2',MESH);api.put('C-run/flow.cfg',BASE)
        endpoint='/su2:solve';body={'config_path':'C-run/flow.cfg'};source='Ref/naca0012.su2';target='flow.vtu'
    elif kind=='paraview':
        api.put('Ref/render.json','{"radius":0.5}');api.put('Retired/last.csv','obsolete')
        api.put('D-post/render.py',SCRIPT+"(root/'Retired/last.csv').unlink()\n")
        endpoint='/paraview:run';body={'script_path':'D-post/render.py'};source='Ref/render.json';target='D-post/paraview-sphere.vtp'
    elif kind=='postprocess':
        for path,data in donor['thermal'].items():seed(store,case,path,data)
        saved=api.call('GET',api.prefix+'/caseDict')
        config={'runDir':'C-run/conduction','fields':{'T':{'unit':'K'}},
            'dataExtraction':{'volumeAverages':{'mean':{'field':'T','time':1}}}}
        api.call('PUT',api.prefix+'/caseDict',{'content':set_block(saved['content'],'postProcessing',config),
            'expected_revision':saved['dict_revision']})
        endpoint='/postprocess:run';body={'run_dir':'C-run/conduction'}
        source='C-run/conduction/1/T';target='D-post/data/latest-postprocess.json'
    elif kind in {'mpi','udf-build','udf-integrate'}:
        for path,data in donor['initial'].items():seed(store,case,path,data)
        if kind!='mpi':
            for path,data in donor['udf'].items():seed(store,case,path,data)
        if kind=='mpi':
            endpoint='/mpi:run';body={'run_dir':RUN,'solver':'icoFoam','subdomains':2}
            source=RUN+'/system/fvSolution';target=RUN+'/0.5/U'
        else:
            operation=kind.split('-')[1];endpoint='/udf:'+operation
            body={} if operation=='build' else {'run_dir':RUN}
            source=UDF+'/myFunctionObject.H';target=UDF_LIB if operation=='build' else RUN+'/0.5/U'
    elif kind=='flotherm-convert':
        api.put('Ref/ir.json',json.dumps(input_ir()));api.put('Ref/settings.json',json.dumps({'power':{'chip':{'value':7500,'unit':'mW'}}}))
        endpoint='/flotherm:convert';body={'ir_path':'Ref/ir.json','settings_path':'Ref/settings.json','output_dir':'C-run/thermal'}
        source='Ref/settings.json';target='C-run/thermal/constant/fvOptions'
    else:
        library_seed(api,'Ref/helper source')
        endpoint='/flotherm:build-library';body={'source_dir':'Ref/helper source','destination_dir':'UDF/flotherm-helper'}
        source='Ref/helper source/flothermHelper.C';target=FLO_LIB
    previous=None
    if kind not in {'postprocess','flotherm-convert','flotherm-library'}:
        previous=donor['udf'][UDF_LIB] if kind=='udf-build' else b'previous user output'
        seed(store,case,target,previous)
    return case,endpoint,body,source,target,previous


def validate_artifact(api,store,kind,job,target,donor):
    publication=job['attempt']['outputs']['publication']['id']
    manifest=json.loads(raw(api,'.vibeflow/publications/'+publication+'/manifest.json'))
    declared=job['attempt']['handler']['toolchain']
    assert declared['schema']=='vibeflow.handler-toolchain.v1'
    assert declared['components']['worker_sha256']==manifest['descriptor']['worker_sha256']
    assert declared['execution_sha256']==job['attempt']['handler']['execution_sha256']
    assert job['attempt']['workspace']['toolchain']['scope']=='submission_host'
    for name in ('native_sha256','common_native_sha256','executable_sha256'):
        if name in manifest['descriptor']:
            assert declared['components'][name]==manifest['descriptor'][name]
    data=raw(api,target)
    assert raw(api,'.vibeflow/publications/'+publication+'/files/'+target)==data
    local=store.root/('verified-'+kind+Path(target).suffix);local.write_bytes(data)
    if kind=='calculix':displacement(data)
    elif kind=='su2':
        field(local);csv=store.root/'verified-su2.csv';csv.write_bytes(raw(api,'history.csv'));history(csv)
    elif kind=='paraview':
        sphere(local);image(raw(api,'D-post/paraview-sphere.png'))
        api.call('GET',api.prefix+'/raw?path=Retired/last.csv',expect=404)
    elif kind=='postprocess':
        result=api.call('GET',api.prefix+'/postprocess/results')
        metric=next(m for m in result['metrics']['metrics'] if m['name']=='mean.T@1')
        assert abs(metric['value']-350)<1e-6
        assert not result['publication']['stale'] and not manifest['descriptor']['input_publications']
    elif kind in {'mpi','udf-integrate'}:
        compare(donor['serial'],data)
        if kind=='mpi':
            assert manifest['descriptor']['configuration']['pmix_gds']=='hash'
            assert manifest['descriptor']['receipt']['native']['steps']['solve']['environment']=={'PMIX_MCA_gds':'hash'}
            status=api.call('GET',api.prefix+'/mpi:status?run_dir='+quote(RUN,safe=''))
            assert status['complete'] and status['subdomains']==2 and '0.5' in status['times'],status
        else:
            assert b'P0_UDF_OK' in raw(api,RUN+'/udf-solve.log')
            assert b'myFunctionObject' in raw(api,RUN+'/system/controlDict')
    elif kind in {'udf-build','flotherm-library'}:assert data.startswith(b'\x7fELF') and len(data)>1000
    else:
        coverage=json.loads(raw(api,'C-run/thermal/conversion-coverage.json'))
        assert coverage['summary']['input_power_w']==7.5
        assert json.loads(raw(api,'C-run/thermal/preflight.json'))['ok']
    if kind not in {'postprocess','flotherm-convert'}:
        assert manifest['descriptor']['receipt']['attempt_id']==job['attempt']['id']
        children=list(job['attempt']['child_processes']['children'].values())
        assert children and all(c['phase']=='stopped' and c['stop']['verified'] for c in children)
    return publication,data


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--group',choices=(*GROUPS,'all'),default='all')
    parser.add_argument('--fs',action='store_true',help='Same native assertions on filesystem storage')
    parser.add_argument('--kinds',nargs='+');parser.add_argument('--modes',nargs='+',choices=('normal','source','destination','cancel'))
    args=parser.parse_args();available=tuple(k for group in GROUPS.values() for k in group) if args.group=='all' else GROUPS[args.group]
    kinds=args.kinds or available
    assert set(kinds)<=set(available)
    modes=args.modes or ('normal','source','destination','cancel');checks=[]
    if args.fs:
        from testsupport import SERVER
        directory=Path(tempfile.mkdtemp(prefix='fs-professional-handlers-',dir=SERVER.parents[1]/'tmp'))
        context=nullcontext(SimpleNamespace(root=directory,backend='fs',settings=lambda:{
            'VIBEFLOW_STORAGE_BACKEND':'fs','VIBEFLOW_STORAGE_FS_ROOT':str(directory/'files')}))
    else:context=ObjectStore(executable=RUNTIME)
    with context as store:
        with professional_api(store) as api:
            donor=openfoam_seed(api,store,kinds) if set(kinds)&set(GROUPS['openfoam']) else {}
            for kind in kinds:
                for mode in modes:
                    (api.root/'mode').write_text('normal')
                    case,endpoint,body,source,target,previous=prepare(api,store,kind,mode,donor)
                    before=raw(api,source)
                    control=raw(api,RUN+'/system/controlDict') if kind=='udf-integrate' else None
                    for marker in ('ready','release'):(api.root/marker).unlink(missing_ok=True)
                    (api.root/'mode').write_text(mode)
                    headers={'Idempotency-Key':kind+'-'+mode}
                    created=api.call('POST',api.prefix+endpoint,body,extra_headers=headers)
                    if mode!='normal':
                        wait_file(api.root/'ready',seconds=300)
                        if previous is None:api.call('GET',api.prefix+'/raw?path='+quote(target,safe='/'),expect=404)
                        else:assert raw(api,target)==previous
                        if control is not None:assert raw(api,RUN+'/system/controlDict')==control
                        if mode=='source':seed(store,case,source,before+b'\n')
                        if mode=='destination':seed(store,case,target,b'concurrent user output')
                        if mode=='cancel':api.call('POST',f'/jobs/{created["job_id"]}:cancel')
                        (api.root/'release').touch()
                    job=settled(api,created['job_id'])
                    assert job['attempt']['workspace'].get('storage',{}).get('backend','fs')==('fs' if args.fs else 's3')
                    (store.root/(kind+'-'+mode+'-job.json')).write_text(json.dumps(job,indent=2),encoding='utf8')
                    assert raw(api,'unrelated.txt')==b'retain user bytes'
                    assert raw(api,job['attempt']['workspace']['inputs_path']+'/'+source)==before
                    if mode!='normal':
                        publication=job['attempt']['outputs']['publication']
                        assert job['status']==('cancelled' if mode=='cancel' else 'failed'),job
                        assert publication['status']!='committed'
                        if mode=='destination':assert raw(api,target)==b'concurrent user output'
                        elif previous is not None:assert raw(api,target)==previous
                        else:api.call('GET',api.prefix+'/raw?path='+quote(target,safe='/'),expect=404)
                        if control is not None:assert raw(api,RUN+'/system/controlDict')==control
                        checks.append(kind+'_'+mode+'_protects_remote_files_and_publication')
                    else:
                        assert job['status']=='succeeded',job
                        assert job['attempt']['outputs']['publication']['status']=='committed'
                        publication,data=validate_artifact(api,store,kind,job,target,donor)
                        assert api.call('POST',api.prefix+endpoint,body,extra_headers=headers)['job_id']==created['job_id']
                        with professional_api(store,peer=api) as reader:
                            reference(reader,publication)
                            seed(store,case,source,before+b'\n')
                            reference(reader,publication,expect=409)
                            reference(reader,publication,current=False)
                            assert raw(reader,'.vibeflow/publications/'+publication+'/files/'+target)==data
                        checks.append(kind+'_native_artifact_replay_fresh_cache_current_and_historical_reference')
                    print('PASS '+checks[-1],flush=True)
                    (store.root/'professional-partial.json').write_text(json.dumps({'checks':checks,'api':str(api.root)},indent=2),encoding='utf8')
            if set(kinds)&set(GROUPS['flotherm']):
                new_case(api,'Remote input inspection');api.put('Ref/model.ecxml','<root><bbox>0 0 0 1 1 1</bbox></root>')
                inspected=api.call('POST',api.prefix+'/flotherm:inspect',{'paths':['Ref/model.ecxml']})
                assert inspected['ok'] and inspected['files'][0]['bbox_count']==1
                checks.append('flotherm_remote_format_and_bbox_inspection')
            result={'passed':True,'backend':'fs' if args.fs else 's3','group':args.group,'kinds':list(kinds),'modes':list(modes),
                'count':len(checks),'checks':checks,'api':str(api.root),'evidence':str(store.root)}
    (store.root/'professional-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('S3_PROFESSIONAL_HANDLERS_OK '+json.dumps(result),flush=True)


if __name__=='__main__':main()
