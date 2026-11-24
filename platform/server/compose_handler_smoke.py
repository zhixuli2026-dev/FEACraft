"""Real upstream CAE evidence plus draft races, exact backups and restart readers."""
from report_handler_smoke import (argparse,asyncio,ThreadPoolExecutor,hashlib,json,os,Path,re,
    subprocess,sys,tempfile,time,uuid,make_url,set_block,thermal_files,script,wait_file,IsolatedServer,SERVER,alembic)
from postprocess_handler_smoke import CONTROLLED as BASE,WORKER as BASE_WORKER

CONTROLLED=BASE.replace("'registered_report_worker.py'}","'registered_report_worker.py','registered_compose_worker.py'}")
CONTROLLED+='''
from app.services import artifact_publication
original_aliases=artifact_publication._write_aliases
def controlled_aliases(prefix,row):
    marker=root/('mirror-control-'+str(row.case_id))
    if row.kind.startswith('report-source') and marker.exists():
        from app import storage
        from app.services.compose_artifacts import directory
        _,manifest=artifact_publication.read_manifest(prefix,row)
        descriptor=manifest['descriptor']
        if marker.read_text()=='target':
            storage._fs_put(prefix,descriptor['output'],b'manual edit after commit')
        else:
            storage._fs_put(prefix,directory(descriptor['execution_id'])+'/previous.md',b'manual backup edit')
    return original_aliases(prefix,row)
artifact_publication._write_aliases=controlled_aliases
'''
WORKER=BASE_WORKER+'''
elif control in {'badcompose','badbackup','badcomposeproof'}:
    folder=next(Path('E-report/compositions').iterdir())
    if control=='badcompose':
        (folder/'report.md').write_bytes(b'forged generated draft')
        document=json.loads((folder/'result.json').read_text(encoding='utf-8'))
        document['content']='forged generated draft'
        (folder/'result.json').write_text(json.dumps(document),encoding='utf-8')
    elif control=='badbackup': (folder/'previous.md').write_bytes(b'forged backup')
    proof=json.loads((folder/'toolchain.json').read_text(encoding='utf-8'))
    for path,value in proof['artifacts'].items():
        data=Path(path).read_bytes()
        value.update(size=len(data),sha256=hashlib.sha256(data).hexdigest())
    if control=='badcomposeproof': proof['execution_id']='00000000-0000-0000-0000-000000000000'
    (folder/'toolchain.json').write_text(json.dumps(proof),encoding='utf-8')
'''


