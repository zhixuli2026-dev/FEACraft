"""Real recovery preparation, native execution and committed-result deadline cases."""
import asyncio,json,time
from pathlib import Path
import aiohttp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine
from app.models import NativeRequest,NativeSession
from app.services import process_identity_native as identity

STAGES=('inspection','admission','settings','owner_commit','pre_release','receipt','complete_commit')
FIXTURE='''
import asyncio,json,time
from pathlib import Path
from contextlib import asynccontextmanager
from app.services import request_deadline as _deadline,sandbox_recovery_execution as _recovery
from app.services import native_request_records as _records,node_storage_budget as _capacity
def _recovery_case():
 path=Path('recovery-deadline-case.json')
 return json.loads(path.read_text()) if path.exists() else None
def _recovery_mark(stage,**facts):
 selected=_recovery_case();armed=Path('recovery-deadline-armed')
 if not selected or selected['stage']!=stage or not armed.exists():return False
 armed.unlink()
 pending=Path('recovery-deadline-paused.pending')
 pending.write_text(json.dumps({'stage':stage,**facts}))
 pending.replace('recovery-deadline-paused.json')
 return True
async def _recovery_pause(stage,**facts):
 if not _recovery_mark(stage,**facts):return
 async with asyncio.timeout(120):
  while not Path('recovery-deadline-resume').exists():await asyncio.sleep(.02)
def _recovery_sync_pause(stage,**facts):
 if not _recovery_mark(stage,**facts):return
 end=time.monotonic()+120
 while not Path('recovery-deadline-resume').exists():
  if time.monotonic()>=end:raise TimeoutError('Recovery deadline fixture was not resumed')
  time.sleep(.02)
_recovery_scope=_deadline.scope
@asynccontextmanager
async def _bounded_recovery(seconds):
 selected=_recovery_case();outer=_deadline.current() is None
 async with _recovery_scope(selected['seconds'] if selected else seconds) as budget:
  if selected and outer:
   Path('recovery-deadline-start.json').write_text(json.dumps({'deadline':budget.deadline,'stage':selected['stage']}))
  yield budget
_deadline.scope=_bounded_recovery
_verified_exit=_recovery.verify_exit
async def _checked_exit(sid,user_id,expected=None):
 value=await _verified_exit(sid,user_id,expected)
 await _recovery_pause('inspection' if expected is None else 'pre_release')
 return value
_recovery.verify_exit=_checked_exit
_admit_recovery=_capacity._admit
def _admitted_recovery(size):
 value=_admit_recovery(size)
 if size==_recovery.BUDGET:_recovery_sync_pause('admission',claim=str(value[1]))
 return value
_capacity._admit=_admitted_recovery
_write_recovery=_recovery.native.write
def _written_recovery(root,name,*args,**kwargs):
 value=_write_recovery(root,name,*args,**kwargs)
 if name=='node-claim.json' and Path(root).parent.name==_recovery.DIRECTORY:
  _recovery_sync_pause('settings',root=str(root))
 return value
_recovery.native.write=_written_recovery
_create_recovery=_records.create
async def _created_recovery(case_id,user_id,kind,*args,**kwargs):
 value=await _create_recovery(case_id,user_id,kind,*args,**kwargs)
 if kind=='terminal.recover':await _recovery_pause('owner_commit',request_id=value.id)
 return value
_records.create=_created_recovery
_completed_recovery=_recovery.completed
def _read_recovery_receipt(selected,expected=None):
 value=_completed_recovery(selected,expected)
 if expected is None:_recovery_sync_pause('receipt',root=selected['root'])
 return value
_recovery.completed=_read_recovery_receipt
_complete_recovery=_records.complete
async def _committed_recovery(owner,error=None,**kwargs):
 value=await _complete_recovery(owner,error,**kwargs)
 if error is None and isinstance(kwargs.get('result'),dict) and kwargs['result'].get('schema')==_recovery.native.SCHEMA:
  await _recovery_pause('complete_commit',request_id=owner.id)
 return value
_records.complete=_committed_recovery
'''


