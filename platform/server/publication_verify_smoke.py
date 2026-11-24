"""Actual Pandoc publications: immutable final hashes leave case control free."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import tempfile
import uuid

from sqlalchemy.engine import make_url

import publication_copy_smoke as copy
from testsupport import SERVER
from workflow_migration_smoke import alembic


CONTROLLED = copy.CONTROLLED.replace("if control()['action']=='copy_error':",
    "if control()['action'] in {'copy_error','validation_error'}:") + '''
import threading
from app.services import attempt_workspace as workspace
verifying=threading.local()
immutable=pub.verify_immutable_files
def checking(prepared):
 verifying.active=True
 if control()['stage'] in {'input_verify','bundle_verify'} and not (root/'blocked.once').exists():
  (root/'context.json').write_text(json.dumps({'prefix':prepared.prefix,'publication':prepared.publication.id,
   'records':prepared.records,'workspace':json.loads(prepared.input_json)['workspace']}))
 try:return immutable(prepared)
 finally:verifying.active=False
pub.verify_immutable_files=checking
inputs=workspace.verify
def input_check(*args):
 if getattr(verifying,'active',False) and blocked('input_verify'):pause()
 return inputs(*args)
workspace.verify=input_check
bundle=pub.verify_prepared_bundle
def bundle_check(*args):
 if getattr(verifying,'active',False) and blocked('bundle_verify'):pause()
 return bundle(*args)
pub.verify_prepared_bundle=bundle_check
validate=pub.verify_files_for_finish
async def validating(*args):
 prepared=await validate(*args)
 if prepared is not None and blocked('verified'):
  assert prepared.verified_binding is not None and prepared.error is None
  (root/'context.json').write_text(json.dumps({'prefix':prepared.prefix,'publication':prepared.publication.id,
   'records':prepared.records,'workspace':json.loads(prepared.input_json)['workspace']}))
  enter()
  deadline=time.monotonic()+60
  while not (root/'blocked.continue').exists() and time.monotonic()<deadline:await asyncio.sleep(.02)
  if not (root/'blocked.continue').exists():raise TimeoutError('Fixture verified bundle barrier')
  if control()['action']=='proof_changed':prepared.descriptor['fixture_changed']=True
  if control()['action']=='proof_missing':prepared.verified_binding=None
 return prepared
pub.verify_files_for_finish=validating
'''

MATRIX = [('input_verify', action) for action in (
    'normal', 'cancel', 'input_changed', 'task_cancel', 'heartbeat_failure', 'validation_error')]
MATRIX += [('bundle_verify', action) for action in (
    'normal', 'cancel', 'bundle_changed', 'manifest_changed', 'task_cancel', 'heartbeat_failure', 'validation_error')]
MATRIX += [('verified', action) for action in (
    'cancel', 'source', 'writer_blocked', 'binding_changed', 'identity_changed', 'proof_changed', 'proof_missing', 'commit_error')]


def verify(database_url=None):
    result = copy.verify(database_url, name='publication-verify', controlled=CONTROLLED, matrix=MATRIX)
    receipts = json.loads((Path(result['evidence'])/'copy-receipts.json').read_text(encoding='utf8'))
    assert len(receipts) == len(MATRIX) == 21
    result['scope'] = 'Immutable sealed inputs and prepared bundle hashes under source reservation; actual newer-generation races remain covered before reservation by publication_copy_smoke'
    return result


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--postgres', action='store_true')
    args = parser.parse_args()
    report = Path(tempfile.mkdtemp(prefix='publication-verify-evidence-', dir=SERVER.parents[1]/'tmp'))
    results = {'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        from app.config import Settings
        import asyncpg
        url = make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername == 'postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
        name = 'tmp_vf_pubverify_' + uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_pubverify_[0-9a-f]{32}', name)
        admin = await asyncpg.connect(host=url.host, port=url.port or 5432, user=url.username,
            password=url.password, database='postgres', timeout=10)
        created = False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"')
            created = True
            temporary = url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic, temporary, report, 'upgrade', 'head')
            results['postgresql'] = await asyncio.to_thread(verify, temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()') == 'postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed'] = True
    (report/'result.json').write_text(json.dumps(results, indent=2), encoding='utf8')
    print('PUBLICATION_VERIFY_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__ == '__main__':
    asyncio.run(main())
