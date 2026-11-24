"""Registered PPT jobs: private configuration, real artifacts and crash recovery."""
import argparse
import asyncio
import base64
from io import BytesIO
import json
from pathlib import Path
import tempfile
import time
import uuid

from pptx import Presentation
from pptx.util import Inches
from testsupport import IsolatedServer, SERVER
from job_release_smoke import wait_file
from workflow_migration_smoke import alembic
from event_replay_smoke import peer

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')
CONFIG='.vibeflow/prepared/ppt.json'
FIXTURE='''
import time
from pathlib import Path
from app.services import artifact_publication as publication
from app.services.ppt_handler import PptHandler,receipt_path
root=Path(__file__).parent
def mode():return (root/'mode').read_text()
original=publication.prepare_bundle
def hold_copy(*args):
 if mode() in {'source','destination','cancel','death'}:
  (root/'ready').touch();deadline=time.monotonic()+120
  while not (root/'release').exists():
   if time.monotonic()>deadline:raise TimeoutError('PPT copy fixture')
   time.sleep(.02)
 return original(*args)
publication.prepare_bundle=hold_copy
artifacts=PptHandler.artifacts
def verify(self,run,descriptor,output,frozen):
 if mode()=='bad_receipt':(run/receipt_path(descriptor['execution_id'])).write_text('{}')
 if mode()=='bad_config':(run/'.vibeflow/prepared/ppt.json').write_text('{}')
 return artifacts(self,run,descriptor,output,frozen)
PptHandler.artifacts=verify
from app.main import app
'''


def template():
    prs=Presentation();slide=prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1)).text='Original template'
    stream=BytesIO();prs.save(stream);return stream.getvalue()


def verify(database_url=None):
    api=IsolatedServer('ppt-publication',app='ppt_fixture:app',database_url=database_url)
    (api.root/'ppt_fixture.py').write_text(FIXTURE,encoding='utf8')
    (api.root/'mode').write_text('normal');checks=[]
    with api:
        for mode in ('normal','text_only','bad_config','bad_receipt','source','destination','cancel','death'):
            api.create_case('registered PPT '+mode)
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            original=template();(root/'template.pptx').write_bytes(original);(root/'pixel.png').write_bytes(PNG)
            body={'title':'Private deck','output':'E-report/build/result.pptx','slides':[
                {'title':'Editable title','bullets':['Alpha','Beta'],'text':'Body\nSecond line',
                 'table':{'headers':['Name','Value'],'rows':[['A',1]]},'image':{'path':'pixel.png'}}],
                'template':'template.pptx'}
            if mode=='text_only':
                body.pop('template');body['slides']=[{'title':'Text only','text':'No external resources'}]
            (api.root/'mode').write_text(mode)
            for name in ('ready','release'):(api.root/name).unlink(missing_ok=True)
            created=api.call('POST',api.prefix+'/ppt:build',body,extra_headers={'Idempotency-Key':'ppt-'+mode})
            identifier=created['job_id'];target=root/created['output']
            if mode in {'source','destination','cancel','death'}:
                wait_file(api.root/'ready',seconds=30)
                assert not target.exists(),'Private artifact leaked before publication'
                if mode=='source':(root/'template.pptx').write_bytes(original+b'changed')
                if mode=='destination':
                    target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'user destination')
                if mode=='cancel':api.call('POST',f'/jobs/{identifier}:cancel')
                if mode=='death':
                    api.proc.terminate();api.proc.wait(timeout=10)
                    with peer(api) as recovered:
                        done=recovered.wait_job(identifier,timeout=45)
                        publication=done['attempt']['outputs']['publication']
                        assert publication['status']=='committed' and target.exists()
                        assert target.read_bytes()==(root/done['attempt']['workspace']['run_path']/created['output']).read_bytes()
                        records=recovered.call('GET',api.prefix+'/jobs')
                        assert len(records)==1 and records[0]['id']==identifier
                    checks.append('API_death_recovers_original_PPT_without_second_attempt')
                    break
                (api.root/'release').touch()
            expected='succeeded' if mode in {'normal','text_only'} else 'cancelled' if mode=='cancel' else 'failed'
            done=api.wait_job(identifier,expected=expected)
            if mode=='cancel':
                deadline=time.monotonic()+20
                while done['attempt']['phase']!='cancelled' and time.monotonic()<deadline:
                    time.sleep(.05);done=api.call('GET',f'/jobs/{identifier}')
                assert done['attempt']['phase']=='cancelled'
            publication=done['attempt']['outputs']['publication'];frozen=done['attempt']['workspace']
            inputs=root/frozen['inputs_path'];private=root/frozen['run_path']
            assert CONFIG in frozen['generated_inputs'] and (inputs/CONFIG).is_file()
            assert not (root/CONFIG).exists() and not (root/'.vibeflow/ppt-jobs').exists()
            assert not (root/'.vibeflow/ppt-receipts').exists()
            if mode in {'normal','text_only'}:
                assert publication['status']=='committed'
                assert target.read_bytes()==(private/created['output']).read_bytes()
                prs=Presentation(BytesIO(target.read_bytes()))
                assert len(prs.slides)==(1 if mode=='text_only' else 2)
                assert prs.core_properties.title==body['title']
                bundle=root/'.vibeflow/publications'/publication['id']
                manifest=json.loads((bundle/'manifest.json').read_text(encoding='utf8'))
                assert manifest['descriptor']['receipt']['attempt_id']==done['attempt']['id']
                assert (bundle/'files'/created['output']).read_bytes()==target.read_bytes()
                replay=api.call('POST',api.prefix+'/ppt:build',body,extra_headers={'Idempotency-Key':'ppt-'+mode})
                assert replay['job_id']==identifier
                api.call('POST',api.prefix+'/ppt:build',body,expect=409)
                if mode=='normal':
                    assert (inputs/'template.pptx').read_bytes()==original and (inputs/'pixel.png').read_bytes()==PNG
                    assert (root/'template.pptx').read_bytes()==original
                    reader=api.call('POST',api.prefix+'/jobs',{'command':'echo PPT_REFERENCE_OK',
                        'input_publications':[{'publication_id':publication['id'],'require_current':True}]})
                    assert 'PPT_REFERENCE_OK' in api.wait_job(reader['id'])['log_backlog']
                    checks.extend(['private_config_resources_and_immutable_PPT_alias',
                        'idempotency_and_exclusive_output','published_PPT_is_current_input'])
                else:checks.append('text_only_request_seals_generated_config_without_live_resources')
            else:
                assert publication['status']!='committed'
                if mode=='destination':assert target.read_bytes()==b'user destination'
                else:assert not target.exists()
                checks.append(mode+'_cannot_publish_or_overwrite_output')
    return {'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='ppt-publication-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_ppt_'+uuid.uuid4().hex
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
    print('PPT_PUBLICATION_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
