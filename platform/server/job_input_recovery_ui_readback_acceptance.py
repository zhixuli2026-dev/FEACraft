"""Read back the actual UI run after its Windows newline assertion failed.

Preserve the failed report. Start no new job, restore no browser metadata, and
replay only the actual original key/body observed before response loss.
"""
import argparse,hashlib,json,shutil,sqlite3,traceback
from pathlib import Path
import httpx
from testsupport import IsolatedServer,SERVER
from job_input_recovery_ui_acceptance import APP,read_rows

ROOT=SERVER.parents[1]
SOURCE=ROOT/'tmp/input-recovery-browser-gc4v2i02'
PREVIOUS=ROOT/'tmp/job-input-recovery-tpa1zazi'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def run():
    from app.services.windows_process_scope import ProcessHandle
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--review',type=Path,required=True);args=parser.parse_args()
    review=json.loads(args.review.read_text(encoding='utf8'));assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest
    failed=json.loads((SOURCE/'UI-result.json').read_text(encoding='utf8'));browser=json.loads((SOURCE/'browser-finished.json').read_text(encoding='utf8'))
    assert failed['passed'] is False and failed['failure']['frames'][-1]['line']==97
    assert failed['actual_API_exited'] and failed['actual_preview_exited'] and not failed.get('cleanup_failures')
    assert browser['passed'] and browser['responseLoss']['status']==202 and browser['reloadReplay']['actualCaseJobIds']==[6,8]
    assert browser['professionalFinal']['id']==9 and browser['professionalFinal']['stopVerified']
    setup=json.loads((SOURCE/'browser-setup.json').read_text(encoding='utf8'))
    source_sha=sha(SOURCE/'test.sqlite')
    api=IsolatedServer('input-UI-readback',app='input_readback_fixture:app');(api.root/'input_readback_fixture.py').write_text(APP,encoding='utf8')
    with sqlite3.connect('file:'+(SOURCE/'test.sqlite').as_posix()+'?mode=ro',uri=True) as old:
        with sqlite3.connect(api.root/'test.sqlite') as new:old.backup(new)
    shutil.copytree(SOURCE/'files',api.root/'files')
    tables=['jobs','job_attempts','job_submission_requests','artifact_publications','artifact_publication_heads','job_input_recoveries',
        'native_sessions','native_cli_sessions','native_viz_sessions','native_session_links']
    # Heads use composite identities, so hash all rows instead of assuming id.
    def history(path):
        with sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True) as db:
            return {table:hashlib.sha256(repr(sorted(db.execute('SELECT * FROM '+table).fetchall(),key=repr)).encode()).hexdigest() for table in tables}
    before=history(api.root/'test.sqlite')
    report={'passed':False,'original_UI_run_failed_preserved':True,'new_business_jobs':0,'new_real_model_calls':0,'new_mock_llm_calls':0,
        'developer_database_modified':False,'source_review_sha256':sha(args.review)}
    handle=None;entered=False
    print('INPUT_UI_READBACK '+str(api.root),flush=True)
    try:
        api.__enter__();entered=True;birth=api.call('GET','/test/input-birth');handle=ProcessHandle.query(birth);report['API_birth']=birth
        api.token=api.call('POST','/auth/login',{'email':setup['email'],'password':'isolated-test-123'})['token']
        original=browser['responseLoss']
        replay=api.call('POST','/jobs/6:recover-inputs',original['requestBody'],extra_headers={'Idempotency-Key':original['key']},expect=202)
        assert replay['id']==8 and replay['created'] is False and replay['input_recovery']==original['body']['input_recovery']
        assert api.call('POST','/jobs/6:recover-inputs',original['requestBody'],extra_headers={'Idempotency-Key':original['key']},expect=202)['id']==8
        report['new_API_exact_original_body_key_same_receipt']=True
        for source_id,target,case in [(6,8,5),(7,9,6)]:
            old=api.call('GET','/jobs/'+str(source_id));new=api.call('GET','/jobs/'+str(target))
            assert old['status']=='cancelled' and old['attempt']['process_identity'] is None
            assert new['status']=='succeeded' and new['attempt']['process_stop']['verified'] is True
            assert old['input_recoveries']==new['input_recoveries'] and len(old['input_recoveries'])==1
            relation=old['input_recoveries'][0];assert relation['source_job_id']==source_id and relation['target_job_id']==target
            assert all(relation[key] is False for key in ['old_execution_replayed','original_inputs_reconstructed','stop_evidence_created'])
            path=new['attempt']['workspace']['run_path']
            if target==8:
                assert api.call('GET','/cases/5/raw?path='+path+'/ui-input-proof.txt',raw=True)==b'CURRENT_UI_INPUT_V2'
                executions=api.call('GET','/cases/5/raw?path='+path+'/ui-input-executions.txt',raw=True)
                assert executions==bytes([111,110,99,101,13,10]) and executions.replace(bytes([13,10]),bytes([10]))==bytes([111,110,99,101,10])
                report['actual_Windows_execution_marker_bytes']=list(executions)
            else:
                assert new['attempt']['handler']['key']=='report.build'
                assert b'REGISTERED_UI_CURRENT_INPUT' in api.call('GET','/cases/6/raw?path='+path+'/E-report/build/report.html',raw=True)
                requests=api.call('GET','/test/input-native-requests/6');assert requests and all(row['cleanup_verified'] is True for row in requests)
                report['actual_native_prepare_stop_proofs']=requests
            report.setdefault('actual_replacements',[]).append({'receipt':relation,'job':new})
        for table in ['native_sessions','native_cli_sessions','native_viz_sessions','native_session_links','job_input_recoveries']:
            previous=read_rows(PREVIOUS/'test.sqlite',table);actual=read_rows(SOURCE/'test.sqlite',table)
            assert all(actual[key]==value for key,value in previous.items())
            if table=='job_input_recoveries':assert len(actual)==len(previous)+2
        assert history(api.root/'test.sqlite')==before and sha(SOURCE/'test.sqlite')==source_sha
        report.update(functional_passed=True,no_new_execution_or_business_row_mutations=True,original_UI_database_unchanged=True,
            original_source_history_preserved=True,two_new_UI_replacements_only=True,
            exact_CRLF_single_execution_verified=True,both_originals_unclaimed_and_cancelled=True,
            natural_UI_reload_replay_evidence='Original browser assertions passed before collapsed-section locator timeout; actual same receipt, two jobs only and pending cleared observed in browser artifact')
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                try:api.call('POST','/test/input-exit')
                except (OSError,httpx.TransportError):pass
                handle.wait_exit(10);report['actual_API_exited']=True
            except BaseException as error:report['cleanup_failure']=type(error).__name__
            api.__exit__(RuntimeError if 'failure' in report else None,None,None)
        if handle is not None:handle.close()
        report['passed']=bool(report.get('functional_passed') and report.get('actual_API_exited') and 'cleanup_failure' not in report)
        (api.root/'readback-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print('INPUT_UI_READBACK_RESULT '+str(api.root/'readback-result.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
