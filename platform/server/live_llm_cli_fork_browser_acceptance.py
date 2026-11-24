import argparse
import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
from cli_managed_session_checks import FIXTURE as MANAGED
from live_llm_acceptance import FIXTURE as LIVE, source
from live_llm_cli_inference_acceptance import FIXTURE as INFERENCE
from testsupport import IsolatedServer, SERVER

FORK_DIAGNOSTICS = '''
from contextlib import asynccontextmanager
from fastapi import HTTPException
from app.services import codex_forks, cli_scope_allocation
from app.models import NativeCliSession, NativeCliTurn, NativeCliFork
import traceback

def failed(operation, error):
 with (_base/'branch-lifecycle-errors.jsonl').open('a',encoding='utf-8') as stream:
  stream.write(json.dumps({'operation':operation,'type':type(error).__name__,
   'code':getattr(error,'code',None),'frames':[
    [Path(entry.filename).name,entry.name,entry.lineno]
    for entry in traceback.extract_tb(error.__traceback__)]})+'\\n')

original_branch_access=sessions.Session.access
@asynccontextmanager
async def branch_access(self,**kwargs):
 try:
  async with original_branch_access(self,**kwargs) as result:
   yield result
 except BaseException as error:
  failed('session.access',error)
  raise
sessions.Session.access=branch_access

def instrument(owner, name):
 original=getattr(owner,name)
 async def observed(*args,**kwargs):
  started=time.monotonic()
  try:return await original(*args,**kwargs)
  except BaseException as error:failed(name,error);raise
  finally:
   with (_base/'branch-timing.jsonl').open('a',encoding='utf-8') as stream:
    stream.write(json.dumps({'operation':name,'seconds':time.monotonic()-started})+'\\n')
 setattr(owner,name,observed)

for obj,name in ((sessions.Session,'start'),(sessions.Session,'prepare'),
 (sessions.Session,'register_bridge'),(sessions.Session,'state_prepared'),
 (cli_scope_allocation,'allocate'),(codex_forks,'source'),(records,'thread_start')):
 instrument(obj,name)

@app.get('/api/v1/test/fork-workspace/{fork_id}/{turn_id}')
async def fork_workspace(fork_id: str, turn_id: str, user=Depends(get_current_user)):
 async with SessionLocal() as db:
  fork=await db.get(NativeCliFork,fork_id)
  turn=await db.get(NativeCliTurn,turn_id)
  if (fork is None or turn is None or fork.created_by!=user.id or turn.created_by!=user.id
      or fork.conversation_id!=turn.conversation_id):
   raise HTTPException(404)
  branch_session=await db.get(NativeCliSession,fork.session_id)
  turn_session=await db.get(NativeCliSession,turn.session_id)
  branch=json.loads(branch_session.profile_json)
  continued=json.loads(turn_session.profile_json)
  return {'same_source':branch['source_prefix']==continued['source_prefix'],
          'separate_sessions':fork.session_id!=turn.session_id,
          'fork_used_private_workspace':Path(branch['cwd']).parts[-3:]==('sessions',fork.session_id,'workspace'),
          'fork_staged_case':branch['staging_prefix'] is not None,
          'continuation_staged_case':continued['staging_prefix'] is not None}
'''


