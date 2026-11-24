"""Real serial/MPI field agreement, restart, decomposition and publication faults."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import tempfile
import time
import uuid

from testsupport import IsolatedServer,SERVER
from cae_fixtures import cavity_2d
from job_release_smoke import wait_file
from event_replay_smoke import peer
from workflow_migration_smoke import alembic

RUN='C-run/case-1'
CONFIG='.vibeflow/prepared/mpi.json'
FLOAT=re.compile(r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?')
FIXTURE='''
import time
from pathlib import Path
from app.services import artifact_publication as publication
from app.services.mpi_handler import MpiHandler
root=Path(__file__).parent
def mode():return (root/'mode').read_text()
original=publication.prepare_bundle
def hold_copy(*args):
 if mode() in {'source','destination','cancel','death'}:
  (root/'ready').touch();deadline=time.monotonic()+90
  while not (root/'release').exists():
   if time.monotonic()>deadline:raise TimeoutError('MPI copy fixture')
   time.sleep(.02)
 return original(*args)
publication.prepare_bundle=hold_copy
artifacts=MpiHandler.artifacts
def verify(self,run,descriptor,output,frozen):
 if mode()=='bad_receipt':(run/'.vibeflow/mpi/receipt.json').write_text('{}')
 if mode()=='bad_config':(run/'.vibeflow/prepared/mpi.json').write_text('{}')
 return artifacts(self,run,descriptor,output,frozen)
MpiHandler.artifacts=verify
from app.main import app
'''


def values(data):
    text=data.decode('utf8').split('internalField',1)[1].split('boundaryField',1)[0]
    return [float(v) for v in FLOAT.findall(text)]


def compare(first,second):
    a,b=values(first),values(second)
    assert len(a)==len(b) and len(a)>1000
    error=max(abs(x-y) for x,y in zip(a,b))/max(1,max(map(abs,a)))
    assert error<=1e-5,error
    return error


def serial_seed(api):
    api.create_case('MPI independent serial reference')
    for path,text in cavity_2d().items():api.put(path,text)
    mesh=api.call('POST',api.prefix+'/jobs',{'type':'mesh','runtime':'wsl',
        'command':'source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd C-run/case-1 && blockMesh'})
    api.wait_job(mesh['id'],timeout=90)
    root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
    initial={p.relative_to(root).as_posix():p.read_bytes() for p in (root/RUN).rglob('*') if p.is_file()}
    solve=api.call('POST',api.prefix+'/jobs',{'type':'solve','runtime':'wsl','run_dir':RUN,
        'command':'source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd C-run/case-1 && icoFoam'})
    api.wait_job(solve['id'],timeout=90)
    return initial,(root/RUN/'0.5/U').read_bytes()


def verify(database_url=None):
    api=IsolatedServer('mpi-publication',app='mpi_fixture:app',database_url=database_url)
    (api.root/'mpi_fixture.py').write_text(FIXTURE,encoding='utf8');(api.root/'mode').write_text('normal')
    checks=[];errors=[]
    with api:
        seed,serial=serial_seed(api)
        for mode in ('normal','configured','repeat','resume','alternate_path','mismatch','bad_config','bad_receipt','source','destination','cancel','death'):
            api.create_case('MPI '+mode)
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            run_dir='Cases/alternate cavity' if mode=='alternate_path' else RUN
            for path,data in seed.items():
                path=run_dir+path[len(RUN):];target=root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
            dictionary=run_dir+'/system/decomposeParDict'
            if mode in {'configured','mismatch'}:
                count=3 if mode=='mismatch' else 2
                api.put(dictionary,f'FoamFile {{ version 2.0; format ascii; class dictionary; object decomposeParDict; }}\nnumberOfSubdomains {count};\nmethod scotch;\n')
            target=root/run_dir/'0.5/U';target.parent.mkdir(exist_ok=True);target.write_bytes(b'previous field')
            (api.root/'mode').write_text(mode)
            for name in ('ready','release'):(api.root/name).unlink(missing_ok=True)
            body={'run_dir':run_dir,'solver':'icoFoam','subdomains':2};headers={'Idempotency-Key':'mpi-'+mode}
            identifier=api.call('POST',api.prefix+'/mpi:run',body,extra_headers=headers)['job_id']
            if mode in {'source','destination','cancel','death'}:
                wait_file(api.root/'ready',seconds=90)
                assert target.read_bytes()==b'previous field'
                assert not (root/dictionary).exists(),'Generated dictionary leaked before publication'
                if mode=='source':(root/run_dir/'system/fvSolution').write_text((root/run_dir/'system/fvSolution').read_text()+'\n// user edit\n')
                if mode=='destination':target.write_bytes(b'user field edit')
                if mode=='cancel':api.call('POST',f'/jobs/{identifier}:cancel')
                if mode=='death':
                    api.proc.terminate();api.proc.wait(timeout=10)
                    with peer(api) as recovered:
                        done=recovered.wait_job(identifier,timeout=60)
                        assert done['attempt']['outputs']['publication']['status']=='committed'
                        errors.append(compare(serial,target.read_bytes()))
                        assert len(recovered.call('GET',api.prefix+'/jobs'))==1
                    checks.append('API_death_recovers_validated_MPI_without_second_solve');break
                (api.root/'release').touch()
            success=mode in {'normal','configured','repeat','resume','alternate_path'}
            done=api.wait_job(identifier,expected='succeeded' if success else 'cancelled' if mode=='cancel' else 'failed',timeout=90)
            if mode=='cancel':
                deadline=time.monotonic()+20
                while done['attempt']['phase']!='cancelled' and time.monotonic()<deadline:
                    time.sleep(.05);done=api.call('GET',f'/jobs/{identifier}')
                assert done['attempt']['phase']=='cancelled'
            frozen=done['attempt']['workspace'];inputs=root/frozen['inputs_path'];private=root/frozen['run_path']
            assert (inputs/run_dir/'0.5/U').read_bytes()==b'previous field'
            assert CONFIG in frozen['generated_inputs'] and not (root/CONFIG).exists()
            publication=done['attempt']['outputs']['publication']
            if not success:
                assert publication['status']!='committed'
                assert target.read_bytes()==(b'user field edit' if mode=='destination' else b'previous field')
                if mode not in {'configured','mismatch'}:assert not (root/dictionary).exists()
                assert not list((root/run_dir).glob('processor*'))
                checks.append(mode+'_cannot_publish_or_change_live_dictionary');print('PASS '+checks[-1],flush=True);continue
            assert publication['status']=='committed'
            errors.append(compare(serial,target.read_bytes()))
            bundle=root/'.vibeflow/publications'/publication['id'];manifest=json.loads((bundle/'manifest.json').read_text())
            assert manifest['descriptor']['receipt']['attempt_id']==done['attempt']['id']
            native=manifest['descriptor']['receipt']['native']
            assert native['uid']!=0 and set(native['processors'])=={'processor0','processor1'}
            assert json.loads((private/'.vibeflow/mpi/stage.json').read_text())['removed']
            children=done['attempt']['child_processes']['children']
            assert len(children)==1 and all(c['purpose']=='mpi_run' and c['phase']=='stopped' and c['stop']['verified']
                and c['stop']['native_state']=='exited' and c['identity']['native']['process']['uid']!=0 for c in children.values())
            assert api.call('POST',api.prefix+'/mpi:run',body,extra_headers=headers)['job_id']==identifier
            status=api.call('GET',api.prefix+'/mpi:status?run_dir='+run_dir.replace(' ','%20'))
            assert status['complete'] and status['subdomains']==2,status
            reader=api.call('POST',api.prefix+'/jobs',{'command':'echo MPI_REFERENCE_OK',
                'input_publications':[{'publication_id':publication['id'],'require_current':True}]})
            api.wait_job(reader['id'])
            if mode in {'repeat','resume'}:
                previous=target.read_bytes()
                if mode=='resume':
                    control=(root/run_dir/'system/controlDict').read_text().replace('startFrom       startTime;','startFrom       latestTime;').replace('endTime         0.5;','endTime         0.6;')
                    assert 'latestTime' in control and '0.6;' in control
                    api.put(run_dir+'/system/controlDict',control)
                second=api.wait_job(api.call('POST',api.prefix+'/mpi:run',body)['job_id'],timeout=90)
                second_pub=second['attempt']['outputs']['publication']
                assert second_pub['status']=='committed'
                assert (bundle/'files'/run_dir/'0.5/U').read_bytes()==previous
                if mode=='resume':
                    assert (root/run_dir/'0.6/U').is_file()
                    evidence=json.loads((root/run_dir/'mpi-evidence.json').read_text())
                    assert min(evidence['parsed_log']['times'])>.5 and evidence['summary']['last_time']==.6
                else:errors.append(compare(serial,target.read_bytes()))
                for previous,current in ((publication['id'],False),(second_pub['id'],True)):
                    reader=api.call('POST',api.prefix+'/jobs',{'command':'echo MPI_HISTORY_OK',
                        'input_publications':[{'publication_id':previous,'require_current':current}]})
                    api.wait_job(reader['id'])
                checks.append(mode+'_real_native_execution_and_immutable_history')
            checks.append(mode+'_actual_two_ranks_reconstructed_fields_and_guarded_publication')
            print('PASS '+checks[-1],flush=True)
    return {'passed':True,'count':len(checks),'checks':checks,'serial_parallel_U_relative_errors':errors,'evidence':str(api.root)}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='mpi-publication-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_mpipublication_'+uuid.uuid4().hex
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
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    print('MPI_PUBLICATION_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
