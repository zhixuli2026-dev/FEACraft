"""Personal organization and plan provisioning across two real API processes."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import uuid

from live_llm_terminal_agent_acceptance import disposable_database
from testsupport import IsolatedServer, SERVER


FIXTURE = '''
import asyncio
from fastapi import Depends
from sqlalchemy import select
from app.main import app
from app.db import get_db
from app.deps import get_current_user
from app.models import Membership, Plan, Subscription, User, Workspace
from app.routers.auth import hash_password

@app.post('/fixture/legacy-user')
async def legacy_user(body:dict,db=Depends(get_db)):
    user=User(email=body['email'],name='Legacy org fixture',
              password_hash=await asyncio.to_thread(hash_password,body['password']))
    db.add(user)
    await db.flush()
    db.add(Workspace(user_id=user.id,name='Default'))
    await db.commit()
    return {'user_id':user.id}

@app.get('/fixture/bootstrap-records')
async def bootstrap_records(user=Depends(get_current_user),db=Depends(get_db)):
    roles=(await db.execute(select(Membership.org_id,Membership.role).where(Membership.user_id==user.id))).all()
    subscriptions=(await db.execute(select(Subscription.org_id,Plan.code,Subscription.status)
        .join(Plan,Plan.id==Subscription.plan_id).where(Subscription.org_id.in_([org_id for org_id,_ in roles])))).all()
    plans=(await db.scalars(select(Plan.code).order_by(Plan.code))).all()
    return {'memberships':[list(row) for row in roles],'subscriptions':[list(row) for row in subscriptions],'plans':plans}
'''


def prepare(api, sqlite):
    env = {'VIBEFLOW_DATABASE_URL': sqlite} if sqlite else {}
    (api.root/'org_bootstrap_fixture.py').write_text('import os\nos.environ.update('+repr(env)+')\n'+FIXTURE, encoding='utf8')


def verify(database):
    api = IsolatedServer('org-bootstrap',app='org_bootstrap_fixture:app',database_url=database)
    peer = IsolatedServer('org-bootstrap-peer',app='org_bootstrap_fixture:app',database_url=database)
    sqlite = None if database else 'sqlite+aiosqlite:///'+(api.root/'org-contract.sqlite').as_posix()
    for service in (api,peer):prepare(service,sqlite)
    report = {'passed':False,'backend':'postgresql' if database else 'sqlite',
              'actual_model_requests':0,'mock_llm_calls':0,'checks':{}}
    print('ORG_BOOTSTRAP_EVIDENCE',api.root,flush=True)

    def account(label):
        return {'email':label+'-'+uuid.uuid4().hex+'@vibeflow.cn','password':'org-bootstrap-fixture-123'}

    def login(service,credentials):
        service.token=service.call('POST','/auth/login',credentials)['token']

    def organization(service):
        return service.call('GET','/org/mine')

    def singleton(service, org):
        value=service.call('GET','/fixture/bootstrap-records',api_prefix=False)
        assert value['memberships']==[[org,'admin']] and value['subscriptions']==[[org,'free','active']], value
        assert value['plans'].count('free')==1, value
        return value

    try:
        with api,peer,ThreadPoolExecutor(max_workers=2) as pool:
            left,right=account('left'),account('right')
            signups=[pool.submit(service.call,'POST','/auth/register',credentials)
                     for service,credentials in ((api,left),(peer,right))]
            for service,signup in zip((api,peer),signups):service.token=signup.result(timeout=35)['token']
            orgs=[organization(service) for service in (api,peer)]
            assert orgs[0]['org']['id']!=orgs[1]['org']['id']
            assert all(value['plan']['code']=='free' and value['usage']['quotas']['seats']==2 for value in orgs)
            for service,value in zip((api,peer),orgs):singleton(service,value['org']['id'])
            report['checks']['first_registrations']={'api_launcher_pids':[api.proc.pid,peer.proc.pid],
                                                     'organizations':[row['org']['id'] for row in orgs]}
            plans=peer.call('GET','/org/plans')
            assert {row['code'] for row in plans}=={'free','pro','team'}
            peer.call('GET',f'/org/{orgs[0]["org"]["id"]}/billing',expect=403)

            legacy=account('legacy')
            api.call('POST','/fixture/legacy-user',legacy,api_prefix=False)
            for service in (api,peer):login(service,legacy)
            before=api.call('GET','/fixture/bootstrap-records',api_prefix=False)
            assert not before['memberships'] and not before['subscriptions'], before
            futures=[pool.submit(organization,service) for service in (api,peer)]
            backfills=[future.result(timeout=35) for future in futures]
            report['checks']['legacy_backfill_responses']=[{'organization':row['org']['id'],
                'plan':row['plan']['code']} for row in backfills]
            org=backfills[0]['org']['id']
            assert backfills[1]['org']['id']==org and all(row['plan']['code']=='free' for row in backfills)
            report['checks']['concurrent_legacy_backfill']=singleton(api,org)

        with api:
            login(api,legacy)
            assert organization(api)['org']['id']==org
            report['checks']['after_api_restart']=singleton(api,org)
        report['passed']=True
    except BaseException as error:
        report['failure_type']=type(error).__name__
        raise
    finally:
        report['api_exited']=api.proc is not None and api.proc.poll() is not None
        report['peer_exited']=peer.proc is not None and peer.proc.poll() is not None
        (api.root/'org-bootstrap-result.json').write_text(json.dumps(report,indent=2),encoding='utf8')


async def main(postgres):
    async with disposable_database(postgres) as database:
        await asyncio.to_thread(verify,database)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres',action='store_true')
    asyncio.run(main(parser.parse_args().postgres))
