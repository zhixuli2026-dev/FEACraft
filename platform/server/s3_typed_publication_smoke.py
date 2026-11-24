"""S3 typed commands: execution, conditional publication and frozen references."""
import argparse
import json
from pathlib import Path
import time

from testsupport import IsolatedServer, SERVER
from s3testsupport import ObjectStore,LEGACY_CASE_FIXTURE,create_legacy_case
from case_command_publication_smoke import FIXTURE
from default_workspace_smoke import command
from professional_launch_smoke import settled
from job_release_smoke import wait_file

RUNTIME=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'


def api_for(store,peer=None,legacy=False):
    api=IsolatedServer('s3-typed-publication',app='s3_typed_fixture:app')
    settings=store.settings()
    if peer:
        settings.update({key:peer.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_JWT_SECRET')})
        api.token=peer.token;api.case_id=peer.case_id;api.prefix=peer.prefix
    (api.root/'s3_typed_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE+(LEGACY_CASE_FIXTURE if legacy else ''),encoding='utf8')
    (api.root/'mode').write_text('normal')
    return api


def extended(api,store):
    from cae_fixtures import cavity_2d
    checks=[];(api.root/'mode').write_text('normal')
    for mode in ('different_outputs','same_output'):
        api.create_case('S3 parallel '+mode);api.put('Ref/input.txt','original')
        marker=api.root/('command-release-'+mode)
        jobs=[]
        for index in range(2):
            path='result.txt' if mode=='same_output' else f'result-{index}.txt'
            code="from pathlib import Path; import time\nlimit=time.monotonic()+45\nwhile not Path("+repr(str(marker))+").exists():\n if time.monotonic()>limit: raise TimeoutError('command barrier')\n time.sleep(.03)\nPath("+repr(path)+").write_text("+repr(str(index))+")"
            payload="exec(bytes.fromhex('"+code.encode('utf8').hex()+"'))"
            jobs.append(api.call('POST',api.prefix+'/jobs',{'type':'convert','command':command(payload)}))
        marker.touch()
        done=[settled(api,job['id']) for job in jobs]
        successful=[i for i,j in enumerate(done) if j['status']=='succeeded']
        assert successful and all(j['status'] in {'failed','succeeded'} for j in done),(mode,[j['status'] for j in done])
        for index,job in enumerate(done):
            path='result.txt' if mode=='same_output' else f'result-{index}.txt'
            private=job['attempt']['workspace']['run_path']+'/'+path
            assert api.call('GET',api.prefix+'/raw?path='+private,raw=True)==str(index).encode()
            publication=job['attempt']['outputs']['publication']
            assert (publication['status']=='committed')==(index in successful)
            if mode=='different_outputs' and index in successful:
                assert api.call('GET',api.prefix+'/raw?path='+path,raw=True)==str(index).encode()
        if mode=='same_output':
            current=api.call('GET',api.prefix+'/raw?path=result.txt',raw=True)
            assert current in [str(index).encode() for index in successful]
            for index in successful:
                if str(index).encode()!=current:
                    assert done[index]['attempt']['outputs']['completion']['alias_error'] is not None
        assert all(j['attempt']['workspace']['storage']['backend']=='s3' for j in done)
        # Two distinct publication channels may both validate before aliases
        # are applied. Immutable branches remain valid; a contested live alias
        # has one winner and the other branch records its repair conflict.
        (api.root/(mode+'-result.json')).write_text(json.dumps(done,indent=2),encoding='utf8')
        checks.append(mode+'_preserves_private_branches_and_conditional_alias_conflicts')
    api.create_case('S3 corrupted captured outputs');api.put('Ref/input.txt','source')
    for name in ('ready','release'):(api.root/name).unlink(missing_ok=True)
    (api.root/'mode').write_text('source')
    job=api.call('POST',api.prefix+'/jobs',{'type':'convert','command':command("from pathlib import Path; Path('result.txt').write_text('valid')")})
    wait_file(api.root/'ready',seconds=60)
    detail=api.call('GET',f'/jobs/{job["id"]}');sealed=detail['attempt']['workspace']
    store.client.put_object(Bucket=store.bucket,Key=sealed['storage']['prefix']+'/'+sealed['run_path']+'/result.txt',Body=b'corrupt captured output')
    (api.root/'release').touch();done=settled(api,job['id'])
    assert done['status']=='failed' and done['attempt']['outputs']['publication']['status']!='committed',done
    api.call('GET',api.prefix+'/raw?path=result.txt',expect=404)
    checks.append('remote_captured_output_corruption_blocks_typed_commit')
    (api.root/'mode').write_text('normal')
    api.create_case('S3 real OpenFOAM')
    seed=cavity_2d()
    for path,text in seed.items():api.put(path,text)
    run='C-run/case-1';command_prefix='source /usr/lib/openfoam/openfoam2406/etc/bashrc && cd '+run+' && '
    mesh=api.call('POST',api.prefix+'/jobs',{'type':'mesh','runtime':'wsl','command':command_prefix+'blockMesh'})
    mesh=settled(api,mesh['id']);assert mesh['status']=='succeeded',mesh
    initial=api.call('GET',api.prefix+'/raw?path='+run+'/0/U',raw=True)
    solve=api.call('POST',api.prefix+'/jobs',{'type':'solve','runtime':'wsl','run_dir':run,'command':command_prefix+'icoFoam'})
    solve=settled(api,solve['id']);assert solve['status']=='succeeded',(solve['log_backlog'],solve['attempt']['outputs'])
    publication=solve['attempt']['outputs']['publication'];assert publication['status']=='committed'
    field=api.call('GET',api.prefix+'/raw?path='+run+'/0.5/U',raw=True);assert len(field)>10000
    assert api.call('GET',api.prefix+'/raw?path='+run+'/0/U',raw=True)==initial
    evidence=json.loads(api.call('GET',api.prefix+'/raw?path=.vibeflow/jobs/job-'+str(solve['id'])+'-evidence.json',raw=True))
    assert evidence['summary']['finished_cleanly']
    with api_for(store,peer=api) as reader:
        ref={'command':'echo REAL_SOLVE_REFERENCE','input_publications':[{'publication_id':publication['id'],'paths':[run+'/0.5/U'],'require_current':True}]}
        got=reader.call('POST',api.prefix+'/jobs',ref);assert settled(reader,got['id'])['status']=='succeeded'
    checks.append('real_S3_blockMesh_icoFoam_fields_evidence_and_fresh_cache_reference')
    for mode,cmd in [('not_solver','echo NOT_A_SOLVER'),('malformed_field',command_prefix+'icoFoam && printf broken > 0.5/U')]:
        bad=api.call('POST',api.prefix+'/jobs',{'type':'solve','runtime':'wsl','run_dir':run,'command':cmd})
        bad=settled(api,bad['id']);assert bad['status']=='failed',(mode,bad)
        assert api.call('GET',api.prefix+'/raw?path='+run+'/0.5/U',raw=True)==field
        checks.append(mode+'_does_not_replace_valid_S3_solve')
    return checks


def main(extended_only=False):
    checks=[]
    with ObjectStore(executable=RUNTIME) as store:
        with api_for(store) as api:
            modes=() if extended_only else ('mesh','solve','geometry','pvpython','convert','wsl','noop','delete','failure','preserved',
                         'source','destination','delete_conflict','cancel','timeout')
            for mode in modes:
                api.create_case('S3 typed '+mode)
                api.put('Ref/input.txt','original input')
                api.put('C-run/probe/0/U','initial field')
                api.put('D-post/result.txt','previous result')
                api.put('Retired/only.txt','obsolete')
                for name in ('ready','release'):(api.root/name).unlink(missing_ok=True)
                (api.root/'mode').write_text(mode)
                code="from pathlib import Path; Path('D-post/result.txt').write_text(Path('Ref/input.txt').read_text()+' result')"
                if mode in {'delete','delete_conflict'}:code+="; Path('Retired/only.txt').unlink()"
                if mode=='failure':code+='; raise SystemExit(7)'
                if mode=='preserved':code+="; Path('C-run/probe/0/U').write_text('invalid')"
                if mode=='timeout':code+='; import time; time.sleep(60)'
                if mode=='noop':code="print('NO_OUTPUT')"
                body={'type':mode if mode in {'mesh','solve','geometry','pvpython','convert'} else 'convert',
                    'command':command(code)}
                if mode=='wsl':body.update(runtime='wsl',command='printf "original input result" > D-post/result.txt')
                if mode=='timeout':body['timeout_seconds']=3
                headers={'Idempotency-Key':'typed-'+mode}
                job=api.call('POST',api.prefix+'/jobs',body,extra_headers=headers)
                identifier=job['id']
                detail=api.call('GET',f'/jobs/{identifier}')
                prefix=detail['attempt']['workspace']['storage']['prefix']
                if mode in {'source','destination','delete_conflict','cancel'}:
                    wait_file(api.root/'ready',seconds=60)
                    assert api.call('GET',api.prefix+'/raw?path=D-post/result.txt',raw=True)==b'previous result'
                    edited={'source':'Ref/input.txt','destination':'D-post/result.txt','delete_conflict':'Retired/only.txt'}
                    if mode in edited:
                        store.client.put_object(Bucket=store.bucket,Key=prefix+'/'+edited[mode],Body=b'external edit')
                    if mode=='cancel':api.call('POST',f'/jobs/{identifier}:cancel')
                    (api.root/'release').touch()
                done=settled(api,identifier)
                success=mode in {'mesh','solve','geometry','pvpython','convert','wsl','noop','delete'}
                assert done['status']==('succeeded' if success else 'cancelled' if mode=='cancel' else 'failed'),(mode,done['status'],done['log_backlog'][-2000:],done['attempt']['outputs'])
                publication=done['attempt']['outputs']['publication']
                assert api.call('GET',api.prefix+'/raw?path=C-run/probe/0/U',raw=True)==b'initial field'
                if success:
                    assert publication['status']=='committed',publication
                    path='.vibeflow/publications/'+publication['id']+'/manifest.json'
                    manifest=json.loads(api.call('GET',api.prefix+'/raw?path='+path,raw=True))
                    assert manifest['storage']['backend']=='s3' and manifest['descriptor']['handler']=='case-command.v1'
                    assert manifest['descriptor']['result']['professional_workflow_proof'] is False
                    expected=b'previous result' if mode=='noop' else b'original input result'
                    deadline=time.monotonic()+10
                    while api.call('GET',api.prefix+'/raw?path=D-post/result.txt',raw=True)!=expected:
                        assert time.monotonic()<deadline,(mode,'alias not applied',done)
                        time.sleep(.1)
                    if mode=='delete':api.call('GET',api.prefix+'/raw?path=Retired/only.txt',expect=404)
                    if mode in {'convert','delete'}:
                        with api_for(store,peer=api) as reader:
                            reader_job=reader.call('POST',api.prefix+'/jobs',{'command':command("from pathlib import Path; print(Path('D-post/result.txt').read_text())"),
                                'input_publications':[{'publication_id':publication['id'],'require_current':True}]})
                            read_done=settled(reader,reader_job['id']);assert read_done['status']=='succeeded',read_done
                            assert 'original input result' in read_done['log_backlog']
                        api.put('Ref/input.txt','edited source')
                        api.call('POST',api.prefix+'/jobs',{'command':'echo STALE_REFERENCE',
                            'input_publications':[{'publication_id':publication['id'],'require_current':True}]},expect=409)
                        historic=api.call('POST',api.prefix+'/jobs',{'command':'echo HISTORICAL_REFERENCE',
                            'input_publications':[{'publication_id':publication['id'],'require_current':False}]})
                        assert settled(api,historic['id'])['status']=='succeeded'
                        checks.append(mode+'_fresh_cache_current_stale_and_historical_references')
                else:
                    assert publication['status']!='committed'
                    expected=b'external edit' if mode=='destination' else b'previous result'
                    assert api.call('GET',api.prefix+'/raw?path=D-post/result.txt',raw=True)==expected
                    expected=b'external edit' if mode=='delete_conflict' else b'obsolete'
                    assert api.call('GET',api.prefix+'/raw?path=Retired/only.txt',raw=True)==expected
                assert api.call('POST',api.prefix+'/jobs',body,extra_headers=headers)['id']==identifier
                checks.append(mode+'_real_S3_private_execution_and_guarded_publication')
                print('PASS '+checks[-1],flush=True)
            checks.extend(extended(api,store))
        result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(store.root),'api':str(api.root)}
    (store.root/'typed-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    with ObjectStore() as old:
        with api_for(old,legacy=True) as legacy:
            create_legacy_case(legacy,'Existing case on unsupported condition backend')
            legacy.call('PUT',legacy.prefix+'/file',{'path':'Ref/input.txt','content':'not admitted'},expect=422)
            before=legacy.call('GET',legacy.prefix+'/jobs')
            error=legacy.call('POST',legacy.prefix+'/jobs',{'type':'convert','command':'echo NOT_ADMITTED'},expect=400)
            assert 'conditional' in str(error)
            assert legacy.call('GET',legacy.prefix+'/jobs')==before
            ordinary=legacy.call('POST',legacy.prefix+'/jobs',{'command':'echo ORDINARY_STILL_WORKS'})
            assert settled(legacy,ordinary['id'])['status']=='succeeded'
            checks.append('old_backend_rejects_typed_admission_without_job_and_keeps_ordinary_execution')
    result.update(count=len(checks),checks=checks,legacy_evidence=str(old.root))
    (store.root/'typed-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('S3_TYPED_PUBLICATION_OK '+json.dumps(result),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--extended-only',action='store_true');args=parser.parse_args()
    main(args.extended_only)
