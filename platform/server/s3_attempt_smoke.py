"""Real S3 input seals/private outputs and independent-cache retry through HTTP."""
import json
from pathlib import Path
import time

from testsupport import IsolatedServer
from s3_typed_publication_smoke import RUNTIME
from s3testsupport import ObjectStore
from default_workspace_smoke import command, FIXTURE as RETRY_FIXTURE
from professional_launch_smoke import settled
from job_release_smoke import wait_file

FAULTS='''
from pathlib import Path
from app import storage
from app.services import attempt_objects as objects
fixture_root=Path(__file__).parent
def mode():
 p=fixture_root/'mode.txt'
 return p.read_text() if p.exists() else 'normal'
original_read=objects.read
def reading(prefix,path,**kwargs):
 if mode()=='source_change' and path=='Ref/input.txt' and kwargs.get('etag'):
  storage._s3_put(prefix,path,b'external edit during sealing')
 return original_read(prefix,path,**kwargs)
objects.read=reading
original_put=objects.put_new
def putting(prefix,path,data):
 if mode()=='output_upload_failure' and '/run/' in path and path.endswith('/b.txt'):
  raise TimeoutError('Injected second output upload failure')
 result=original_put(prefix,path,data)
 if mode()=='sealed_corruption' and '/inputs/' in path and path.endswith('/Ref/input.txt'):
  storage._s3_put(prefix,path,b'corrupted sealed object')
 return result
objects.put_new=putting
original_outputs=objects.persist_outputs
def outputs(prefix,manifest,run,records,directories):
 result=original_outputs(prefix,manifest,run,records,directories)
 if mode()=='output_corruption':storage._s3_put(prefix,manifest['run_path']+'/a.txt',b'changed')
 return result
objects.persist_outputs=outputs
from app.services import jobrunner,job_execution
from app.db import SessionLocal
original_start=jobrunner.start_job
async def starting(job_id,*args,**kwargs):
 if mode()=='cancel_before_claim':
  async with SessionLocal() as session:
   await job_execution.request_cancel(session,job_id)
   await session.commit()
 return await original_start(job_id,*args,**kwargs)
jobrunner.start_job=starting
'''


