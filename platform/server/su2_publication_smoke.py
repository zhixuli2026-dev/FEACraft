"""Real SU2 fields, restart/multizone inputs and interrupted immutable publication."""
import argparse
import asyncio
import csv
import io
import json
import math
from pathlib import Path
import tempfile
import time
import uuid

from testsupport import IsolatedServer, SERVER
from job_release_smoke import wait_file
from workflow_migration_smoke import alembic
from event_replay_smoke import peer

EXAMPLES=SERVER/'examples/su2'
MESH=(EXAMPLES/'mesh_NACA0012_inv.su2').read_text()
BASE=(EXAMPLES/'inv_NACA0012.cfg').read_text().replace('ITER= 250','ITER= 3').replace(
    'MESH_FILENAME= mesh_NACA0012_inv.su2','MESH_FILENAME= Ref/naca0012.su2')
PATH='C-run/su2/naca0012.cfg'
CONFIG='.vibeflow/prepared/su2.json'
FIXTURE='''
import time
import sys
from pathlib import Path
from app.services import artifact_publication as publication
from app.services.su2_handler import Su2Handler
from app.services.native_execution import NativeExecutor
root=Path(__file__).parent
def mode():return (root/'mode').read_text()
original=publication.prepare_bundle
def hold_copy(*args):
 if mode() in {'source','optional','destination','cancel','death'}:
  (root/'ready').touch();deadline=time.monotonic()+90
  while not (root/'release').exists():
   if time.monotonic()>deadline:raise TimeoutError('SU2 copy fixture')
   time.sleep(.02)
 return original(*args)
publication.prepare_bundle=hold_copy
artifacts=Su2Handler.artifacts
def verify(self,run,descriptor,output,frozen):
 if mode()=='bad_receipt':(run/'.vibeflow/su2/receipt.json').write_text('{}')
 if mode()=='bad_config':(run/'.vibeflow/prepared/su2.json').write_text('{}')
 return artifacts(self,run,descriptor,output,frozen)
Su2Handler.artifacts=verify
command=NativeExecutor._command
def invoke(self,purpose,parameters):
 result=command(self,purpose,parameters)
 if self.kind=='su2' and mode()=='noop':
  return [sys.executable,'-c','print("Exit Success (SU2_CFD)")'],10,65536
 return result
NativeExecutor._command=invoke
from app.main import app
'''


def field(path):
    from vtkmodules.vtkIOXML import vtkXMLUnstructuredGridReader
    reader=vtkXMLUnstructuredGridReader();reader.SetFileName(str(path));reader.Update()
    mesh=reader.GetOutput()
    assert mesh.GetNumberOfPoints()==5233 and mesh.GetNumberOfCells()==10216
    for name,low,high in [('Density',.1,5),('Pressure',10000,500000),('Temperature',100,1000),('Mach',0,3)]:
        values=mesh.GetPointData().GetArray(name)
        assert values is not None and values.GetNumberOfTuples()==5233,name
        numbers=[values.GetValue(i) for i in range(5233)]
        assert all(math.isfinite(v) and low<=v<=high for v in numbers),name
        assert max(numbers)-min(numbers)>1e-3,name


def history(path):
    rows=list(csv.DictReader(io.StringIO(path.read_text()),skipinitialspace=True))
    assert rows
    for row in rows:
        assert all(math.isfinite(float(v)) for v in row.values()),row
    return rows


