"""Actual file API edits/crash recovery keep Linux modes; real DeepSeek readback."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid

from live_llm_acceptance import source
from live_llm_sandbox_publication_acceptance import ROOT, verify

FAULT = '''
from app.services import local_file_edits, s3_file_transactions
local_apply = local_file_edits.PreparedEdit.install_file
remote_apply = s3_file_transactions._apply
def exit_after_file(flag):
 if flag.exists():
  flag.unlink(); os._exit(88)
def local_mode_fault(edit, case, target, index, entry):
 result = local_apply(edit, case, target, index, entry)
 if entry['path']=='Ref/file-to-dir/child':
  exit_after_file(root/'exit-mode-edit')
 return result
def remote_mode_fault(case, directory, index, entry, side, expected):
 result = remote_apply(case, directory, index, entry, side, expected)
 if entry['path']=='Ref/file-to-dir/child' and side=='after':
  exit_after_file(root/'exit-mode-edit')
 return result
local_file_edits.PreparedEdit.install_file = local_mode_fault
s3_file_transactions._apply = remote_mode_fault
'''


async def edits(api):
    result={'passed':False,'mock_llm_calls':0,'checks':[]}
    def save():
        (api.root/'mode-edits.json').write_text(json.dumps(result,indent=2)+'\n','utf8')
    def record(name):
        result['checks'].append(name);save();print('MODE_PASS '+name,flush=True)
    async def call(method,path,**kw):
        deadline=time.monotonic()+30
        while True:
            try:return await asyncio.to_thread(api.call,method,path,**kw)
            except AssertionError as error:
                if '"code":"storage_quota_busy"' not in str(error).replace(' ','') or time.monotonic()>=deadline:raise
                await asyncio.sleep(.2)
    try:
        # Exit after the ordinary bytes changed but before the descriptor changes.
        (api.root/'exit-mode-edit').write_text('actual first applied file; no model substitution','utf8')
        try:
            await call('PUT',api.prefix+'/file',body={'path':'Ref/file-to-dir/child','content':'UNCOMMITTED'})
        except OSError:pass
        else:raise AssertionError('Actual process exit was required')
        process=api.proc
        await asyncio.to_thread(process.wait,10)
        assert process.returncode==88
        api.proc=subprocess.Popen(process.args,cwd=api.root,env=api.env,stdout=api.log,
            stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        deadline=time.monotonic()+40
        while True:
            assert api.proc.poll() is None
            try:
                if (await call('GET','/api/health',api_prefix=False)).get('ok'):break
            except OSError:pass
            assert time.monotonic()<deadline
            await asyncio.sleep(.2)
        assert await call('GET',api.prefix+'/raw?path=Ref/file-to-dir/child',raw=True)==b'CHILD'
        record('API exits after actual content write; restart rolls back content and its pending mode descriptor')
        for path,content in [('Ref/file-to-dir/child','EDITED_CHILD'),('Ref/dir-to-file','EDITED_FILE')]:
            await call('PUT',api.prefix+'/file',body={'path':path,'content':content})
            assert await call('GET',api.prefix+'/raw?path='+path,raw=True)==content.encode()
        record('ordinary file API changes both tracked contents through their complete journals')
        await call('DELETE',api.prefix+'/file?path=Ref/proof.txt')
        proof='RETIREMENT_'+uuid.uuid4().hex
        await call('PUT',api.prefix+'/file',body={'path':'Ref/proof.txt','content':proof+'\n'})
        record('delete and recreate removes the old fingerprint and mode association')
        current=await call('GET',api.prefix+'/caseDict')
        await call('PUT',api.prefix+'/caseDict',body={'content':current['content']+'\n// ordinary mode edit\n',
                                                    'expected_revision':current['dict_revision']})
        assert (await call('GET',api.prefix+'/caseDict'))['dict_revision']==current['dict_revision']+1
        record('ordinary caseDict revision and both histories also publish with descriptor updates')
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
    check="""
assert Path('Ref/file-to-dir/child').read_text()=='EDITED_CHILD'
assert Path('Ref/dir-to-file').read_text()=='EDITED_FILE'
assert stat.S_IMODE(Path('Ref/proof.txt').stat().st_mode)==0o777
assert 'ordinary mode edit' in Path('0-caseDict/caseDict').read_text()
"""
    options=dict(after_publish=edits,fixture_extra=FAULT,next_sandbox_check=check)
    if args.s3:
        from s3testsupport import ObjectStore
        with ObjectStore(executable=ROOT/'third_party/seaweed-4.52-runtime/weed') as store:
            passed=await verify(configuration,store,**options)
        assert store.proc.poll() is not None
    else:passed=await verify(configuration,**options)
    return 0 if passed else 1


if __name__=='__main__':raise SystemExit(asyncio.run(main()))
