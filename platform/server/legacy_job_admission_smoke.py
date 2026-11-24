"""Concurrent legacy HTTP submissions and actual API-death/PPT recovery."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from job_owner_startup_smoke import shared
from job_delivery_recovery_smoke import kill_owner
from workflow_migration_smoke import alembic

FIXTURE = '''import os
os.environ.update(OVERRIDES)
import json,sys
from fastapi import Depends
from sqlalchemy import select
from app.main import app
from app.db import SessionLocal
from app.models import Job,JobAttempt,JobSubmissionRequest,DurableEvent,User,ArtifactPublicationHead
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.routers import calculix,su2,paraview
from app.services import job_delivery,batch_jobs
if PAUSED:
 async def pause(stop):await stop.wait()
 async def defer(job_id,**kwargs):raise job_delivery.DeliveryDeferred('Fixture holds committed admission')
 job_delivery.recover_forever=pause
 job_delivery.deliver=defer
 async def defer_batch(state):pass
 batch_jobs.start_batch=defer_batch
# Exercise route admission without requiring solver installations in this suite.
# The real CAE engines are checked by the existing isolated writes suite.
calculix.calculix_executable=lambda:sys.executable
su2.su2_executable=lambda:sys.executable
paraview.paraview_executable=lambda backend:sys.executable
@app.get('/api/v1/fixture/prefix/{case_id}')
async def prefix(case_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  return (await get_owned_case(case_id,user,db)).s3_prefix
@app.post('/api/v1/fixture/registered/{case_id}')
async def registered(case_id:int,user:User=Depends(get_current_user)):
 from app.services.case_write import coordinate
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db)
  async with coordinate(db,case):db.add(ArtifactPublicationHead(case_id=case_id,kind='solve'))
  await db.commit()
 return {'ok':True}
@app.get('/api/v1/fixture/admissions/{case_id}')
async def rows(case_id:int,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  await get_owned_case(case_id,user,db)
  result=[]
  for j in await db.scalars(select(Job).where(Job.case_id==case_id).order_by(Job.id)):
   attempts=list(await db.scalars(select(JobAttempt).where(JobAttempt.job_id==j.id)))
   requests=list(await db.scalars(select(JobSubmissionRequest).where(JobSubmissionRequest.job_id==j.id)))
   events=list(await db.scalars(select(DurableEvent).where(DurableEvent.attempt_id.in_([a.id for a in attempts]))))
   result.append({'id':j.id,'status':j.status,'command':j.command,
    'attempts':[a.id for a in attempts],'requests':len(requests),
    'inputs':[json.loads(a.input_manifest_json) for a in attempts],
    'spec':json.loads(requests[0].execution_spec_json) if requests else None,
    'intents':sum(e.type==job_delivery.EVENT for e in events),
    'started':sum(e.type=='attempt.claimed' for e in events)})
  return result
@app.get('/api/v1/fixture/birth')
async def birth(user:User=Depends(get_current_user)):
 from app.services.process_identity_native import observe
 return observe(os.getpid())
'''


def new_api(label, url=None, overrides=None, *, paused=True):
    api = IsolatedServer(label, app='legacy_fixture:app', database_url=url)
    code = FIXTURE.replace('OVERRIDES', repr(overrides or {})).replace('PAUSED', repr(paused))
    (api.root / 'legacy_fixture.py').write_text(code, encoding='utf8')
    return api


def rows(api):
    return api.call('GET', f'/fixture/admissions/{api.case_id}')


def files(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def verify(url=None):
    checks = []
    def passed(name):
        checks.append(name)
        print('PASS ' + name, flush=True)
    with new_api('legacy-admission', url) as api:
        case = api.create_case('Legacy durable admission')
        root = Path(api.env['VIBEFLOW_STORAGE_FS_ROOT']) / api.call('GET', f'/fixture/prefix/{api.case_id}')
        api.put('deck.inp', '*HEADING\nfixture\n')
        api.put('model.cfg', 'SOLVER= EULER\n')
        # Registered SU2 admission seals the default MESH_FILENAME dependency.
        # Delivery remains paused here; native solving has separate real tests.
        api.put('mesh',(SERVER/'examples/su2/mesh_NACA0012_inv.su2').read_text())
        api.put('view.py', 'print("fixture")\n')
        api.put('C-run/case-1/system/controlDict', 'application icoFoam;\n')
        api.put('ir.json', '{}')
        api.put('source/Make/files', 'helper.C\nLIB = libhelper\n')
        api.put('source/Make/options', '')
        api.put('source/helper.C', '// fixture\n')
        api.call('POST', api.prefix + '/udf/scaffold', {})
        # Delivery is paused: this tests library selection/admission, not loading.
        # Native acceptance separately compiles a real ELF and rejects invalid ELF.
        (root/'UDF/myFunctionObject/libmyFunctionObject.so').write_bytes(b'paused admission fixture; never executed')
        # Only PDF existence is needed for admission; native PDF generation is
        # exercised by pdf_number_smoke in the isolated writes profile.
        api.put('source.pdf', '%PDF-1.4\n')
        endpoints = [
            ('/calculix:solve', {'input_path': 'deck.inp'}, 'windows'),
            ('/su2:solve', {'config_path': 'model.cfg'}, 'windows'),
            ('/paraview:run', {'script_path': 'view.py'}, 'windows'),
            ('/mpi:run', {}, 'windows'),
            ('/udf:build', {}, 'windows'),
            ('/udf:integrate', {'run_dir': 'C-run/case-1'}, 'windows'),
            ('/flotherm:build-library', {'source_dir': 'source'}, 'windows'),
            ('/flotherm:convert', {'ir_path': 'ir.json'}, 'windows'),
            ('/postprocess:run', {'run_dir': 'C-run/case-1'}, 'windows'),
            ('/ppt:build', {'slides': [{'title': 'Admission'}]}, 'windows'),
            ('/pdf:number', {'input': 'source.pdf'}, 'windows'),
        ]
        peer = new_api('legacy-admission-peer', url, shared(api))
        with peer:
            peer.token = api.token
            peer.case_id = api.case_id
            for endpoint, body, runtime in endpoints:
                key = uuid.uuid4().hex
                headers = {'Idempotency-Key': key}
                with ThreadPoolExecutor(max_workers=2) as pool:
                    replies = [f.result(timeout=40) for f in [pool.submit(p.call, 'POST', api.prefix + endpoint,
                        body, extra_headers=headers) for p in (api, peer)]]
                assert replies[0]['job_id'] == replies[1]['job_id'], replies
                row = next(r for r in rows(api) if r['id'] == replies[0]['job_id'])
                assert row['requests'] == row['intents'] == len(row['attempts']) == 1, row
                assert row['status'] == 'queued' and row['spec']['command'] == row['command'], row
                assert row['spec']['runtime'] == runtime, row
                snapshot = files(root)
                assert peer.call('POST', api.prefix + endpoint, body, extra_headers=headers)['job_id'] == row['id']
                assert files(root) == snapshot, endpoint
                if endpoint == '/postprocess:run':
                    post_key, post_job = key, row['id']
                # A key identifies the endpoint too, not merely a body with the same shape.
                conflict = peer.call('POST', api.prefix + '/calculix:solve', {'input_path': 'different.inp'},
                    extra_headers=headers, expect=409)
                assert conflict['detail']['code'] == 'idempotency_conflict', conflict
                passed(endpoint + '_two_apis_one_attempt_replay_without_file_changes')
            assert len(rows(api)) == len(endpoints)
            mpi = rows(api)[3]
            assert mpi['inputs'][0]['handler']['key']=='mpi.openfoam'
            assert Path(mpi['spec']['argv'][1]).name=='mpi_worker.py'
            assert '--distro=vibeflow-linux' in mpi['spec']['argv']
            assert '.vibeflow/prepared/mpi.json' in mpi['inputs'][0]['workspace']['generated_inputs']
            assert not (root/'C-run/case-1/system/decomposeParDict').exists()
            for index,mode in ((4,'build'),(5,'integrate')):
                udf=rows(api)[index]
                assert udf['inputs'][0]['handler']['key']=='udf.'+mode
                assert '.vibeflow/prepared/udf.json' in udf['inputs'][0]['workspace']['generated_inputs']
                assert Path(udf['spec']['argv'][1]).name=='udf_worker.py'
            assert (root/'C-run/case-1/system/controlDict').read_text()=='application icoFoam;\n'
            library=rows(api)[6]
            assert library['inputs'][0]['handler']['key']=='flotherm.build-library'
            assert '.vibeflow/prepared/flotherm-library.json' in library['inputs'][0]['workspace']['generated_inputs']
            assert not (root/'UDF/flotherm-helper').exists()
            flotherm=rows(api)[7]
            assert flotherm['inputs'][0]['handler']['key']=='flotherm.convert'
            assert '.vibeflow/prepared/flotherm-convert.json' in flotherm['inputs'][0]['workspace']['generated_inputs']
            assert Path(flotherm['spec']['argv'][1]).name=='flotherm_convert_worker.py'
            assert not (root/'C-run/flotherm').exists()
            post = rows(api)[8]
            assert post['inputs'][0]['handler']['key']=='postprocess.case'
            assert post['inputs'][0]['workspace']['source']['job_id']==post['id']
            assert post['spec']['argv'][-1]=='--case-inputs'
            ppt = rows(api)[9]
            assert ppt['inputs'][0]['handler']['key']=='ppt.build'
            assert ppt['inputs'][0]['workspace']['source']['job_id']==ppt['id']
            assert '.vibeflow/prepared/ppt.json' in ppt['inputs'][0]['workspace']['generated_inputs']
            assert not (root/'.vibeflow/ppt-jobs').exists()
            passed('native_argv_and_private_job_bound_configuration_preserved')
            api.call('POST', api.prefix + '/calculix:solve', {'input_path': 'missing.inp'},
                     extra_headers={'Idempotency-Key': 'validation-retry'}, expect=404)
            api.call('POST', api.prefix + '/calculix:solve', {'input_path': 'deck.inp'},
                     extra_headers={'Idempotency-Key': 'bad key'}, expect=400)
            assert len(rows(api)) == len(endpoints)
            other = peer.call('POST', '/auth/register', {'email': uuid.uuid4().hex + '@example.com',
                'password': 'isolated-test-123', 'name': 'Other user'})['token']
            peer.token = other
            peer.call('POST', api.prefix + '/ppt:build', {'slides': []}, expect=404)
            passed('validation_and_access_failures_create_no_admission')
            peer.token = api.token
            api.call('POST', f'/fixture/registered/{api.case_id}')
            replay = peer.call('POST', api.prefix + '/postprocess:run', {'run_dir': 'C-run/case-1'},
                               extra_headers={'Idempotency-Key': post_key})
            assert replay['job_id'] == post_job and len(rows(api)) == len(endpoints)
            passed('legacy_postprocess_replay_survives_registered_handler_adoption')
            batch_body = {'commands': ['echo legacy-one', 'echo legacy-two'], 'max_concurrent': 1}
            batch_headers = {'Idempotency-Key': uuid.uuid4().hex}
            with ThreadPoolExecutor(max_workers=2) as pool:
                replies = [f.result(timeout=40) for f in [pool.submit(p.call, 'POST', api.prefix + '/jobs:batch',
                    batch_body, extra_headers=batch_headers, expect=202) for p in (api, peer)]]
            assert replies[0]['id'] == replies[1]['id']
            assert len(rows(api)) == len(endpoints) + 2
            snapshot = files(root)
            assert peer.call('POST', api.prefix + '/jobs:batch', batch_body,
                             extra_headers=batch_headers, expect=202)['id'] == replies[0]['id']
            assert files(root) == snapshot
            conflict = peer.call('POST', api.prefix + '/jobs:batch', {**batch_body, 'max_concurrent': 2},
                                 extra_headers=batch_headers, expect=409)
            assert conflict['detail']['code'] == 'idempotency_conflict', conflict
            passed('batch_two_apis_share_original_group_and_reject_changed_policy')

    owner = new_api('legacy-death', url)
    with owner:
        case = owner.create_case('PPT resumes after API death')
        body = {'title': 'Recovered deck', 'slides': [{'title': 'Exactly once'}]}
        key = uuid.uuid4().hex
        reply = owner.call('POST', owner.prefix + '/ppt:build', body, extra_headers={'Idempotency-Key': key})
        before = rows(owner)[0]
        assert before['status'] == 'queued'
        case_prefix = owner.call('GET', f'/fixture/prefix/{owner.case_id}')
        kill_owner(owner, owner.call('GET', '/fixture/birth'))
        peer = new_api('legacy-death-restart', url, shared(owner), paused=False)
        with peer:
            peer.token = owner.token
            peer.case_id = owner.case_id
            peer.wait_job(reply['job_id'], timeout=60)
            result = peer.call('POST', owner.prefix + '/ppt:build', body, extra_headers={'Idempotency-Key': key})
            assert result['job_id'] == reply['job_id'] and result['status'] == 'succeeded', result
            after = rows(peer)
            assert len(after) == 1 and after[0]['attempts'] == before['attempts'] and after[0]['intents'] == 1, after
            from pptx import Presentation
            output = Path(owner.env['VIBEFLOW_STORAGE_FS_ROOT']) / case_prefix / 'E-report/build/vibeflow.pptx'
            presentation = Presentation(output)
            assert len(presentation.slides) == 1
            assert any(s.has_text_frame and s.text == 'Exactly once' for s in presentation.slides[0].shapes)
            passed('actual_api_death_recovers_original_ppt_attempt_and_replays_after_output_exists')
    return {'passed': True, 'count': len(checks), 'checks': checks, 'developer_database_touched': False}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--postgres', action='store_true')
    args = parser.parse_args()
    report = Path(tempfile.mkdtemp(prefix='legacy-admission-evidence-', dir=SERVER.parents[1] / 'tmp'))
    hashes = {p.relative_to(SERVER).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (SERVER / 'app').rglob('*.py')}
    (report / 'source.json').write_text(json.dumps(hashes, indent=2), encoding='utf8')
    results = {'sqlite': await asyncio.to_thread(verify)}
    (report / 'sqlite-result.json').write_text(json.dumps(results['sqlite'], indent=2), encoding='utf8')
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url = make_url(Settings(_env_file=SERVER / '.env').database_url)
        assert url.drivername == 'postgresql+asyncpg' and url.host in {'127.0.0.1', 'localhost', '::1'}
        name = 'tmp_vf_legacy_' + uuid.uuid4().hex
        admin = await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username,
                                      password=url.password, database='postgres', timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "' + name + '"')
            created = True
            target = url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic, target, report, 'upgrade', 'head')
            results['postgresql'] = await asyncio.to_thread(verify, target)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()') == 'postgres'
                await admin.execute('DROP DATABASE "' + name + '"')
            await admin.close()
        results['postgresql']['temporary_database_removed'] = True
    assert hashes == {p.relative_to(SERVER).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in (SERVER / 'app').rglob('*.py')}
    (report / 'result.json').write_text(json.dumps(results, indent=2), encoding='utf8')
    print('LEGACY_ADMISSION_OK ' + str(report))


if __name__ == '__main__':
    asyncio.run(main())
