"""Real HTTP history pagination; synthetic failed reservations, not report bytes."""
import json
from testsupport import IsolatedServer

FIXTURE='''
from fastapi import Depends
from sqlalchemy import select,func
from app.main import app
from app.db import SessionLocal
from app.models import User,ArtifactPublication
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import job_execution
from app.services.case_write import coordinate
@app.post('/api/v1/fixture/history/{case_id}')
async def seed(case_id:int,data:dict,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db)
  async with coordinate(db,case):
   from app.models import JobAttempt
   spec={'command':'history metadata fixture','runtime':'windows','argv':None,'case_prefix':case.s3_prefix,
     'timeout_seconds':30,'max_log_bytes':1048576,'evidence_run_dir':None}
   job,_=await job_execution.submit(db,case_id=case.id,user_id=user.id,key=None,
     digest=job_execution.request_hash(spec),job_type='shell',command=spec['command'],spec=spec,input_paths=['Ref/source.txt'])
   attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
   latest=await db.scalar(select(func.max(ArtifactPublication.generation)).where(ArtifactPublication.case_id==case.id,ArtifactPublication.kind=='report')) or 0
   for generation in range(latest+1,latest+1+data['count']):
    db.add(ArtifactPublication(case_id=case.id,kind='report',generation=generation,attempt_id=attempt.id,
      status='failed',immutable_path='',manifest_sha256=''))
   db.add(ArtifactPublication(case_id=case.id,kind='geometry',generation=latest+1,attempt_id=attempt.id,
      status='failed',immutable_path='',manifest_sha256=''))
  await db.commit()
  return {'created':data['count']}
'''


def main():
    checks=[]
    api=IsolatedServer('report-history',app='history_fixture:app')
    (api.root/'history_fixture.py').write_text(FIXTURE,encoding='utf8')
    with api:
        api.create_case('paged report history')
        prefix=api.prefix
        assert api.call('GET',prefix+'/report/publications')==[]
        api.put('Ref/source.txt','metadata fixture input')
        api.call('POST',f'/fixture/history/{api.case_id}',{'count':55})
        first=api.call('GET',prefix+'/report/publications')
        assert [row['generation'] for row in first]==list(range(55,5,-1))
        assert all(row['status']=='failed' and row['current'] is False for row in first)
        checks.append('legacy_array_default_50_preserves_status_without_claiming_failed_reports_readable')
        api.call('POST',f'/fixture/history/{api.case_id}',{'count':1})
        second=api.call('GET',prefix+'/report/publications?before_generation=6')
        assert [row['generation'] for row in second]==[5,4,3,2,1]
        assert len({row['id'] for row in first+second})==55
        checks.append('older_than_50_reachable_without_duplicates_when_new_generation_arrives')
        assert api.call('GET',prefix+'/report/publications?before_generation=1')==[]
        assert [r['generation'] for r in api.call('GET',prefix+'/report/publications?limit=1')]==[56]
        assert len(api.call('GET',prefix+'/report/publications?limit=100'))==56
        checks.append('stable_channel_scoped_cursor_terminal_page_and_bounded_page_size')
        for query in ('limit=0','limit=101','before_generation=0','before_generation=bad'):
            api.call('GET',prefix+'/report/publications?'+query,expect=422)
        checks.append('invalid_cursors_and_unbounded_limits_rejected')
        token=api.token
        auth=api.call('POST','/auth/register',{'email':'history-other@example.com','name':'Other',
            'password':'history-isolated-123'})
        api.token=auth['token']
        api.call('GET',prefix+'/report/publications?limit=100',expect=404)
        api.token=token
        api.create_case('empty history')
        assert api.call('GET',api.prefix+'/report/publications')==[]
        checks.append('foreign_owner_denied_and_other_case_history_isolated')
    result={'passed':True,'count':len(checks),'checks':checks,
        'scope':'HTTP list pagination only; failed metadata fixtures do not prove artifact readability'}
    (api.root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('REPORT_HISTORY_OK '+json.dumps({'evidence':str(api.root),**result}),flush=True)


if __name__=='__main__':main()
