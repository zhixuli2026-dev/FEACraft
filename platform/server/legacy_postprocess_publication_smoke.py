"""Real case-file extraction migrates with immutable history and document consumers."""
import argparse
import asyncio
import json
from pathlib import Path
import tempfile
import time
import uuid

from app.services.casedict import set_block
from cae_benchmarks import thermal_files
from testsupport import IsolatedServer,SERVER
from job_release_smoke import wait_file
from workflow_migration_smoke import alembic
from event_replay_smoke import peer

FIXTURE='''
import time
from pathlib import Path
from app.services import artifact_publication as publication
root=Path(__file__).parent
original=publication.prepare_bundle
def hold_copy(*args):
 if (root/'mode').read_text() in {'source','new_time','cancel','death'}:
  (root/'ready').touch();deadline=time.monotonic()+120
  while not (root/'release').exists():
   if time.monotonic()>deadline:raise TimeoutError('Postprocessing copy fixture')
   time.sleep(.02)
 return original(*args)
publication.prepare_bundle=hold_copy
from app.main import app
from app.db import SessionLocal
from app.models import ArtifactPublicationHead,User
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services.case_write import coordinate
from fastapi import Depends
@app.post('/api/v1/fixture/solver/{case_id}')
async def adopt_solver(case_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db)
  async with coordinate(db,case):db.add(ArtifactPublicationHead(case_id=case.id,kind='solve'))
  await db.commit()
 return {'ok':True}
'''


