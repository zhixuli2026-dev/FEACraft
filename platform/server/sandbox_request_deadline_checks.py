"""Actual image-request deadlines around locks, I/O, admission and commits."""
import asyncio,json,time
from pathlib import Path
import aiohttp
from sqlalchemy import select,update
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from app.models import CaseEventCounter,NativeRequest,DurableEvent

STAGES=('db_lock','archive','admission','settings','owner_commit','release_archive','before_commit','after_commit')
FIXTURE='''
import asyncio,json,time
from pathlib import Path
from contextlib import asynccontextmanager,contextmanager
from sqlalchemy.ext.asyncio import AsyncSession
from app.services import request_deadline as _deadline,sandbox_image_cleanup as _cleanup
from app.services import node_storage_budget as _capacity,native_request_records as _requests
def _selected_http():
 path=Path('http-deadline-case.json')
 return json.loads(path.read_text()) if path.exists() else None
def _http_mark(stage,**facts):
 armed=Path('http-deadline-armed')
 selected=_selected_http()
 if not selected or selected['stage']!=stage or not armed.exists():return False
 armed.unlink()
 pending=Path('http-deadline-paused.pending')
 pending.write_text(json.dumps({'stage':stage,**facts}))
 pending.replace('http-deadline-paused.json')
 return True
def _http_sync_pause(stage,**facts):
 if not _http_mark(stage,**facts):return
 end=time.monotonic()+100
 while not Path('http-deadline-resume').exists():
  if time.monotonic()>=end:raise TimeoutError('Actual HTTP deadline fixture was not resumed')
  time.sleep(.02)
async def _http_pause(stage,**facts):
 if not _http_mark(stage,**facts):return
 async with asyncio.timeout(100):
  while not Path('http-deadline-resume').exists():await asyncio.sleep(.02)
_request_scope=_deadline.scope
_http_archive_count=0
@asynccontextmanager
async def _http_scope(seconds):
 global _http_archive_count
 selected=_selected_http()
 outer=_deadline.current() is None
 async with _request_scope(selected['seconds'] if selected else seconds) as budget:
  if selected and outer:
   _http_archive_count=0
   pending=Path('http-deadline-start.pending')
   pending.write_text(json.dumps({'deadline':budget.deadline,'stage':selected['stage']}))
   pending.replace('http-deadline-start.json')
  yield budget
_deadline.scope=_http_scope
_archive_download=_cleanup.recovery.download
@contextmanager
def _checked_archive(*args,**kwargs):
 global _http_archive_count
 with _archive_download(*args,**kwargs) as result:yield result
 _http_archive_count+=1
 _http_sync_pause('archive')
 if _http_archive_count==2:_http_sync_pause('release_archive')
_cleanup.recovery.download=_checked_archive
_node_admit=_capacity._admit
def _admitted_node(size):
 result=_node_admit(size)
 if size==3*_cleanup.native.MAX_CONFIG:_http_sync_pause('admission',claim=str(result[1]))
 return result
_capacity._admit=_admitted_node
_native_module=_cleanup.native.module
def _settings_module(name):
 result=_native_module(name)
 if name=='sandbox_export_native':
  original=result.write
  def written(root,name,*args,**kwargs):
   value=original(root,name,*args,**kwargs)
   if name=='settings.json' and Path(root).parent.name=='.terminal-image-cleanup':
    _http_sync_pause('settings',config=str(Path(root)/name))
   return value
  result.write=written
 return result
_cleanup.native.module=_settings_module
_created_owner=_requests.create
async def _owner_committed(case_id,user_id,kind,*args,**kwargs):
 result=await _created_owner(case_id,user_id,kind,*args,**kwargs)
 if kind=='terminal.cleanup':await _http_pause('owner_commit',request_id=result.id)
 return result
_requests.create=_owner_committed
_locked_case=_cleanup.lock_case_execution
async def _waiting_case(db,case_id):
 _http_mark('db_lock')
 return await _locked_case(db,case_id)
_cleanup.lock_case_execution=_waiting_case
_append_cleanup=_cleanup.append_event
async def _queued_completion(db,case_id,kind,*args,**kwargs):
 result=await _append_cleanup(db,case_id,kind,*args,**kwargs)
 if kind==_cleanup.DONE:db.info['http_deadline_completion']=True
 return result
_cleanup.append_event=_queued_completion
_completed_cleanup=_cleanup.completed
async def _checked_completion(db,*args,**kwargs):
 result=await _completed_cleanup(db,*args,**kwargs)
 if db.info.get('http_deadline_completion'):await _http_pause('before_commit')
 return result
_cleanup.completed=_checked_completion
_db_commit=AsyncSession.commit
async def _committed_completion(self):
 result=await _db_commit(self)
 if self.info.pop('http_deadline_completion',False):await _http_pause('after_commit')
 return result
AsyncSession.commit=_committed_completion
'''

