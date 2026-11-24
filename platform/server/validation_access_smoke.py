"""Case validation: sealed-input admission, result reads and queued batch cancel.

The reservation owner is a synthetic never-started validator. Batch dispatch is
disabled in the fixture so real batch routes persist an unclaimed queue. Real
Windows Jobs and Pandoc publications are used for the sealed-input/read checks.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import tempfile
import uuid

from sqlalchemy.engine import make_url
from case_validation_smoke import FIXTURE as VALIDATION
from testsupport import IsolatedServer, SERVER
from professional_launch_smoke import settled
from workflow_migration_smoke import alembic


FIXTURE = VALIDATION + '''
from app.services import batch_jobs
from app.models import JobSubmissionRequest
async def keep_queued(state):pass
batch_jobs.start_batch=keep_queued

@app.get('/api/v1/fixture/case-state')
async def case_state(case_id:int,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  await get_owned_case(case_id,user,db)
  jobs=list(await db.scalars(select(Job).where(Job.case_id==case_id).order_by(Job.id)))
  ids=[j.id for j in jobs]
  attempts=list(await db.scalars(select(JobAttempt).where(JobAttempt.job_id.in_(ids)).order_by(JobAttempt.id)))
  heads=list(await db.scalars(select(JobExecutionHead).where(JobExecutionHead.job_id.in_(ids)).order_by(JobExecutionHead.job_id)))
  requests=list(await db.scalars(select(JobSubmissionRequest).where(JobSubmissionRequest.case_id==case_id).order_by(JobSubmissionRequest.job_id)))
  return {'jobs':[[j.id,j.status] for j in jobs],
   'attempts':[[a.id,a.job_id,a.phase,a.input_manifest_json] for a in attempts],
   'heads':[[h.job_id,h.active_attempt_id,h.next_number,h.version] for h in heads],
   'requests':[r.job_id for r in requests]}

@app.post('/api/v1/fixture/retry/{attempt_id}')
async def retry(attempt_id:str,user=Depends(get_current_user)):
 async with SessionLocal() as db:
  a=await db.get(JobAttempt,attempt_id);j=await db.get(Job,a.job_id)
  await get_owned_case(j.case_id,user,db)
  job,attempt=await job_execution.allocate_retry(db,attempt_id)
  await db.commit()
  return {'job_id':job.id,'attempt_id':attempt.id}
'''


def files(root):
    """Include empty directories as well as bytes; no size/mtime shortcut."""
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            if p.is_file() else None for p in sorted(root.rglob('*'))}


def peer_server(api, database_url=None):
    peer=IsolatedServer('validation-access-peer',app='peer:app',database_url=database_url)
    overrides={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
    (peer.root/'peer.py').write_text('import os\nos.environ.update('+repr(overrides)+')\n'+FIXTURE,encoding='utf8')
    peer.token=api.token
    return peer


def verify(database_url=None):
    api=IsolatedServer('validation-access',app='fixture:app',database_url=database_url)
    (api.root/'fixture.py').write_text(FIXTURE,encoding='utf8')
    checks=[]
    def passed(name):
        checks.append(name)
        print('PASS '+name,flush=True)
    with api:
        api.create_case('Validation content and read access')
        root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
        api.put('probe.txt','BASE')
        for path in ('A-geo/scene.json','A-geo/validation.json'):
            api.put(path,json.dumps({'fixture':'legacy geometry','path':path}))
        failed_body={'command':'python -c "raise SystemExit(7)"','input_paths':['probe.txt']}
        key={'Idempotency-Key':'sealed-failed'}
        failed=api.call('POST',api.prefix+'/jobs',failed_body,extra_headers=key)
        failed=settled(api,failed['id'])
        assert failed['status']=='failed' and failed['attempt']['process_stop']['verified'],failed
        previous=failed['attempt']['id']
        api.put('E-report/report.md','# Actual Pandoc read during validation\n')
        report=api.call('POST',api.prefix+'/report:build',{})
        report=settled(api,report['job_id'])
        assert report['status']=='succeeded' and report['attempt']['process_stop']['verified'],report
        published=api.call('GET',api.prefix+'/report/results')
        publication_id=published['publication']['id']
        artifact=next(path for path in published['manifest']['artifacts'] if path.endswith('.html'))
        batch=api.call('POST',api.prefix+'/jobs:batch',{'commands':['echo QUEUED_A','echo QUEUED_B']},expect=202)
        workspace=api.call('GET',api.prefix)['workspace_id']
        other_case=api.call('POST',f'/workspaces/{workspace}/cases',{'name':'Unrelated writable case'})
        stranger=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
            'name':'Other owner','password':'isolated-other-123'})['token']
        reads=[('/geometry/scene',200),('/geometry/validation',200),('/geometry/publications',200),
            ('/mesh/results',404),('/mesh/publications',200),('/solve/results',404),('/solve/publications',200),
            ('/postprocess/results',404),('/postprocess/publications',200),
            ('/report/compose-result',404),('/report/compositions',200),
            ('/report/compose-artifact?path=E-report/report.md',404),
            ('/report/results',200),('/report/publications',200),('/report/artifact?path='+artifact,200),
            ('/latex/results',404),('/latex/publications',200),('/latex/artifact?path=report.pdf',404),
            ('/latex/source?path=report.tex',404),('/job-batches',200),('/job-batches/'+batch['id'],200)]
        expected={suffix:api.call('GET',api.prefix+suffix,expect=status,raw=True) for suffix,status in reads}
        with peer_server(api,database_url) as peer:
            owner=api.call('POST',f'/fixture/owner?case_id={api.case_id}')['job_id']
            def op(action,expect=200):
                return api.call('POST',f'/fixture/op?job_id={owner}&action={action}&seconds=3600',expect=expect)
            def state():return api.call('GET',f'/fixture/case-state?case_id={api.case_id}')
            op('reserve')
            original_files=files(root)
            original_state=state()
            for body in ({'command':'echo MUST_NOT_START','input_paths':['probe.txt']},
                         {'command':'echo MUST_NOT_START','input_publications':[{'publication_id':publication_id}]}):
                result=peer.call('POST',api.prefix+'/jobs',body,expect=409)
                assert result['detail']['code']=='validation_busy' and 'job_id' not in result['detail'],result
                assert state()==original_state and files(root)==original_files
            passed('held_reservation_rejects_path_and_publication_seal_without_files_or_rows')
            result=peer.call('POST',f'/fixture/retry/{previous}',expect=409)
            assert result['detail']['code']=='validation_busy'
            assert state()==original_state and files(root)==original_files
            passed('rejected_retry_preserves_head_attempts_requests_and_all_bytes')
            replay=peer.call('POST',api.prefix+'/jobs',failed_body,extra_headers=key)
            assert replay['id']==failed['id'] and replay['status']=='failed'
            assert state()==original_state and files(root)==original_files
            passed('idempotent_read_replay_remains_available')
            for suffix,status in reads:
                assert peer.call('GET',api.prefix+suffix,expect=status,raw=True)==expected[suffix],suffix
            assert files(root)==original_files and state()==original_state
            passed('all_21_result_history_artifact_and_batch_reads_preserve_bytes_and_metadata')
            assert published['publication']['current'] and not published['publication']['stale']
            assert peer.call('GET',api.prefix+'/report/artifact?path='+artifact,raw=True)==(root/artifact).read_bytes()
            passed('real_Pandoc_publication_and_download_remain_available')
            for suffix,_ in reads:
                peer.call('GET',api.prefix+suffix,expect=404,extra_headers={'Authorization':'Bearer '+stranger})
            peer.call('POST',api.prefix+'/job-batches/'+batch['id']+':cancel',expect=404,
                extra_headers={'Authorization':'Bearer '+stranger})
            passed('held_reservation_does_not_bypass_case_authorization')
            for suffix in ('/geometry/scene','/mesh/results','/solve/results','/report/results','/latex/results','/report/compose-result'):
                peer.call('GET',api.prefix+suffix+'?publication_id='+str(uuid.uuid4()),expect=404)
            peer.call('GET',api.prefix+'/postprocess/results?publication_id=x&job_id=1',expect=400)
            peer.call('GET',api.prefix+'/report/artifact?path=../0-caseDict/caseDict',expect=404)
            peer.call('GET',api.prefix+'/job-batches/not-a-batch',expect=404)
            passed('missing_wrong_selection_and_path_validation_remain_enforced')
            # Deliberate out-of-band corruption, restored before subsequent checks.
            immutable=root/'.vibeflow/publications'/publication_id/'files'/artifact
            original=immutable.read_bytes()
            try:
                immutable.write_bytes(original+b'CORRUPTED')
                bad=peer.call('GET',api.prefix+'/report/results',expect=409)
                assert bad['detail']['code']=='evidence_invalid',bad
            finally:immutable.write_bytes(original)
            passed('corrupt_immutable_report_is_rejected_while_reserved')
            op('heartbeat');op('renew');assert op('current')['ok']
            passed('heartbeat_renewal_and_current_binding_remain_valid')
            peer.call('PUT',f"/cases/{other_case['id']}/file",{'path':'other.txt','content':'ALLOWED'})
            passed('unrelated_case_remains_writable')
            api.call('POST','/fixture/clock?seconds=4000')
            assert op('current',409)['detail']['code']=='validation_stale'
            for path,body in [(api.prefix+'/jobs',{'command':'echo MUST_NOT_START','input_paths':['probe.txt']}),
                              (f'/fixture/retry/{previous}',None)]:
                assert peer.call('POST',path,body,expect=409)['detail']['code']=='validation_busy'
                assert state()==original_state and files(root)==original_files
            passed('expired_held_reservation_still_rejects_seal_and_retry_without_mutation')
            for suffix,status in reads:
                assert peer.call('GET',api.prefix+suffix,expect=status,raw=True)==expected[suffix]
            passed('expired_held_reservation_does_not_block_read_routes')
            cancelled=peer.call('POST',api.prefix+'/job-batches/'+batch['id']+':cancel')
            assert cancelled['ok'] and cancelled['counts']['cancelled']==2 and cancelled['active']==0,cancelled
            assert api.call('GET',api.prefix+'/job-batches/'+batch['id'])['counts']['cancelled']==2
            repeated=peer.call('POST',api.prefix+'/job-batches/'+batch['id']+':cancel')
            assert not repeated['ok'] and repeated['counts']['cancelled']==2
            assert files(root)==original_files
            passed('peer_queued_batch_cancel_is_durable_idempotent_and_does_not_write_case_files')
            op('cancel')
            assert op('current',409)['detail']['code']=='validation_stale'
            assert peer.call('PUT',api.prefix+'/file',{'path':'probe.txt','content':'FORBIDDEN'},expect=409)['detail']['code']=='validation_busy'
            assert op('release')['released']
            api.call('POST','/fixture/clock?seconds=0')
            passed('validator_cancel_does_not_release_files_until_exact_token_cleanup')
            peer.call('PUT',api.prefix+'/file',{'path':'probe.txt','content':'EDITED_AFTER_RELEASE'})
            retried=peer.call('POST',f'/fixture/retry/{previous}')
            assert (root/'.vibeflow/attempts'/retried['attempt_id']/'inputs/probe.txt').read_text()=='BASE'
            passed('released_retry_uses_original_sealed_inputs')
            fresh=peer.call('POST',api.prefix+'/jobs',{'command':'echo AFTER_RELEASE','input_paths':['probe.txt']})
            final=settled(peer,fresh['id'])
            assert final['status']=='succeeded' and final['attempt']['process_stop']['verified'],final
            assert (root/'.vibeflow/attempts'/final['attempt']['id']/'inputs/probe.txt').read_text()=='EDITED_AFTER_RELEASE'
            passed('released_submission_seals_current_bytes_and_finishes_with_verified_stop')
    result={'count':len(checks),'checks':checks,'api_root':str(api.root),'peer_root':str(peer.root),
        'read_routes':[p for p,_ in reads],
        'scope':'Synthetic validator and unclaimed batch; actual Windows sealed Jobs and Pandoc publication; other publication families cover empty/history/error reads'}
    (api.root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='validation-access-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_valaccess_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_valaccess_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,target)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('VALIDATION_ACCESS_OK '+json.dumps({'evidence':str(report),'counts':{key:value['count'] for key,value in results.items()}}))


if __name__=='__main__':asyncio.run(main())
