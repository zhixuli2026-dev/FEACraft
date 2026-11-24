"""Hosted API credentials: actual owned process scopes and disposable databases."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import tempfile
import uuid

from sqlalchemy.engine import make_url
from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic
from codex_turn_api_smoke import CONTROLLED, WORKER
from codex_session_smoke import wait_for
from app.services.codex_projection import Secrets

SECRET_ENV = 'VIBEFLOW_CODEX_PROVIDER_TOKEN'
NATIVE = WORKER.replace("elif kind=='capacity':", "elif kind=='credential_echo':terminal('PREFIX '+os.environ['VIBEFLOW_CODEX_PROVIDER_TOKEN']+' SUFFIX')\n        elif kind=='capacity':")
NATIVE = NATIVE.replace("'cwd':os.getcwd()", "'cwd':os.getcwd(),'credential_sha256':__import__('hashlib').sha256(os.environ.get('VIBEFLOW_CODEX_PROVIDER_TOKEN','').encode()).hexdigest()")
NATIVE = NATIVE.replace("'userAgent':'fixture/turns'", "'userAgent':('display '+os.environ['VIBEFLOW_CODEX_PROVIDER_TOKEN']) if mode=='credential_display' else 'fixture/turns'")
NATIVE = NATIVE.replace("result={'account':None,'requiresOpenaiAuth':False}", "result={'account':{'type':'apiKey','id':os.environ['VIBEFLOW_CODEX_PROVIDER_TOKEN']} if mode=='credential_display' else None,'requiresOpenaiAuth':False}")
NATIVE = NATIVE.replace("result={'data':[],'nextCursor':None}", "result={'data':[{'id':'display','description':os.environ['VIBEFLOW_CODEX_PROVIDER_TOKEN']}] if mode=='credential_display' else [],'nextCursor':None}")
NATIVE = NATIVE.replace("elif kind=='resolved':", "elif kind=='credential_resolved':\n            send({'method':'serverRequest/resolved','params':{'threadId':thread,'requestId':os.environ['VIBEFLOW_CODEX_PROVIDER_TOKEN']}})\n            terminal()\n        elif kind=='resolved':")
FIXTURE = CONTROLLED + '''
from app.models import Membership,Organization,User
@app.get('/api/v1/fixture/credential-session/{sid}')
async def credential_session(sid:str,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        row=await db.get(NativeCliSession,sid)
        assert row.created_by==user.id
        return {'profile':json.loads(row.profile_json),'process':json.loads(row.process_json),'initialize':json.loads(row.initialize_json)}
credential_root=sessions.Session.register_root
async def register_credential_root(self,birth):
    await credential_root(self,birth)
    if mode=='credential_rotate_at_root':
        async with SessionLocal() as db:
            row=await db.get(ProviderProfile,self.credential['profile_id'])
            row.key_ciphertext=llm.encrypt_key('rotated-at-root-test-only');await db.commit()
sessions.Session.register_root=register_credential_root
@app.post('/api/v1/fixture/share-member/{uid}')
async def share_member(uid:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        org=Organization(name='Credential test');db.add(org);await db.flush()
        db.add_all([Membership(org_id=org.id,user_id=user.id,role='admin'),Membership(org_id=org.id,user_id=uid,role='member')])
        await db.commit();return {'id':org.id}
@app.get('/api/v1/fixture/user-id')
async def user_id(user=Depends(get_current_user)):return {'id':user.id}
@app.post('/api/v1/fixture/profile-selection/{cid}/{pid}')
async def profile_selection(cid:int,pid:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        row=await db.get(Conversation,cid);row.provider_profile_id=pid;await db.commit()
        return {'ok':True}
@app.post('/api/v1/fixture/disable-owner/{pid}')
async def disable_owner(pid:int,user=Depends(get_current_user)):
    async with SessionLocal() as db:
        profile=await db.get(ProviderProfile,pid);owner=await db.get(User,profile.user_id)
        owner.status='disabled';await db.commit();return {'ok':True}
'''


def run_database(url, folder):
    folder.mkdir(parents=True)
    if url is not None:
        alembic(url, folder, 'upgrade', 'head')
    api = IsolatedServer('cli-credentials', app='credential_fixture:app', database_url=url)
    (api.root / 'cli.py').write_text(NATIVE, encoding='utf8')
    (api.root / 'credential_fixture.py').write_text(FIXTURE, encoding='utf8')
    checks, receipts, secrets = [], [], []
    with api:
        case = api.create_case('Hosted credential owner')
        owner_token = api.token
        def profile(**kw):
            key = 'local-only-' + uuid.uuid4().hex
            secrets.append(key)
            result = api.call('POST', '/provider-profiles', {'provider': 'custom', 'name': 'Responses test',
                'base_url': 'http://127.0.0.1:1/v1', 'default_model': 'fixture-model', 'api_key': key, **kw})
            return result['id'], key
        pid, secret = profile()
        def conversation(case_id=None, **kw):
            return api.call('POST', '/conversations', {'case_id': case_id or case['id'],
                'permission_mode': 'approve_writes', **kw})
        def session(conv, profile_id=pid, **kw):
            return api.call('POST', '/codex/sessions', {'case_id': conv['case_id'], 'conversation_id': conv['id'],
                'provider_profile_id': profile_id, 'provider_wire_api': 'responses' if profile_id else None, **kw})
        def submit(parent, content='normal', **kw):
            return api.call('POST', '/codex/sessions/' + parent['id'] + '/turns',
                {'content': content, 'client_message_id': uuid.uuid4().hex, **kw})
        def details(parent):
            return api.call('GET', '/fixture/credential-session/' + parent['id'])
        def done(turn, phase='completed'):
            result = wait_for(lambda: api.call('GET', '/codex/turns/' + turn['id']),
                lambda row: row['phase'] in {'completed', 'failed', 'cancelled', 'interrupted'}, seconds=30)
            assert result['phase'] == phase and result['cleanup_verified'], result
            receipt = api.call('GET', '/fixture/turn/' + turn['id'])
            receipts.append(receipt)
            return receipt
        def held(conv=None, profile_id=pid):
            parent = session(conv or conversation(), profile_id)
            turn = submit(parent, 'command')
            pending = wait_for(lambda: api.call('GET', '/codex/turns/' + turn['id'] + '/approvals'), bool)[0]
            return parent, turn, pending
        unbound = session(conversation(), None)
        api.call('POST', '/codex/sessions/' + unbound['id'] + '/turns',
            {'content': 'normal', 'client_message_id': uuid.uuid4().hex}, expect=409)
        assert not (api.root / 'requests.log').exists()
        api.call('DELETE', '/codex/sessions/' + unbound['id'])
        checks.append('unbound_metadata_cannot_infer_or_borrow_personal_login')
        conv = conversation()
        api.call('POST', '/codex/sessions', {'case_id': case['id'], 'conversation_id': conv['id'], 'provider_profile_id': pid}, expect=400)
        checks.append('custom_provider_requires_explicit_responses_protocol')
        bad, _ = profile(provider='anthropic', base_url='https://api.anthropic.com')
        api.call('POST', '/codex/sessions', {'provider_profile_id': bad, 'provider_wire_api': 'responses'}, expect=400)
        bad_url, _ = profile(base_url='https://example.test/v1?token=do-not-embed')
        api.call('POST', '/codex/sessions', {'provider_profile_id': bad_url, 'provider_wire_api': 'responses'}, expect=400)
        checks.append('messages_protocol_and_credential_bearing_urls_rejected_before_launch')
        invalid, _ = profile(api_key='invalid\nheader')
        api.call('POST', '/codex/sessions', {'provider_profile_id': invalid, 'provider_wire_api': 'responses'}, expect=400)
        checks.append('invalid_header_credential_rejected_before_child_environment')
        root_profile, _ = profile()
        count_before = len(list(api.root.glob('cli-*.json')))
        api.call('POST', '/fixture/mode/credential_rotate_at_root')
        api.call('POST', '/codex/sessions', {'provider_profile_id': root_profile, 'provider_wire_api': 'responses'}, expect=409)
        assert len(list(api.root.glob('cli-*.json'))) == count_before
        rows = api.call('GET', '/codex/sessions')
        assert all(row['cleanup_verified'] for row in rows)
        api.call('POST', '/fixture/mode/normal')
        checks.append('rotation_after_root_registration_prevents_native_payload_launch')
        first = session(conv)
        selected = details(first)['profile']
        home = Path(selected['auth_home'])
        configuration = (home / 'config.toml').read_text(encoding='utf8')
        assert SECRET_ENV in configuration and secret not in configuration and secret not in json.dumps(selected)
        native = [json.loads(p.read_text()) for p in api.root.glob('cli-*.json')]
        assert any(item['credential_sha256'] == hashlib.sha256(secret.encode()).hexdigest() for item in native)
        assert first['credential']['mode'] == 'api_key' and first['credential']['provider_profile_id'] == pid
        assert api.call('GET', '/codex/sessions/' + first['id'] + '/account')['credential'] == first['credential']
        checks.append('authorized_api_key_only_in_owned_child_environment')
        receipt = done(submit(first, 'credential_echo'))
        assert 'PREFIX *** SUFFIX' in json.dumps(receipt) and secret not in json.dumps(receipt)
        invalid_id = session(conversation())
        api.call('POST', '/codex/sessions/' + invalid_id['id'] + '/turns',
            {'content': 'normal', 'client_message_id': secret}, expect=400)
        done(submit(invalid_id, 'credential_resolved'), 'failed')
        checks.append('credential_redacted_across_native_deltas_and_canonical_history')
        resumed = session(conv)
        assert details(resumed)['profile']['auth_home'] == str(home)
        done(submit(resumed, resume=True))
        checks.append('unchanged_credential_resumes_verified_native_thread')
        secret_session = session(conversation())
        turn = submit(secret_session, 'secret')
        pending = wait_for(lambda: api.call('GET', '/codex/turns/' + turn['id'] + '/approvals'), bool)[0]
        api.call('POST', '/codex/turns/' + turn['id'] + '/approvals/' + pending['id'] + ':respond', {'answers': {'q1': [secret]}})
        done(turn)
        assert secret not in Secrets(home).load()
        checks.append('api_key_never_copied_to_persistent_question_redaction_vault')
        api.call('POST', '/fixture/mode/credential_display')
        display = session(conversation())
        responses = [details(display), api.call('GET', '/codex/sessions/' + display['id'] + '/account'),
                     api.call('GET', '/codex/sessions/' + display['id'] + '/models')]
        assert secret not in json.dumps(responses) and '***' in json.dumps(responses)
        api.call('DELETE', '/codex/sessions/' + display['id'])
        api.call('POST', '/fixture/mode/normal')
        checks.append('initialization_account_and_model_metadata_redact_bound_secret')
        (home / 'config.toml').write_text(configuration + '# changed\n', encoding='utf8')
        count_before = len(list(api.root.glob('cli-*.json')))
        api.call('POST', '/codex/sessions', {'provider_profile_id': pid, 'provider_wire_api': 'responses'}, expect=502)
        assert len(list(api.root.glob('cli-*.json'))) == count_before
        (home / 'config.toml').write_text(configuration, encoding='utf8')
        checks.append('modified_private_configuration_rejected_before_credential_injection')
        old, turn, pending = held()
        effect = Path(details(old)['profile']['cwd']) / 'Ref/native-effect.txt'
        assert not effect.exists()
        replies_before = (api.root / 'replies.log').read_bytes()
        replacement = 'rotated-only-' + uuid.uuid4().hex
        secrets.append(replacement)
        api.call('PATCH', '/provider-profiles/' + str(pid), {'api_key': replacement})
        done(turn, 'failed')
        api.call('POST', '/codex/turns/' + turn['id'] + '/approvals/' + pending['id'] + ':respond', {'decision': 'accept'}, expect=409)
        assert not effect.exists() and (api.root / 'replies.log').read_bytes() == replies_before
        checks.append('key_rotation_retires_waiting_scope_and_forbids_late_approval')
        rotated = session(conv)
        rotated_home = details(rotated)['profile']['auth_home']
        assert rotated_home != str(home)
        done(submit(rotated, resume=True), 'failed')
        done(submit(session(conv)))
        checks.append('rotated_key_has_new_home_and_requires_new_thread')
        idle = session(conversation())
        api.call('PATCH', '/provider-profiles/' + str(pid), {'name': 'Renamed only'})
        assert api.call('GET', '/codex/sessions/' + idle['id'] + '/account')['credential']['inference_configured']
        done(submit(idle))
        checks.append('display_name_edit_preserves_credential_version')
        old, turn, _ = held()
        api.call('PATCH', '/provider-profiles/' + str(pid), {'base_url': 'http://127.0.0.1:2/v1'})
        done(turn, 'failed')
        checks.append('endpoint_change_retires_scope_without_provider_substitution')
        selection = conversation()
        old, turn, _ = held(selection)
        api.call('POST', f"/fixture/profile-selection/{selection['id']}/{pid}")
        done(turn, 'failed')
        checks.append('conversation_provider_change_invalidates_existing_session')
        # Same shared profile must still have distinct private homes per user.
        owner_parent = session(conversation())
        owner_home = details(owner_parent)['profile']['auth_home']
        api.call('DELETE', '/codex/sessions/' + owner_parent['id'])
        member_case = api.create_case('Hosted credential member')
        member_token = api.token
        member_id = api.call('GET', '/fixture/user-id')['id']
        api.call('POST', '/codex/sessions', {'provider_profile_id': pid, 'provider_wire_api': 'responses'}, expect=403)
        assert not api.call('GET', '/codex/sessions')
        checks.append('foreign_private_profile_denied_before_cli_creation')
        api.token = owner_token
        org = api.call('POST', '/fixture/share-member/' + str(member_id))['id']
        api.call('PUT', f'/org/{org}/provider-profiles/{pid}', {'enabled': True})
        api.token = member_token
        member_conv = conversation(member_case['id'])
        shared, turn, _ = held(member_conv)
        member_home = details(shared)['profile']['auth_home']
        assert member_home != owner_home
        checks.append('shared_api_profile_has_private_user_home_and_history')
        api.token = owner_token
        api.call('DELETE', f'/org/{org}/provider-profiles/{pid}')
        api.token = member_token
        done(turn, 'failed')
        api.call('POST', '/codex/sessions', {'provider_profile_id': pid, 'provider_wire_api': 'responses'}, expect=403)
        checks.append('shared_profile_revocation_stops_actual_scope_and_denies_reopen')
        api.token = owner_token
        api.call('PUT', f'/org/{org}/provider-profiles/{pid}', {'enabled': True})
        api.token = member_token
        shared, turn, _ = held(conversation(member_case['id']))
        api.call('POST', '/fixture/disable-owner/' + str(pid))
        done(turn, 'failed')
        checks.append('disabled_credential_owner_revokes_shared_inference')
        for known in secrets:
            assert known not in json.dumps(receipts)
            for path in (api.root / 'cli-state').rglob('*'):
                if path.is_file():
                    assert known.encode() not in path.read_bytes(), 'Provider credential persisted in CLI state'
        checks.append('all_public_receipts_and_private_cli_files_exclude_plaintext_credentials')
    result = {'passed': True, 'count': len(checks), 'checks': checks, 'fixture_cli': True,
              'actual_owned_scopes': True, 'hosted_inference': False, 'evidence': str(api.root)}
    (api.root / 'credential-receipts.json').write_text(json.dumps(receipts, indent=2), encoding='utf8')
    (folder / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--postgres', action='store_true')
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='cli-credential-evidence-', dir=SERVER.parents[1] / 'tmp'))
    results = {'sqlite': await asyncio.to_thread(run_database, None, root / 'sqlite')}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url = make_url(Settings(_env_file=SERVER / '.env').database_url)
        assert url.host in {'127.0.0.1', 'localhost', '::1'} and url.drivername == 'postgresql+asyncpg'
        name = 'tmp_vf_clicredential_' + uuid.uuid4().hex
        assert re.fullmatch('tmp_vf_clicredential_[0-9a-f]{32}', name)
        admin = await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username,
            password=url.password, database='postgres', timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "' + name + '"')
            created = True
            results['postgresql'] = await asyncio.to_thread(run_database,
                url.set(database=name).render_as_string(hide_password=False), root / 'postgresql')
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()') == 'postgres'
                await admin.execute('DROP DATABASE "' + name + '"')
            await admin.close()
        results['postgresql']['temporary_database_removed'] = True
    (root / 'result.json').write_text(json.dumps(results, indent=2), encoding='utf8')
    print(json.dumps({'passed': True, 'counts': {k: v['count'] for k, v in results.items()}, 'evidence': str(root)}))


if __name__ == '__main__':
    asyncio.run(main())
