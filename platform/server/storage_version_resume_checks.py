"""Actual S3 partial deletion plus injected lost response; no model calls."""
import argparse
import hashlib
import json
from pathlib import Path

from botocore.exceptions import ClientError
from s3testsupport import ObjectStore
from testsupport import SERVER
from app.services.s3_live_store import DIRECTORY
from app.services.s3_version_maintenance import plan, apply

ROOT=SERVER.parents[1]


def verify():
    with ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed') as store:
        result={'passed':False,'real_llm_calls':0,'mock_llm_calls':0,
                'kind':'actual S3 with explicitly injected post-delete response loss'}
        output=store.root/'version-resume-result.json'
        print('VERSION_RESUME_EVIDENCE '+str(output),flush=True)
        client,bucket,prefix=store.client,store.bucket,'workspaces/900003/cases/900003'
        def put(data):client.put_object(Bucket=bucket,Key=prefix+'/source.txt',Body=data)
        put(b'first');put(b'second');put(b'current')
        approved=plan(client,bucket,prefix,retention_days=0)
        assert len(approved['candidates'])==2
        original_delete=client.raw.delete_object
        lost=[]
        def delete_then_lose_response(**args):
            actual=original_delete(**args)
            if args['Key'].startswith(prefix+'/'+DIRECTORY+'/objects/'):
                lost.append(args['Key'])
                raise OSError('Injected response loss after actual conditional deletion')
            return actual
        try:
            client.raw.delete_object=delete_then_lose_response
            try:apply(client,approved,confirm_offline=True)
            except OSError:pass
            else:raise AssertionError('Response loss was not raised')
        finally:
            client.raw.delete_object=original_delete
        assert len(lost)==1
        try:store.raw_client.head_object(Bucket=bucket,Key=lost[0])
        except ClientError as error:
            assert error.response['ResponseMetadata']['HTTPStatusCode']==404
        else:raise AssertionError('Object was not actually deleted')
        try:apply(client,approved,confirm_offline=True)
        except ValueError:pass
        else:raise AssertionError('Old plan should not replay after its HEAD fence')
        fresh=plan(client,bucket,prefix,retention_days=0)
        assert len(fresh['candidates'])==1 and fresh['head_etag']!=approved['head_etag']
        finished=apply(client,fresh,confirm_offline=True)
        assert finished['deleted_objects']==1 and not finished['errors']
        assert client.read_snapshot(client.snapshot(bucket,prefix),'source.txt')==b'current'
        assert not plan(client,bucket,prefix,retention_days=0)['candidates']
        result.update(passed=True,actual_first_delete=lost[0],resume_result=finished,
                      checks=['actual_delete_before_response_loss','old_plan_replay_refused',
                              'new_preview_only_remaining_version','retry_preserves_current_source','no_remaining_orphans'])
        output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    assert store.proc.poll() is not None
    result['object_store_launcher_exited']=True
    output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print('VERSION_RESUME_OK '+str(output),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--review',type=Path,required=True)
    args=parser.parse_args();review=json.loads(args.review.read_text(encoding='utf-8'))
    assert review['entry']=='platform/server/storage_version_resume_checks.py' and review['classification']=='no_llm'
    for name,digest in review['sources'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
    verify()
