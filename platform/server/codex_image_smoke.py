"""Native image admission, byte identity and failure behavior on isolated DBs."""
import argparse,asyncio,base64,hashlib,json,re,sys,tempfile,uuid
from pathlib import Path
from sqlalchemy.engine import make_url
from testsupport import IsolatedServer,SERVER
from workflow_migration_smoke import alembic
from codex_session_smoke import wait_for, CONTROLLED as BASE
from codex_turn_api_smoke import WORKER,CONTROLLED
from codex_session_staging_smoke import CONTROLLED as STAGING

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Z3XcAAAAASUVORK5CYII=')
IMAGE={'name':'fixture.png','mime':'image/png','data':base64.b64encode(PNG).decode()}
IMAGE_WORKER=WORKER.replace("    elif method=='turn/start':",'''    elif method=='turn/start':
        import hashlib
        images=[]
        for part in params['input'][1:]:
            assert part['type']=='localImage'
            path=Path(part['path']);data=path.read_bytes()
            images.append({'path':str(path),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        with (root/'images.log').open('a') as stream:stream.write(json.dumps(images)+'\\n')
''')
IMAGE_APP=CONTROLLED+'''
from app.services import codex_images,quota
image_storage_quota=quota.check_storage_quota
async def check_images(db,user,size):
    if mode=='image_quota':
        from fastapi import HTTPException
        raise HTTPException(429,'Fixture image quota')
    return await image_storage_quota(db,user,size)
quota.check_storage_quota=check_images
image_append=agent._append_event
async def append_image(db,cid,kind,payload,**kwargs):
    if payload.get('images'):
        if mode=='image_admission_failure':raise RuntimeError('Image admission failure')
        if mode=='image_commit_unknown':db.info['image_commit_unknown']=True
    return await image_append(db,cid,kind,payload,**kwargs)
agent._append_event=append_image
image_commit=AsyncSession.commit
async def commit_image(db):
    unknown=db.info.pop('image_commit_unknown',False)
    result=await image_commit(db)
    if unknown:raise OSError('Image commit acknowledgement lost')
    return result
AsyncSession.commit=commit_image
image_before=codex_turns.Execution.before_attempt
async def image_attempt(self,identifier):
    if mode=='image_tamper' and self.images.files:
        self.images.files[0][0].write_bytes(b'changed')
    return await image_before(self,identifier)
codex_turns.Execution.before_attempt=image_attempt
'''