def verify(database_url=None):
    checks=[]
    def done(name):
        checks.append(name)
        print('PASS '+name,flush=True)
    server=IsolatedServer('compose-handler',app='controlled_compose:app',database_url=database_url)
    (server.root/'controlled_compose.py').write_text(CONTROLLED,encoding='utf-8')
    (server.root/'controlled_worker.py').write_text(WORKER,encoding='utf-8')
    with server as api:
        run_dir='C-run/conduction'
        def new_case(name):
            api.create_case(name)
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            saved=api.call('GET',api.prefix+'/caseDict')
            text=set_block(saved['content'],'mesh',{'source':'blockMesh',
                'qualityTargets':{'maxNonOrtho':70,'maxSkewness':4,'maxAspectRatio':100,'policy':'fail'}})
            text=set_block(text,'numericalMethod',{'solver':'laplacianFoam'})
            post={'runDir':run_dir,'fields':{'T':{'unit':'K'}},'dataExtraction':{
                'volumeAverages':{'mean':{'field':'T','time':1}},
                'pointProbes':{'center':{'fields':['T'],'points':[.5,.5,.5],'times':[1]}},
                'lineSamples':{'profile':{'fields':['T'],'start':[.05,.5,.5],'end':[.95,.5,.5],'nPoints':19,'times':[1]}}},
                'derivedQuantities':{'referenceValues':{'analytic_mean':{'metric':'"mean.T@1"','value':350,
                    'unit':'K','kind':'verification','source':'"analytic T(x)=300+100*x"','l2_max':1e-6}},
                    'referenceFields':{'analytic_field':{'field':'T','path':'references/T','time':1,
                        'kind':'verification','source':'"analytic cell-center temperature"','l2_max':1e-6}}},
                'artifacts':{'plots':{'temperature':{'source':'profile','field':'T','time':1,
                    'formats':['pdf','svg','png'],'title':'"Temperature profile"'}}}}
            text=set_block(text,'postProcessing',post)
            api.call('PUT',api.prefix+'/caseDict',{'content':text,'expected_revision':saved['dict_revision']})
            for relative,content in thermal_files().items():
                target=root/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_text(content.replace('(20 2 2)','(20 4 4)'),encoding='utf-8')
            (root/'A-geo/geometry.py').write_text(script(2),encoding='utf-8')
            (root/'references').mkdir()
            values=[300+100*((i%20)+.5)/20 for i in range(320)]
            (root/'references/T').write_text('internalField nonuniform List<scalar>\n320\n(\n'+
                '\n'.join(map(str,values))+'\n);\n',encoding='utf-8')
            return root
        def submit(target='report'):
            revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
            return api.call('POST',api.prefix+'/workflow-runs',{'target':target,'run_dir':run_dir,
                'expected_revision':revision,'budget':{'timeout_seconds':120,'max_log_bytes':1048576}})
        def detail(run):
            return api.call('GET',f'/workflow-runs/{run["id"]}')
        def wait(run,confirmation=None,status=None):
            deadline=time.monotonic()+160
            while time.monotonic()<deadline:
                result=detail(run)
                steps={step['key']:step for step in result['steps']}
                if status and result['status']==status:
                    return result
                if confirmation and steps[confirmation]['status']=='awaiting_confirmation':
                    for producer in result['steps']:
                        if producer['status']=='succeeded' and producer['attempts']:
                            api.wait_job(producer['attempts'][-1]['job_id'])
                    return detail(run)
                if result['status'] in {'failed','interrupted','cancelled'}:
                    raise AssertionError(result)
                time.sleep(.15)
            raise TimeoutError(result)
        def confirm(run,key,expect=200):
            step=next(step for step in run['steps'] if step['key']==key)
            return api.call('POST',api.prefix+'/caseDict:advance',{'state':step['confirmation_state'],
                'confirmed':True,'expected_revision':step['confirmation_revision'],
                'workflow_step_id':step['id'],'expected_step_version':step['version']},expect=expect)
        def through_solver(flow):
            for key in ('confirm_B','confirm_C'):
                flow=wait(flow,key)
                confirm(flow,key)
            return wait(flow,'confirm_D')
        root=new_case('registered composition')
        flow=through_solver(submit('postprocess'))
        confirm(flow,'confirm_D')
        ready=wait(flow,'confirm_E')
        confirm(ready,'confirm_E')
        wait(flow,status='succeeded')
        def request(body=None,key=None,*,client=api,wait=True,expect=200):
            return client.call('POST',api.prefix+'/report:compose'+('' if wait else '?wait=false'),body or {},
                extra_headers={'Idempotency-Key':key} if key else None,expect=expect)
        def result(pub=None,output=None,*,client=api,expect=200):
            params=[]
            if pub: params.append('publication_id='+pub)
            if output: params.append('output='+output)
            return client.call('GET',api.prefix+'/report/compose-result'+('?'+'&'.join(params) if params else ''),expect=expect)
        target=root/'E-report/report.md'
        original=b'# Existing draft\r\nExact bytes: \xff\xfe\r\n'
        target.write_bytes(original)
        before=api.call('GET',api.prefix+'/jobs')
        request(expect=409)
        revision=api.call('GET',api.prefix+'/caseDict')['dict_revision']
        for body in ({'overwrite':True,'expected_revision':revision+1},
                     {'overwrite':True,'expected_sha256':None},{'overwrite':True,'expected_sha256':'0'*64}):
            request(body,expect=409)
        assert api.call('GET',api.prefix+'/jobs')==before and target.read_bytes()==original
        done('overwrite_revision_and_hash_preconditions_reject_before_job_creation')
        body={'overwrite':True,'expected_revision':revision,'expected_sha256':hashlib.sha256(original).hexdigest()}
        first=request(body,'initial-compose')
        assert first['publication']['applied'] and len(first['chapters'])==9 and first['evidence']['report_ready']
        assert target.read_text(encoding='utf-8')==first['content'] and (root/first['backup']).read_bytes()==original
        job=api.wait_job(first['job_id'])
        frozen=root/'.vibeflow/attempts'/job['attempt']['id']/'inputs'
        assert (frozen/'E-report/report.md').read_bytes()==original
        assert len(first['toolchain']['input_publications'])==2
        done('synchronous_compatibility_and_exact_non_utf8_backup_from_sealed_inputs')
        assert request(body,'initial-compose')['job_id']==first['job_id']
        request({'overwrite':True},'initial-compose',expect=409)
        done('idempotency_replay_precedes_now_obsolete_file_preconditions')
        secondary=request({'output':'E-report/report.generated.md','expected_sha256':None})
        assert secondary['backup'] is None and result()['publication']['id']==first['publication_id']
        assert result(output='E-report/report.generated.md')['publication']['id']==secondary['publication_id']
        done('generated_and_primary_drafts_have_independent_current_heads')
        for item in (first,secondary):
            for path in (item['source_path'],item['backup']):
                if path:
                    assert api.call('GET',api.prefix+'/report/compose-artifact?publication_id='+item['publication_id']+'&path='+path,raw=True)==(root/path).read_bytes()
        post_id=next(step for step in ready['steps'] if step['key']=='postprocess')['publication']['id']
        result(post_id,expect=404)
        api.call('GET',api.prefix+'/report/compose-artifact?path=../0-caseDict/caseDict',expect=404)
        other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'other-compose-123','name':'Other'})['token']
        for suffix in ('/report/compose-result','/report/compositions','/report/compose-artifact?path='+first['source_path']):
            api.call('GET',api.prefix+suffix,expect=404,extra_headers={'Authorization':'Bearer '+other})
        done('history_downloads_check_owned_publication_kind_and_selected_paths')
        immutable=root/'.vibeflow/publications'/first['publication_id']/'files'/first['source_path']
        old=immutable.read_bytes()
        try:
            immutable.write_bytes(old+b'damaged')
            result(first['publication_id'],expect=409)
        finally: immutable.write_bytes(old)
        done('corrupt_immutable_draft_is_not_returned_as_valid_history')
        build=api.call('POST',api.prefix+'/report:build',{})
        built=api.wait_job(build['job_id'])
        assert first['publication_id'] in [item['publication_id'] for item in built['attempt']['workspace']['publications']]
        done('manual_HTML_DOCX_build_records_generated_draft_publication_lineage')
        api.put('E-report/report.md','# Manually revised draft\n')
        replay=request(body,'initial-compose')
        assert not replay['publication']['applied'] and target.read_text()=='# Manually revised draft\n'
        assert replay['content']==first['content']
        result(expect=404)
        manual=api.wait_job(api.call('POST',api.prefix+'/report:build',{})['job_id'])
        assert len(manual['attempt']['workspace']['publications'])==2
        done('manual_draft_edit_invalidates_head_and_replay_preserves_the_edit')

        peer=IsolatedServer('compose-peer')
        peer.token=api.token
        peer.prefix=api.prefix
        def start_peer():
            peer.log=(peer.root/'api.log').open('a',encoding='utf-8')
            peer.proc=subprocess.Popen([sys.executable,'-m','uvicorn','controlled_compose:app','--app-dir',str(SERVER),
                '--host','127.0.0.1','--port',str(peer.port),'--no-access-log'],cwd=api.root,env=api.env,
                stdout=peer.log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                if peer.proc.poll() is not None: raise AssertionError((peer.root/'api.log').read_text(encoding='utf-8'))
                try:
                    if peer.call('GET','/api/health',api_prefix=False).get('ok'): return
                except (OSError,ValueError): pass
                time.sleep(.1)
            raise TimeoutError('Peer API did not start')
        def stop_peer():
            if peer.proc:
                peer.proc.terminate()
                try: peer.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    peer.proc.kill()
                    peer.proc.wait(timeout=5)
            peer.log.close()
        start_peer()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending=[pool.submit(request,{'overwrite':True},'two-process-compose',client=client) for client in (api,peer)]
                responses=[future.result(timeout=30) for future in pending]
            assert responses[0]['job_id']==responses[1]['job_id'] and all(row['publication']['applied'] for row in responses)
            current=responses[0]
            stop_peer()
            start_peer()
            assert result(client=peer)['publication']['id']==current['publication_id']
            done('two_API_submissions_share_one_job_and_restart_reads_the_same_durable_head')
            marker=api.root/f'report-control-{api.case_id}'
            for failure in ('noop','badcompose','badbackup','badcomposeproof'):
                previous=target.read_bytes()
                marker.write_text(failure)
                try:
                    submitted=request({'overwrite':True},wait=False)
                    api.wait_job(submitted['job_id'],expected='failed')
                finally: marker.unlink(missing_ok=True)
                assert target.read_bytes()==previous and result()['publication']['id']==current['publication_id']
                done(failure+'_cannot_publish_forged_draft_or_backup')
            for scenario in ('raw_target','api_target','field','post_alias','superseded','cancel'):
                if scenario=='api_target': current=request({'overwrite':True})
                previous=target.read_bytes()
                barrier=api.root/f'hold-report-{api.case_id}'
                barrier.with_suffix('.ready').unlink(missing_ok=True)
                barrier.with_suffix('.release').unlink(missing_ok=True)
                marker.write_text('hold')
                changed=None
                try:
                    submitted=request({'overwrite':True},wait=False)
                    wait_file(barrier.with_suffix('.ready'))
                    marker.unlink()
                    if scenario=='raw_target': target.write_bytes(b'manual edit during composition')
                    elif scenario=='api_target': api.put('E-report/report.md','API edit during composition')
                    elif scenario in {'field','post_alias'}:
                        path=run_dir+'/1/T' if scenario=='field' else api.call('GET',api.prefix+'/postprocess/results')['result_dir']+'/metrics.json'
                        changed=root/path
                        old=changed.read_bytes()
                        changed.write_bytes(old+b'\nchanged')
                    elif scenario=='superseded': current=request({'overwrite':True},client=peer,wait=False)
                    else: api.call('POST',f'/jobs/{submitted["job_id"]}:cancel',{})
                finally:
                    marker.unlink(missing_ok=True)
                    barrier.with_suffix('.release').touch()
                if scenario=='superseded':
                    # Final validation waits for every case writer to drain;
                    # release the old worker before awaiting the newer result.
                    peer.wait_job(current['job_id'])
                api.wait_job(submitted['job_id'],expected='cancelled' if scenario=='cancel' else 'succeeded' if scenario in {'api_target','superseded'} else 'failed')
                if scenario=='raw_target':
                    assert target.read_bytes()==b'manual edit during composition'
                    assert result(current['publication_id'])['publication']['stale']
                    target.write_bytes(previous)
                elif scenario=='api_target':
                    assert target.read_text()=='API edit during composition'
                    assert not result(submitted['publication_id'])['publication']['applied']
                    current=request({'overwrite':True})
                elif changed:
                    assert target.read_bytes()==previous
                    changed.write_bytes(old)
                elif scenario=='superseded':
                    assert result()['publication']['id']==current['publication_id']
                    assert result(submitted['publication_id'])['publication']['status']=='superseded'
                else: assert target.read_bytes()==previous
                done('held_composition_'+scenario+'_preserves_current_draft_and_generation_fences')
            for scenario in ('target','backup'):
                previous=target.read_bytes()
                mirror=api.root/f'mirror-control-{api.case_id}'
                mirror.write_text(scenario)
                try:
                    rejected=request({'overwrite':True},expect=409)
                    submitted=rejected['detail']
                    assert submitted['code']=='composition_not_applied'
                    api.wait_job(submitted['job_id'])
                finally: mirror.unlink(missing_ok=True)
                saved=result(submitted['publication_id'])
                assert not saved['publication']['applied']
                assert target.read_bytes()==(b'manual edit after commit' if scenario=='target' else previous)
                assert api.call('GET',api.prefix+'/report/compose-artifact?publication_id='+saved['publication_id']+'&path='+saved['backup'],raw=True)==previous
                current=request({'overwrite':True})
                done('postcommit_'+scenario+'_edit_is_preserved_with_immutable_history_available')
        finally:
            stop_peer()
        target.write_bytes(b'')
        empty=request()
        assert empty['backup'] and (root/empty['backup']).read_bytes()==b'' and empty['publication']['applied']
        done('existing_empty_draft_is_backed_up_without_requiring_overwrite')
        name='--case-name=not-an-option'
        api.call('PATCH',api.prefix,{'name':name})
        named=request({'overwrite':True})
        assert named['toolchain']['case_name']==name and name in named['content']
        assert sum(row['current'] for row in api.call('GET',api.prefix+'/report/compositions'))==2
        done('leading_dash_case_name_is_data_and_both_channels_are_listed')
        previous=target.read_bytes()
        foreign=api.root/'outside-case-draft.md'
        foreign.write_bytes(b'outside case')
        target.unlink()
        try:
            os.link(foreign,target)
            before=api.call('GET',api.prefix+'/jobs')
            request({'overwrite':True},expect=400)
            assert api.call('GET',api.prefix+'/jobs')==before and foreign.read_bytes()==b'outside case'
        finally:
            target.unlink(missing_ok=True)
            target.write_bytes(previous)
        done('hardlinked_target_is_rejected_before_job_and_external_bytes_are_preserved')
        barrier=api.root/f'hold-report-{api.case_id}'
        barrier.with_suffix('.ready').unlink(missing_ok=True)
        barrier.with_suffix('.release').unlink(missing_ok=True)
        marker.write_text('hold')
        try:
            pending=request({'overwrite':True},'sync-timeout',expect=504)['detail']
            assert pending['code']=='composition_pending' and barrier.with_suffix('.ready').exists()
        finally:
            marker.unlink(missing_ok=True)
            barrier.with_suffix('.release').touch()
        api.wait_job(pending['job_id'])
        assert request({'overwrite':True},'sync-timeout')['publication']['applied']
        done('synchronous_timeout_reports_pending_job_and_does_not_cancel_or_duplicate_it')
        result_data={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
        (api.root/'compose-handler-result.json').write_text(json.dumps(result_data,indent=2)+'\n',encoding='utf-8')
        return result_data


async def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='compose-handler-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_compose_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_compose_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,
            password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"')
            created=True
            temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf-8')
    print('COMPOSE_HANDLER_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__':
    asyncio.run(main())
