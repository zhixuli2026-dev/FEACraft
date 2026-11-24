"""Real HTTP multi-channel pagination and legacy source-version rejection.

Failed reservations are metadata fixtures; native PDF/SyncTeX proof is separate.
"""
import json
from urllib.parse import quote
from testsupport import IsolatedServer

FIXTURE='''
import json
from datetime import datetime,timezone
from fastapi import Depends
from sqlalchemy import select,func
from app.main import app
from app.db import SessionLocal
from app.models import User,ArtifactPublication,JobAttempt
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services import job_execution,latex_artifacts
from app.services.case_write import coordinate
@app.post('/api/v1/fixture/latex-history/{case_id}')
async def seed(case_id:int,data:dict,user:User=Depends(get_current_user)):
 async with SessionLocal() as db:
  case=await get_owned_case(case_id,user,db)
  async with coordinate(db,case):
   spec={'command':'history metadata fixture','runtime':'windows','argv':None,'case_prefix':case.s3_prefix,
     'timeout_seconds':30,'max_log_bytes':1048576,'evidence_run_dir':None}
   job,_=await job_execution.submit(db,case_id=case.id,user_id=user.id,key=None,
     digest=job_execution.request_hash(spec),job_type='shell',command=spec['command'],spec=spec,input_paths=['Ref/source.txt'])
   attempt=await db.scalar(select(JobAttempt).where(JobAttempt.job_id==job.id))
   attempt.publication_json=json.dumps({'descriptor':{'main':data['main'],'recipe':'pdflatex'}})
   kind=latex_artifacts.channel(data['main'])
   latest=await db.scalar(select(func.max(ArtifactPublication.generation)).where(ArtifactPublication.case_id==case.id,ArtifactPublication.kind==kind)) or 0
   for generation in range(latest+1,latest+1+data['count']):
    db.add(ArtifactPublication(case_id=case.id,kind=kind,generation=generation,attempt_id=attempt.id,
      status='failed',immutable_path='',manifest_sha256='',created_at=datetime(2026,1,1,tzinfo=timezone.utc)))
  await db.commit();return {'created':data['count']}
'''


def main():
    checks=[];api=IsolatedServer('latex-history',app='history_fixture:app')
    (api.root/'history_fixture.py').write_text(FIXTURE,encoding='utf8')
    with api:
        api.create_case('latex history');prefix=api.prefix
        api.put('Ref/source.txt','metadata fixture')
        first_main='E-report/main.tex';other_main='Ref/other.tex'
        for source,count in ((first_main,55),(other_main,3)):
            api.call('POST',f'/fixture/latex-history/{api.case_id}',{'main':source,'count':count})
        all_rows=api.call('GET',prefix+'/latex/publications?limit=100');assert len(all_rows)==58
        expected=sorted([r['id'] for r in all_rows],reverse=True)
        assert [r['id'] for r in all_rows]==expected
        checks.append('same_timestamp_ties_have_total_order_across_channels')
        seen=[];cursor=None
        while True:
            rows=api.call('GET',prefix+'/latex/publications?limit=11'+('&before_id='+cursor if cursor else ''))
            if not rows:break
            seen.extend(r['id'] for r in rows);cursor=rows[-1]['id']
        assert seen==expected and len(set(seen))==58
        checks.append('all_channels_pagination_reaches_beyond_50_without_duplicates')
        query=prefix+'/latex/publications?main='+quote(first_main)+'&limit=100'
        rows=api.call('GET',query);assert len(rows)==55 and all(r['main']==first_main for r in rows)
        cursor=rows[20]['id'];older=api.call('GET',query+'&before_id='+cursor)
        assert [r['id'] for r in older]==[r['id'] for r in rows[21:]]
        other=next(r for r in all_rows if r['main']==other_main)
        api.call('GET',query+'&before_id='+other['id'],expect=400)
        api.call('GET',query+'&before_id=missing',expect=400)
        checks.append('filtered_history_cursor_cannot_cross_main_channel_or_unknown_id')
        for suffix in ('limit=0','limit=101','before_id='+'x'*37):
            api.call('GET',prefix+'/latex/publications?'+suffix,expect=422)
        owner=api.token
        other_user=api.call('POST','/auth/register',{'email':'latex-other@example.com','name':'Other','password':'isolated-latex-123'})
        api.token=other_user['token'];api.call('GET',query,expect=404);api.token=owner
        api.create_case('different case');api.put('Ref/source.txt','other fixture')
        api.call('GET',api.prefix+'/latex/publications?before_id='+cursor,expect=400)
        checks.append('bounds_owner_and_cross_case_cursor_rejected')
        api.put('Ref/legacy.pdf','%PDF metadata fixture; must reject before parsing or native execution')
        payload={'pdf':'Ref/legacy.pdf','page':1,'x':10,'y':10,'expected_file_version':'0'*64}
        refused=api.call('POST',api.prefix+'/synctex:query',payload,expect=409)
        assert refused['detail']['code']=='file_changed_during_read'
        api.call('POST',api.prefix+'/synctex:query',{**payload,'publication_id':'other'},expect=422)
        checks.append('legacy_source_jump_rejects_changed_display_version_before_native_lookup')
    result={'passed':True,'count':len(checks),'checks':checks,'scope':'metadata pagination and stale legacy rejection; no native PDF proof'}
    (api.root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print('LATEX_HISTORY_OK '+json.dumps({'evidence':str(api.root),**result}),flush=True)


if __name__=='__main__':main()
