"""Actual VTK renderer resource limits on local and authenticated S3 workspaces."""
import hashlib
import json
from contextlib import nullcontext
from pathlib import Path
import sys
import time

from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER
from viz_execution_smoke import STL


FIXTURE = '''
from pathlib import Path
from fastapi import Depends, HTTPException
from app.config import get_settings
from app.deps import get_current_user
from app.main import app
from app.routers import viz
from app.services import process_identity_native as identity

root = Path(__file__).parent
original_finalize = viz._finalize

async def observed_finalize(session):
    workdir = Path(session.selected['case_root'])
    assert workdir.is_relative_to(root/'files')
    log = workdir/'.vibeflow'/'viz'/f'viz-{session.port}.log'
    budget = session._budget
    if budget is not None:
        import json
        observation = {'log_bytes':log.stat().st_size if log.is_file() else None,
                       'reason':budget.violation,'workspace':budget.storage_observation}
        (root/f'observation-{session.id}.json').write_text(json.dumps(observation),encoding='utf8')
    await original_finalize(session)

viz._finalize = observed_finalize

@app.get('/fixture/process')
def process(user=Depends(get_current_user)):
    import os
    return identity.observe(os.getpid())

@app.post('/fixture/viz-budget')
async def configure(body:dict,user=Depends(get_current_user)):
    settings = get_settings()
    settings.viz_session_lifetime_seconds = body.get('seconds',0)
    settings.viz_session_max_output_bytes = body.get('log',0)
    settings.viz_session_workspace_limit_mb = body.get('workspace',0)
    return {'ok':True}

@app.post('/fixture/viz-grow/{sid}')
async def grow(sid:int,user=Depends(get_current_user)):
    session=viz._sessions.get(sid)
    if session is None or session.user_id!=user.id or session.alive is not True:
        raise HTTPException(404)
    workdir=Path(session.selected['case_root'])
    assert workdir.is_relative_to(root/'files')
    target=workdir/'Ref'/'runtime-budget-growth.bin'
    with target.open('xb') as file:
        for _ in range(2):file.write(bytes(1024*1024))
    return {'bytes':target.stat().st_size}
'''

def wait_until(action,predicate,seconds=35):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        latest=action()
        if predicate(latest):return latest
        time.sleep(.1)
    raise AssertionError('Actual rendering state did not change before the test deadline')


def run(backend):
    from app.services import process_identity_native as identity

    api=IsolatedServer('viz-runtime-budget-'+backend,app='viz_budget_fixture:app')
    report={'passed':False,'backend':backend,'api_port':api.port,'actual_model_calls':0,'mock_llm_calls':0,
            'checks':{},'api_exits':[]}
    evidence=api.root/'viz-runtime-budget.json'
    fixture=api.root/'viz_budget_fixture.py'
    runtime=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    context=ObjectStore(executable=runtime,volume_max=8,authenticated=True) if backend=='s3' else nullcontext()
    print('VIZ_RUNTIME_BUDGET_EVIDENCE',api.root,flush=True)
    try:
        with context as store:
            settings=store.settings() if store else {}
            if store:report['store_receipt']=str(store.root/'store.json')
            temp=api.root/'native-temp';temp.mkdir()
            settings.update(TEMP=str(temp),TMP=str(temp),TMPDIR=str(temp),
                            VIBEFLOW_EXECUTION_STORAGE_POLL_SECONDS='.1',VIBEFLOW_NODE_STORAGE_MIN_FREE_MB='0')
            fixture.write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,encoding='utf8')

            def config(**limits):
                api.call('POST','/fixture/viz-budget',limits,api_prefix=False)

            def launch(expect=200):
                return api.call('POST',api.prefix+'/viz:sessions',{'file_path':'D-post/cube.stl'},expect=expect)

            def history():
                return api.call('GET',api.prefix+'/viz:sessions?include_closed=true')

            def session(sid):
                return api.call('GET',f'/viz:sessions/{sid}')

            def stopped(sid,reason):
                value=wait_until(lambda:session(sid),lambda row:row['phase']=='failed' and not row['finalizing'])
                assert value['error']=='VizResourceLimit' and value['cleanup_verified'] is True, value
                assert value['stop']['reason']==reason and value['stop']['scope_empty'], value
                assert identity.probe(value['process']['host'])['state']=='exited', value
                if value['ready']:
                    assert identity.probe(value['ready']['process'])['state']=='exited', value
                observation=json.loads((api.root/f'observation-{sid}.json').read_text(encoding='utf8'))
                assert observation['reason']==reason, observation
                report['checks'][reason]={'row':value,'observation':observation}
                return observation

            with api:
                api.create_case('Renderer resource budget')
                first=api.call('GET','/fixture/process',api_prefix=False)
                email=api.call('GET','/auth/me')['email']
                api.put('D-post/cube.stl',STL)
                config(log=128)
                error=launch(409)
                assert error['detail']['code']=='viz_log_limit',error
                log=stopped(history()[-1]['id'],'log_limit')
                assert log['log_bytes']==128,log

                config(workspace=1)
                active=launch();assert active['phase']=='active' and active['ready']
                assert api.call('POST',f"/fixture/viz-grow/{active['id']}",api_prefix=False)=={'bytes':2097152}
                disk=stopped(active['id'],'disk_limit')
                assert disk['workspace']['peak_bytes']>=2097152 and disk['workspace']['samples']>=2, disk
                raw=api.call('GET',api.prefix+'/raw?path=Ref/runtime-budget-growth.bin',raw=True)
                assert len(raw)==2097152 and hashlib.sha256(raw).digest()==hashlib.sha256(bytes(2097152)).digest()
                error=launch(409)
                assert error['detail']['code']=='viz_disk_limit',error
                rejected=history()[-1]
                assert rejected['phase']=='failed' and rejected['error']=='VizResourceLimit' and rejected['process'] is None, rejected
                report['checks']['disk_admission']={'http':409,'no_root':True}

                config(seconds=15)
                active=launch();assert active['phase']=='active' and active['ready']
                page=api.call('GET',f"/viz-proxy/{active['id']}/index.html",api_prefix=False,raw=True)
                assert b'trame' in page.lower()
                config()  # The existing session keeps its originally selected lifetime.
                stopped(active['id'],'timeout')
                fresh=launch();assert fresh['phase']=='active' and fresh['ready']
                api.call('DELETE',f"/viz:sessions/{fresh['id']}")
                assert session(fresh['id'])['cleanup_verified'] is True
                report['checks']['reopen_after_limit']={'real_trame':True,'cleanup_verified':True}
            report['api_exits'].append(wait_until(lambda:identity.probe(first),lambda state:state['state']=='exited',10))
            with api:
                api.token=api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})['token']
                second=api.call('GET','/fixture/process',api_prefix=False)
                for reason in ('log_limit','disk_limit','timeout'):
                    old=report['checks'][reason]['row']
                    archived=session(old['id'])
                    assert archived['stop']==old['stop'] and archived['error']==old['error']
                    assert archived['owner_available'] is False
            report['api_exits'].append(wait_until(lambda:identity.probe(second),lambda state:state['state']=='exited',10))
            report['checks']['cold_replay']=True
        if store:assert store.proc.poll()==0
        report['passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)[:1400]}
        raise
    finally:
        fixture.unlink(missing_ok=True)
        evidence.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print('VIZ_RUNTIME_BUDGET_PASS',evidence,flush=True)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce

    enforce([Path(__file__)])
    run('fs')
    run('s3')