async def before_recovery(api,sid,proof):
    report={'passed':False,'cases':[],'checks':[],'llm_calls':0,'mock_llm_calls':0,
        'database':'postgresql' if api.database_url else 'sqlite'}
    engine=create_async_engine(api.database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
    first=None
    async def call(method,path,expect=200):
        async with aiohttp.ClientSession(headers={'Authorization':'Bearer '+api.token},timeout=aiohttp.ClientTimeout(total=150)) as client:
            async with client.request(method,api.base+'/api/v1'+path) as response:
                raw=await response.text()
                assert response.status==expect,(method,path,response.status,raw[:1000])
                return json.loads(raw)
    async def rows():
        async with engine.connect() as db:
            return {v['id']:dict(v) for v in (await db.execute(select(NativeRequest.id,NativeRequest.phase,
                NativeRequest.error,NativeRequest.children_json,NativeRequest.result_json,NativeRequest.profile_json)
                .where(NativeRequest.kind=='terminal.recover',NativeRequest.case_id==api.case_id))).mappings()}
    def claims():
        root=api.root/'files'/'.node-storage-budget'
        return {p.name:json.loads(p.read_text()) for p in root.glob('*.json')}
    def record(name):
        report['checks'].append({'name':name,'kind':'actual_infrastructure'})
        print('RECOVERY_DEADLINE_PASS '+name,flush=True)
    try:
        async with engine.connect() as db:
            selected=json.loads(await db.scalar(select(NativeSession.profile_json).where(NativeSession.id==sid)))
        image=api.root/'files'/'.terminal-export'/selected['terminal']['command'][-1].rsplit('/',1)[-1]/'volume.ext4'
        import hashlib
        original_hash=hashlib.sha256(image.read_bytes()).hexdigest()
        endpoint='/terminal/sessions/'+sid+'/recoveries'
        for stage in STAGES:
            for name in ('recovery-deadline-start.json','recovery-deadline-paused.json','recovery-deadline-resume'):
                (api.root/name).unlink(missing_ok=True)
            seconds=45 if stage in {'pre_release','receipt','complete_commit'} else 12
            (api.root/'recovery-deadline-case.json').write_text(json.dumps({'stage':stage,'seconds':seconds}))
            (api.root/'recovery-deadline-armed').write_text('pause after the actual operation')
            before=await rows();old_claims=claims()
            first=asyncio.create_task(call('POST',endpoint,expect=502))
            async with asyncio.timeout(70):
                while not (api.root/'recovery-deadline-paused.json').exists():
                    assert not first.done(),await first if first.done() else 'Recovery stage not reached'
                    await asyncio.sleep(.02)
            marker=json.loads((api.root/'recovery-deadline-paused.json').read_text())
            clock=json.loads((api.root/'recovery-deadline-start.json').read_text())
            case={'stage':stage,'seconds':seconds,'marker':marker,'clock':clock};report['cases'].append(case)
            if stage=='owner_commit':
                current=await rows();row=current[marker['request_id']]
                assert row['phase']=='active' and not json.loads(row['children_json']).get('children')
            if stage=='settings':assert (Path(marker['root'])/'node-claim.json').is_file()
            if stage=='receipt':assert (Path(marker['root'])/'complete.json').is_file()
            await asyncio.sleep(max(0,clock['deadline']-time.monotonic()+.3))
            if stage in {'admission','settings','owner_commit','receipt','complete_commit'}:
                assert not first.done(),'Recovery abandoned actual resource/commit responsibility'
                if stage=='admission':assert Path(marker['claim']).exists()
                if stage=='complete_commit':
                    case['committed_query']=await call('GET',endpoint+'/'+marker['request_id'])
                    assert case['committed_query']['phase']=='succeeded'
                (api.root/'recovery-deadline-resume').write_text('release protected callback')
            if stage=='pre_release':
                async with asyncio.timeout(10):case['response']=await asyncio.shield(first)
            else:case['response']=await first
            case['response_monotonic']=time.monotonic()
            if stage=='pre_release':assert case['response_monotonic']-clock['deadline']<10
            assert case['response']['detail']['code']=='sandbox_recovery_incomplete'
            after=await rows();case['new_requests']={k:v for k,v in after.items() if k not in before}
            for key,value in before.items():assert after[key]==value
            if stage in {'inspection','admission','settings'}:assert not case['new_requests']
            else:
                assert len(case['new_requests'])==1
                row=next(iter(case['new_requests'].values()));children=json.loads(row['children_json']).get('children',{})
                if stage=='complete_commit':
                    assert row['phase']=='succeeded' and row['error'] is None
                    assert await call('GET',endpoint+'/'+row['id'])==case['committed_query']
                else:assert row['phase']=='failed' and row['error']=='TimeoutError',row
                if stage=='owner_commit':assert not children
                else:
                    assert len(children)==1
                    child=next(iter(children.values()));assert child['stop']['verified']
                    if stage=='pre_release':assert child['stop']['reason']=='timeout',child['stop']
            remaining_claims=claims()
            assert set(remaining_claims)<=set(old_claims),'New recovery capacity was leaked'
            case['old_claim_reclamation']=[]
            for name in set(old_claims)-set(remaining_claims):
                observed=identity.probe(old_claims[name]['owner'])
                assert observed['state']=='exited','A prior live or unknown owner lost its claim'
                case['old_claim_reclamation'].append({'claim':name,'owner':old_claims[name]['owner'],'observation':observed})
            for name,value in remaining_claims.items():assert value==old_claims[name]
            assert hashlib.sha256(image.read_bytes()).hexdigest()==original_hash
            (api.root/'recovery-deadline-case.json').unlink()
            (api.root/'recovery-deadline-armed').unlink(missing_ok=True)
            record(stage+'_preserves_original_image_and_owned_records')
        report['passed']=True
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error)[:1600]};raise
    finally:
        (api.root/'recovery-deadline-resume').write_text('drain protected recovery operation')
        if first is not None and not first.done():await asyncio.gather(first,return_exceptions=True)
        await engine.dispose()
        (api.root/'recovery-deadline-checks.json').write_text(json.dumps(report,indent=2),encoding='utf8')
