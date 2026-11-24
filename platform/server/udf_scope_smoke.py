"""Actual wmake/cc1plus children: bad registration, cancel, deadline, owner death."""
import hashlib
import json
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from testsupport import IsolatedServer, SERVER
from mpi_scope_smoke import inspect_native
from professional_launch_smoke import settled
from app.services.owned_process import wsl_path
from app.services import process_identity_native as identity

CONTROLLED='''import asyncio,copy,json,os
from pathlib import Path
from app.services import jobrunner,job_execution
from app.services import process_identity_native as identity
root=Path(__file__).parent
original_init=jobrunner.JobProcess.__init__
def init(self,*args,**kwargs):
    if (root/'mode').read_text()=='timeout':
        argv=kwargs.get('argv') or []
        kwargs['timeout_seconds']=(25 if any(str(v).endswith('udf_worker.py') for v in argv)
            else 12 if any(str(v).endswith('flotherm_library_worker.py') for v in argv) else 8)
    original_init(self,*args,**kwargs)
jobrunner.JobProcess.__init__=init
original_phase=job_execution.phase
async def phase(owner,target,*,identity=None):
    if target=='running':
        (root/'launch.json').write_text(json.dumps({'job_id':owner.job_id,'identity':identity}))
        if (root/'mode').read_text()=='bad_registration':
            identity=copy.deepcopy(identity);identity['launch_nonce']='0'*64
    return await original_phase(owner,target,identity=identity)
job_execution.phase=phase
from app.main import app
@app.on_event('startup')
async def own_exit():
    (root/'actual-owner.json').write_text(json.dumps(identity.observe(os.getpid())))
    async def watch():
        while not (root/'exit-owner').exists():await asyncio.sleep(.02)
        os._exit(125)
    asyncio.create_task(watch())
'''


def until(callback, seconds=15):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        value=callback()
        if value:return value
        time.sleep(.05)
    raise TimeoutError('UDF scope observation timed out')


def compiler_location(member, directory):
    script='''import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import process_identity_native as identity
request=json.load(sys.stdin);record=request['identity']
assert identity.probe(record)['state']=='match'
cwd=os.readlink('/proc/'+str(record['pid'])+'/cwd')
assert Path(cwd).resolve()==Path(request['expected']).resolve(),cwd
assert identity.probe(record)['state']=='match'
print(json.dumps({'identity':record,'cwd':cwd}))
'''
    result=subprocess.run(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-c',script,
        wsl_path(SERVER/'app/services')],input=json.dumps({'identity':member['identity'],
            'expected':str(directory) if str(directory).startswith('/') else wsl_path(directory)}).encode(),
        capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode==0,result.stderr.decode('utf8','replace')
    return json.loads(result.stdout)


def inventory(directory):
    return {p.relative_to(directory).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in directory.rglob('*') if p.is_file()}


