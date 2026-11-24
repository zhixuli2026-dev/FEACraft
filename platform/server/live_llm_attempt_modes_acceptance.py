"""Actual sealed WSL jobs, failed-input retry, cold S3 cache and DeepSeek readback."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shlex
import time
import uuid

from live_llm_acceptance import source
from live_llm_sandbox_publication_acceptance import ROOT, verify

COLD = '''
from app import storage
from app.services import attempt_objects, attempt_workspace
original_ensure_inputs = attempt_objects.ensure_inputs
def cold_inputs(prefix, manifest, **kwargs):
 flag = root/'cold-mode-inputs'
 if flag.exists() and storage._use_s3():
  attempt_workspace.validate_identity(manifest)
  case_root=storage._fs_path(prefix,'.').resolve()
  target=(case_root/manifest['inputs_path']).parent.resolve()
  assert target.is_relative_to(case_root/'.vibeflow/attempts')
  if target.is_dir():
   backup=(root/('cold-mode-'+target.name)).resolve()
   assert target.is_relative_to(root.resolve()) and backup.is_relative_to(root.resolve())
   assert not backup.exists()
   target.rename(backup)
   flag.unlink()
   (root/'cold-mode-observed.json').write_text(json.dumps({'attempt_id':manifest['attempt_id'],'cache_moved':True}))
 return original_ensure_inputs(prefix,manifest,**kwargs)
attempt_objects.ensure_inputs=cold_inputs
'''


async def jobs(api):
    result={'passed':False,'mock_llm_calls':0,'jobs':[],'checks':[]}
    def save():(api.root/'attempt-modes.json').write_text(json.dumps(result,indent=2)+'\n','utf8')
    def record(name):result['checks'].append(name);save();print('ATTEMPT_MODE_PASS '+name,flush=True)
    async def call(method,path,**kw):
        deadline=time.monotonic()+45
        while True:
            try:return await asyncio.to_thread(api.call,method,path,**kw)
            except AssertionError as error:
                detail=str(error).replace(' ','')
                if not any('"code":"'+c+'"' in detail for c in ('storage_quota_busy','validation_busy')) or time.monotonic()>=deadline:raise
                await asyncio.sleep(.2)
    async def wait(identifier,expected):
        deadline=time.monotonic()+180
        while True:
            job=await call('GET','/jobs/'+str(identifier))
            if job['status'] not in {'queued','running'}:break
            assert time.monotonic()<deadline
            await asyncio.sleep(.3)
        assert job['status']==expected, {'job':identifier,'status':job['status'],'log':job.get('log_backlog','')[-1600:]}
        assert job['attempt']['process_stop']['verified']
        result['jobs'].append({'id':identifier,'status':job['status'],'attempt_id':job['attempt']['id'],'stop_verified':True})
        return job
    check='''from pathlib import Path
import stat
for p,m in {'Ref/empty':0o701,'Ref/file-to-dir':0o700,'Ref/file-to-dir/child':0o600,'Ref/dir-to-file':0o640}.items():
 assert stat.S_IMODE(Path(p).stat().st_mode)==m,(p,oct(Path(p).stat().st_mode))
assert Path('Ref/file-to-dir/child').read_text()=='CHILD'
assert Path('Ref/empty').is_dir() and not list(Path('Ref/empty').iterdir())
print('SEALED_MODES_'+'VERIFIED',flush=True)
'''
    try:
        (api.root/'cold-mode-inputs').write_text('evict actual isolated S3 local input cache before launch','utf8')
        first=await call('POST',api.prefix+'/jobs',body={'command':'python3 -c '+shlex.quote(check+'raise SystemExit(7)\n'),
            'runtime':'wsl-sandbox','input_paths':['Ref'],'timeout_seconds':120})
        failed=await wait(first['id'],'failed')
        assert failed['exit_code']==7 and 'SEALED_MODES_VERIFIED' in failed['log_backlog']
        manifest=failed['attempt']['workspace']
        assert '.vibeflow/tree-state.json' in manifest['files']
        assert 'Ref/empty' in manifest['directories']
        record('actual sealed sandbox job receives empty directory and four exact modes before intentional exit 7')
        await call('PUT',api.prefix+'/file',body={'path':'Ref/file-to-dir/child','content':'NEW_CURRENT'})
        option=await call('GET','/jobs/'+str(first['id'])+'/operations')
        assert option['retry']['available']
        body={'action':'retry','source_attempt_id':option['source_attempt_id'],'source_version':option['source_version']}
        retried=await call('POST','/jobs/'+str(first['id'])+':repeat',body=body,expect=202,
            extra_headers={'Idempotency-Key':uuid.uuid4().hex})
        repeated=await wait(retried['id'],'failed')
        assert repeated['exit_code']==7 and 'SEALED_MODES_VERIFIED' in repeated['log_backlog']
        assert repeated['attempt']['workspace']['files']==manifest['files']
        record('retry uses original sealed content and descriptor despite an actual newer live file edit')
        await call('PUT',api.prefix+'/file',body={'path':'Ref/file-to-dir/child','content':'CHILD'})
        command=check+"import secrets\nPath('Ref/proof.txt').write_text('RETIREMENT_'+secrets.token_hex(16)+'\\n')\n"
        published=await call('POST',api.prefix+'/jobs',body={'type':'convert','command':'python3 -c '+shlex.quote(command),
            'runtime':'wsl-sandbox','timeout_seconds':120})
        done=await wait(published['id'],'succeeded')
        assert 'SEALED_MODES_VERIFIED' in done['log_backlog']
        assert (await call('GET',api.prefix+'/raw?path=Ref/proof.txt',raw=True)).startswith(b'RETIREMENT_')
        record('typed real sandbox command publishes fresh random proof while preserving handler input contract')
        if 'storage' in manifest:
            assert json.loads((api.root/'cold-mode-observed.json').read_text())['cache_moved']
            record('actual local S3 attempt cache eviction is reconstructed from immutable remote inputs including descriptor')
        result['passed']=True;save()
    except BaseException as error:
        result['failure_type']=type(error).__name__;save();raise


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--review',type=Path,required=True)
    parser.add_argument('--s3',action='store_true')
    args=parser.parse_args()
    for name,digest in json.loads(args.review.read_text('utf8'))['sources'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,name
    configuration=await source(args.source_profile_id)
    options=dict(after_publish=jobs,fixture_extra=COLD)
    if args.s3:
        from s3testsupport import ObjectStore
        with ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed') as store:
            passed=await verify(configuration,store,**options)
        assert store.proc.poll() is not None
    else:passed=await verify(configuration,**options)
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
