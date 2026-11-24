"""Explicitly retain a real history with missing stop proof after actual API exit."""
import argparse, hashlib, json, shutil, sqlite3, traceback
from pathlib import Path

import httpx
from testsupport import SERVER, IsolatedServer
from native_session_lineage_acceptance import APP

ROOT = SERVER.parents[1]
SOURCE = ROOT / 'tmp/native-session-lineage-67dywquk'
SESSION = 'b923dc30-16a9-4b86-9d37-30e7385c2eb9'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def raw_session(path):
    with sqlite3.connect('file:' + path.as_posix() + '?mode=ro', uri=True) as db:
        db.row_factory=sqlite3.Row
        row=dict(db.execute('SELECT * FROM native_sessions WHERE id=?',(SESSION,)).fetchone())
        return row


def run():
    from app.services.windows_process_scope import ProcessHandle
    from app.services import process_identity_native as identity
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--review',type=Path,required=True);args=parser.parse_args()
    review=json.loads(args.review.read_text(encoding='utf8'))
    assert review['fixture_sha256']==hashlib.sha256(APP.encode()).hexdigest()
    for name,digest in review['source_hashes'].items():assert sha(ROOT/name)==digest
    runtime=json.loads((SOURCE/'lineage-result.json').read_text(encoding='utf8'))
    assert len(runtime['API_exit_witnesses'])==2 and all(row['state']=='exited' for row in runtime['API_exit_witnesses'])
    failure=json.loads((ROOT/'tmp/N5-native-lineage-post-runtime-cleanup-failure-20261110.json').read_text(encoding='utf8'))
    assert failure['passed'] is False and failure['session_id']==SESSION
    original_sha=sha(SOURCE/'test.sqlite');before=raw_session(SOURCE/'test.sqlite')
    assert before['phase']=='active' and json.loads(before['stop_json'])=={}
    api=IsolatedServer('native-history-retained',app='retained_fixture:app')
    assert api.root.parent==SOURCE.parent and api.root!=SOURCE
    with sqlite3.connect('file:'+(SOURCE/'test.sqlite').as_posix()+'?mode=ro',uri=True) as source:
        with sqlite3.connect(api.root/'test.sqlite') as target:source.backup(target)
    shutil.copytree(SOURCE/'files',api.root/'files');(api.root/'retained_fixture.py').write_text(APP,encoding='utf8')
    report={'passed':False,'source':SOURCE.relative_to(ROOT).as_posix(),'source_sha256':original_sha,
        'session_id':SESSION,'new_real_model_calls':0,'new_mock_llm_calls':0,'new_native_roots':0,
        'developer_database_modified':False,'source_review_sha256':sha(args.review)}
    handle=None;entered=False
    try:
        api.__enter__();entered=True
        birth=api.call('GET','/test/lineage-process');handle=ProcessHandle.query(birth);report['API_birth']=birth
        api.token=api.call('POST','/auth/login',{'email':'3b442597109a428694a0fd6aafd54646@example.com','password':'isolated-test-123'})['token']
        plan=api.call('GET','/native-sessions/terminal/'+SESSION)
        assert plan['phase']=='recovery_required' and plan['cleanup_verified'] is None and plan['owner_available'] is False and plan['alive'] is None
        body={'action':'retain_for_review','snapshot_sha256':plan['snapshot_sha256'],
            'note':'Actual historical API/host/gate exits observed; original complete scope-stop proof missing, retain restrictions'}
        receipt=api.call('POST','/native-sessions/terminal/'+SESSION+':retain-for-review',body)
        assert api.call('POST','/native-sessions/terminal/'+SESSION+':retain-for-review',body)==receipt
        assert receipt['execution_authorized'] is False and receipt['stop_evidence_created'] is False
        rejected=api.call('DELETE','/terminal/sessions/'+SESSION,expect=409)
        assert api.call('GET','/native-sessions/terminal/'+SESSION)['review']['id']==receipt['id']
        assert raw_session(api.root/'test.sqlite')==before
        assert raw_session(SOURCE/'test.sqlite')==before and sha(SOURCE/'test.sqlite')==original_sha
        journal=json.loads(before['process_json']);observations={'owner':identity.probe(json.loads(before['owner_identity_json'])),
            'host':identity.probe(journal['root']['host']),'gate':identity.probe(journal['gate']['process'])}
        assert all(row['state']=='exited' for row in failure['actual_process_observations'].values())
        report.update(functional_passed=True,receipt=receipt,original_row_unchanged=True,
            unknown_control_rejected_status=409,original_persisted_stop_still_missing=True,
            actual_identity_observations=observations,original_exit_observations=failure['actual_process_observations'],physical_scope_stop_claimed=False)
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'frames':[{'file':Path(v.filename).name,'line':v.lineno,'function':v.name} for v in traceback.extract_tb(error.__traceback__)]}
    finally:
        if entered:
            try:
                try:api.call('POST','/test/lineage-exit')
                except (OSError,httpx.TransportError):pass
                handle.wait_exit(10);report['actual_API_exited']=True
            except BaseException as error:report['cleanup_failure']=type(error).__name__
            api.__exit__(RuntimeError if 'failure' in report else None,None,None)
        if handle is not None:handle.close()
        report['passed']=bool(report.get('functional_passed') and report.get('actual_API_exited') and 'cleanup_failure' not in report)
        (api.root/'retained-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print('NATIVE_RETAINED_HISTORY_RESULT '+str(api.root/'retained-result.json'),flush=True)
    return 0 if report['passed'] else 1


if __name__=='__main__':raise SystemExit(run())
