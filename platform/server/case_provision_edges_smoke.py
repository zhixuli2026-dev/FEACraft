"""Owner locking, legacy orphan collision, reused-prefix job execution, and old S3 rejection."""
import json,tempfile,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import httpx
from testsupport import IsolatedServer,SERVER
from case_provision_smoke import FIXTURE,peer
from s3_case_provision_smoke import boot
from s3testsupport import ObjectStore


def run():
    checks=[]
    api=IsolatedServer('case-provision-edges',app='edge_provision:app')
    (api.root/'edge_provision.py').write_text(FIXTURE,encoding='utf8')
    with api:
        case=api.create_case('Initial edges');ws=api.call('GET','/workspaces')[0]['id'];user=api.call('GET','/auth/me')['id']
        api.call('POST','/case-provisions:recover')
        root=Path(api.env['VIBEFLOW_STORAGE_FS_ROOT'])
        orphan=root/f'workspaces/{user}/cases/{case["id"]+1}/foreign.txt'
        orphan.parent.mkdir(parents=True);orphan.write_bytes(b'UNOWNED ORPHAN')
        created=api.call('POST',f'/workspaces/{ws}/cases',{'name':'After legacy orphan'})
        plan=json.loads(next(root.glob('workspaces/*/.case-provisions/pending/*.json')).read_text())
        assert (root/plan['binding']['prefix'])!=orphan.parent and orphan.read_bytes()==b'UNOWNED ORPHAN'
        assert not any(item['path']=='foreign.txt' for item in api.call('GET',f'/cases/{created["id"]}/tree')['entries'])
        checks.append('legacy_orphan_preserved_and_excluded_from_new_case')
        job=api.call('POST',f'/cases/{created["id"]}/jobs',{'type':'shell','command':'cmd /c echo PROVISION_RECOVERED','runtime':'windows'})
        result=api.wait_job(job['id'])
        assert result['status']=='succeeded' and 'PROVISION_RECOVERED' in api.call('GET',f'/jobs/{job["id"]}/log')['chunk']
        checks.append('generation_prefix_executes_real_isolated_windows_job')
        api.call('POST','/case-provisions:recover')
        (api.root/'mode').write_text('before_commit')
        with peer(api) as second:
            with ThreadPoolExecutor(max_workers=2) as pool:
                creation=pool.submit(api.call,'POST',f'/workspaces/{ws}/cases',{'name':'Held creation'})
                deadline=time.monotonic()+20
                while not (api.root/'ready').exists():
                    assert time.monotonic()<deadline,'Creation did not reach owner-lock boundary';time.sleep(.03)
                recovery=pool.submit(second.call,'POST','/case-provisions:recover')
                try:
                    time.sleep(.4)
                    assert not recovery.done(),'Recovery bypassed active creator owner lock'
                finally:(api.root/'release').write_text('release')
                created=creation.result(timeout=20);outcome=recovery.result(timeout=20)
                assert any(row.get('case_id')==created['id'] and row['status']=='committed' for row in outcome['results']),outcome
                assert second.call('GET',f'/cases/{created["id"]}/tree')['entries']
        checks.append('second_api_recovery_waits_for_creator_then_uses_committed_proof')
    with ObjectStore() as legacy:
        with boot(legacy.settings(),'case-provision-legacy') as api:
            auth=api.call('POST','/auth/register',{'email':'legacy-provision@example.com','name':'legacy','password':'isolated-test-123'})
            api.token=auth['token'];ws=api.call('GET','/workspaces')[0]['id']
            failure=api.call('POST',f'/workspaces/{ws}/cases',{'name':'Unsupported conditions'},expect=409)
            assert failure['detail']['code']=='storage_conditions_unavailable',failure
            assert api.call('GET',f'/workspaces/{ws}/cases')==[]
            keys=[row['Key'] for page in legacy.client.get_paginator('list_objects_v2').paginate(Bucket=legacy.bucket) for row in page.get('Contents',[])]
            assert not any('/cases/' in key or '/pending/' in key for key in keys),keys
        checks.append('legacy_s3_rejected_before_case_data_or_pending_plan')
    output=Path(tempfile.mkdtemp(prefix='case-provision-edges-evidence-',dir=SERVER.parents[1]/'tmp'))
    result={'passed':True,'count':len(checks),'checks':checks}
    (output/'checks.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'count':len(checks),'evidence':str(output)}),flush=True)


if __name__=='__main__':run()
