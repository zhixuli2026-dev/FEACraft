"""Two APIs race durable professional delivery after HTTP admission commits."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import time
import uuid

from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import alembic

CONTROLLED='''import asyncio,json
from pathlib import Path
from app.services import job_delivery,step_handlers
root=Path(__file__).parent
async def idle(stop):await stop.wait()
job_delivery.recover_forever=idle
original_launch=step_handlers.launch
async def launch(job_id):
 if (root/'hold-launch').exists():
  (root/'ready').write_text(json.dumps({'job_id':job_id}))
  while not (root/'release').exists():await asyncio.sleep(.02)
 return await original_launch(job_id)
step_handlers.launch=launch
original_capacity=job_delivery.require_capacity
async def capacity(*args,**kwargs):
 if (root/'defer').exists():raise job_delivery.DeliveryDeferred('Controlled capacity occupied after admission')
 return await original_capacity(*args,**kwargs)
job_delivery.require_capacity=capacity
from fastapi import Depends,HTTPException
from sqlalchemy import select,func
from app.main import app
from app.db import get_db
from app.deps import get_current_user
from app.models import Job,JobAttempt
from app.routers.workspaces import get_owned_case
@app.post('/fixture/deliver/{job_id}')
async def deliver(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,job_id)
 if job is None:raise HTTPException(404)
 await get_owned_case(job.case_id,user,db)
 await db.rollback()
 return {'delivery':await job_delivery.deliver(job_id)}
@app.get('/fixture/jobs/{job_id}')
async def counts(job_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 job=await db.get(Job,job_id)
 if job is None:raise HTTPException(404)
 await get_owned_case(job.case_id,user,db,write=False)
 return {'jobs':await db.scalar(select(func.count()).select_from(Job).where(Job.case_id==job.case_id)),
  'attempts':await db.scalar(select(func.count()).select_from(JobAttempt).where(JobAttempt.job_id==job_id))}
'''


def wait_file(path):
    deadline=time.monotonic()+90
    while not path.exists():
        if time.monotonic()>deadline:raise TimeoutError('Admission did not reach committed launch barrier')
        time.sleep(.03)


def verify(database_url=None):
    checks=[]
    api=IsolatedServer('registered-delivery-race',app='delivery_fixture:app',database_url=database_url)
    (api.root/'delivery_fixture.py').write_text(CONTROLLED,encoding='utf8')
    with api:
        peer=IsolatedServer('registered-delivery-peer',app='delivery_fixture:app')
        settings={key:api.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
        (peer.root/'delivery_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+CONTROLLED,encoding='utf8')
        # A terminal-before-response scenario intentionally waits for the real
        # native result while the original HTTP request is still outstanding.
        opening=api.opener.open
        api.opener.open=lambda request,timeout=30:opening(request,timeout=180)
        with peer:
            for kind,mode in [('report','running'),('report','terminal'),('report','cancelled'),
                    ('report','deferred'),('compose','running'),('geometry','running'),('latex','running')]:
                api.create_case(kind+' '+mode);peer.token=api.token
                root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
                (root/'E-report/report.md').write_text('# Owned delivery\n\nA real native document.\n',encoding='utf8')
                if kind=='geometry':
                    from geometry_publication_smoke import script
                    (root/'A-geo/geometry.py').write_text(script(2),encoding='utf8')
                if kind=='latex':
                    (root/'E-report/main.tex').write_text(r'\documentclass{article}\begin{document}Owned delivery\end{document}',encoding='utf8')
                suffix,body={
                    'report':('/report:build',{}),'compose':('/report:compose?wait=false',{'overwrite':True}),
                    'geometry':('/geometry:preview',{}),'latex':('/latex:build',{'main':'E-report/main.tex','recipe':'pdflatex'})}[kind]
                for name in ('ready','release'):(api.root/name).unlink(missing_ok=True)
                (api.root/'hold-launch').touch()
                key=kind+'-'+mode
                with ThreadPoolExecutor(max_workers=1) as pool:
                    request=pool.submit(api.call,'POST',api.prefix+suffix,body,extra_headers={'Idempotency-Key':key})
                    try:
                        wait_file(api.root/'ready');job_id=json.loads((api.root/'ready').read_text())['job_id']
                        if mode=='deferred':(api.root/'defer').touch()
                        else:
                            assert peer.call('POST',f'/fixture/deliver/{job_id}',api_prefix=False)['delivery']=='started'
                            if mode=='terminal':peer.wait_job(job_id)
                            elif mode=='cancelled':
                                peer.call('POST',f'/jobs/{job_id}:cancel',{})
                                peer.wait_job(job_id,expected='cancelled')
                    finally:
                        (api.root/'hold-launch').unlink(missing_ok=True)
                        (api.root/'release').touch()
                    submitted=request.result(timeout=180)
                assert submitted['job_id']==job_id
                if mode=='deferred':
                    assert submitted['status']=='queued',submitted
                    (api.root/'defer').unlink()
                    assert peer.call('POST',f'/fixture/deliver/{job_id}',api_prefix=False)['delivery']=='started'
                elif mode in {'terminal','cancelled'}:
                    assert submitted['status']==('succeeded' if mode=='terminal' else 'cancelled'),submitted
                assert api.call('POST',api.prefix+suffix,body,extra_headers={'Idempotency-Key':key})['job_id']==job_id
                assert peer.call('POST',f'/fixture/deliver/{job_id}',api_prefix=False)['delivery']=='already_owned_or_terminal'
                completed=peer.wait_job(job_id,expected='cancelled' if mode=='cancelled' else 'succeeded')
                assert peer.call('GET',f'/fixture/jobs/{job_id}',api_prefix=False)=={'jobs':1,'attempts':1}
                if mode!='cancelled':assert completed['attempt']['outputs']['publication']['status']=='committed'
                checks.append(kind+'_'+mode+'_returns_durable_job_without_duplicate_attempt')
                print('PASS '+checks[-1],flush=True)
        result={'passed':True,'count':len(checks),'checks':checks,'api':str(api.root),'peer':str(peer.root)}
        (api.root/'delivery-race-result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
        return result


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    import tempfile
    report=Path(tempfile.mkdtemp(prefix='registered-delivery-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_deliveryrace_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_deliveryrace_[0-9a-f]{32}',name)
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
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    print('REGISTERED_DELIVERY_RACE_OK '+json.dumps({'evidence':str(report),'databases':list(results)}),flush=True)


if __name__=='__main__':asyncio.run(main())
