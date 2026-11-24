"""Crash real APIs between caseDict body, event history, provenance and SQL."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
import uuid

import httpx

from app.services.casedict import set_block
from file_transactions_smoke import interrupt, peer
from testsupport import IsolatedServer, SERVER

PATHS = ('0-caseDict/caseDict', '0-caseDict/case-events.jsonl', '0-caseDict/provenance.jsonl')
FIXTURE = '''
import time
from pathlib import Path
from types import SimpleNamespace
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import file_transactions as journal
from app.services import casedict, local_file_edits
from app import storage
from app.main import app
root=Path(__file__).parent
def mode(): return (root/'mode').read_text() if (root/'mode').exists() else ''
def hold():
    (root/'ready').touch()
    deadline=time.monotonic()+90
    while not (root/'release').exists():
        if time.monotonic()>deadline: raise TimeoutError('caseDict transaction fixture')
        time.sleep(.02)
original_apply, original_commit=journal._apply, AsyncSession.commit
install=local_file_edits.PreparedEdit.install_file
def applying(case,directory,index,entry,side,expected):
    result=original_apply(case,directory,index,entry,side,expected)
    if side=='after' and ((mode()=='after_body' and index==0) or (mode()=='after_events' and index==1)): hold()
    return result
def installing(edit,case,target,index,entry):
    result=install(edit,case,target,index,entry)
    if (mode()=='after_body' and index==0) or (mode()=='after_events' and index==1): hold()
    return result
async def committing(self):
    owned=self.info.get('local_file_transactions',{})
    if owned and mode()=='before_commit': hold()
    result=await original_commit(self)
    if owned and mode()=='after_commit': hold()
    return result
journal._apply, AsyncSession.commit=applying, committing
local_file_edits.PreparedEdit.install_file=installing
original_prepare=casedict.prepare_case_dict
def preparing(case,*args,**kwargs):
    result=original_prepare(case,*args,**kwargs)
    if mode()=='history_race':
        path='0-caseDict/case-events.jsonl'
        storage._fs_put(case.s3_prefix,path,storage.get_bytes(case.s3_prefix,path)+b'{"external":true}\\n')
    return result
casedict.prepare_case_dict=preparing
@app.post('/fixture/fallback/{case_id}')
async def fallback(case_id:int,db=Depends(get_db),user=Depends(get_current_user)):
    from app.services.workflow_fallback import apply_failure_fallback
    case=await get_owned_case(case_id,user,db)
    result=await apply_failure_fallback(db,case,SimpleNamespace(id=999999,type='geometry',exit_code=7,log_s3_key='failure.log'))
    await db.commit()
    await journal.finish_committed(db,case)
    return result
@app.post('/fixture/proposal/{case_id}')
async def proposal(case_id:int,body:dict,db=Depends(get_db),user=Depends(get_current_user)):
    from app.services.tools import ToolExecutor
    case=await get_owned_case(case_id,user,db)
    executor=ToolExecutor(case.s3_prefix,case_id=case.id,user_id=user.id)
    result=await executor.apply_proposal(db,case,body.pop('test_tool','propose_file_update'),body)
    await db.commit()
    await journal.finish_committed(db,case)
    return result
'''


MODES = ('dict_after_body', 'dict_after_events', 'dict_before_commit', 'dict_after_commit',
         'advance_after_body', 'advance_after_commit', 'fallback_after_events', 'fallback_after_commit',
         'agent_after_events', 'agent_after_commit', 'external', 'corrupt_history', 'history_race', 'all_endpoints')


def snapshot(root):
    return {path: (root/path).read_bytes() if (root/path).exists() else None for path in PATHS}


def run(database_url=None, modes=None):
    checks, evidence = [], []
    for mode in modes or MODES:
        api = IsolatedServer('casedict-transaction-' + mode, app='dict_fixture:app', database_url=database_url)
        (api.root/'dict_fixture.py').write_text(FIXTURE, encoding='utf8')
        with api:
            case = api.create_case('Dictionary journal ' + mode)
            if mode.startswith('fallback'):
                api.advance('V')
            root = next((api.root/'files/workspaces').glob(f'*/cases/{case["id"]}'))
            saved = api.call('GET', api.prefix+'/caseDict')
            old = snapshot(root)
            old_mirror = api.call('GET', api.prefix)
            content = set_block(saved['content'], 'caseMeta', {'caseName': 'journal-new'})
            headers = {'Authorization': 'Bearer ' + api.token}
            with httpx.Client(base_url=api.base, headers=headers, trust_env=False, timeout=45) as client:
                endpoint = '/api/v1'+api.prefix
                if mode == 'history_race':
                    (api.root/'mode').write_text(mode)
                    response = client.put(endpoint+'/caseDict', json={'content': content})
                    assert response.status_code == 409, response.text
                    assert response.json()['detail']['code'] == 'file_conflict'
                    after = snapshot(root)
                    assert after[PATHS[0]] == old[PATHS[0]] and after[PATHS[2]] == old[PATHS[2]]
                    assert after[PATHS[1]] == old[PATHS[1]] + b'{"external":true}\n'
                    assert api.call('GET', api.prefix) == old_mirror
                    assert not (root/'.vibeflow/file-transactions/pending').exists()
                    checks.append(mode)
                    evidence.append({'mode': mode, 'api': str(api.root)})
                    continue
                if mode == 'corrupt_history':
                    (root/PATHS[2]).write_bytes(b'not JSON\n')
                    corrupt = snapshot(root)
                    response = client.put(endpoint+'/caseDict', json={'content': content})
                    assert response.status_code == 422, response.text
                    assert snapshot(root) == corrupt
                    assert api.call('GET', api.prefix) == old_mirror
                    assert not (root/'.vibeflow/file-transactions/pending').exists()
                    checks.append(mode)
                    evidence.append({'mode': mode, 'api': str(api.root)})
                    continue
                if mode == 'all_endpoints':
                    revision = saved['dict_revision']
                    for method, suffix, body in [
                        ('PUT', '/caseDict', {'content': content}),
                        ('PUT', '/caseDict/block', {'block': 'intent', 'entries': {'userIntent': 'transaction test'}}),
                        ('PUT', '/caseDict/extensions/journal', {'version': '1', 'enabled': True, 'settings': {}}),
                        ('DELETE', '/caseDict/extensions/journal', None),
                        ('PUT', '/caseDict/block', {'block': 'errors', 'entries': {'entries': {'E1': {'title': 'test', 'stage': 'mesh',
                            'category': 'meshQuality', 'severity': 'error', 'message': 'test', 'status': 'open', 'blocking': False}}}}),
                        ('PATCH', '/caseDict/errors/E1', {'status': 'verified', 'fixOptions': {'text': 'repair'},
                            'resolution': 'fixed', 'verification': 'checked'}),
                    ]:
                        response = client.request(method, endpoint+suffix, json=body)
                        assert response.status_code == 200, (suffix, response.text)
                        revision += 1
                        assert response.json()['dict_revision'] == revision
                        assert not (root/'.vibeflow/file-transactions/pending').exists()
                    assert api.call('GET', api.prefix+'/provenance')['chain_valid']
                    # A live Agent proposal uses the shared writer, not the
                    # legacy synchronous callback used by standalone tooling.
                    proposal = client.post('/fixture/proposal/'+str(case['id']), json={
                        'path': PATHS[0], 'content': api.call('GET', api.prefix+'/caseDict')['content']})
                    assert proposal.status_code == 200, proposal.text
                    replacement = client.post('/fixture/proposal/'+str(case['id']), json={
                        'test_tool': 'propose_text_replacement', 'path': PATHS[0],
                        'old_text': 'journal-new', 'new_text': 'journal-replaced'})
                    assert replacement.status_code == 200, replacement.text
                    assert api.call('GET', api.prefix+'/caseDict')['parsed']['caseMeta']['caseName'] == 'journal-replaced'
                    assert not (root/'.vibeflow/file-transactions/pending').exists()
                    checks.append(mode)
                    evidence.append({'mode': mode, 'api': str(api.root)})
                    continue
                fault = mode.split('_', 1)[1] if '_' in mode else 'after_events'
                (api.root/'mode').write_text(fault)
                if mode.startswith('advance'):
                    request = lambda: client.post(endpoint+'/caseDict:advance', json={
                        'state': 'V', 'confirmed': True, 'expected_revision': saved['dict_revision']})
                elif mode.startswith('fallback'):
                    request = lambda: client.post('/fixture/fallback/'+str(case['id']))
                elif mode.startswith('agent'):
                    request = lambda: client.post('/fixture/proposal/'+str(case['id']), json={
                        'path': PATHS[0], 'content': content, 'baseline_sha256': saved['content_sha256']})
                else:
                    request = lambda: client.put(endpoint+'/caseDict', json={'content': content,
                        'expected_revision': saved['dict_revision'], 'expected_sha256': saved['content_sha256']})
                interrupt(api, request)
            if mode == 'external':
                (root/PATHS[2]).write_bytes(b'EXTERNAL HISTORY\n')
                mixed = snapshot(root)
            committed = mode.endswith('after_commit')
            with peer(api) as recovered:
                diagnosis = recovered.call('GET', api.prefix+'/file-transactions')
                assert diagnosis['database_committed'] == committed, diagnosis
                if mode == 'external':
                    assert diagnosis['status'] == 'conflict'
                    for suffix in ('/caseDict', '/events', '/provenance'):
                        failure = recovered.call('GET', api.prefix+suffix, expect=409)
                        assert failure['detail']['code'] == 'file_recovery_required'
                    assert snapshot(root) == mixed, 'Conflict must not partially roll back other files'
                else:
                    fresh = recovered.call('GET', api.prefix+'/caseDict')
                    mirror = recovered.call('GET', api.prefix)
                    if not committed:
                        assert fresh == saved, (mode, fresh)
                        assert snapshot(root) == old
                        assert mirror == old_mirror
                    else:
                        assert fresh['dict_revision'] == saved['dict_revision'] + 1
                        assert mirror['dict_revision'] == fresh['dict_revision']
                        assert mirror['workflow_state'] == fresh['workflow_state']
                        expected_state = 'V' if mode.startswith('advance') else 'A'
                        assert fresh['workflow_state'] == expected_state
                        if mode.startswith(('dict', 'agent')):
                            assert fresh['content'] == content
                            assert json.loads(mirror['case_meta'])['caseName'] == 'journal-new'
                        provenance = recovered.call('GET', api.prefix+'/provenance')
                        assert provenance['chain_valid']
                        newest = provenance['events'][0]
                        assert newest['revision'] == fresh['dict_revision']
                        assert newest['casedict_sha256'] == hashlib.sha256(fresh['content'].encode()).hexdigest()
                        events = recovered.call('GET', api.prefix+'/events')['events']
                        assert events[0]['revision'] == fresh['dict_revision']
                        if mode.startswith('fallback'):
                            assert events[0]['type'] == 'workflow.fallback' and events[0]['regressed']
                            assert events[1]['type'] == 'caseDict.updated'
                    settled = snapshot(root)
                    assert recovered.call('GET', api.prefix+'/caseDict') == fresh
                    assert snapshot(root) == settled
                    assert not (root/'.vibeflow/file-transactions/pending').exists()
            evidence.append({'mode': mode, 'api': str(api.root), 'peer': str(recovered.root)})
            checks.append(mode)
        print('PASS '+mode, flush=True)
    return {'checks': checks, 'count': len(checks), 'runs': evidence}


def s3_proposals():
    from s3testsupport import ObjectStore
    from sandbox_normal_close_resource_smoke import RUNTIME
    with ObjectStore(executable=RUNTIME) as store:
        api = IsolatedServer('dictionary-s3-proposals', app='dict_fixture:app')
        (api.root/'dict_fixture.py').write_text('import os\nos.environ.update('+repr(store.settings())+')\n'+FIXTURE,
                                              encoding='utf8')
        checks = []
        with api:
            api.create_case('Remote proposal storage')
            def proposal(body):
                return api.call('POST', '/fixture/proposal/'+str(api.case_id), body, api_prefix=False)
            proposal({'path': 'Ref/proposal.txt', 'content': 'same old same'})
            assert api.call('GET', api.prefix+'/file?path=Ref/proposal.txt')['content'] == 'same old same'
            checks.append('ordinary_proposal_writes_authoritative_S3_object')
            saved = api.call('GET', api.prefix+'/file:version?path=Ref/proposal.txt')
            proposal({'test_tool': 'propose_text_replacement', 'path': 'Ref/proposal.txt',
                'old_text': 'same', 'new_text': 'new', 'replace_all': True, 'baseline_sha256': saved['content_sha256']})
            assert api.call('GET', api.prefix+'/file?path=Ref/proposal.txt')['content'] == 'new old new'
            checks.append('replacement_reads_and_updates_remote_bytes')
            saved = api.call('GET', api.prefix+'/caseDict')
            proposal({'path': PATHS[0], 'content': set_block(saved['content'], 'caseMeta', {'caseName': 'S3-proposal'}),
                      'baseline_sha256': saved['content_sha256']})
            current = api.call('GET', api.prefix+'/caseDict')
            assert current['dict_revision'] == saved['dict_revision'] + 1
            assert current['parsed']['caseMeta']['caseName'] == 'S3-proposal'
            provenance = api.call('GET', api.prefix+'/provenance')
            assert provenance['chain_valid'] and provenance['events'][0]['casedict_sha256'] == current['content_sha256']
            checks.append('remote_dictionary_proposal_updates_mirror_and_hash_chain')
        return {'checks': checks, 'count': len(checks), 'api': str(api.root),
                'backend': 's3', 'crash_recovery_exercised_in_this_suite': False}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--modes', nargs='+')
    parser.add_argument('--postgres-only', action='store_true')
    parser.add_argument('--s3-proposals', action='store_true')
    args = parser.parse_args()
    output = Path(tempfile.mkdtemp(prefix='casedict-transactions-evidence-', dir=SERVER.parent.parent/'tmp'))
    if args.s3_proposals:
        result = await asyncio.to_thread(s3_proposals)
    elif args.postgres_only:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        pg = make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert pg.drivername == 'postgresql+asyncpg' and pg.host in {'127.0.0.1', 'localhost', '::1'}
        name = 'tmp_vf_dictjournal_'+uuid.uuid4().hex
        admin = await asyncpg.connect(host=pg.host, port=pg.port or 5432, user=pg.username,
                                      password=pg.password, database='postgres', timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"')
            created = True
            url = pg.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic, url, output, 'upgrade', 'head')
            result = await asyncio.to_thread(run, url, args.modes)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()') == 'postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        result.update(database='postgresql', temporary_database_removed=True)
    else:
        result = await asyncio.to_thread(run, modes=args.modes)
        result['database'] = 'sqlite'
    (output/'checks.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps({'status': 'passed', 'count': result['count'], 'evidence': str(output)}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
