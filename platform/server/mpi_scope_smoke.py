"""Real MPI ranks under non-root WSL supervision, cancellation and owner death."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from testsupport import IsolatedServer,SERVER
from cae_fixtures import cavity_2d
from professional_launch_smoke import settled
from app.services import owned_process as owned
from app.services import process_identity_native as identity

CONTROLLED='''import asyncio,copy,json,os
from pathlib import Path
from app.services import native_child_records
from app.services.mpi_handler import MpiHandler
from app.services import process_identity_native as identity
root=Path(__file__).parent
original_prepare=MpiHandler.prepare
async def prepare(self,db,case,user,*,parameters,budget):
    budget=dict(budget)
    mode=(root/'mode').read_text() if (root/'mode').exists() else ''
    if mode=='timeout':budget['timeout_seconds']=45
    if mode=='logs':budget['max_log_bytes']=1024*1024
    return await original_prepare(self,db,case,user,parameters=parameters,budget=budget)
MpiHandler.prepare=prepare
original_register=native_child_records.register
async def register(owner,parent_nonce,identifier,identity,distribution):
    if identity['native'] and identity['native']['process']['uid']!=0:
        (root/'mpi-launch.json').write_text(json.dumps({'job_id':owner.job_id,'identity':identity}))
        mode=(root/'mode').read_text() if (root/'mode').exists() else ''
        if mode in {'bad_uid','bad_distribution'}:
            identity=copy.deepcopy(identity)
            if mode=='bad_uid': identity['native']['process']['uid']=0
            else: identity['native']['distribution']='vibeflow-test-nonexistent-distribution'
    return await original_register(owner,parent_nonce,identifier,identity,distribution)
native_child_records.register=register
from app.main import app
@app.on_event('startup')
async def owned_test_exit():
    (root/'api-identity.json').write_text(json.dumps(identity.observe(os.getpid())))
    async def watch():
        while not (root/'exit-owned-api').exists(): await asyncio.sleep(.02)
        os._exit(125)
    asyncio.create_task(watch())
'''

INSPECT=r'''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import process_identity_native as identity
request=json.load(sys.stdin)
if 'probe' in request:
 print(json.dumps([identity.probe(row) for row in request['probe']]))
else:
 root=request['root'];assert identity.probe(root)['state']=='match'
 pending=[root['pid']];seen=set();result=[]
 while pending:
  pid=pending.pop()
  if pid in seen:continue
  assert len(seen)<128
  seen.add(pid)
  try:
   record=identity.observe(pid)
   comm=Path('/proc/'+str(pid)+'/comm').read_text().strip()
   mapping=Path('/proc/'+str(pid)+'/uid_map').read_text().strip()
   for task in Path('/proc/'+str(pid)+'/task').iterdir():
    try:pending.extend(map(int,(task/'children').read_text().split()))
    except FileNotFoundError:pass
   result.append({'identity':record,'comm':comm,'uid_map':mapping})
  except (FileNotFoundError,ProcessLookupError):pass
 print(json.dumps(result))
'''


def inspect_native(request):
    process=subprocess.run(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-c',INSPECT,
        owned.wsl_path(SERVER/'app/services')],input=json.dumps(request).encode(),capture_output=True,
        timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
    assert process.returncode==0,process.stderr.decode('utf-8','replace')
    return json.loads(process.stdout)


def wait_job(api,job_id):
    return settled(api,job_id)


def wait_ranks(api,job_id):
    deadline=time.monotonic()+35
    while time.monotonic()<deadline:
        row=api.call('GET',f'/jobs/{job_id}')
        assert row['status']=='running',row
        children=row['attempt']['child_processes']['children']
        running=[c for c in children.values() if c['purpose']=='mpi_run' and c['phase']=='running']
        if not running:time.sleep(.03);continue
        assert len(running)==1
        record=running[0]['identity']
        members=inspect_native({'root':record['native']['process']})
        ranks=[item for item in members if item['comm']=='icoFoam']
        if len(ranks)==2:
            assert record['native']['process']['uid']!=0
            uid=record['native']['process']['uid']
            assert all(item['identity']['uid']==uid for item in members),members
            assert len({r['identity']['pid_namespace'] for r in ranks})==1
            assert all(r['uid_map'].split()==[str(uid),str(uid),'1'] for r in ranks),ranks
            return row,members
        time.sleep(.03)
    raise TimeoutError('Two real icoFoam ranks did not appear')


def verify(database_url=None):
    server=IsolatedServer('mpi-scope',app='controlled_mpi_scope:app',database_url=database_url)
    (server.root/'controlled_mpi_scope.py').write_text(CONTROLLED,encoding='utf-8')
    checks=[];runs=[]
    base=getattr(sys,'_base_executable',sys.executable)
    marker='import sys;print("READY",flush=True);sys.stdin.buffer.read(1)'
    sentinels=[subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW)
        for argv in ([base,'-I','-S','-u','-c',marker],
            ['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-u','-c',marker])]
    assert all(child.stdout.readline().strip()==b'READY' for child in sentinels)
    try:
        with server as api:
            for mode in ('cancel','timeout','logs','bad_uid','bad_distribution','api_death'):
                api.create_case('MPI '+mode)
                files=cavity_2d()
                control='C-run/case-1/system/controlDict'
                files[control]=files[control].replace('endTime         0.5;','endTime         1000000;').replace('deltaT          0.005;','deltaT          0.00001;').replace('writeInterval   20;','writeInterval   1000000000;')
                mesh='C-run/case-1/system/blockMeshDict'
                files[mesh]=files[mesh].replace('(20 20 1)','(100 100 1)')
                for path,text in files.items():api.put(path,text)
                row=api.call('POST',api.prefix+'/jobs',{'type':'mesh','runtime':'wsl',
                    'command':'source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd C-run/case-1 && blockMesh'})
                assert wait_job(api,row['id'])['status']=='succeeded'
                root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
                sources={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (root/'C-run/case-1').rglob('*') if p.is_file()}
                (api.root/'mode').write_text(mode)
                if mode.startswith('bad_'):
                    started=api.call('POST',api.prefix+'/mpi:run',{'subdomains':2})
                    row=wait_job(api,started['job_id'])
                    launch=json.loads((api.root/'mpi-launch.json').read_text())
                    assert launch['job_id']==row['id'] and row['status']=='failed'
                    children=list(row['attempt']['child_processes']['children'].values())
                    assert len(children)==1 and children[0]['phase']=='failed'
                    private=root/row['attempt']['workspace']['run_path']
                    assert not (private/'.vibeflow/mpi/stage.json').exists(),'Rejected identity released native payload'
                    assert inspect_native({'probe':[launch['identity']['native']['process']]})[0]['state']=='exited'
                    records=[];stop=None
                else:
                    started=api.call('POST',api.prefix+'/mpi:run',{'subdomains':2})
                    row,records=wait_ranks(api,started['job_id'])
                    if mode=='api_death':
                        api_identity=json.loads((api.root/'api-identity.json').read_text())
                        assert identity.probe(api_identity)['state']=='match'
                        (api.root/'exit-owned-api').touch()
                        for _ in range(100):
                            if identity.probe(api_identity)['state']=='exited':break
                            time.sleep(.05)
                        else:raise AssertionError('The actual API owner did not exit')
                        stop=None
                    else:
                        if mode=='cancel':assert api.call('POST',f'/jobs/{row["id"]}:cancel',{})['ok']
                        row=wait_job(api,row['id'])
                        stop=row['attempt']['process_stop']
                        assert row['status']==('cancelled' if mode=='cancel' else 'failed'),row
                        assert stop['verified'],stop
                        child=list(row['attempt']['child_processes']['children'].values())[0]
                        assert child['stop']['verified'] and child['stop']['native_state']=='exited',child
                        if mode=='logs':assert child['stop']['reason']=='log_limit',child
                        else:assert stop['reason']=={'cancel':'cancelled','timeout':'timeout'}[mode],stop
                    for _ in range(100):
                        observations=inspect_native({'probe':[item['identity'] for item in records]})
                        if all(item['state']=='exited' for item in observations):break
                        time.sleep(.05)
                    else:raise AssertionError('An owned MPI rank or helper survived stop')
                for path,digest in sources.items():assert hashlib.sha256((root/path).read_bytes()).hexdigest()==digest,path
                assert not list((root/'C-run/case-1').glob('processor*'))
                assert all(child.poll() is None for child in sentinels)
                checks.append(mode);runs.append({'mode':mode,'job_id':row['id'],'members':records,'stop':stop,
                    'input_files_unchanged':len(sources),'api_death_has_no_fabricated_terminal_receipt':mode=='api_death'})
                (api.root/'mpi-scope-progress.json').write_text(json.dumps({'checks':checks,'runs':runs},indent=2)+'\n',encoding='utf-8')
                print('PASS '+mode+'_preserves_native_ownership_inputs_and_external_sentinels',flush=True)
    finally:
        for child in sentinels:child.communicate(b'x',timeout=10)
    result={'passed':True,'count':len(checks),'checks':checks,'runs':runs,'evidence':str(server.root)}
    (server.root/'mpi-scope-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='mpi-scope-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_mpi_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_mpi_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print('MPI_SCOPE_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
