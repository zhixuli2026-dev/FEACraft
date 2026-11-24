"""Real ParaView data/rendering, dynamic dependencies and guarded file changes."""
import argparse
import asyncio
import io
import json
from pathlib import Path
import tempfile
import time
import uuid

from testsupport import IsolatedServer, SERVER
from job_release_smoke import wait_file
from workflow_migration_smoke import alembic
from event_replay_smoke import peer

PATH='D-post/render.py'
TARGET='D-post/paraview-sphere.vtp'
PNG='D-post/paraview-sphere.png'
CONFIG='.vibeflow/prepared/paraview.json'
SCRIPT='''import json,os
from pathlib import Path
from paraview.simple import CreateView,Render,SaveData,SaveScreenshot,Show,Sphere
root=Path(os.environ['VIBEFLOW_CASE_ROOT'])
settings=json.loads((root/'Ref/render.json').read_text())
sphere=Sphere(ThetaResolution=16,PhiResolution=16,Radius=settings['radius']);sphere.UpdatePipeline()
view=CreateView('RenderView');view.ViewSize=[320,240]
Show(sphere,view);Render(view)
SaveData(str(root/'D-post/paraview-sphere.vtp'),proxy=sphere)
SaveScreenshot(str(root/'D-post/paraview-sphere.png'),view=view)
print('PARAVIEW_REAL_POINTS',sphere.GetDataInformation().GetNumberOfPoints())
'''
DATA='''import os
from pathlib import Path
from paraview.simple import XMLPolyDataReader,Transform,SaveData
root=Path(os.environ['VIBEFLOW_CASE_ROOT'])
reader=XMLPolyDataReader(FileName=[str(root/'D-post/paraview-sphere.vtp')])
changed=Transform(Input=reader);changed.Transform.Scale=[2,2,2];changed.UpdatePipeline()
SaveData(str(root/'D-post/transformed.vtp'),proxy=changed)
print('PARAVIEW_DATA_POINTS',changed.GetDataInformation().GetNumberOfPoints())
'''
FIXTURE='''
import time
from pathlib import Path
from app.services import artifact_publication as publication
from app.services.paraview_handler import ParaViewHandler
root=Path(__file__).parent
def mode():return (root/'mode').read_text()
original=publication.prepare_bundle
def hold_copy(*args):
 if mode() in {'source','new_input','destination','delete_conflict','cancel','death'}:
  (root/'ready').touch();deadline=time.monotonic()+90
  while not (root/'release').exists():
   if time.monotonic()>deadline:raise TimeoutError('ParaView copy fixture')
   time.sleep(.02)
 return original(*args)
publication.prepare_bundle=hold_copy
artifacts=ParaViewHandler.artifacts
def verify(self,run,descriptor,output,frozen):
 if mode()=='bad_receipt':(run/'.vibeflow/paraview/receipt.json').write_text('{}')
 if mode()=='bad_config':(run/'.vibeflow/prepared/paraview.json').write_text('{}')
 return artifacts(self,run,descriptor,output,frozen)
ParaViewHandler.artifacts=verify
from app.main import app
'''


def sphere(path,radius=.5):
    from vtkmodules.vtkIOXML import vtkXMLPolyDataReader
    reader=vtkXMLPolyDataReader();reader.SetFileName(str(path));reader.Update();mesh=reader.GetOutput()
    assert mesh.GetNumberOfPoints()==226 and mesh.GetNumberOfCells()==448
    bounds=mesh.GetBounds()
    assert abs(bounds[4]+radius)<1e-6 and abs(bounds[5]-radius)<1e-6,bounds
    assert all(.98*radius<abs(v)<=1.01*radius for v in bounds),bounds


def image(data):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as picture:
        assert picture.format=='PNG' and picture.size==(320,240)
        extrema=picture.convert('RGB').getextrema()
        assert any(high-low>40 for low,high in extrema),extrema