def api_for(store,*,peer=None):
    api=IsolatedServer('s3-attempt',app='s3_attempt_fixture:app')
    settings=store.settings()
    if peer:
        settings.update({key:peer.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_JWT_SECRET')})
        api.token=peer.token;api.case_id=peer.case_id;api.prefix=peer.prefix
    (api.root/'s3_attempt_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+RETRY_FIXTURE+FAULTS,encoding='utf8')
    return api


def main():
    checks=[]
    with ObjectStore(executable=RUNTIME) as store:
        with api_for(store) as api:
            api.create_case('real object attempts')
            api.put('Ref/input.txt','original S3 input')
            api.put('C-run/probe/0/U','initial field')
            body={'command':command("from pathlib import Path; import time; time.sleep(1); print(Path('Ref/input.txt').read_text()); Path('result.txt').write_text('private result'); Path('C-run/probe/0/U').write_text('private edit')")}
            job=api.call('POST',api.prefix+'/jobs',body,extra_headers={'Idempotency-Key':'s3-default'})
            api.put('Ref/input.txt','edited live input')
            done=settled(api,job['id'])
            assert done['status']=='succeeded',(done['status'],done['log_backlog'])
            manifest=done['attempt']['workspace'];output=done['attempt']['outputs']
            assert manifest['storage']['backend']=='s3' and output['object_store']['manifest_path']
            assert 'original S3 input' in done['log_backlog']
            assert api.call('GET',api.prefix+'/raw?path='+manifest['run_path']+'/result.txt',raw=True)==b'private result'
            assert api.call('GET',api.prefix+'/raw?path=C-run/probe/0/U',raw=True)==b'initial field'
            api.call('GET',api.prefix+'/raw?path=result.txt',expect=404)
            api.call('PUT',api.prefix+'/file',{'path':manifest['run_path']+'/result.txt','content':'overwrite'},expect=409)
            assert api.call('POST',api.prefix+'/jobs',body,extra_headers={'Idempotency-Key':'s3-default'})['id']==job['id']
            checks.append('real_S3_default_seal_private_outputs_initial_fields_and_idempotency')
            selected=api.call('POST',api.prefix+'/jobs',{'input_paths':['Ref/input.txt'],
                'command':command("from pathlib import Path; print(Path('Ref/input.txt').read_text()); Path('selected.txt').write_text('selected')")})
            selected=settled(api,selected['id']);assert selected['status']=='succeeded'
            assert list(selected['attempt']['workspace']['files'])==['Ref/input.txt']
            checks.append('real_S3_explicit_selection')
            failed=api.call('POST',api.prefix+'/jobs',{'command':command("from pathlib import Path; print(Path('Ref/input.txt').read_text()); Path('partial.txt').write_text('partial evidence'); raise SystemExit(7)")})
            failed=settled(api,failed['id']);assert failed['status']=='failed'
            private=failed['attempt']['workspace']['run_path']+'/partial.txt'
            assert api.call('GET',api.prefix+'/raw?path='+private,raw=True)==b'partial evidence'
            api.call('GET',api.prefix+'/raw?path=partial.txt',expect=404)
            api.put('Ref/input.txt','later edit before retry')
            with api_for(store,peer=api) as reader:
                original=reader.call('GET',f'/jobs/{job["id"]}')
                assert original['status']=='succeeded' and 'original S3 input' in original['log_backlog']
                assert reader.call('GET',api.prefix+'/raw?path='+manifest['run_path']+'/result.txt',raw=True)==b'private result'
                retry=reader.call('POST','/fixture/retry/'+failed['attempt']['id'])
                retried=settled(reader,retry['job_id'])
                assert retried['status']=='failed'
                assert 'edited live input' in retried['log_backlog'] and 'later edit before retry' not in retried['log_backlog']
                assert (reader.root/'files').resolve()!=(api.root/'files').resolve()
                checks.append('fresh_API_separate_cache_reads_remote_history_and_retries_original_inputs')
            checks.append('failed_S3_outputs_retained_privately_without_current_alias_writes')
            for mode in ('wsl','batch','cancel','timeout','output_upload_failure','output_corruption','source_change','sealed_corruption','cancel_before_claim'):
                (api.root/'mode.txt').write_text('normal')
                api.create_case('S3 '+mode)
                api.put('Ref/input.txt','original mode input')
                (api.root/'mode.txt').write_text(mode)
                code="from pathlib import Path; Path('a.txt').write_text('output A'); Path('b.txt').write_text('output B')"
                body={'command':command(code)}
                if mode in {'cancel','timeout'}:body['command']=command(code+'; import time; time.sleep(60)')
                if mode=='timeout':body['timeout_seconds']=3
                if mode=='wsl':body.update(runtime='wsl',command='cat Ref/input.txt; printf WSL_PRIVATE > a.txt')
                if mode=='batch':
                    batch=api.call('POST',api.prefix+'/jobs:batch',{'commands':[
                        command("from pathlib import Path; Path('same.txt').write_text('"+value+"')") for value in ('one','two')],
                        'max_concurrent':2},expect=202)
                    paths=[]
                    for item,value in zip(batch['jobs'],('one','two')):
                        done=settled(api,item['id']);assert done['status']=='succeeded'
                        path=done['attempt']['workspace']['run_path']+'/same.txt';paths.append(path)
                        assert api.call('GET',api.prefix+'/raw?path='+path,raw=True)==value.encode()
                    assert len(set(paths))==2
                    api.call('GET',api.prefix+'/raw?path=same.txt',expect=404)
                elif mode=='source_change':
                    api.call('POST',api.prefix+'/jobs',body,expect=409)
                    assert api.call('GET',api.prefix+'/jobs')==[]
                    assert api.call('GET',api.prefix+'/raw?path=Ref/input.txt',raw=True)==b'external edit during sealing'
                elif mode=='cancel_before_claim':
                    admitted=api.call('POST',api.prefix+'/jobs',body)
                    assert admitted['status']=='cancelled' and admitted['attempt']['phase']=='cancelled'
                    detail=api.call('GET',f'/jobs/{admitted["id"]}')
                    assert detail['attempt']['process_identity'] is None and not detail['log_backlog']
                elif mode=='sealed_corruption':
                    api.call('POST',api.prefix+'/jobs',body,expect=409)
                    rows=api.call('GET',api.prefix+'/jobs');assert len(rows)==1 and rows[0]['status']=='failed'
                    done=api.call('GET',f'/jobs/{rows[0]["id"]}')
                    assert done['attempt']['process_identity'] is None
                else:
                    job=api.call('POST',api.prefix+'/jobs',body)
                    if mode=='cancel':
                        running=api.call('GET',f'/jobs/{job["id"]}');work=running['attempt']['workspace']
                        wait_file(api.root/'files'/work['storage']['prefix']/work['run_path']/'a.txt',seconds=15)
                        api.call('POST',f'/jobs/{job["id"]}:cancel')
                    done=settled(api,job['id'])
                    expected='succeeded' if mode=='wsl' else 'cancelled' if mode=='cancel' else 'failed'
                    assert done['status']==expected,(mode,done['status'],done['log_backlog'])
                    work=done['attempt']['workspace']
                    assert api.call('GET',api.prefix+'/raw?path='+work['run_path']+'/a.txt',raw=True)==(b'WSL_PRIVATE' if mode=='wsl' else b'changed' if mode=='output_corruption' else b'output A')
                    if mode=='output_upload_failure':
                        marker=work['run_path'].rsplit('/',1)[0]+'/outputs.json'
                        api.call('GET',api.prefix+'/raw?path='+marker,expect=404)
                        assert done['attempt']['outputs']['object_store']['state']=='pending'
                        assert done['attempt']['outputs']['files']['b.txt']['size']==8
                    api.call('GET',api.prefix+'/raw?path=a.txt',expect=404)
                checks.append('real_S3_'+mode);print('PASS '+checks[-1],flush=True)
            (api.root/'mode.txt').write_text('normal')
            api.create_case('S3 legacy staging protects versions')
            existing=api.call('POST',api.prefix+'/jobs',{'command':'echo SEALED_HISTORY'})
            existing=settled(api,existing['id']);work=existing['attempt']['workspace']
            history=work['inputs_path']+'/0-caseDict/caseDict'
            before=api.call('GET',api.prefix+'/raw?path='+history,raw=True)
            legacy=api.call('POST',api.prefix+'/jobs',{'type':'convert','command':command(
                "from pathlib import Path; print('NO_VERSION_COPY',not Path('.vibeflow/attempts').exists()); p=Path('.vibeflow/attempts/invalid/inputs'); p.mkdir(parents=True); (p/'forged.txt').write_text('forged'); Path('legacy-output.txt').write_text('legacy')")})
            legacy=settled(api,legacy['id']);assert legacy['status']=='succeeded',legacy['log_backlog']
            assert 'NO_VERSION_COPY True' in legacy['log_backlog']
            assert api.call('GET',api.prefix+'/raw?path=legacy-output.txt',raw=True)==b'legacy'
            assert api.call('GET',api.prefix+'/raw?path='+history,raw=True)==before
            api.call('GET',api.prefix+'/raw?path=.vibeflow/attempts/invalid/inputs/forged.txt',expect=404)
            checks.append('legacy_S3_staging_cannot_copy_or_sync_immutable_attempt_objects')
            result={'passed':True,'count':len(checks),'checks':checks,'api_evidence':str(api.root),'object_store':str(store.root)}
        (store.root/'attempt-result.json').write_text(json.dumps(result,indent=2)+'\n')
        print('S3_ATTEMPT_OK '+json.dumps(result),flush=True)


if __name__=='__main__':main()