def verify(database_url=None):
    api=IsolatedServer('su2-publication',app='su2_fixture:app',database_url=database_url)
    (api.root/'su2_fixture.py').write_text(FIXTURE,encoding='utf8')
    (api.root/'mode').write_text('normal');checks=[]
    with api:
        for mode in ('normal','restart','ascii','multizone','spaces','invalid','noop','bad_config','bad_receipt','source','optional','destination','cancel','death'):
            api.create_case('SU2 '+mode)
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            path='C-run/space folder/flow model.cfg' if mode=='spaces' else PATH
            config=BASE
            if mode=='ascii':config=config.replace('(RESTART, PARAVIEW, SURFACE_CSV)','(RESTART_ASCII, PARAVIEW, SURFACE_CSV)')+'\nREAD_BINARY_RESTART= NO\n'
            if mode=='multizone':
                config='SOLVER= MULTIPHYSICS\nMULTIZONE_MESH= NO\nCONFIG_LIST= (Ref/zone0.cfg, Ref/zone1.cfg)\nOUTER_ITER= 2\nMESH_FILENAME= Ref/naca0012.su2\n'
                for zone in range(2):api.put(f'Ref/zone{zone}.cfg',BASE.replace('ITER= 3','INNER_ITER= 3'))
            api.put('Ref/naca0012.su2',MESH);api.put(path,config)
            target=root/'flow.vtu';target.write_bytes(b'previous result')
            (root/'user.notes').write_text('user notes')
            if mode=='invalid':
                before=api.call('GET',api.prefix+'/jobs')
                for text,status in [('MESH_FILENAME= missing.su2\n',404),('MESH_FILENAME= ../outside.su2\n',400),
                        ('SOLVER= MULTIPHYSICS\nMULTIZONE_MESH= NO\nCONFIG_LIST= ('+path+')\n',400)]:
                    api.put(path,text);api.call('POST',api.prefix+'/su2:solve',{'config_path':path},expect=status)
                assert api.call('GET',api.prefix+'/jobs')==before
                assert target.read_bytes()==b'previous result'
                checks.append('missing_escape_and_cyclic_config_fail_before_job');continue
            (api.root/'mode').write_text(mode)
            for name in ('ready','release'):(api.root/name).unlink(missing_ok=True)
            body={'config_path':path};headers={'Idempotency-Key':'su2-'+mode}
            identifier=api.call('POST',api.prefix+'/su2:solve',body,extra_headers=headers)['job_id']
            if mode in {'source','optional','destination','cancel','death'}:
                wait_file(api.root/'ready',seconds=40)
                assert target.read_bytes()==b'previous result'
                if mode=='source':(root/'Ref/naca0012.su2').write_text(MESH+'\n')
                if mode=='optional':(root/'runtime.dat').write_text('TIME_ITER= 1\n')
                if mode=='destination':target.write_bytes(b'user edit')
                if mode=='cancel':api.call('POST',f'/jobs/{identifier}:cancel')
                if mode=='death':
                    api.proc.terminate();api.proc.wait(timeout=10)
                    with peer(api) as recovered:
                        done=recovered.wait_job(identifier,timeout=45)
                        assert done['attempt']['outputs']['publication']['status']=='committed'
                        field(target)
                        assert len(recovered.call('GET',api.prefix+'/jobs'))==1
                    checks.append('API_death_recovers_validated_SU2_without_second_solve');break
                (api.root/'release').touch()
            success=mode in {'normal','restart','ascii','multizone','spaces'}
            done=api.wait_job(identifier,expected='succeeded' if success else 'cancelled' if mode=='cancel' else 'failed',timeout=45)
            if mode=='cancel':
                deadline=time.monotonic()+20
                while done['attempt']['phase']!='cancelled' and time.monotonic()<deadline:
                    time.sleep(.05);done=api.call('GET',f'/jobs/{identifier}')
                assert done['attempt']['phase']=='cancelled'
            frozen=done['attempt']['workspace'];private=root/frozen['run_path'];inputs=root/frozen['inputs_path']
            assert not (inputs/'flow.vtu').exists()
            assert (inputs/'Ref/naca0012.su2').read_text()==MESH
            assert CONFIG in frozen['generated_inputs'] and not (root/CONFIG).exists()
            assert (root/'user.notes').read_text()=='user notes'
            publication=done['attempt']['outputs']['publication']
            if not success:
                assert publication['status']!='committed'
                assert target.read_bytes()==(b'user edit' if mode=='destination' else b'previous result')
                checks.append(mode+'_cannot_publish_or_overwrite');continue
            assert publication['status']=='committed'
            bundle=root/'.vibeflow/publications'/publication['id']
            manifest=json.loads((bundle/'manifest.json').read_text())
            assert manifest['descriptor']['receipt']['attempt_id']==done['attempt']['id']
            assert manifest['descriptor']['receipt']['returncode']==0
            expected={path,'Ref/naca0012.su2'}|({'Ref/zone0.cfg','Ref/zone1.cfg'} if mode=='multizone' else set())
            assert set(manifest['descriptor']['source']['files'])==expected
            children=done['attempt']['child_processes']['children']
            assert len(children)==1 and all(c['purpose']=='su2_solve' and c['phase']=='stopped' and c['stop']['verified'] for c in children.values())
            assert api.call('POST',api.prefix+'/su2:solve',body,extra_headers=headers)['job_id']==identifier
            reader=api.call('POST',api.prefix+'/jobs',{'command':'echo SU2_REFERENCE_OK',
                'input_publications':[{'publication_id':publication['id'],'require_current':True}]})
            assert 'SU2_REFERENCE_OK' in api.wait_job(reader['id'])['log_backlog']
            flows=[p for p in manifest['files'] if p.endswith('.vtu')]
            assert len(flows)==(2 if mode=='multizone' else 1),flows
            for output in flows:
                assert (root/output).read_bytes()==(private/output).read_bytes();field(root/output)
            histories=[path.split('.',1)[0]+'.csv'] if mode=='multizone' else ['history.csv']
            assert all(p in manifest['files'] for p in histories)
            assert histories
            for output in histories:history(root/output)
            if mode=='normal':
                api.put('Ref/naca0012.su2',MESH+'\n')
                api.call('POST',api.prefix+'/jobs',{'command':'echo STALE',
                    'input_publications':[{'publication_id':publication['id'],'require_current':True}]},expect=409)
                checks.append('changed_mesh_invalidates_current_result_reference')
            if mode in {'restart','ascii'}:
                extension='csv' if mode=='ascii' else 'dat'
                restart_path='restart_flow.'+extension
                restart=(root/restart_path).read_bytes();assert restart
                # The native restart metadata carries the previous steady iteration.
                config=config.replace('RESTART_SOL= NO','RESTART_SOL= YES').replace('ITER= 3','ITER= 6')
                config=config.replace('SOLUTION_FILENAME= solution_flow','SOLUTION_FILENAME= restart_flow')
                api.put(path,config)
                second=api.wait_job(api.call('POST',api.prefix+'/su2:solve',body)['job_id'],timeout=45)
                second_publication=second['attempt']['outputs']['publication']
                assert second_publication['status']=='committed'
                assert (root/second['attempt']['workspace']['inputs_path']/restart_path).read_bytes()==restart
                assert (bundle/'files/flow.vtu').read_bytes()!=target.read_bytes()
                field(target)
                for previous,current in ((publication['id'],False),(second_publication['id'],True)):
                    reader=api.call('POST',api.prefix+'/jobs',{'command':'echo SU2_HISTORY_OK',
                        'input_publications':[{'publication_id':previous,'require_current':current}]})
                    api.wait_job(reader['id'])
                checks.append(mode+'_real_restart_and_immutable_old_result_and_current_new_reference')
            checks.append(mode+'_native_fields_sealed_dependencies_owned_child_and_publication')
            print('PASS '+checks[-1],flush=True)
    return {'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='su2-publication-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_supublication_'+uuid.uuid4().hex
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
    print('SU2_PUBLICATION_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