async def after_retirement(api,sid,rid,proof,continuation=None,stages=STAGES):
    report={'passed':False,'cases':[],'checks':[],'llm_calls':0,'mock_llm_calls':0,
        'database':'postgresql' if api.database_url else 'sqlite'}
    engine=create_async_engine(api.database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    first=None;holder=None;transaction=None
    async def call(method,path,body=None,*,expect=200,api_prefix=True):
        async with aiohttp.ClientSession(headers={'Authorization':'Bearer '+api.token},
                timeout=aiohttp.ClientTimeout(total=120)) as client:
            async with client.request(method,api.base+('/api/v1' if api_prefix else '')+path,json=body) as response:
                raw=await response.text();assert response.status==expect,(method,path,response.status,raw[:1000])
                return json.loads(raw)
    async def rows():
        async with sessions() as db:
            result={row.id:{'phase':row.phase,'error':row.error,'children_json':row.children_json,'result_json':row.result_json}
                for row in await db.scalars(select(NativeRequest).where(NativeRequest.kind=='terminal.cleanup'))}
            done=list(await db.scalars(select(DurableEvent.id).where(DurableEvent.type=='terminal.image_cleanup_completed')))
            return result,done
    def record(name):
        report['checks'].append({'name':name,'kind':'actual_infrastructure'});print('HTTP_DEADLINE_PASS '+name,flush=True)
    try:
        async with sessions() as db:selected=json.loads((await db.get(NativeRequest,rid)).profile_json)
        image=Path(selected['image_root'])/'volume.ext4';archive=Path(selected['root'])/'recovery.zip';retained=archive.read_bytes()
        endpoint='/terminal/sessions/'+sid+'/image-cleanup';body={'recovery_id':rid,'confirm':True}
        for stage in stages:
            for name in ('http-deadline-resume','http-deadline-paused.json','http-deadline-start.json'):(api.root/name).unlink(missing_ok=True)
            seconds=30 if stage in {'release_archive','before_commit','after_commit'} else 8
            (api.root/'http-deadline-case.json').write_text(json.dumps({'stage':stage,'seconds':seconds}))
            (api.root/'http-deadline-armed').write_text('pause one actual phase')
            before,_=await rows()
            had_image=image.exists()
            if stage=='db_lock':
                holder=await engine.connect();transaction=await holder.begin()
                await holder.execute(update(CaseEventCounter).where(CaseEventCounter.case_id==api.case_id)
                    .values(next_seq=CaseEventCounter.next_seq))
            first=asyncio.create_task(call('POST',endpoint,body=body,expect=409))
            async with asyncio.timeout(45):
                while not (api.root/'http-deadline-paused.json').exists():
                    assert not first.done(),await first if first.done() else 'HTTP deadline phase not reached'
                    await asyncio.sleep(.02)
            marker=json.loads((api.root/'http-deadline-paused.json').read_text())
            clock=json.loads((api.root/'http-deadline-start.json').read_text())
            case={'stage':stage,'seconds':seconds,'marker':marker,'clock':clock,'image_before_request':had_image};report['cases'].append(case)
            if stage=='archive':
                # A real competing writer can acquire the case while archive
                # verification drains, proving this I/O no longer holds its lock.
                started=time.monotonic()
                async with asyncio.timeout(3):
                    async with engine.begin() as db:
                        await db.execute(update(CaseEventCounter).where(CaseEventCounter.case_id==api.case_id)
                            .values(next_seq=CaseEventCounter.next_seq))
                case['competing_case_write_seconds']=time.monotonic()-started
            if stage=='admission':assert Path(marker['claim']).is_file()
            if stage=='settings':assert Path(marker['config']).is_file()
            if stage=='owner_commit':
                current,_=await rows();assert current[marker['request_id']]['phase']=='active'
                assert not json.loads(current[marker['request_id']]['children_json']).get('children')
            await asyncio.sleep(max(0,clock['deadline']-time.monotonic()+.35))
            if stage=='db_lock':
                await transaction.rollback();await holder.close();transaction=None;holder=None
            elif stage in {'archive','admission','settings','owner_commit','release_archive','after_commit'}:
                assert not first.done(),'In-flight resource/commit responsibility was abandoned at expiry'
                if stage=='admission':assert Path(marker['claim']).exists()
                if stage=='after_commit':
                    committed=await call('GET',endpoint);assert committed['status']=='completed'
                    case['committed_while_response_pending']=committed
                (api.root/'http-deadline-resume').write_text('allow protected operation to finish')
            response=await first;case['response']=response
            assert response['detail']['code']=='image_cleanup_incomplete',response
            after,done=await rows();case['new_requests']={key:row for key,row in after.items() if key not in before}
            if stage=='owner_commit':
                row=after[marker['request_id']];assert row['phase']=='failed' and row['error']=='TimeoutError'
                assert not json.loads(row['children_json']).get('children')
            if stage=='release_archive':
                assert len(case['new_requests'])==1
                row=next(iter(case['new_requests'].values()))
                assert row['phase']=='failed' and row['error']=='TimeoutError'
                child=next(iter(json.loads(row['children_json'])['children'].values()))
                assert child['stop']['verified'] and child['stop']['reason']=='timeout'
            if stage=='admission':assert not Path(marker['claim']).exists()
            if stage not in {'owner_commit','release_archive','before_commit','after_commit'}:assert after==before
            assert archive.read_bytes()==retained
            assert image.exists()==(stage not in {'before_commit','after_commit'})
            if stage=='after_commit':assert len(done)==1
            else:assert not done
            (api.root/'http-deadline-case.json').unlink();(api.root/'http-deadline-armed').unlink(missing_ok=True)
            record(stage+'_shared_request_deadline_retains_actual_responsibility')
        original,done=await rows();existed=image.exists();result=await call('POST',endpoint,body=body)
        if 'after_commit' in stages:
            assert result==report['cases'][-1]['committed_while_response_pending']
            assert await rows()==(original,done)
        else:
            final,completed=await rows();assert len(completed)==1
            for key,row in original.items():
                assert final[key]['children_json']==row['children_json'] and final[key]['result_json']==row['result_json']
        expected_deleted=report['cases'][-1]['image_before_request'] if 'after_commit' in stages else existed
        assert result['status']=='completed' and result['deleted_this_request']==expected_deleted
        assert await call('GET',endpoint)==result
        for mode in ('validation','admin'):await call('GET',f'/fixture/{api.case_id}/{mode}',api_prefix=False)
        assert archive.read_bytes()==retained;report['completion']=result
        record('committed_result_retries_without_rewriting_failed_or_successful_requests')
        if continuation:
            report['llm_calls']=await continuation(api,sid,rid,proof)
            report['checks'].append({'name':'real_model_reads_retained_snapshot','kind':'real_llm'})
        report['passed']=True;return report['llm_calls']
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error)[:1200]};raise
    finally:
        (api.root/'http-deadline-resume').write_text('drain actual protected phase')
        if transaction is not None:await transaction.rollback()
        if holder is not None:await holder.close()
        if first is not None and not first.done():await asyncio.gather(first,return_exceptions=True)
        await engine.dispose()
        trace=api.root/'upstream.jsonl'
        if trace.exists():report['llm_calls']=sum(v['kind']=='http' and v['path'].endswith('/chat/completions') for v in map(json.loads,trace.read_text(encoding='utf8').splitlines()))
        (api.root/'http-request-deadline-checks.json').write_text(json.dumps(report,indent=2),encoding='utf8')