def verify(backend='udf', *, copy_source=False):
    assert backend in {'udf','flotherm'}
    assert not copy_source or backend=='flotherm'
    api=IsolatedServer(backend+('-copy' if copy_source else '')+'-scope',app='controlled_udf:app')
    (api.root/'controlled_udf.py').write_text(CONTROLLED,encoding='utf8')
    (api.root/'mode').write_text('bad_registration')
    sentinel=subprocess.Popen(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-u','-c',
        'import sys;print("READY",flush=True);sys.stdin.buffer.read(1)'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW)
    assert sentinel.stdout.readline().strip()==b'READY'
    checks=[];receipts=[];readbacks=[]
    try:
        with api:
            for mode in ('bad_registration','cancel','timeout','api_death'):
                (api.root/'mode').write_text(mode)
                api.create_case('Actual '+backend+' '+mode)
                api.call('POST',api.prefix+'/udf/scaffold',{'name':'ownedCompile','objective':'Isolated compiler lifecycle'})
                path='UDF/ownedCompile/myFunctionObject.C'
                content=api.call('GET',api.prefix+'/file?path='+path)['content']
                # Bounded source keeps the real optimizer busy until captured;
                # no sleep wrapper stands in for the compiler being tested.
                content+='\n'+''.join(f'double scope_fn_{n}(double x) {{ return x*x+{n}.0; }}\n' for n in range(12000))
                api.put(path,content)
                directory=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))/'UDF/ownedCompile'
                sources=inventory(directory)
                destination=directory.parent/'copiedCompile' if copy_source else directory
                endpoint='/udf:build' if backend=='udf' else '/flotherm:build-library'
                request={'name':'ownedCompile'} if backend=='udf' else {'source_dir':'UDF/ownedCompile',
                    'destination_dir':'UDF/copiedCompile' if copy_source else 'UDF/ownedCompile'}
                location=None;cancel_replies=[]
                if mode=='bad_registration':
                    error=api.call('POST',api.prefix+endpoint,request,expect=409)
                    launch=json.loads((api.root/'launch.json').read_text())
                    (api.root/'registration-response.json').write_text(json.dumps(error,indent=2),encoding='utf8')
                    assert error['detail'].get('job_id')==launch['job_id'],error
                    row=api.call('GET',f"/jobs/{launch['job_id']}")
                    assert row['status']=='failed',row
                    stored=row['attempt']['process_identity']
                    assert stored['launch_nonce']==launch['identity']['launch_nonce']!='0'*64
                    assert stored['host']==launch['identity']['host'] and row['attempt']['process_stop']['verified']
                    assert not row['attempt']['child_processes']['children'],'Rejected parent released a native compiler'
                    assert identity.probe(launch['identity']['host'])['state']=='exited'
                    assert not (destination/'Make/linux64GccDPInt32Opt').exists()
                    captured=[];stop=None
                else:
                    job_id=api.call('POST',api.prefix+endpoint,request)['job_id']
                    row=api.call('GET',f'/jobs/{job_id}')
                    record=row['attempt']['process_identity']
                    purpose='udf_run' if backend=='udf' else 'flotherm_library'
                    def compiling():
                        children=api.call('GET',f'/jobs/{job_id}')['attempt']['child_processes']['children']
                        running=[c for c in children.values() if c['purpose']==purpose and c['phase']=='running']
                        if not running:return None
                        assert len(running)==1
                        native=running[0]['identity']
                        members=inspect_native({'root':native['native']['process']})
                        return members if any(m['comm']=='cc1plus' for m in members) else None
                    captured=until(compiling,seconds=20)
                    if copy_source:
                        from app.services.mpi_native import path_mapping
                        private=directory.parent.parent/row['attempt']['workspace']['run_path']
                        stage=json.loads((private/'.vibeflow/flotherm-library/stage.json').read_text())
                        mapped=path_mapping(request['destination_dir'],stage['execution_id'])(request['destination_dir'])
                        location=compiler_location(next(m for m in captured if m['comm']=='cc1plus'),stage['path']+'/'+mapped)
                        assert stage['uid']!=0 and not destination.exists()
                    if mode=='api_death':
                        owner=json.loads((api.root/'actual-owner.json').read_text())
                        assert identity.probe(owner)['state']=='match'
                        (api.root/'exit-owner').touch()
                        until(lambda:identity.probe(owner)['state']=='exited')
                        stop=None
                    else:
                        if mode=='cancel':
                            if copy_source:
                                with ThreadPoolExecutor(max_workers=2) as pool:
                                    cancel_replies=list(pool.map(lambda _:api.call('POST',f'/jobs/{job_id}:cancel'),range(2)))
                                assert any(reply['ok'] for reply in cancel_replies),cancel_replies
                            else:assert api.call('POST',f'/jobs/{job_id}:cancel')['ok']
                        row=settled(api,job_id)
                        assert row['status']==('cancelled' if mode=='cancel' else 'failed'),row
                        stop=row['attempt']['process_stop']
                        assert stop['verified']
                        child=list(row['attempt']['child_processes']['children'].values())[0]
                        assert child['purpose']==purpose and child['stop']['verified'] and child['stop']['native_state']=='exited'
                        assert stop['reason']==('cancelled' if mode=='cancel' else 'timeout'),stop
                        if copy_source and mode=='cancel':
                            cancel_replies.append(api.call('POST',f'/jobs/{job_id}:cancel'))
                            assert not cancel_replies[-1]['ok']
                            assert api.call('GET',f'/jobs/{job_id}')['attempt']['process_stop']==stop
                    until(lambda:all(p['state']=='exited' for p in inspect_native({'probe':[m['identity'] for m in captured]})))
                    assert not (destination/'libmyFunctionObject.so').exists()
                assert all(hashlib.sha256((directory/path).read_bytes()).hexdigest()==digest for path,digest in sources.items())
                if copy_source:
                    assert inventory(directory)==sources,'Compilation modified the original directory'
                    assert not destination.exists(),'Failed or cancelled copied build published user files'
                    frozen=directory.parent.parent/row['attempt']['workspace']['inputs_path']/'UDF/ownedCompile'
                    assert inventory(frozen)==sources
                assert sentinel.poll() is None
                receipts.append({'mode':mode,'job_id':row['id'],'members':captured,'stop':stop,
                                 'sources_unchanged':len(sources),'external_sentinel_alive':True})
                if copy_source:
                    receipts[-1].update(source_sha256=sources,source_dir=str(directory),destination_dir=str(destination),
                        compiler_location=location,cancel_replies=cancel_replies,destination_inventory=inventory(destination))
                    readbacks.append((api.token,row,directory,destination,sources,inventory(destination)))
                checks.append(mode+'_real_'+backend+'_scope_preserves_sources_and_retires_children')
                (api.root/'scope-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
                print('PASS '+checks[-1],flush=True)
        if copy_source:
            reader=IsolatedServer('flotherm-copy-reader',app='copy_readback:app')
            overrides={key:api.env[key] for key in ('VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET','VIBEFLOW_DATABASE_URL')}
            (reader.root/'copy_readback.py').write_text('import os\nos.environ.update('+repr(overrides)+')\nfrom app.main import app\n',encoding='utf8')
            history=[]
            with reader as fresh:
                for token,row,directory,destination,sources,copied in readbacks:
                    fresh.token=token;persisted=fresh.call('GET',f'/jobs/{row["id"]}')
                    assert persisted['status']==('interrupted' if row['status']=='running' else row['status'])
                    assert persisted['attempt']['id']==row['attempt']['id']
                    assert persisted['attempt']['process_identity']==row['attempt']['process_identity']
                    assert persisted['attempt']['process_stop']==row['attempt']['process_stop']
                    if row['status']!='running':
                        assert persisted['exit_code']==row['exit_code'] and persisted['ended_at']==row['ended_at']
                    observation=fresh.call('GET',f'/jobs/{row["id"]}/process')
                    assert observation['state']==('exited' if row['attempt']['process_identity'] else 'unverified'),observation
                    if row['attempt']['process_identity'] is None:
                        assert observation['reason']=='invalid_or_legacy_identity' and observation['identity'] is None
                    assert inventory(directory)==sources and inventory(destination)==copied
                    history.append({'job':persisted,'observation':observation})
                fresh.token=readbacks[-1][0]
                fresh.call('GET',f'/jobs/{readbacks[0][1]["id"]}',expect=404)
                fresh.call('GET',f'/jobs/{readbacks[0][1]["id"]}/process',expect=404)
                assert sentinel.poll() is None
            (api.root/'readback-receipts.json').write_text(json.dumps(history,indent=2),encoding='utf8')
            checks.append('copied_compile_fresh_api_reads_durable_history_without_replay_or_fabricated_cleanup')
            print('PASS '+checks[-1],flush=True)
    finally:
        sentinel.communicate(b'x',timeout=10)
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),
            'actual_wmake_and_compiler':True,'backend':backend,'api_death_has_no_fabricated_terminal_receipt':True}
    if copy_source:result.update(copy_source=True,reader_evidence=str(reader.root),restart_readback_count=len(readbacks))
    (api.root/'scope-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result))
    return result


if __name__=='__main__':verify()