def verify(database_url=None):
    api=IsolatedServer('paraview-publication',app='paraview_fixture:app',database_url=database_url)
    (api.root/'paraview_fixture.py').write_text(FIXTURE,encoding='utf8')
    (api.root/'mode').write_text('normal');checks=[]
    with api:
        for mode in ('normal','repeat','data','pvbatch','spaces','delete','noop','exception','bad_config','bad_receipt',
                'source','new_input','destination','delete_conflict','cancel','death'):
            api.create_case('ParaView '+mode)
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            path='D-post/space folder/render %VIBEFLOW_CASE_ROOT% & literal.py' if mode=='spaces' else PATH
            script=SCRIPT
            if mode in {'delete','delete_conflict'}:script+="(root/'D-post/obsolete.csv').unlink()\n"
            if mode=='exception':script+="raise RuntimeError('Intentional failure after real rendering')\n"
            if mode=='noop':script="print('PV_NO_OUTPUT_OK')\n"
            api.put('Ref/render.json','{"radius":0.5}')
            api.put('D-post/obsolete.csv','previous CSV')
            api.put(path,script)
            target=root/TARGET;target.write_bytes(b'previous result')
            (root/'user.notes').write_text('user notes')
            (api.root/'mode').write_text(mode)
            for name in ('ready','release'):(api.root/name).unlink(missing_ok=True)
            body={'script_path':path,'backend':'pvbatch' if mode=='pvbatch' else 'pvpython'}
            headers={'Idempotency-Key':'paraview-'+mode}
            identifier=api.call('POST',api.prefix+'/paraview:run',body,extra_headers=headers)['job_id']
            if mode in {'source','new_input','destination','delete_conflict','cancel','death'}:
                wait_file(api.root/'ready',seconds=45)
                assert target.read_bytes()==b'previous result'
                if mode=='source':(root/'Ref/render.json').write_text('{"radius":1}')
                if mode=='new_input':(root/'Ref/new-input.csv').write_text('new input')
                if mode=='destination':target.write_bytes(b'user edit')
                if mode=='delete_conflict':(root/'D-post/obsolete.csv').write_text('user CSV edit')
                if mode=='cancel':api.call('POST',f'/jobs/{identifier}:cancel')
                if mode=='death':
                    api.proc.terminate();api.proc.wait(timeout=10)
                    with peer(api) as recovered:
                        done=recovered.wait_job(identifier,timeout=45)
                        assert done['attempt']['outputs']['publication']['status']=='committed'
                        sphere(target);image((root/PNG).read_bytes())
                        assert len(recovered.call('GET',api.prefix+'/jobs'))==1
                    checks.append('API_death_recovers_validated_render_without_second_script_execution');break
                (api.root/'release').touch()
            success=mode in {'normal','repeat','data','pvbatch','spaces','delete','noop'}
            done=api.wait_job(identifier,expected='succeeded' if success else 'cancelled' if mode=='cancel' else 'failed',timeout=45)
            if mode=='cancel':
                deadline=time.monotonic()+20
                while done['attempt']['phase']!='cancelled' and time.monotonic()<deadline:
                    time.sleep(.05);done=api.call('GET',f'/jobs/{identifier}')
                assert done['attempt']['phase']=='cancelled'
            frozen=done['attempt']['workspace'];private=root/frozen['run_path'];inputs=root/frozen['inputs_path']
            assert (inputs/TARGET).read_bytes()==b'previous result'
            assert (inputs/'Ref/render.json').read_text()=='{"radius":0.5}'
            assert CONFIG in frozen['generated_inputs'] and not (root/CONFIG).exists()
            assert (root/'user.notes').read_text()=='user notes'
            publication=done['attempt']['outputs']['publication']
            if not success:
                assert publication['status']!='committed'
                assert target.read_bytes()==(b'user edit' if mode=='destination' else b'previous result')
                assert (root/'D-post/obsolete.csv').read_text()==('user CSV edit' if mode=='delete_conflict' else 'previous CSV')
                checks.append(mode+'_cannot_publish_or_overwrite_or_delete');print('PASS '+checks[-1],flush=True);continue
            assert publication['status']=='committed'
            bundle=root/'.vibeflow/publications'/publication['id']
            manifest=json.loads((bundle/'manifest.json').read_text())
            assert manifest['descriptor']['receipt']['attempt_id']==done['attempt']['id']
            assert manifest['descriptor']['receipt']['returncode']==0
            children=done['attempt']['child_processes']['children']
            assert len(children)==1 and all(c['purpose']=='paraview_run' and c['phase']=='stopped' and c['stop']['verified'] for c in children.values())
            assert api.call('POST',api.prefix+'/paraview:run',body,extra_headers=headers)['job_id']==identifier
            if mode=='noop':
                assert manifest['files']=={} and target.read_bytes()==b'previous result'
                assert 'PV_NO_OUTPUT_OK' in done['log_backlog']
                checks.append('legitimate_log_only_script_does_not_republish_previous_outputs');continue
            assert set(manifest['files'])=={TARGET,PNG},manifest['files'].keys()
            assert target.read_bytes()==(private/TARGET).read_bytes()
            sphere(target);image((root/PNG).read_bytes())
            reader=api.call('POST',api.prefix+'/jobs',{'command':'echo PARAVIEW_REFERENCE_OK',
                'input_publications':[{'publication_id':publication['id'],'require_current':True}]})
            assert 'PARAVIEW_REFERENCE_OK' in api.wait_job(reader['id'])['log_backlog']
            if mode=='delete':
                assert not (root/'D-post/obsolete.csv').exists()
                assert set(manifest['descriptor']['deleted_files'])=={'D-post/obsolete.csv'}
                checks.append('script_deletion_is_guarded_and_current_reference_remains_valid')
            if mode=='normal':
                api.put('Ref/render.json','{"radius":1}')
                api.call('POST',api.prefix+'/jobs',{'command':'echo STALE',
                    'input_publications':[{'publication_id':publication['id'],'require_current':True}]},expect=409)
                checks.append('dynamic_data_change_invalidates_current_reference')
            if mode in {'repeat','data'}:
                old=target.read_bytes()
                if mode=='data':api.put(path,DATA)
                second=api.wait_job(api.call('POST',api.prefix+'/paraview:run',body)['job_id'],timeout=45)
                second_pub=second['attempt']['outputs']['publication']
                assert second_pub['status']=='committed'
                second_manifest=json.loads((root/'.vibeflow/publications'/second_pub['id']/'manifest.json').read_text())
                assert set(second_manifest['files'])==({'D-post/transformed.vtp'} if mode=='data' else {TARGET,PNG})
                if mode=='data':sphere(root/'D-post/transformed.vtp',1)
                assert (bundle/'files'/TARGET).read_bytes()==old
                for previous,current in ((publication['id'],False),(second_pub['id'],True)):
                    reader=api.call('POST',api.prefix+'/jobs',{'command':'echo PARAVIEW_HISTORY_OK',
                        'input_publications':[{'publication_id':previous,'require_current':current}]})
                    api.wait_job(reader['id'])
                checks.append(mode+'_actual_input_reuse_with_immutable_history_and_current_new_result')
            checks.append(mode+'_real_data_image_sealed_dependencies_owned_child_and_publication')
            print('PASS '+checks[-1],flush=True)
    return {'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='paraview-publication-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_paraview_'+uuid.uuid4().hex
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
    print('PARAVIEW_PUBLICATION_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