def verify(database_url=None):
    api=IsolatedServer('legacy-postprocess-publication',app='case_post_fixture:app',database_url=database_url)
    (api.root/'case_post_fixture.py').write_text(FIXTURE,encoding='utf8');(api.root/'mode').write_text('normal')
    checks=[];run_dir='C-run/conduction'
    with api:
        api.create_case('native heat source')
        seed=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
        for path,content in thermal_files().items():
            target=seed/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_text(content,encoding='utf8')
        job=api.call('POST',api.prefix+'/jobs',{'type':'solve','runtime':'wsl',
            'command':'source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd C-run/conduction && blockMesh && laplacianFoam'})
        api.wait_job(job['id'],timeout=120)
        source_files={p.relative_to(seed).as_posix():p.read_bytes() for p in (seed/run_dir).rglob('*') if p.is_file()}
        assert run_dir+'/1/T' in source_files
        for mode in ('normal','override','source','new_time','cancel','death'):
            api.create_case('case extraction '+mode)
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            for path,data in source_files.items():
                target=root/path;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
            saved=api.call('GET',api.prefix+'/caseDict')
            post={'runDir':'C-run/unused' if mode=='override' else run_dir,'fields':{'T':{'unit':'K'}},
                'dataExtraction':{'volumeAverages':{'mean':{'field':'T','time':1}}},
                'derivedQuantities':{'referenceValues':{'analytic':{'metric':'"mean.T@1"','value':350,
                    'unit':'K','kind':'verification','source':'"T(x)=300+100*x"','l2_max':1e-6}}}}
            text=set_block(saved['content'],'postProcessing',post)
            api.call('PUT',api.prefix+'/caseDict',{'content':text,'expected_revision':saved['dict_revision']})
            (api.root/'mode').write_text(mode)
            for name in ('ready','release'):(api.root/name).unlink(missing_ok=True)
            body={'run_dir':run_dir};headers={'Idempotency-Key':'post-'+mode}
            created=api.call('POST',api.prefix+'/postprocess:run',body,extra_headers=headers);identifier=created['job_id']
            pointer=root/'D-post/data/latest-postprocess.json'
            if mode in {'source','new_time','cancel','death'}:
                wait_file(api.root/'ready',seconds=60);assert not pointer.exists()
                if mode=='source':(root/run_dir/'1/T').write_bytes(source_files[run_dir+'/1/T']+b'\n// edited\n')
                if mode=='new_time':
                    extra=root/run_dir/'2/T';extra.parent.mkdir();extra.write_bytes(source_files[run_dir+'/1/T'])
                if mode=='cancel':api.call('POST',f'/jobs/{identifier}:cancel')
                if mode=='death':
                    api.proc.terminate();api.proc.wait(timeout=10)
                    with peer(api) as recovered:
                        done=recovered.wait_job(identifier,timeout=60)
                        assert done['attempt']['outputs']['publication']['status']=='committed'
                        result=recovered.call('GET',api.prefix+'/postprocess/results')
                        assert result['job_id']==identifier and not result['publication']['stale']
                        assert len(recovered.call('GET',api.prefix+'/jobs'))==1
                    checks.append('API_death_recovers_original_validated_extraction_without_rerunning_solver_or_worker')
                    break
                (api.root/'release').touch()
            expected='succeeded' if mode in {'normal','override'} else 'cancelled' if mode=='cancel' else 'failed'
            done=api.wait_job(identifier,expected=expected,timeout=90)
            if mode=='cancel':
                deadline=time.monotonic()+30
                while done['attempt']['phase']!='cancelled' and time.monotonic()<deadline:
                    time.sleep(.05);done=api.call('GET',f'/jobs/{identifier}')
                assert done['attempt']['phase']=='cancelled'
            frozen=done['attempt']['workspace'];inputs=root/frozen['inputs_path']
            assert (inputs/run_dir/'1/T').read_bytes()==source_files[run_dir+'/1/T']
            assert not (root/f'.vibeflow/jobs/postprocess-{identifier}-config.json').exists()
            if mode not in {'normal','override'}:
                assert not pointer.exists() and done['attempt']['outputs']['publication']['status']!='committed'
                checks.append(mode+'_preserves_sealed_input_and_rejects_stale_or_cancelled_publication');continue
            result=api.call('GET',api.prefix+'/postprocess/results')
            assert not result['publication']['stale'] and result['metrics']['readiness']['report_ready']
            mean=next(row for row in result['metrics']['metrics'] if row['name']=='mean.T@1')
            assert abs(mean['value']-350)<1e-6
            publication=result['publication']['id'];bundle=root/'.vibeflow/publications'/publication
            manifest=json.loads((bundle/'manifest.json').read_text(encoding='utf8'))
            assert manifest['descriptor']['handler']=='postprocess-case.v1'
            assert not manifest['descriptor']['input_publications'] and manifest['descriptor']['source']['run_inputs']
            assert (bundle/'files'/result['result_dir']/'metrics.json').read_bytes()==(root/result['result_dir']/'metrics.json').read_bytes()
            assert api.call('POST',api.prefix+'/postprocess:run',body,extra_headers=headers)['job_id']==identifier
            if mode=='override':
                checks.append('explicit_legacy_run_override_retained_without_claiming_solver_publication');continue
            checks.extend(['real_350K_extraction_uses_frozen_fields_and_private_derived_configuration',
                'immutable_metrics_original_download_and_idempotent_replay'])
            composed=api.call('POST',api.prefix+'/report:compose?wait=false',{'overwrite':True})
            api.wait_job(composed['job_id'],timeout=90)
            draft=api.call('GET',api.prefix+'/report/compose-result')
            assert not draft['publication']['stale'] and 'mean.T@1' in draft['content'] and str(mean['value']) in draft['content']
            built=api.call('POST',api.prefix+'/report:build',{})
            api.wait_job(built['job_id'],timeout=90)
            assert (root/'E-report/build/report.docx').stat().st_size>100
            api.put('E-report/latex/main.tex','\\documentclass{article}\\begin{document}Sealed heat evidence\\end{document}\n')
            latex=api.call('POST',api.prefix+'/latex:build',{})
            api.wait_job(latex['job_id'],timeout=90)
            checks.append('case_extraction_supports_composition_HTML_DOCX_and_real_LaTeX_without_registered_mesh')
            second=api.call('POST',api.prefix+'/postprocess:run',body);api.wait_job(second['job_id'],timeout=90)
            history=api.call('GET',api.prefix+'/postprocess/results?job_id='+str(identifier))
            assert history['metrics']==result['metrics'] and not history['publication']['current'] and history['publication']['stale']
            assert api.call('GET',api.prefix+'/postprocess/results')['job_id']==second['job_id']
            checks.append('superseded_results_remain_readable_while_current_pointer_moves')
            api.call('POST',f'/fixture/solver/{api.case_id}')
            assert api.call('GET',api.prefix+'/postprocess/results')['publication']['stale']
            api.call('POST',api.prefix+'/report:build',{},expect=409)
            api.call('POST',api.prefix+'/postprocess:run',body,expect=409)
            assert api.call('POST',api.prefix+'/postprocess:run',body,extra_headers=headers)['job_id']==identifier
            checks.append('solver_registration_invalidates_case_lineage_without_breaking_old_request_replay')
    return {'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='legacy-postprocess-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'localhost','127.0.0.1','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_casepost_'+uuid.uuid4().hex
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
    print('LEGACY_POSTPROCESS_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':asyncio.run(main())
