"""Actual S3 catalog selection, legacy fallback and conditional byte reads.

Pure storage infrastructure: no model round, no simulated provider or response.
"""
import json,os,uuid
from collections import Counter
from pathlib import Path
from testsupport import SERVER
from s3testsupport import ObjectStore


def main():
    store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',volume_max=3)
    report={'passed':False,'llm_calls':0,'mock_llm_calls':0}
    print('QUERY_CATALOG_STORE '+str(store.root),flush=True)
    try:
        with store:
            os.environ.update(store.settings())
            os.environ.update(VIBEFLOW_STORAGE_FS_ROOT=str(store.root/'cache'),
                VIBEFLOW_CODEX_STATE_ROOT=str(store.root/'codex'),VIBEFLOW_CODEX_WSL_STATE_ROOT='')
            from app.config import get_settings
            get_settings.cache_clear()
            from app import storage
            from app.services import attempt_objects as objects
            from app.services.case_write import CaseWriteConflict
            storage._client=None;client=storage.s3();bucket=get_settings().s3_bucket
            prefix='workspaces/725031/cases/725032';calls=[]
            def observed(**kwargs):
                model=kwargs['model'];response=kwargs['http_response']
                calls.append({'operation':model.name,'status':response.status_code})
            client.meta.events.register('after-call.s3',observed)
            def put(path,data,**conditions):
                return client.put_object(Bucket=bucket,Key=prefix+'/'+path,Body=data,**conditions)
            # A real pre-catalog key must remain readable without a mutation.
            client.raw.put_object(Bucket=bucket,Key=prefix+'/legacy.txt',Body=b'ACTUAL_LEGACY')
            assert client.public_file_rows(bucket,prefix) is None
            old=objects.selection(prefix,['legacy.txt'],False)
            assert set(old)=={'legacy.txt'}
            assert objects.read(prefix,'legacy.txt',etag=old['legacy.txt']['etag'],limit=13)==b'ACTUAL_LEGACY'
            assert client.public_file_rows(bucket,prefix) is None
            report['legacy_raw_read_without_catalog_creation']=True
            # This authorized fixture write performs the original migration.
            put('new.txt',b'CURRENT_1',IfNoneMatch='*')
            put('unrelated.bin',b'ACTUAL_UNSELECTED_BYTES'*100000,IfNoneMatch='*')
            for i in range(12):
                client.raw.put_object(Bucket=bucket,Key=prefix+'/.vibeflow/attempts/'+str(uuid.uuid4())+'/private.bin',Body=b'PRIVATE_'+str(i).encode())
            reference={r['path']:{'size':r['size'],'etag':r['etag']} for r in storage._s3_list(prefix)
                if r['path'] in {'legacy.txt','new.txt'}}
            absent=['missing-header-'+str(i)+'.H' for i in range(600)]
            calls.clear()
            before=objects.selection(prefix,['new.txt'],False,optional_paths=['legacy.txt',*absent])
            first_counts=dict(Counter(r['operation'] for r in calls))
            report['selection_comparison']={'actual':before,'original_listing':reference,'requests':first_counts}
            assert before==reference and first_counts=={'GetObject':1},first_counts
            report['actual_600_missing_candidates_one_catalog_GET']=first_counts
            report['actual_private_history_objects']=12
            report['logical_rows_match_original_listing']=True
            assert len(before)==2 and 'unrelated.bin' not in before
            put('new.txt',b'CURRENT_2',IfMatch=before['new.txt']['etag'])
            changed=objects.selection(prefix,['new.txt'],False,optional_paths=['legacy.txt',*absent])
            assert changed['new.txt']['etag']!=before['new.txt']['etag']
            try:objects.read(prefix,'new.txt',etag=before['new.txt']['etag'],limit=9)
            except CaseWriteConflict as error:assert error.code=='input_stale'
            else:raise AssertionError('Actual changed selected object was accepted using its old version')
            put('new.txt',b'CURRENT_1',IfMatch=changed['new.txt']['etag'])
            aba=objects.selection(prefix,['new.txt'],False)
            assert aba['new.txt']['etag'] not in {before['new.txt']['etag'],changed['new.txt']['etag']}
            assert objects.read(prefix,'new.txt',etag=aba['new.txt']['etag'],limit=9)==b'CURRENT_1'
            client.delete_object(Bucket=bucket,Key=prefix+'/legacy.txt',IfMatch=changed['legacy.txt']['etag'])
            deleted=objects.selection(prefix,['new.txt'],False,optional_paths=['legacy.txt',*absent])
            assert set(deleted)=={'new.txt'}
            calls.clear();all_files=objects.selection(prefix,[],True)
            all_counts=dict(Counter(r['operation'] for r in calls));assert all_counts=={'GetObject':1}
            assert set(all_files)=={'new.txt','unrelated.bin'}
            report.update(passed=True,selected_version_change_detected=True,actual_old_version_read_refused=True,
                actual_equal_bytes_ABA_has_new_identity=True,actual_deleted_optional_is_missing=True,
                private_objects_not_admitted=True,unselected_body_not_downloaded=True,
                all_public_files_one_catalog_GET=all_counts)
    finally:
        report['store_stopped']=store.proc is not None and store.proc.poll() is not None
        (store.root/'catalog-query-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print(json.dumps({'passed':report['passed'],'store_stopped':report['store_stopped'],
        'evidence':str(store.root/'catalog-query-result.json'),'llm_calls':0,'mock_llm_calls':0}))


if __name__=='__main__':main()
