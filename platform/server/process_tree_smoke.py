"""Scoped tree cleanup and durable stop evidence through isolated dual-database APIs."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import uuid

from testsupport import IsolatedServer,SERVER
from owned_tree_smoke import TREE,native_records
from app.services import process_identity_native as identity

CONTROLLED='''from pathlib import Path
from app.services.owned_process import OwnedProcess
from app.services.windows_process_lifetime import LifetimeObserver
root=Path(__file__).parent
original=OwnedProcess._control_reports
def reports(self):
    if (root/'bad-control').exists(): return [],False
    return original(self)
OwnedProcess._control_reports=reports
capture_member=LifetimeObserver._capture
def capture(self,pid):
    if (root/'bad-members').exists(): return
    capture_member(self,pid)
LifetimeObserver._capture=capture
from app.main import app
'''


def verify(database_url=None):
    checks=[];runs=[]
    def done(name): checks.append(name);print('PASS '+name,flush=True)
    server=IsolatedServer('process-tree',app='controlled_process_tree:app',database_url=database_url)
    (server.root/'controlled_process_tree.py').write_text(CONTROLLED,encoding='utf-8')
    with server as api:
        sentinel=subprocess.Popen([getattr(sys,'_base_executable',sys.executable),'-I','-S','-u','-c',
            'import sys; print("READY",flush=True); sys.stdin.buffer.read(1)'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        assert sentinel.stdout.readline().strip()==b'READY'
        def wait(job_id,*,ready=False):
            deadline=time.monotonic()+25
            while time.monotonic()<deadline:
                row=api.call('GET',f'/jobs/{job_id}')
                if ready and 'TREE_READY' in row['log_backlog']: return row
                if (not ready and row['attempt'].get('process_stop') is not None
                        and row['status'] not in {'queued','running'} and row['ended_at'] is not None): return row
                time.sleep(.04)
            raise TimeoutError(json.dumps(row))
        try:
            for runtime in ('windows','wsl'):
                for scenario in ('cooperate','ignore','detach','root_exit','timeout','logs'):
                    api.create_case(runtime+' '+scenario)
                    root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
                    mode='ignore' if scenario in {'timeout','logs'} else scenario
                    script=TREE
                    if scenario=='logs':
                        script=script.replace('for _ in range(2000):\n (root/',
                            "for _ in range(2000):\n if level==2 and (root/'release-logs').exists(): print('X'*8192,flush=True)\n (root/")
                    api.put('tree.py',script)
                    command=subprocess.list2cmdline([sys.executable,'-u','tree.py','2',mode]) if runtime=='windows' else 'exec python3 -u tree.py 2 '+mode
                    started=api.call('POST',api.prefix+'/jobs',{'type':'shell','command':command,'runtime':runtime,
                        'timeout_seconds':6 if scenario=='timeout' else 30,'max_log_bytes':1024 if scenario=='logs' else 8192})
                    row=wait(started['id'],ready=True)
                    record=row['attempt']['process_identity']
                    assert api.call('GET',f'/jobs/{row["id"]}/process')['state']=='match'
                    descendants=[identity.observe(int((root/('node-'+str(n))).read_text())) for n in range(3)] if runtime=='windows' else native_records({'root':record['native']['process']})
                    if scenario=='root_exit': (root/'release-root').touch()
                    elif scenario=='logs': (root/'release-logs').touch()
                    elif scenario!='timeout':
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            answers=list(pool.map(lambda _:api.call('POST',f'/jobs/{row["id"]}:cancel',{}),range(2)))
                        assert any(answer['ok'] for answer in answers),answers
                        assert all(answer['ok'] or 'cancelled' in answer.get('message','') for answer in answers),answers
                    final=wait(row['id'])
                    stop=final['attempt']['process_stop']
                    expected='succeeded' if scenario=='root_exit' else 'failed' if scenario in {'timeout','logs'} else 'cancelled'
                    assert final['status']==expected and stop['verified'] and stop['scope_empty'],final
                    observation=stop['scope_observation']
                    assert observation['complete'] and observation['pending']==0
                    assert observation['total']==observation['observed'],observation
                    assert stop['reason']=={'root_exit':'root_exit','timeout':'timeout','logs':'log_limit'}.get(scenario,'cancelled'),stop
                    assert stop['launch_nonce']==record['launch_nonce'] and stop['completed_at']
                    if scenario in {'ignore','timeout','logs'}: assert stop['forced'],stop
                    if scenario=='cooperate': assert (root/'stopped-2').exists()
                    observations=[identity.probe(item) for item in descendants] if runtime=='windows' else native_records({'probe':descendants})
                    assert all(item['state']=='exited' for item in observations),observations
                    assert sentinel.poll() is None
                    assert api.call('GET',f'/jobs/{row["id"]}/process')['state']=='exited'
                    assert api.call('GET',f'/jobs/{row["id"]}')['attempt']['process_stop']==stop
                    if scenario=='logs': assert (root/f'.vibeflow/jobs/job-{row["id"]}.log').stat().st_size<=1024
                    runs.append({'runtime':runtime,'scenario':scenario,'job_id':row['id'],'stop':stop,'descendants':descendants})
                    done(runtime+'_'+scenario+'_waits_for_observed_tree_exit_and_commits_cleanup_evidence')
            api.create_case('Bad control completion')
            (api.root/'bad-control').touch()
            row=api.call('POST',api.prefix+'/jobs',{'command':'echo NORMAL_ZERO_EXIT','runtime':'windows'})
            failed=wait(row['id'])
            assert failed['status']=='failed' and failed['attempt']['reason_code']=='process_cleanup_failed',failed
            assert not failed['attempt']['process_stop']['verified']
            (api.root/'bad-control').unlink()
            done('invalid_control_completion_cannot_report_native_success')
            api.create_case('Missing membership completion')
            (api.root/'bad-members').touch()
            row=api.call('POST',api.prefix+'/jobs',{'command':'echo NORMAL_ZERO_EXIT','runtime':'windows'})
            failed=wait(row['id'])
            assert failed['status']=='failed' and failed['attempt']['reason_code']=='process_cleanup_failed',failed
            stop=failed['attempt']['process_stop']
            assert not stop['verified'] and not stop['scope_observation']['complete'],stop
            assert stop['scope_observation']['total']>stop['scope_observation']['observed']
            (api.root/'bad-members').unlink()
            done('missing_lifetime_notifications_fail_closed_and_persist_cleanup_failure')
            api.create_case('Cancelled with unverified cleanup')
            root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
            api.put('tree.py',TREE)
            (api.root/'bad-members').touch()
            row=api.call('POST',api.prefix+'/jobs',{'command':subprocess.list2cmdline([sys.executable,'-u','tree.py','2','ignore']),
                'runtime':'windows','timeout_seconds':30})
            wait(row['id'],ready=True)
            descendants=[identity.observe(int((root/('node-'+str(n))).read_text())) for n in range(3)]
            assert api.call('POST',f'/jobs/{row["id"]}:cancel',{})['ok']
            failed=wait(row['id'])
            stop=failed['attempt']['process_stop']
            assert failed['status']=='cancelled' and not stop['verified'] and stop['error'],failed
            assert not stop['scope_observation']['complete']
            assert all(identity.probe(item)['state']=='exited' for item in descendants)
            assert api.call('GET',f'/jobs/{row["id"]}')['attempt']['process_stop']==stop
            assert sentinel.poll() is None
            (api.root/'bad-members').unlink()
            done('accepted_cancel_preserves_unverified_cleanup_and_cannot_become_success')
        finally:
            sentinel.communicate(b'x',timeout=5)
        result={'passed':True,'count':len(checks),'checks':checks,'runs':runs,'evidence':str(api.root)}
        (api.root/'process-tree-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='process-tree-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_tree_'+uuid.uuid4().hex
        assert re.fullmatch(r'tmp_vf_tree_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
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
    print('PROCESS_TREE_OK '+json.dumps({'databases':list(results),'evidence':str(report)}))


if __name__=='__main__': asyncio.run(main())
