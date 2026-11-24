"""Actual recovery of a retained isolated validator-completion failure.

Restores only an owned stopped test store under its original immutable identity.
The original actual failure report stays byte-identical. No developer DB writes,
new solver invocation, model inference or simulated native results.
"""
import argparse,hashlib,json,re,shutil,sqlite3,time
from pathlib import Path
from urllib.parse import urlsplit
from testsupport import SERVER,IsolatedServer
from s3testsupport import ObjectStore
from app.services import process_identity_native as identity


def verify(api_root,store_root):
    workspace=(SERVER.parents[1]/'tmp').resolve()
    api_root=Path(api_root).resolve(strict=True);store_root=Path(store_root).resolve(strict=True)
    assert api_root.is_relative_to(workspace) and store_root.is_relative_to(workspace)
    assert re.fullmatch('professional-finalizing-[A-Za-z0-9_-]+',api_root.name)
    assert re.fullmatch('s3-attempt-store-[A-Za-z0-9_-]+',store_root.name)
    original=api_root/'professional-result.json';failure=json.loads(original.read_text(encoding='utf8'))
    original_hash=hashlib.sha256(original.read_bytes()).hexdigest()
    assert not failure['passed'] and failure['failure']['type']=='AssertionError' and failure['store_stopped']
    assert failure['model_calls']==failure['mock_llm_calls']==0
    for observed in failure['API_exit_probes']:assert observed['state']=='exited'
    deadline=failure['before_exit']['original_deadline_unix'];assert time.time()>deadline,'Use the retained actual expired failure without changing its deadline'
    database=api_root/'test.sqlite'
    with sqlite3.connect('file:'+database.as_posix()+'?mode=ro',uri=True) as conn:
        user=conn.execute('SELECT email FROM users ORDER BY id LIMIT 1').fetchone()[0]
        rows=conn.execute('SELECT phase,children_json FROM native_requests WHERE kind=?',('job.finalize',)).fetchall()
    assert any(phase=='active' and any(c['stop'] and c['stop']['verified'] for c in json.loads(value)['children'].values()) for phase,value in rows)
    old_result=(api_root/'result.json').read_bytes();old_base=json.loads(old_result)['api'];parsed=urlsplit(old_base)
    assert parsed.hostname=='127.0.0.1' and parsed.port not in {8000,5173,5432,6380}
    with (api_root/'post-failure-original-result.json').open('xb') as stream:stream.write(old_result)
    with (api_root/'post-failure-original-api.log').open('xb') as stream:stream.write((api_root/'api.log').read_bytes())
    store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',restore_from=store_root)
    api=IsolatedServer('post-failure-unused-bootstrap',app='output_fixture:app')
    unused_root=api.root;api.root=api_root;api.port=parsed.port;api.base=old_base
    result={'passed':False,'source_failure':str(original.relative_to(SERVER.parents[1])),
        'source_failure_sha256':original_hash,'original_deadline_unix':deadline,'model_calls':0,'mock_llm_calls':0,
        'developer_database_modified':False,'unused_empty_bootstrap_root':str(unused_root),'store_root':str(store.root)}
    print('POST_FAILURE_RECOVERY '+str(api_root)+' STORE '+str(store.root),flush=True)
    try:
        with store:
            saved=json.loads((store_root/'store.json').read_text());assert store.bucket==saved['bucket'] and store.endpoint==saved['endpoint']
            with api:
                api.token=api.call('POST','/auth/login',{'email':user,'password':'isolated-test-123'})['token']
                birth=api.call('GET','/test/output-process');result['actual_API_birth']=birth
                job_id=failure['job_id'];before=api.call('GET','/test/output-state/'+str(job_id));result['before']=before
                count=(api_root/'actual-root-releases.jsonl').read_bytes();assert len(count.splitlines())==1
                options=api.call('GET','/jobs/'+str(job_id)+'/output-recovery')
                payload={'attempt_id':before['attempt_id'],'inventory_sha256':options['inventory_sha256']}
                import httpx
                with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=180) as client:
                    response=client.post('/jobs/'+str(job_id)+':finalize-outputs',json=payload)
                    assert response.status_code==200,{'status':response.status_code}
                    done=response.json();result['response']=done
                after=api.call('GET','/test/output-state/'+str(job_id));result['after']=after
                assert done['state']=='failed' and after['reason_code']=='resource_limit' and after['phase']=='failed'
                assert not done['native_execution_started'] and not done['solver_execution_started']
                assert after['attempt_id']==before['attempt_id'] and after['input']==before['input'] and after['original_deadline_unix']==deadline
                assert len(after['finalization_requests'])==len(before['finalization_requests'])+1
                assert after['write_checkpoint']['schema']=='vibeflow.job-write-checkpoint.v5'
                assert after['write_checkpoint']['stop']==failure['before_exit']['actual_in_memory_stop']
                assert (api_root/'actual-root-releases.jsonl').read_bytes()==count
                assert api.call('POST','/jobs/'+str(job_id)+':finalize-outputs',payload)==done
                result['functional_passed']=True
    except BaseException as error:
        result['failure']={'type':type(error).__name__,'detail':str(error)[:500]}
    finally:
        if result.get('actual_API_birth'):
            until=time.monotonic()+10
            while True:
                observed=identity.probe(result['actual_API_birth'])
                if observed['state']=='exited' or time.monotonic()>=until:break
                time.sleep(.03)
            result['actual_API_exit']=observed
        result['store_stopped']=store.proc is not None and store.proc.poll() is not None
        result['source_store_bytes_unchanged']=json.loads((store.root/'restore-final.json').read_text())['original_unchanged']
        result['original_failure_unchanged']=hashlib.sha256(original.read_bytes()).hexdigest()==original_hash
        result['passed']=bool(result.get('functional_passed') and result['store_stopped'] and result['source_store_bytes_unchanged']
            and result['original_failure_unchanged'] and result.get('actual_API_exit',{}).get('state')=='exited')
        result['source_hashes']={p:hashlib.sha256((SERVER/p).read_bytes()).hexdigest() for p in [
            'attempt_finalizer_post_failure_acceptance.py','testsupport.py','s3testsupport.py',
            'app/services/job_output_finalizing.py','app/services/finalizing_request_recovery.py',
            'app/services/finalizing_native_preparation.py','app/services/finalizing_native_usage.py','app/services/finalizing_native_scope.py']}
        with (api_root/'post-failure-recovery-result.json').open('x',encoding='utf8') as stream:json.dump(result,stream,indent=2)
    print('POST_FAILURE_RECOVERY_RESULT '+str(api_root/'post-failure-recovery-result.json'),flush=True)
    return result['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--api-root',required=True);parser.add_argument('--store-root',required=True)
    args=parser.parse_args();raise SystemExit(0 if verify(args.api_root,args.store_root) else 1)
