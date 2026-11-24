"""WSL-specific API admission, committed gates and history on disposable DBs."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import tempfile
import uuid
from sqlalchemy.engine import make_url

from testsupport import IsolatedServer, SERVER
from codex_session_smoke import CONTROLLED, WORKER, wait_for
from codex_wsl_test_support import NativeStateFixture
from workflow_migration_smoke import alembic

NATIVE = r'''
from app.config import get_settings
from app.services.owned_process import wsl_path
from app.services.codex_wsl_state import mounted_path
settings=get_settings();state_root=settings.codex_wsl_state_root
program=str(Path('\\\\wsl.localhost\\vibeflow-linux\\usr\\bin\\python3.12'))
def native_command(credential=None,*,distro=None):
    return program,[mounted_path(program,distro),'-I','-S','-u',wsl_path(root/'cli.py'),mode,wsl_path(root),wsl_path(Path(records.identity.__file__))]
sessions.native_command=native_command
@app.post('/api/v1/fixture/native-state/{kind}')
async def native_state(kind:str,user=Depends(get_current_user)):
    settings.codex_wsl_state_root='' if kind=='missing' else state_root+'/changed' if kind=='changed' else state_root
    return {'ok':True}
@app.get('/api/v1/fixture/profile/{identifier}')
async def profile(identifier:str,user=Depends(get_current_user)):
    row=await sessions.row(identifier,user.id)
    return json.loads(row.profile_json)
'''


def run_database(url, folder):
    folder.mkdir(parents=True)
    if url:alembic(url,folder,'upgrade','head')
    api=IsolatedServer('cli-wsl-session',app='native_controlled:app',database_url=url)
    (api.root/'cli.py').write_text(WORKER.replace("'platformFamily':'windows','platformOs':'windows'", "'platformFamily':'unix','platformOs':'linux'"),encoding='utf8')
    (api.root/'native_controlled.py').write_text(CONTROLLED+NATIVE,encoding='utf8')
    checks=[];receipts=[]
    with NativeStateFixture(),api:
        case=api.create_case('WSL admission')
        conv=api.call('POST','/conversations',{'case_id':case['id'],'permission_mode':'plan'})
        body={'case_id':case['id'],'conversation_id':conv['id'],'runtime':'wsl'}
        def create(mode='normal',expect=200):
            api.call('POST','/fixture/mode/'+mode)
            return api.call('POST','/codex/sessions',body,expect=expect)
        value=create();path='/codex/sessions/'+value['id']
        assert value['runtime']=='wsl' and value['process']['root']['native']['process']['uid']==65534
        assert value['process']['bridge']['platform']=='linux' and value['process']['bridge']['uid']==65534
        selected=api.call('GET','/fixture/profile/'+value['id'])
        assert selected['distro']=='vibeflow-linux' and selected['transport']['native_probe_sha256']
        assert selected['auth_home'].startswith('\\\\wsl.localhost\\vibeflow-linux\\tmp\\vibeflow-cli-fixture-')
        checks.append('durable_windows_root_and_linux_bridge_births_with_private_native_state')
        assert api.call('GET',path+'/sandbox')=={'runtime':'wsl','legacy_readiness':'not_applicable','mxc_availability':'not_applicable','setup_authority':'deployment_operator'}
        checks.append('wsl_does_not_call_windows_sandbox_rpc')
        assert api.call('POST',path+'/thread')['thread_id']
        api.call('POST',path+'/thread',expect=409)
        receipts.append(api.call('DELETE',path));assert receipts[-1]['cleanup_verified']
        checks.append('native_thread_admission_and_verified_close')
        for patch in ({'runtime':'linux'},{'distro':'foreign'},{'state_root':'/tmp/arbitrary'},{'program':'/bin/sh'}):
            api.call('POST','/codex/sessions',{**body,**patch},expect=422)
        checks.append('http_cannot_supply_distribution_private_directory_or_executable')
        api.call('POST','/fixture/native-state/missing')
        create(expect=503)
        api.call('POST','/fixture/native-state/restore')
        checks.append('unprovisioned_native_state_returns_actionable_503')
        home=Path(selected['auth_home'])
        for name in ('config.toml','../owner.json'):
            file=home/name;original=file.read_bytes()
            try:
                file.write_text('{}' if name.endswith('.json') else '# changed\n',encoding='utf8')
                create(expect=502)
            finally:file.write_bytes(original)
            checks.append('changed_'+('owner' if name.endswith('.json') else 'configuration')+'_refuses_adoption')
        for mode in ('root_commit','bridge_commit','bad_birth','bad_nonce','bad_init','activate_commit'):
            before={p.name for p in api.root.glob('cli-*.json')}
            create(mode,expect=502)
            states=api.call('GET','/codex/sessions')
            state=next(x for x in states if x['case_id']==case['id'])
            assert state['phase']=='failed' and state['cleanup_verified'],(mode,state)
            if mode not in {'bad_init','activate_commit'}:
                assert {p.name for p in api.root.glob('cli-*.json')}==before
            receipts.append(state)
            checks.append(mode+'_native_failure_waits_verified_exit')
        value=create();path='/codex/sessions/'+value['id']
        api.call('POST','/fixture/native-state/changed')
        api.call('GET',path+'/account',expect=409)
        api.call('POST','/fixture/native-state/restore')
        state=wait_for(lambda:api.call('GET',path),lambda r:r['phase']=='failed' and r['cleanup_verified'])
        assert state['phase']=='failed' and state['cleanup_verified']
        checks.append('native_state_configuration_change_revokes_live_session')
        value=create();path='/codex/sessions/'+value['id']
        (api.root/'runtime.json').write_text(json.dumps({'wsl_distro':'openfoam2406-default'}),encoding='utf8')
        api.call('GET',path+'/models',expect=409)
        (api.root/'runtime.json').write_text('{}',encoding='utf8')
        assert wait_for(lambda:api.call('GET',path),lambda r:r['cleanup_verified'])['phase']=='failed'
        checks.append('distribution_change_revokes_live_session')
        value=create();path='/codex/sessions/'+value['id']
        original=api.token;api.create_case('Foreign native user')
        api.call('GET',path+'/sandbox',expect=404);api.call('DELETE',path,expect=404)
        api.token=original;receipts.append(api.call('DELETE',path))
        assert receipts[-1]['cleanup_verified']
        checks.append('native_session_cannot_be_read_or_stopped_by_another_user')
    result={'passed':True,'count':len(checks),'checks':checks,'actual_wsl':True,'actual_codex':False,'evidence':str(api.root)}
    (api.root/'native-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
    (folder/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    root=Path(tempfile.mkdtemp(prefix='cli-wsl-session-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(run_database,None,root/'sqlite')}
    if args.postgres:
        import asyncpg
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.host in {'127.0.0.1','localhost','::1'} and url.drivername=='postgresql+asyncpg'
        name='tmp_vf_cliwsl_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_cliwsl_[0-9a-f]{32}',name)
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
