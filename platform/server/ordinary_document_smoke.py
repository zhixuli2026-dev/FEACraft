"""No engineering head: native documents, exact drafts, durable history and races."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
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
import zipfile

from sqlalchemy.engine import make_url
from app.services.casedict import set_block
from compose_handler_smoke import CONTROLLED, WORKER as COMPOSE_WORKER
from report_handler_smoke import WORKER as REPORT_WORKER
from postprocess_handler_smoke import WORKER as BASE_WORKER
from geometry_publication_smoke import wait_file
from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic

WORKER=COMPOSE_WORKER+REPORT_WORKER[len(BASE_WORKER):]
BLUE='<svg xmlns="http://www.w3.org/2000/svg" width="30" height="20"><rect width="30" height="20" fill="blue"/></svg>'

# These operations are available only in the disposable test process, not HTTP.
MUTATION='''import asyncio,json,sys
from sqlalchemy import select
from app.db import SessionLocal,engine
from app.models import ArtifactPublicationHead,Case,JobAttempt,JobSubmissionRequest
from app.services.job_execution import request_hash
async def main():
    async with SessionLocal() as db:
        case_id=int(sys.argv[1])
        if sys.argv[2]=='strict-report':
            from app.services.report_handler import ReportHandler
            from app.services.workflow_state import ExecutionConflict
            try:
                await ReportHandler().prepare(db,await db.get(Case,case_id),None,
                    parameters={'run_dir':'C-run/cavity','mode':'compose'},
                    budget={'timeout_seconds':120,'max_log_bytes':1048576})
            except ExecutionConflict as error:
                assert error.code=='input_stale'
            else: raise AssertionError('Workflow report accepted missing registered evidence')
        elif sys.argv[2]=='head':
            db.add(ArtifactPublicationHead(case_id=case_id,kind='postprocess'))
        else:
            row=await db.scalar(select(JobSubmissionRequest).where(
                JobSubmissionRequest.case_id==case_id,JobSubmissionRequest.idempotency_key==sys.argv[2]))
            attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==row.job_id))
            descriptor=json.loads(attempt.publication_json)['descriptor']
            row.request_hash=request_hash({'handler':'report.v1','mode':'markdown','run_dir':descriptor['run_dir']})
        await db.commit()
    await engine.dispose()
asyncio.run(main())
'''


def verify(database_url=None):
    checks=[]
    def done(name):
        checks.append(name)
        print('PASS '+name,flush=True)
    server=IsolatedServer('ordinary-document',app='controlled_document:app',database_url=database_url)
    (server.root/'controlled_document.py').write_text(CONTROLLED,encoding='utf-8')
    (server.root/'controlled_worker.py').write_text(WORKER,encoding='utf-8')
    (server.root/'mutate_owned_database.py').write_text(MUTATION,encoding='utf-8')
    with server as api:
        def new_case(name):
            api.create_case(name)
            return next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
        def build(key=None,*,client=api,expect=200):
            return client.call('POST',api.prefix+'/report:build',{},expect=expect,
                extra_headers={'Idempotency-Key':key} if key else None)
        def compose(body=None,key=None,*,wait=True,client=api,expect=200):
            return client.call('POST',api.prefix+'/report:compose'+('' if wait else '?wait=false'),
                body if body is not None else {'overwrite':True},expect=expect,
                extra_headers={'Idempotency-Key':key} if key else None)
        def result(kind='report',pub=None,*,client=api,expect=200):
            route='/report/results' if kind=='report' else '/report/compose-result'
            return client.call('GET',api.prefix+route+('?publication_id='+pub if pub else ''),expect=expect)
        def mutate(action):
            completed=subprocess.run([sys.executable,'-B',str(api.root/'mutate_owned_database.py'),str(api.case_id),action],
                cwd=SERVER,env=dict(api.env,PYTHONPATH=str(SERVER)+os.pathsep+api.env['PYTHONPATH']),
                capture_output=True,text=True,encoding='utf-8',timeout=30)
            assert completed.returncode==0,completed.stderr
        root=new_case('Ordinary documents')
        mutate('strict-report')
        done('workflow_handler_does_not_inherit_standalone_document_evidence_compatibility')
        target=root/'E-report/report.md'
        api.put('Ref/plot.svg',BLUE)
        markdown='# Ordinary native document\n\n![figure](../Ref/plot.svg)\n\n$x^2$\n'
        api.put('E-report/report.md',markdown)
        initial=build('initial-build')
        job=api.wait_job(initial['job_id'])
        current=result()
        assert not job['attempt']['workspace'].get('publications')
        assert not current['manifest']['input_publications']
        html=(root/'E-report/build/report.html').read_bytes()
        assert b'Ordinary native document' in html and b'data:image/svg+xml' in html and b'<math' in html
        with zipfile.ZipFile(root/'E-report/build/report.docx') as archive:
            assert any(b'fill="blue"' in archive.read(name) for name in archive.namelist() if name.startswith('word/media/'))
        assert target.read_text(encoding='utf-8')==markdown
        done('ordinary_HTML_DOCX_have_native_content_frozen_resources_and_persistent_publication')

        dictionary=root/'0-caseDict/caseDict'
        saved=dictionary.read_bytes()
        try:
            target.unlink()
            dictionary.write_bytes(b'invalid case dictionary')
            replay=build('initial-build')
            assert replay['job_id']==initial['job_id'] and replay['publication_id']==initial['publication_id']
            mutate('initial-build')
            assert build('initial-build')['publication_id']==initial['publication_id']
        finally:
            dictionary.write_bytes(saved)
            target.write_text(markdown,encoding='utf-8')
        done('new_and_pre_unification_idempotency_keys_replay_before_mutable_source_validation')
        before=api.call('GET',api.prefix+'/jobs')
        compose(key='initial-build',expect=409)
        compose({'output':'elsewhere.md'},expect=400)
        revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
        for body in ({'overwrite':False},{'overwrite':True,'expected_revision':revision+1},
                     {'overwrite':True,'expected_sha256':None},{'overwrite':True,'expected_sha256':'0'*64}):
            compose(body,expect=409)
        assert api.call('GET',api.prefix+'/jobs')==before
        done('overwrite_CAS_and_cross_operation_idempotency_conflicts_create_no_jobs')

        original=b'Original exact bytes\r\n\xff\xfe\r\n'
        target.write_bytes(original)
        body={'overwrite':True,'expected_revision':revision,'expected_sha256':hashlib.sha256(original).hexdigest()}
        draft=compose(body,'initial-compose')
        assert draft['publication']['applied'] and len(draft['chapters'])==9 and draft['evidence']['report_ready']
        assert not draft['evidence']['metrics'] and not draft['toolchain']['input_publications']
        assert (root/draft['backup']).read_bytes()==original
        assert draft['toolchain']['generated_at'] in draft['content']
        assert compose(body,'initial-compose')['job_id']==draft['job_id']
        done('ordinary_composition_has_frozen_generation_time_and_exact_non_UTF8_backup')
        secondary=root/'E-report/report.generated.md'
        secondary.write_bytes(b'')
        other=compose({'output':'E-report/report.generated.md','expected_sha256':hashlib.sha256(b'').hexdigest()})
        assert other['backup'] and (root/other['backup']).read_bytes()==b''
        assert result('compose')['publication_id']==draft['publication_id']
        done('empty_file_backup_and_independent_generated_draft_channel')
        linked=build()
        linked_job=api.wait_job(linked['job_id'])
        refs=linked_job['attempt']['workspace']['publications']
        assert len(refs)==1 and refs[0]['publication_id']==draft['publication_id']
        done('ordinary_generated_draft_lineage_is_recorded_in_HTML_DOCX_build')

        api.put('E-report/report.md','# Manual edit\n')
        replay=compose(body,'initial-compose')
        assert not replay['publication']['applied'] and target.read_text()=='# Manual edit\n'
        manual=api.wait_job(build()['job_id'])
        assert not manual['attempt']['workspace'].get('publications')
        assert result('report',initial['publication_id'])['manifest']==current['manifest']
        done('manual_edits_remain_inputs_and_previous_versions_remain_readable')

        peer=IsolatedServer('ordinary-document-peer')
        peer.token=api.token
        def start_peer():
            peer.log=(peer.root/'api.log').open('a',encoding='utf-8')
            peer.proc=subprocess.Popen([sys.executable,'-m','uvicorn','controlled_document:app',
                '--app-dir',str(SERVER),'--host','127.0.0.1','--port',str(peer.port),'--no-access-log'],
                cwd=api.root,env=api.env,stdout=peer.log,stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                if peer.proc.poll() is not None: raise AssertionError((peer.root/'api.log').read_text(encoding='utf-8'))
                try:
                    if peer.call('GET','/api/health',api_prefix=False).get('ok'): return
                except (OSError,ValueError): pass
                time.sleep(.1)
            raise TimeoutError('Peer API did not start')
        def stop_peer():
            if peer.proc and peer.proc.poll() is None:
                peer.proc.terminate()
                try: peer.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    peer.proc.kill()
                    peer.proc.wait(timeout=5)
            if getattr(peer,'log',None): peer.log.close()
        try:
            start_peer()
            for kind in ('compose','report'):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures=[pool.submit(compose,key='two-api-compose',client=client) if kind=='compose'
                        else pool.submit(build,'two-api-build',client=client) for client in (api,peer)]
                    rows=[future.result(timeout=40) for future in futures]
                assert rows[0]['job_id']==rows[1]['job_id']
                api.wait_job(rows[0]['job_id'])
            active={kind:result(kind) for kind in ('report','compose')}
            stop_peer()
            start_peer()
            assert all(result(kind,client=peer)['publication']==active[kind]['publication'] for kind in active)
            done('two_API_idempotency_and_reader_restart_preserve_both_document_heads')

            marker=api.root/f'report-control-{api.case_id}'
            for kind,failures in (('report',('noop','badhtml','baddocx','badsource','badtoolchain')),
                                  ('compose',('noop','badcompose','badbackup','badcomposeproof'))):
                for failure in failures:
                    before=target.read_bytes()
                    active_id=result(kind)['publication']['id']
                    marker.write_text(failure)
                    try:
                        row=build() if kind=='report' else compose(wait=False)
                        job=api.wait_job(row['job_id'],expected='failed')
                    finally: marker.unlink(missing_ok=True)
                    assert job['attempt']['reason_code']=='publication_failed'
                    assert target.read_bytes()==before and result(kind)['publication']['id']==active_id
                done(kind+'_no_op_and_self_rehashed_forged_outputs_cannot_publish')

            for kind,scenarios in (('report',('source','image','superseded','cancel')),
                                    ('compose',('target','quality_added','superseded','cancel'))):
                for scenario in scenarios:
                    if kind=='report': api.put('E-report/report.md',markdown)
                    barrier=api.root/f'hold-report-{api.case_id}'
                    barrier.with_suffix('.ready').unlink(missing_ok=True)
                    barrier.with_suffix('.release').unlink(missing_ok=True)
                    marker.write_text('hold')
                    changed=None
                    old=None
                    try:
                        row=build() if kind=='report' else compose(wait=False)
                        wait_file(barrier.with_suffix('.ready'))
                        marker.unlink()
                        if scenario in {'source','target','image','quality_added'}:
                            changed=root/('Ref/plot.svg' if scenario=='image' else
                                'B-mesh/quality.json' if scenario=='quality_added' else 'E-report/report.md')
                            old=changed.read_bytes() if changed.exists() else None
                            changed.write_bytes(BLUE.replace('blue','red').encode() if scenario=='image' else
                                b'{"status":"passed","metrics":{"cells":7}}' if scenario=='quality_added' else b'# Edited during build\n')
                        elif scenario=='superseded':
                            newer=build(client=peer) if kind=='report' else compose(client=peer,wait=False)
                            deadline=time.monotonic()+90
                            while peer.call('GET',f'/jobs/{newer["job_id"]}')['attempt']['phase']!='finalizing':
                                assert time.monotonic()<deadline,'New document did not reach finalizing'
                                time.sleep(.05)
                            barrier.with_suffix('.release').touch()
                            api.wait_job(newer['job_id'])
                        else: api.call('POST',f'/jobs/{row["job_id"]}:cancel',{})
                    finally:
                        marker.unlink(missing_ok=True)
                        barrier.with_suffix('.release').touch()
                    job=api.wait_job(row['job_id'],expected='cancelled' if scenario=='cancel' else
                        'succeeded' if scenario=='superseded' else 'failed')
                    if changed:
                        assert job['attempt']['outputs']['publication']['status']=='failed'
                        if old is None: changed.unlink()
                        else: changed.write_bytes(old)
                    elif scenario=='superseded':
                        assert result(kind,row['publication_id'])['publication']['status']=='superseded'
                        assert result(kind)['publication']['id']==newer['publication_id']
                    done(kind+'_held_'+scenario+'_respects_frozen_inputs_and_generation_fence')
        finally: stop_peer()

        root=new_case('Ordinary quality metadata')
        quality=root/'B-mesh/quality.json'
        quality.write_text(json.dumps({'status':'passed','metrics':{'cells':731}}),encoding='utf-8')
        draft=compose()
        assert '731' in draft['content'] and 'B-mesh/quality.json' in draft['toolchain']['inputs']
        quality.write_text(json.dumps({'status':'passed','metrics':{'cells':947}}),encoding='utf-8')
        assert result('compose',draft['publication_id'])['publication']['stale']
        before=api.call('GET',api.prefix+'/jobs')
        build(expect=409)
        assert api.call('GET',api.prefix+'/jobs')==before
        fresh=compose()
        assert '947' in fresh['content'] and fresh['publication']['applied']
        api.wait_job(build()['job_id'])
        done('ordinary_quality_metadata_is_frozen_and_stale_generated_conclusions_require_recomposition')

        root=new_case('Legacy gate still required')
        saved=api.call('GET',api.prefix+'/caseDict')
        text=set_block(saved['content'],'postProcessing',{'runDir':'C-run/cavity','fields':{'p':{}},
            'dataExtraction':{'volumeAverages':{'mean':{'field':'p','time':1}}}})
        api.call('PUT',api.prefix+'/caseDict',{'content':text,'expected_revision':saved['dict_revision']})
        before=api.call('GET',api.prefix+'/jobs')
        build(expect=409)
        compose(expect=409)
        assert api.call('GET',api.prefix+'/jobs')==before
        done('configured_postprocessing_without_evidence_never_falls_back_to_ordinary_success')

        for kind in ('report','compose'):
            root=new_case('Head registered during '+kind)
            baseline=build() if kind=='report' else compose()
            api.wait_job(baseline['job_id'])
            marker=api.root/f'report-control-{api.case_id}'
            barrier=api.root/f'hold-report-{api.case_id}'
            marker.write_text('hold')
            try:
                row=build() if kind=='report' else compose(wait=False)
                wait_file(barrier.with_suffix('.ready'))
                mutate('head')
            finally:
                marker.unlink(missing_ok=True)
                barrier.with_suffix('.release').touch()
            api.wait_job(row['job_id'],expected='failed')
            assert result(kind,baseline['publication_id'])['publication']['stale']
            before=api.call('GET',api.prefix+'/jobs')
            build(expect=409)
            compose(expect=409)
            assert api.call('GET',api.prefix+'/jobs')==before
            done(kind+'_new_or_invalid_engineering_head_blocks_inflight_and_new_ordinary_builds')
        result_data={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
        (api.root/'ordinary-document-result.json').write_text(json.dumps(result_data,indent=2)+'\n',encoding='utf-8')
        return result_data


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='ordinary-document-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_document_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_document_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,
            password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"')
            created=True
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
    print('ORDINARY_DOCUMENT_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':
    asyncio.run(main())
