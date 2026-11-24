"""Real numbered PDFs retain compatibility aliases through immutable publication."""
import argparse
import asyncio
import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import time
import uuid

from pypdf import PdfReader,PdfWriter

from testsupport import IsolatedServer,SERVER
from job_release_smoke import wait_file
from workflow_migration_smoke import alembic
from event_replay_smoke import peer

FIXTURE='''
import time
from pathlib import Path
from app.services import artifact_publication as publication
from app.services.pdf_number_handler import PdfNumberHandler,receipt_path
root=Path(__file__).parent
def mode():return (root/'mode').read_text()
original=publication.prepare_bundle
def hold_copy(*args):
 if mode() in {'source','destination','cancel','death'}:
  (root/'ready').touch();deadline=time.monotonic()+120
  while not (root/'release').exists():
   if time.monotonic()>deadline:raise TimeoutError('PDF copy fixture')
   time.sleep(.02)
 return original(*args)
publication.prepare_bundle=hold_copy
artifacts=PdfNumberHandler.artifacts
def verify(self,run,descriptor,output,frozen):
 if mode()=='bad_receipt':(run/receipt_path(descriptor['execution_id'])).write_text('{}')
 return artifacts(self,run,descriptor,output,frozen)
PdfNumberHandler.artifacts=verify
from app.main import app
'''


def pdf():
    writer=PdfWriter()
    for _ in range(2):writer.add_blank_page(width=300,height=400)
    writer.add_metadata({'/Title':'Original private source'})
    stream=BytesIO();writer.write(stream);return stream.getvalue()


def verify(database_url=None):
    api=IsolatedServer('pdf-publication',app='pdf_fixture:app',database_url=database_url)
    (api.root/'pdf_fixture.py').write_text(FIXTURE,encoding='utf8')
    (api.root/'mode').write_text('normal')
    checks=[]
    with api:
        for mode in ('normal','invalid','bad_receipt','source','destination','cancel','death'):
            api.create_case('registered PDF '+mode)
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            source=root/'E-report/source.pdf';source.parent.mkdir(parents=True,exist_ok=True)
            original=pdf();source.write_bytes(b'invalid PDF' if mode=='invalid' else original)
            (api.root/'mode').write_text(mode)
            for name in ('ready','release'):(api.root/name).unlink(missing_ok=True)
            body={'input':'E-report/source.pdf'}
            created=api.call('POST',api.prefix+'/pdf:number',body,extra_headers={'Idempotency-Key':'pdf-'+mode})
            identifier=created['job_id'];target=root/created['output']
            if mode in {'source','destination','cancel','death'}:
                wait_file(api.root/'ready',seconds=30)
                assert not target.exists(),'Private output leaked before commit'
                if mode=='source':source.write_bytes(original+b'\n% changed source')
                if mode=='destination':target.write_bytes(b'user destination')
                if mode=='cancel':api.call('POST',f'/jobs/{identifier}:cancel')
                if mode=='death':
                    api.proc.terminate();api.proc.wait(timeout=10)
                    with peer(api) as recovered:
                        done=recovered.wait_job(identifier,timeout=45)
                        publication=done['attempt']['outputs']['publication']
                        assert publication['status']=='committed' and target.exists()
                        frozen=done['attempt']['workspace'];private=root/frozen['run_path']
                        assert target.read_bytes()==(private/created['output']).read_bytes()
                        records=recovered.call('GET',api.prefix+'/jobs')
                        assert len(records)==1 and records[0]['id']==identifier
                    checks.append('API_death_before_copy_recovers_original_PDF_without_new_attempt')
                    break
                (api.root/'release').touch()
            expected='succeeded' if mode=='normal' else 'cancelled' if mode=='cancel' else 'failed'
            done=api.wait_job(identifier,expected=expected)
            if mode=='cancel':
                deadline=time.monotonic()+20
                while done['attempt']['phase']!='cancelled' and time.monotonic()<deadline:
                    time.sleep(.05);done=api.call('GET',f'/jobs/{identifier}')
                assert done['attempt']['phase']=='cancelled',done
            publication=done['attempt']['outputs']['publication']
            if mode=='normal':
                assert publication['status']=='committed'
                frozen=done['attempt']['workspace'];private=root/frozen['run_path']
                assert (root/frozen['inputs_path']/'E-report/source.pdf').read_bytes()==original
                assert source.read_bytes()==original and target.read_bytes()==(private/created['output']).read_bytes()
                rendered=PdfReader(BytesIO(target.read_bytes()))
                assert len(rendered.pages)==2 and rendered.metadata.title=='Original private source'
                assert all(f'{i} / 2' in p.extract_text() for i,p in enumerate(rendered.pages,1))
                bundle=root/'.vibeflow/publications'/publication['id']
                manifest=json.loads((bundle/'manifest.json').read_text(encoding='utf8'))
                assert manifest['descriptor']['receipt']['attempt_id']==done['attempt']['id']
                assert (bundle/'files'/created['output']).read_bytes()==target.read_bytes()
                assert not (root/'.vibeflow/pdf-receipts').exists()
                replay=api.call('POST',api.prefix+'/pdf:number',body,extra_headers={'Idempotency-Key':'pdf-'+mode})
                assert replay['job_id']==identifier
                api.call('POST',api.prefix+'/pdf:number',body,expect=409)
                api.call('POST',api.prefix+'/pdf:number',{'input':'../outside.pdf'},expect=400)
                reader=api.call('POST',api.prefix+'/jobs',{'command':'echo PDF_REFERENCE_OK',
                    'input_publications':[{'publication_id':publication['id'],'require_current':True}]})
                assert 'PDF_REFERENCE_OK' in api.wait_job(reader['id'])['log_backlog']
                checks.extend(['native_PDF_private_input_bundle_and_compatible_download',
                    'page_count_labels_metadata_and_source_bytes_preserved',
                    'idempotent_original_job_and_existing_output_no_overwrite',
                    'published_PDF_is_a_current_readable_input_for_following_attempt'])
            else:
                assert publication['status']!='committed'
                if mode=='destination':assert target.read_bytes()==b'user destination'
                else:assert not target.exists()
                checks.append(mode+'_cannot_publish_or_overwrite_user_output')
    return {'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='pdf-publication-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_pdf_'+uuid.uuid4().hex
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
    print('PDF_PUBLICATION_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
