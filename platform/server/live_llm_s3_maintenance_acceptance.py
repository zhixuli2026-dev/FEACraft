"""Real SeaweedFS retention/compaction, restarted API and real DeepSeek readback."""
import argparse
import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import types
import uuid
import zipfile

from botocore.exceptions import ClientError
from live_llm_acceptance import source
from live_llm_shared_index_acceptance import SharedIndexServer, configure
from live_llm_sandbox_recovery_acceptance import model_read
from s3testsupport import ObjectStore
from testsupport import SERVER
from app.services.process_identity_native import probe
from app.services.s3_live_store import COMPACT_SCHEMA, DIRECTORY, HEAD, PENDING
from app.services.s3_version_maintenance import plan, apply

ROOT = SERVER.parents[1]
LEGACY_ARCHIVE = ROOT/'platform/backups/release-review-shared-index-20261116.zip'


def verify(configuration):
    api = SharedIndexServer('live-s3-version-maintenance', app='live_fixture:app')
    result = {'passed':False, 'checks':[], 'mock_llm_calls':0, 'api_exits':[]}
    def save():
        raw = json.dumps(result, ensure_ascii=False, indent=2)
        assert configuration['key'] not in raw
        (api.root/'maintenance-result.json').write_text(raw+'\n',encoding='utf-8')
    def passed(name):
        result['checks'].append(name);save();print('VERSION_MAINTENANCE_PASS '+name,flush=True)
    def denied(operation, kind=ValueError):
        try: operation()
        except kind: return
        raise AssertionError('Operation unexpectedly accepted')
    def stopped():
        deadline = time.monotonic()+10
        while probe(api.birth)['state']=='match' and time.monotonic()<deadline:time.sleep(.05)
        observed = probe(api.birth)
        result['api_exits'].append({'birth':api.birth,'probe':observed})
        assert observed['state']=='exited'
    print('VERSION_MAINTENANCE_EVIDENCE '+str(api.root),flush=True)
    try:
        with ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed') as store:
            result['object_store']=str(store.root)
            client, bucket, prefix = store.client,store.bucket,'workspaces/900001/cases/900001'
            def put(path,data):return client.put_object(Bucket=bucket,Key=prefix+'/'+path,Body=data)
            def delete(path):return client.delete_object(Bucket=bucket,Key=prefix+'/'+path)
            put('current.txt',b'original');put('current.txt',b'current')
            old = client.snapshot(bucket,prefix)
            put('removed.txt',b'deleted');delete('removed.txt')
            unknown=prefix+'/'+DIRECTORY+'/objects/not-a-version/keep.txt'
            store.raw_client.put_object(Bucket=bucket,Key=unknown,Body=b'unknown-retained')
            conservative = plan(client,bucket,prefix)
            assert not conservative['candidates'] and conservative['retained']['recent']>=2
            assert conservative['retained']['unknown']==1
            denied(lambda:apply(client,conservative))
            compacted=apply(client,conservative,confirm_offline=True)
            assert compacted['deleted_objects']==0 and compacted['compacted_tombstones']==1
            passed('default_30_days_preserves_recent_versions_and_requires_offline_confirmation')
            # The exact old release reader rejects the actual compacted catalog.
            with zipfile.ZipFile(LEGACY_ARCHIVE) as archive:
                old_code=archive.read('platform/server/app/services/s3_live_store.py')
            module=types.ModuleType('app.services.maintenance_legacy_live_store')
            module.__package__='app.services';sys.modules[module.__name__]=module
            exec(compile(old_code,str(LEGACY_ARCHIVE)+'!s3_live_store.py','exec'),module.__dict__)
            old_client=module.Client(store.raw_client)
            denied(lambda:old_client._load(bucket,prefix))
            passed('actual_previous_release_reader_refuses_compacted_schema')
            denied(lambda:client.commit_snapshot(old,{'removed.txt':lambda:b'stale'},watch_paths={'removed.txt'}),ClientError)
            after=client._freeze(bucket,prefix,'removed.txt',b'stale-single',{})
            denied(lambda:client._change(bucket,prefix,old.value,old.etag,'removed.txt',None,after),ClientError)
            passed('pre_compaction_batch_and_single_writes_cannot_recreate_deleted_path')
            v2old=client.snapshot(bucket,prefix)
            put('removed.txt',b'second');delete('removed.txt')
            zero=plan(client,bucket,prefix,retention_days=0)
            assert zero['candidate_bytes']>0
            swept=apply(client,zero,confirm_offline=True)
            assert swept['deleted_objects']==len(zero['candidates']) and not swept['errors']
            assert client._load(bucket,prefix)[0]['schema']==COMPACT_SCHEMA
            assert store.raw_client.head_object(Bucket=bucket,Key=unknown)['ContentLength']==16
            for row in zero['candidates']:
                denied(lambda row=row:store.raw_client.head_object(Bucket=bucket,Key=row['key']),ClientError)
            assert client.read_snapshot(client.snapshot(bucket,prefix),'current.txt')==b'current'
            assert client.read_snapshot(client.snapshot(bucket,prefix),'removed.txt') is None
            denied(lambda:client.commit_snapshot(v2old,{'removed.txt':lambda:b'v2 stale'},watch_paths={'removed.txt'}),ClientError)
            assert not client.matches_snapshot(v2old,{'removed.txt'})
            passed('actual_old_objects_deleted_current_and_unknown_objects_preserved_v2_ABA_refused')
            stale=plan(client,bucket,prefix,retention_days=0)
            put('other.txt',b'new-write')
            denied(lambda:apply(client,stale,confirm_offline=True))
            put(PENDING,b'pending-transaction')
            denied(lambda:plan(client,bucket,prefix,retention_days=0))
            delete(PENDING)
            passed('changed_plan_and_pending_file_transaction_refuse_maintenance')
            # Actual application lifecycle, then the shipped CLI against only
            # this disposable service/database. No live developer data touched.
            configure(api,store.settings(),False)
            proof='RETIREMENT_'+uuid.uuid4().hex
            path='Ref/maintenance-proof.txt'
            try:
                with api:
                    api.create_case('Actual S3 version retention')
                    api.put(path,'old-content');api.put(path,proof+'\n')
                    with closing(sqlite3.connect(api.root/'test.sqlite')) as db:
                        api_prefix=db.execute('SELECT s3_prefix FROM cases WHERE id=?',(api.case_id,)).fetchone()[0]
            finally:stopped()
            env={**os.environ,**api.env,**store.settings()}
            plan_path=api.root/'approved-plan.json'; receipt=api.root/'cli-result.json'
            cli=[sys.executable,'-X','utf8','-B',str(ROOT/'platform/scripts/maintain-s3-versions.py')]
            preview=subprocess.run(cli+['plan','--plan',str(plan_path),'--case-prefix',api_prefix,'--retention-days','0'],
                                   env=env,capture_output=True,timeout=90)
            assert preview.returncode==0,preview.stderr.decode('utf-8','replace')[-800:]
            approved=json.loads(plan_path.read_text(encoding='utf-8'))
            applied=subprocess.run(cli+['apply','--plan',str(plan_path),'--plan-sha256',approved['plan_sha256'],
                                   '--confirm-offline','--result',str(receipt)],env=env,capture_output=True,timeout=90)
            assert applied.returncode==0,applied.stderr.decode('utf-8','replace')[-800:]
            result['cli_result']=json.loads(receipt.read_text(encoding='utf-8'))
            assert result['cli_result']['status']=='complete' and result['cli_result']['deleted_objects']>0
            passed('shipped_cli_previews_and_reclaims_actual_api_old_versions_after_stop')
            configure(api,{**store.settings(),'VIBEFLOW_JWT_SECRET':api.env['VIBEFLOW_JWT_SECRET']},False)
            try:
                with api:
                    assert api.call('GET',api.prefix+'/raw?path='+path,raw=True)==(proof+'\n').encode()
                    api.put('Ref/after-maintenance.txt','write-after-v2')
                    assert api.call('GET',api.prefix+'/raw?path=Ref/after-maintenance.txt',raw=True)==b'write-after-v2'
                    result['real_llm_calls']=model_read(api,'offline-maintenance','current-file',proof,configuration,
                        already_restored=True,path=path)
                    passed('restarted_api_reads_and_writes_v2_real_model_reads_preserved_source')
            finally:stopped()
            result['removed_bytes']=swept['deleted_bytes']+result['cli_result']['deleted_bytes']
        result['object_store_launcher_exited']=store.proc.poll() is not None
        assert result['object_store_launcher_exited']
        result['passed']=True;save()
        print('VERSION_MAINTENANCE_OK '+str(api.root/'maintenance-result.json'),flush=True)
        return 0
    except BaseException as error:
        result['failure_type']=type(error).__name__
        result['failure']=str(error).replace(configuration['key'],'[REDACTED]')[:1000]
        save();print('VERSION_MAINTENANCE_FAILED '+result['failure_type'],flush=True)
        return 1


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--review',type=Path,required=True)
    args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf-8'))
    assert review['entry']=='platform/server/live_llm_s3_maintenance_acceptance.py' and review['classification']=='real_llm'
    for path,digest in review['sources'].items():
        assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==digest,path
    assert hashlib.sha256(LEGACY_ARCHIVE.read_bytes()).hexdigest()==review['legacy_archive_sha256']
    return verify(asyncio.run(source(args.source_profile_id)))


if __name__=='__main__':sys.exit(main())
