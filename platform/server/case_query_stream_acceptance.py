"""Actual S3 streaming source hashes, catalog ABA fence and corrupt-body refusal.

Pure infrastructure; no model replies, CLI inference or provider substitutions.
"""
import hashlib,json,os,threading,time
from pathlib import Path
from urllib.parse import unquote,urlparse
from testsupport import SERVER
from s3testsupport import ObjectStore


def main():
    store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',volume_max=3)
    report={'passed':False,'llm_calls':0,'mock_llm_calls':0}
    print('CASE_QUERY_STREAM_STORE '+str(store.root),flush=True)
    try:
        with store:
            os.environ.update(store.settings());os.environ['VIBEFLOW_STORAGE_FS_ROOT']=str(store.root/'cache')
            from app.config import get_settings
            get_settings.cache_clear()
            from app import storage
            from app.services.case_query_source import _remote_snapshot
            from app.services.case_file_delta import parents
            from app.services.s3_live_store import HEAD
            from botocore.exceptions import ClientError
            storage._client=None;client=storage.s3();bucket=store.bucket;prefix='workspaces/725041/cases/725042'
            legacy='workspaces/725041/cases/725043'
            client.raw.put_object(Bucket=bucket,Key=legacy+'/old.txt',Body=b'ACTUAL_LEGACY')
            assert client.public_snapshot(bucket,legacy) is None
            try:client.raw.head_object(Bucket=bucket,Key=legacy+'/'+HEAD)
            except ClientError as error:assert error.response['ResponseMetadata']['HTTPStatusCode']==404
            else:raise AssertionError('Read-only snapshot created a legacy catalog')
            payloads={'Ref/input-'+str(i)+'.txt':('ACTUAL_'+str(i)).encode() for i in range(64)}
            payloads['Ref/large.bin']=b'ACTUAL_STREAM_BYTES_'*120000
            for path,data in payloads.items():client.put_object(Bucket=bucket,Key=prefix+'/'+path,Body=data,IfNoneMatch='*')
            client.raw.put_object(Bucket=bucket,Key=prefix+'/.vibeflow/attempts/private.bin',Body=b'ACTUAL_PRIVATE')
            calls=[]
            def observed(**kw):calls.append({'operation':kw['model'].name,'status':kw['http_response'].status_code})
            client.meta.events.register('after-call.s3',observed)
            started=time.monotonic();source=_remote_snapshot(prefix);elapsed=time.monotonic()-started
            expected={'files':{p:{'size':len(data),'sha256':hashlib.sha256(data).hexdigest(),'executable':False} for p,data in sorted(payloads.items())},
                'directories':sorted(parents(payloads))}
            assert source==expected
            assert len(calls)==len(payloads)+3 and all(c=={'operation':'GetObject','status':200} for c in calls),calls
            report.update(actual_public_files=len(payloads),actual_large_file_bytes=len(payloads['Ref/large.bin']),
                actual_GETs=len(calls),constant_catalog_GETs=3,full_stream_seconds=elapsed,
                exact_all_file_hashes=True,private_history_excluded=True,legacy_catalog_not_created=True)
            # Actual A/B/A logical writes occur after the read captured its
            # catalog. Pinned old bodies stay readable; the final fence rejects
            # the newer identity even though content has returned to A.
            lock=threading.Lock();mutated=[]
            def changing(**kw):
                path=unquote(urlparse(kw['params']['url']).path)
                if '/.vibeflow/live-store/objects/' not in path:return
                with lock:
                    if mutated:return
                    mutated.append(True)
                store.client.put_object(Bucket=bucket,Key=prefix+'/Ref/input-0.txt',Body=b'CHANGED')
                store.client.put_object(Bucket=bucket,Key=prefix+'/Ref/input-0.txt',Body=payloads['Ref/input-0.txt'])
            client.meta.events.register('before-call.s3.GetObject',changing)
            try:
                try:_remote_snapshot(prefix)
                except ValueError as error:assert 'changed during' in str(error)
                else:raise AssertionError('Actual equal-content catalog ABA was accepted')
            finally:client.meta.events.unregister('before-call.s3.GetObject',changing)
            assert mutated and _remote_snapshot(prefix)==expected
            snap=client.public_snapshot(bucket,prefix);entry=snap.value['files']['Ref/input-1.txt']
            store.raw_client.put_object(Bucket=bucket,Key=entry['key'],Body=b'CORRUPTED_ACTUAL_IMMUTABLE_BODY')
            calls.clear()
            try:_remote_snapshot(prefix)
            except ValueError:pass
            else:raise AssertionError('Changed immutable source body was accepted')
            report['corruption_fault_GETs']=sum(c['operation']=='GetObject' for c in calls)
            assert report['corruption_fault_GETs']<len(payloads),'Failed source read continued scheduling the complete remaining inventory'
            report.update(passed=True,actual_catalog_ABA_refused=True,actual_corrupt_physical_body_refused=True)
    finally:
        report['store_stopped']=store.proc is not None and store.proc.poll() is not None
        (store.root/'case-query-stream-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print(json.dumps({'passed':report['passed'],'evidence':str(store.root/'case-query-stream-result.json'),'llm_calls':0,'mock_llm_calls':0}))


if __name__=='__main__':main()