def run_database(url,folder):
    folder.mkdir(parents=True)
    if url:alembic(url,folder,'upgrade','head')
    api=IsolatedServer('codex-image-api',app='image_fixture:app',database_url=url)
    (api.root/'cli.py').write_text(IMAGE_WORKER,encoding='utf8')
    (api.root/'image_fixture.py').write_text(IMAGE_APP+STAGING[len(BASE):],encoding='utf8')
    checks=[]
    with api:
        case=api.create_case('Native image fixture');profile=api.call('POST','/fixture/profile')['id']
        def setup(mode='normal'):
            api.call('POST','/fixture/mode/'+mode)
            conv=api.call('POST','/conversations',{'case_id':case['id'],'provider_profile_id':profile})
            session=api.call('POST','/codex/sessions',{'case_id':case['id'],'conversation_id':conv['id'],'provider_profile_id':profile,'provider_wire_api':'responses'})
            return conv,session
        def submit(session,**kwargs):
            body={'content':'normal','client_message_id':uuid.uuid4().hex,'images':[IMAGE],**kwargs}
            return api.call('POST',f"/codex/sessions/{session['id']}/turns",body),body
        def finish(turn):
            return wait_for(lambda:api.call('GET','/codex/turns/'+turn['id']),lambda v:v['phase'] in {'completed','failed'})
        def history(conv):return api.call('GET',f"/conversations/{conv['id']}")['events']
        conv,session=setup();turn,body=submit(session);value=finish(turn)
        assert value['phase']=='completed' and value['cleanup_verified'],value
        event=next(e for e in history(conv) if e['payload'].get('role')=='user')
        metadata=event['payload']['images'][0]
        assert metadata['sha256']==hashlib.sha256(PNG).hexdigest() and 'data' not in metadata
        assert api.call('GET',f"/cases/{case['id']}/raw?path={metadata['path']}",raw=True)==PNG
        images=json.loads((api.root/'images.log').read_text().splitlines()[-1])
        assert images[0]['sha256']==metadata['sha256'] and '.private-cli' in images[0]['path']
        assert Path(images[0]['path']).is_file()
        checks.append('native_input_uses_owned_private_copy_and_public_history_references_exact_case_bytes')
        before=(api.root/'images.log').read_text()
        assert api.call('POST',f"/codex/sessions/{session['id']}/turns",body)['id']==turn['id']
        assert (api.root/'images.log').read_text()==before
        api.call('POST',f"/codex/sessions/{session['id']}/turns",{**body,'images':[{**IMAGE,'name':'different.png'}]},expect=409)
        api.call('POST',f"/codex/sessions/{session['id']}/turns",{**body,'images':[{**IMAGE,'data':base64.b64encode(PNG+b'changed').decode()}]},expect=409)
        checks.append('image_message_idempotency_includes_attachment_bytes_and_metadata')
        next_session=api.call('POST','/codex/sessions',{'case_id':case['id'],'conversation_id':conv['id'],'provider_profile_id':profile,'provider_wire_api':'responses'})
        continuation,_=submit(next_session,images=[])
        assert finish(continuation)['phase']=='completed'
        handoff=json.loads((api.root/'requests.log').read_text().splitlines()[-1])['kind']
        assert metadata['path'] in handoff and 'visual content is not embedded' in handoff
        checks.append('new_thread_text_handoff_preserves_prior_image_file_references')
        conv,session=setup();turn,_=submit(session,content='')
        assert finish(turn)['phase']=='completed'
        assert next(e for e in history(conv) if e['payload'].get('role')=='user')['payload']['content']==''
        checks.append('image_only_turn_keeps_original_empty_user_text')
        conv,session=setup();turn,_=submit(session,images=[IMAGE,IMAGE,IMAGE])
        assert finish(turn)['phase']=='completed' and len(json.loads((api.root/'images.log').read_text().splitlines()[-1]))==3
        checks.append('three_images_use_distinct_owned_paths')
        conv,session=setup();turn,_=submit(session,images=[{**IMAGE,'name':'fixture-native-api-credential.png'}])
        assert finish(turn)['phase']=='completed'
        metadata=next(e for e in history(conv) if e['payload'].get('role')=='user')['payload']['images'][0]
        assert 'fixture-native-api-credential' not in json.dumps(metadata)
        assert api.call('GET',f"/cases/{case['id']}/raw?path={metadata['path']}",raw=True)==PNG
        checks.append('filename_redaction_does_not_change_the_opaque_attachment_storage_key')
        api.call('POST','/fixture/staging-enable')
        conv,session=setup();turn,_=submit(session)
        assert finish(turn)['phase']=='completed'
        metadata=next(e for e in history(conv) if e['payload'].get('role')=='user')['payload']['images'][0]
        assert api.call('GET',f"/cases/{case['id']}/raw?path={metadata['path']}",raw=True)==PNG
        checks.append('staging_delivery_preserves_image_published_after_initial_case_snapshot')
        for label,images,content,status in [
            ('bad_base64',[{**IMAGE,'data':'!!!'}],'x',400),
            ('mime_mismatch',[{**IMAGE,'mime':'image/jpeg'}],'x',400),
            ('oversized',[{**IMAGE,'data':base64.b64encode(PNG+b'x'*(4*1024*1024)).decode()}],'x',422),
            ('empty',[],'',400),('too_many',[IMAGE]*4,'x',422),
            ('arbitrary_path',[{**IMAGE,'path':'C:/Windows/secrets.png'}],'x',422)]:
            conv,session=setup()
            api.call('POST',f"/codex/sessions/{session['id']}/turns",{'content':content,'client_message_id':uuid.uuid4().hex,'images':images},expect=status)
            assert not any(e['type']=='message' for e in history(conv))
            api.call('DELETE','/codex/sessions/'+session['id'])
            checks.append(label+'_rejected_before_message_admission')
        conv,session=setup('image_quota')
        api.call('POST',f"/codex/sessions/{session['id']}/turns",{'content':'x','client_message_id':uuid.uuid4().hex,'images':[IMAGE]},expect=429)
        assert not history(conv)
        api.call('DELETE','/codex/sessions/'+session['id'])
        checks.append('storage_quota_rejects_before_image_publication')
        conv,session=setup('image_admission_failure')
        inputs=list(api.root.rglob('inputs/*/*.png'))
        api.call('POST',f"/codex/sessions/{session['id']}/turns",{'content':'x','client_message_id':uuid.uuid4().hex,'images':[IMAGE]},expect=502,raw=True)
        assert not history(conv) and list(api.root.rglob('inputs/*/*.png'))==inputs
        api.call('DELETE','/codex/sessions/'+session['id'])
        checks.append('known_admission_failure_removes_uncommitted_private_image_copy')
        conv,session=setup('image_tamper');before=(api.root/'images.log').read_text()
        turn,_=submit(session);value=finish(turn)
        assert value['phase']=='failed' and value['cleanup_verified'] and (api.root/'images.log').read_text()==before,value
        checks.append('changed_private_image_fails_before_native_turn_rpc')
        conv,session=setup('image_commit_unknown')
        body={'content':'x','client_message_id':uuid.uuid4().hex,'images':[IMAGE]}
        api.call('POST',f"/codex/sessions/{session['id']}/turns",body,expect=502,raw=True)
        event=next(e for e in history(conv) if e['payload'].get('role')=='user')
        assert api.call('GET',f"/cases/{case['id']}/raw?path={event['payload']['images'][0]['path']}",raw=True)==PNG
        before=(api.root/'images.log').read_text()
        assert api.call('POST',f"/codex/sessions/{session['id']}/turns",body)['id']==event['payload']['native_execution_id']
        assert (api.root/'images.log').read_text()==before
        checks.append('unknown_commit_retains_committed_image_without_replaying_inference')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
    (folder/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result

async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='codex-image-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(run_database,None,root/'sqlite')}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_cliimage_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_cliimage_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            results['postgresql']=await asyncio.to_thread(run_database,url.set(database=name).render_as_string(hide_password=False),root/'postgresql')
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (root/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'counts':{k:v['count'] for k,v in results.items()},'evidence':str(root)}))

if __name__=='__main__':asyncio.run(main())
