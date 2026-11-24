"""Real S3 staging, SQL contention, partial unlink and committed timeout results."""
import asyncio,hashlib,json,time
from pathlib import Path
import aiohttp
from sqlalchemy import select,update
from sqlalchemy.ext.asyncio import create_async_engine
from app.models import CaseEventCounter,DurableEvent,NativeRequest,NativeSession

STAGES=('archive','inventory','plan','db_lock','intent_commit','second_archive','plan_write','digest','unlink','done_commit')
FIXTURE='''
import asyncio,json,time
from pathlib import Path
from contextlib import asynccontextmanager,contextmanager
from sqlalchemy.ext.asyncio import AsyncSession
from app.services import request_deadline as _deadline,sandbox_staging_cleanup as _cleanup
def _staging_case():
 p=Path('staging-deadline-case.json')
 return json.loads(p.read_text()) if p.exists() else None
def _staging_mark(stage,**facts):
 selected=_staging_case();armed=Path('staging-deadline-armed')
 if not selected or selected['stage']!=stage or not armed.exists():return False
 armed.unlink();pending=Path('staging-deadline-paused.pending')
 pending.write_text(json.dumps({'stage':stage,**facts}));pending.replace('staging-deadline-paused.json')
 return True
def _staging_pause(stage,**facts):
 if not _staging_mark(stage,**facts):return
 end=time.monotonic()+100
 while not Path('staging-deadline-resume').exists():
  if time.monotonic()>=end:raise TimeoutError('Actual staging callback was not resumed')
  time.sleep(.02)
async def _staging_async_pause(stage,**facts):
 if not _staging_mark(stage,**facts):return
 async with asyncio.timeout(100):
  while not Path('staging-deadline-resume').exists():await asyncio.sleep(.02)
_scope_staging=_deadline.scope
_archive_count=0
@asynccontextmanager
async def _staging_scope(seconds):
 global _archive_count
 selected=_staging_case();outer=_deadline.current() is None
 async with _scope_staging(selected['seconds'] if selected else seconds) as budget:
  if selected and outer:
   _archive_count=0
   Path('staging-deadline-start.json').write_text(json.dumps({'stage':selected['stage'],'deadline':budget.deadline}))
  yield budget
_deadline.scope=_staging_scope
_download_staging=_cleanup.recovery.download
@contextmanager
def _archive_staging(*args,**kwargs):
 global _archive_count
 with _download_staging(*args,**kwargs) as value:yield value
 _archive_count+=1
 if _archive_count==1:_staging_pause('archive')
 if _archive_count==2:_staging_pause('second_archive')
_cleanup.recovery.download=_archive_staging
_identity_staging=_cleanup.identity
def _observed_staging(path):
 value=_identity_staging(path);selected=_staging_case()
 if selected and Path(path)!=Path(selected['stage_root']) and Path(selected['stage_root']) in Path(path).parents:
  _staging_pause('inventory',entry=str(path))
 return value
_cleanup.identity=_observed_staging
_plan_staging=_cleanup.build_plan
def _built_staging(binding):
 value=_plan_staging(binding);_staging_pause('plan');return value
_cleanup.build_plan=_built_staging
_lock_staging=_cleanup.lock_case_execution
async def _locked_staging(db,case_id):
 _staging_mark('db_lock');return await _lock_staging(db,case_id)
_cleanup.lock_case_execution=_locked_staging
_write_staging=_cleanup.write_expected
def _written_staging(path,*args):
 value=_write_staging(path,*args)
 if path.parent.name==_cleanup.DIRECTORY:_staging_pause('plan_write',plan=str(path))
 return value
_cleanup.write_expected=_written_staging
_checking_staging=False
_check_staging=_cleanup.check_remaining
def _checked_staging(plan):
 global _checking_staging
 _checking_staging=True
 try:return _check_staging(plan)
 finally:_checking_staging=False
_cleanup.check_remaining=_checked_staging
_digest_staging=_cleanup.digest
def _digested_staging(*args):
 value=_digest_staging(*args)
 if _checking_staging:_staging_pause('digest')
 return value
_cleanup.digest=_digested_staging
_remove_staging=_cleanup.remove_entry
def _removed_staging(root,name,entry,*args):
 value=_remove_staging(root,name,entry,*args)
 if entry['kind']=='file':_staging_pause('unlink',removed=name)
 return value
_cleanup.remove_entry=_removed_staging
_event_staging=_cleanup.append_event
async def _event_queued_staging(db,case_id,kind,*args,**kwargs):
 value=await _event_staging(db,case_id,kind,*args,**kwargs)
 if kind in {_cleanup.INTENT,_cleanup.DONE}:db.info['staging_deadline_commit']='intent_commit' if kind==_cleanup.INTENT else 'done_commit'
 return value
_cleanup.append_event=_event_queued_staging
_commit_staging=AsyncSession.commit
async def _committed_staging(self):
 kind=self.info.pop('staging_deadline_commit',None)
 value=await _commit_staging(self)
 if kind:await _staging_async_pause(kind)
 return value
AsyncSession.commit=_committed_staging
'''