def run(configuration, runtime, playwright, web_dist, scenario, database_url=None, store=None):
    api = IsolatedServer('live-native-fork-browser', app='live_fixture:app', database_url=database_url)
    settings = 'import os\nos.environ.update(' + repr(store.settings()) + ')\n' if store else ''
    (api.root / 'live_fixture.py').write_text(settings + LIVE + MANAGED + INFERENCE + FORK_DIAGNOSTICS,
                                             encoding='utf-8')
    print(f'FF_F44_RUNTIME={runtime} EVIDENCE={api.root}', flush=True)
    with api:
        case_title = 'Live native fork and text handoff'
        api.create_case(case_title)
        if runtime == 'wsl':
            api.call('POST', '/test/prepare-cli-inference')
        profile = api.call('POST', '/provider-profiles', {
            'provider': 'deepseek', 'name': 'Actual DeepSeek', 'base_url': configuration['base_url'],
            'default_model': configuration['model'], 'api_key': configuration['key'],
        })
        conversation = api.call('POST', '/conversations', {
            'case_id': api.case_id, 'title': 'Native branch source', 'permission_mode': 'approve_writes',
            'provider_profile_id': profile['id'], 'model': configuration['model'],
        })
        nonce = 'FILE_' + uuid.uuid4().hex
        api.put('Ref/cli-inference-proof.txt', nonce + '\n')
        proof = None if store else next((api.root / 'files').rglob('cli-inference-proof.txt')).as_posix()
        web = ROOT / 'platform/web'
        node = shutil.which('node')
        edge = Path(os.environ['ProgramFiles(x86)']) / 'Microsoft/Edge/Application/msedge.exe'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        url = f'http://127.0.0.1:{port}'
        setup = api.root / 'browser-setup.json'
        setup.write_text(json.dumps({
            'url': url, 'case_title': case_title, 'conversation_id': conversation['id'],
            'email': api.call('GET', '/auth/me')['email'], 'runtime': runtime,
            'proof': proof, 'nonce': nonce, 'model': configuration['model'],
            'scenario': scenario,
            'database': 'PostgreSQL' if database_url else 'SQLite',
            'storage': 'authenticated SeaweedFS' if store else 'filesystem',
            'shell': (Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe').as_posix(),
        }), encoding='utf-8')
        with (api.root / 'vite.log').open('w', encoding='utf-8') as log:
            vite = subprocess.Popen([node, str(web / 'node_modules/vite/bin/vite.js'), 'preview',
                '--host', '127.0.0.1', '--port', str(port), '--strictPort', '--outDir',
                str(web_dist)], cwd=web,
                env=dict(os.environ, VITE_API_TARGET=api.base), stdout=log,
                stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                with httpx.Client(trust_env=False, timeout=2) as client:
                    for _ in range(100):
                        assert vite.poll() is None
                        try:
                            if client.get(url).status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(.1)
                    else:
                        raise TimeoutError('Isolated preview could not start')
                result = subprocess.run([node, str(HERE / 'cli_fork_browser.cjs'), str(setup), str(playwright), str(edge)],
                                        cwd=web, timeout=600)
                receipt = json.loads((api.root / 'browser-result.json').read_text(encoding='utf-8'))
                assert configuration['key'] not in json.dumps(receipt)
                if result.returncode or not receipt['passed']:
                    raise RuntimeError(receipt.get('failure', 'Browser workflow failed'))
            finally:
                vite.terminate()
                vite.wait(timeout=10)
                for row in api.call('GET', f'/conversations?caseId={api.case_id}'):
                    with contextlib.suppress(Exception):
                        active = api.call('GET', f'/codex/conversations/{row["id"]}/activity').get('active_turn')
                        if active:
                            api.call('POST', f'/codex/turns/{active["id"]}:cancel')
    print(f'FF_F44_RUNTIME={runtime} real_rounds={len(receipt["turns"])} passed=True', flush=True)
    return api.root


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', choices=('windows', 'wsl'), required=True)
    parser.add_argument('--playwright', type=Path, required=True)
    parser.add_argument('--source-profile-id', type=int, required=True)
    parser.add_argument('--web-dist', type=Path, default=ROOT / 'platform/web/dist')
    parser.add_argument('--postgres', action='store_true')
    parser.add_argument('--s3', action='store_true')
    parser.add_argument('--scenario', choices=('full', 'handoff', 'mid'), default='full')
    args = parser.parse_args()
    web_dist = args.web_dist.resolve()
    if not (web_dist / 'index.html').is_file():
        parser.error('--web-dist must contain the built platform frontend')
    evidence_root = ROOT / 'tmp'
    evidence_root.mkdir(exist_ok=True)
    receipt_root = Path(tempfile.mkdtemp(prefix='live-cli-fork-browser-run-', dir=evidence_root))
    print(f'RUN_RECEIPT={receipt_root}', flush=True)
    configuration = await source(args.source_profile_id)
    store = None
    if args.s3:
        from s3testsupport import ObjectStore
        store = ObjectStore(executable=ROOT / 'third_party/seaweed-4.52-runtime/weed', authenticated=True)
    database_details = None
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        selected = make_url(Settings(_env_file=SERVER / '.env').database_url)
        assert selected.drivername == 'postgresql+asyncpg' and selected.host in {'127.0.0.1', 'localhost', '::1'}
        admin = await asyncpg.connect(host=selected.host, port=selected.port or 5432, user=selected.username,
                                      password=selected.password, database='postgres', timeout=10)
        name = 'tmp_vf_cliforkbrowser_' + uuid.uuid4().hex
        database_details = {'name': name, 'created': False, 'dropped': False}
        url = selected.set(database=name).render_as_string(hide_password=False)
    else:
        url = None
    suffix = '-pg-s3' if database_details and store else ''
    receipt = {}
    try:
        if database_details:
            assert await admin.fetchval('SELECT current_database()') == 'postgres'
            await admin.execute('CREATE DATABASE "' + name + '"')
            database_details['created'] = True
            await asyncio.to_thread(alembic, url, receipt_root, 'upgrade', 'head')
        with store or contextlib.nullcontext():
            result = await asyncio.to_thread(run, configuration, args.runtime, args.playwright.resolve(),
                                             web_dist, args.scenario, url, store)
            receipt.update(passed=True, evidence=str(result))
    finally:
        if database_details:
            try:
                if database_details['created']:
                    assert await admin.fetchval('SELECT current_database()') == 'postgres'
                    await admin.execute('DROP DATABASE "' + name + '"')
                    database_details['dropped'] = True
            finally:
                await admin.close()
                receipt['database'] = database_details
        if store:
            receipt['seaweedfs'] = {'evidence': str(store.root),
                                    'exited': store.proc is not None and store.proc.poll() is not None}
        (receipt_root / ('runtime-' + args.runtime + suffix + '.json')).write_text(
            json.dumps(receipt, indent=2), encoding='utf-8')


if __name__ == '__main__':
    asyncio.run(main())