async def after_retirement(api,sid,rid,proof,continuation=None):
    report={'passed':False,'cases':[],'checks':[],'llm_calls':0,'mock_llm_calls':0,
        'database':'postgresql' if api.database_url else 'sqlite'}
    engine=create_async_engine(api.database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
    first=None;holder=None;transaction=None
    async def call(method,path,body=None,expect=200,api_prefix=True):
        async with aiohttp.ClientSession(headers={'Authorization':'Bearer '+api.token},timeout=aiohttp.ClientTimeout(total=120)) as client:
            async with client.request(method,api.base+('/api/v1' if api_prefix else '')+path,json=body) as response:
                raw=await response.text();assert response.status==expect,(response.status,raw[:1000])
                return json.loads(raw)
    async def rows():
        async with engine.connect() as db:
            return [tuple(row) for row in await db.execute(select(DurableEvent.id,DurableEvent.type,DurableEvent.payload_json)
                .where(DurableEvent.case_id==api.case_id,DurableEvent.type.in_(['terminal.staging_cleanup_started','terminal.staging_cleanup_completed']))
                .order_by(DurableEvent.type))]
    def record(name):
        report['checks'].append({'name':name,'kind':'actual_infrastructure'})
        print('STAGING_DEADLINE_PASS '+name,flush=True)
    try:
        async with engine.connect() as db:
            selected=json.loads(await db.scalar(select(NativeSession.profile_json).where(NativeSession.id==sid)))
            snapshot=json.loads(await db.scalar(select(NativeRequest.profile_json).where(NativeRequest.id==rid)))
        stage_root=api.root/'files'/selected['staging_prefix']
        manifest=stage_root.with_name(stage_root.name+'.manifest.json')
        baseline=json.loads(manifest.read_bytes());total=len(baseline['files']);assert total>1
        archive=Path(snapshot['root'])/'recovery.zip';retained=archive.read_bytes()
        image=Path(snapshot['image_root'])/'volume.ext4';image_hash=hashlib.sha256(image.read_bytes()).hexdigest()
        endpoint='/terminal/sessions/'+sid+'/staging-cleanup';body={'recovery_id':rid,'confirm':True}
        for stage in STAGES:
            for name in ('staging-deadline-start.json','staging-deadline-paused.json','staging-deadline-resume'):
                (api.root/name).unlink(missing_ok=True)
            (api.root/'staging-deadline-case.json').write_text(json.dumps({'stage':stage,'seconds':8,'stage_root':str(stage_root)}))
            (api.root/'staging-deadline-armed').write_text('pause an actual stage')
            if stage=='db_lock':
                holder=await engine.connect();transaction=await holder.begin()
                await holder.execute(update(CaseEventCounter).where(CaseEventCounter.case_id==api.case_id).values(next_seq=CaseEventCounter.next_seq))
            first=asyncio.create_task(call('POST',endpoint,body=body,expect=409))
            async with asyncio.timeout(30):
                while not (api.root/'staging-deadline-paused.json').exists():
                    assert not first.done(),await first if first.done() else 'Staging phase not reached'
                    await asyncio.sleep(.02)
            clock=json.loads((api.root/'staging-deadline-start.json').read_text())
            marker=json.loads((api.root/'staging-deadline-paused.json').read_text())
            case={'stage':stage,'clock':clock,'marker':marker};report['cases'].append(case)
            if stage in {'archive','inventory','plan','second_archive'}:
                started=time.monotonic()
                async with asyncio.timeout(3):
                    async with engine.begin() as db:
                        await db.execute(update(CaseEventCounter).where(CaseEventCounter.case_id==api.case_id).values(next_seq=CaseEventCounter.next_seq))
                case['competing_write_seconds']=time.monotonic()-started
            if stage=='plan_write':assert Path(marker['plan']).is_file()
            if stage=='unlink':assert not (stage_root/marker['removed']).exists()
            await asyncio.sleep(max(0,clock['deadline']-time.monotonic()+.3))
            if stage=='db_lock':
                await transaction.rollback();await holder.close();transaction=None;holder=None
            else:
                assert not first.done(),'Actual protected callback was abandoned'
                if stage in {'intent_commit','done_commit'}:
                    case['committed_query']=await call('GET',endpoint)
                    assert case['committed_query']['status']==('completed' if stage=='done_commit' else 'pending')
                (api.root/'staging-deadline-resume').write_text('allow actual callback to finish')
            case['response']=await first
            assert case['response']['detail']['code']=='staging_cleanup_incomplete'
            events=await rows();case['events']=events
            done=[v for v in events if v[1]=='terminal.staging_cleanup_completed']
            intents=[v for v in events if v[1]=='terminal.staging_cleanup_started']
            if stage in {'archive','inventory','plan','db_lock'}:assert not events
            else:assert len(intents)==1
            survivors=[p for p in stage_root.rglob('*') if p.is_file()] if stage_root.exists() else []
            case['remaining_files']=len(survivors)
            if stage=='done_commit':assert len(done)==1 and not stage_root.exists()
            else:
                assert not done
                assert len(survivors)==total-(1 if stage=='unlink' else 0)
            assert archive.read_bytes()==retained and hashlib.sha256(image.read_bytes()).hexdigest()==image_hash
            (api.root/'staging-deadline-case.json').unlink()
            (api.root/'staging-deadline-armed').unlink(missing_ok=True)
            record(stage+'_shared_deadline_and_actual_state_retained')
        original=await rows();finished=await call('POST',endpoint,body=body)
        assert finished==report['cases'][-1]['committed_query'] and await rows()==original
        assert await call('GET',endpoint)==finished
        await call('GET',f'/fixture/{api.case_id}/admin',api_prefix=False)
        record('one_committed_result_and_admin_admission_after_retry')
        if continuation:
            report['llm_calls']=await continuation(api,sid,rid,proof)
            report['checks'].append({'name':'actual_model_reads_retained_snapshot','kind':'real_llm'})
        report['passed']=True;return report['llm_calls']
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error)[:1500]};raise
    finally:
        (api.root/'staging-deadline-resume').write_text('drain actual operation')
        if transaction is not None:await transaction.rollback()
        if holder is not None:await holder.close()
        if first is not None and not first.done():await asyncio.gather(first,return_exceptions=True)
        await engine.dispose()
        trace=api.root/'upstream.jsonl'
        if trace.exists():report['llm_calls']=sum(v['kind']=='http' and v['path'].endswith('/chat/completions')
            for v in map(json.loads,trace.read_text(encoding='utf8').splitlines()))
        (api.root/'staging-deadline-checks.json').write_text(json.dumps(report,indent=2),encoding='utf8')
