"""Actual API deaths around drained scope and reconciliation commits; no LLM substitution."""
import asyncio
import json
from pathlib import Path
import subprocess

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from app.models import NativeRequest,DurableEvent
from app.services import sandbox_image_scope as scopes,sandbox_image_reconciliation as reconciliation
from app.services import process_identity_native as identity
from app.services.owned_process import probe_native
from app.services.native_scope_control import control

FIXTURE = '''
import asyncio,json,os
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession as _ReceiptSession
from app.services import sandbox_image_scope as _rs, sandbox_image_launch as _rl
from app.services import sandbox_image_reconciliation as _rr
from app.services.owned_process import OwnedProcess as _ReceiptOwned
from app.services.process_identity_native import observe as _receipt_observe
_receipt_context = None
def _receipt_die(stage,extra=None):
 armed=Path('scope-commit-fault.json')
 if _receipt_context is None or not armed.exists() or json.loads(armed.read_text())['stage']!=stage:return
 armed.unlink()
 with Path('scope-commit-deaths.jsonl').open('a',encoding='utf8') as stream:
  stream.write(json.dumps(dict(_receipt_context,stage=stage,owner=_receipt_observe(os.getpid()),extra=extra))+'\\n')
  stream.flush();os.fsync(stream.fileno())
 os._exit(81)
_receipt_observed=_rl.observed
async def _cancel_before_native_admission(owner,child_id,value):
 global _receipt_context
 await _receipt_observed(owner,child_id,value)
 if value['stage']=='host_ready' and _receipt_context is None and Path('scope-commit-fault.json').exists():
  _receipt_context={'request_id':owner.id,'child_id':child_id}
  Path('scope-commit-cancel.json').write_text(json.dumps(_receipt_context))
  asyncio.current_task().cancel()
  await asyncio.sleep(0)
_rl.observed=_cancel_before_native_admission
_receipt_guardian=_ReceiptOwned.retire_guardian
async def _retired_guardian(self):
 value=await _receipt_guardian(self)
 if value is not None:_receipt_die('guardian_receipt',value)
 return value
_ReceiptOwned.retire_guardian=_retired_guardian
_receipt_control=_rs.control.control
async def _revoked_scope(distribution,operation,arguments,**kwargs):
 value=await _receipt_control(distribution,operation,arguments,**kwargs)
 if operation=='retire':_receipt_die('native_revoked',value)
 return value
_rs.control.control=_revoked_scope
_receipt_append=_rs.append
async def _append_closed(db,row,child,stage,value):
 await _receipt_append(db,row,child,stage,value)
 if stage=='closed' and _receipt_context and row.id==_receipt_context['request_id']:
  await db.flush()
  _receipt_die('before_closed_commit',value)
_rs.append=_append_closed
_receipt_close=_rs.close
async def _close_committed(owner,child_id,**kwargs):
 await _receipt_close(owner,child_id,**kwargs)
 _receipt_die('after_closed_commit')
_rs.close=_close_committed
_receipt_event=_rr.append_event
async def _append_reconciliation(db,case_id,event_type,payload,**kwargs):
 result=await _receipt_event(db,case_id,event_type,payload,**kwargs)
 if event_type==_rr.EVENT and _receipt_context and payload['request_id']==_receipt_context['request_id']:
  db.info['receipt_reconciliation_commit']=payload
 return result
_rr.append_event=_append_reconciliation
_receipt_commit=_ReceiptSession.commit
async def _commit_boundary(self):
 payload=self.info.pop('receipt_reconciliation_commit',None)
 if payload is not None:
  await self.flush()
  _receipt_die('before_reconcile_commit',payload)
 result=await _receipt_commit(self)
 if payload is not None:_receipt_die('after_reconcile_commit',payload)
 return result
_ReceiptSession.commit=_commit_boundary
_receipt_reconcile=_rr.reconcile
async def _diagnosed_reconciliation(*args,**kwargs):
 try:return await _receipt_reconcile(*args,**kwargs)
 except Exception as error:
  with Path('scope-reconciliation-errors.jsonl').open('a',encoding='utf8') as stream:
   stream.write(json.dumps({'type':type(error).__name__,'detail':str(error)[:1000]})+'\\n')
  raise
_rr.reconcile=_diagnosed_reconciliation
_receipt_inspection=_rr.inspection.observe
async def _diagnosed_observation(snapshot):
 value=await _receipt_inspection(snapshot)
 if value['status']!='processes_exited':
  expected={role:birth for role,birth,_ in snapshot.owners}
  expected.update({role:birth for role,_,birth in snapshot.processes})
  current={}
  for check in value['checks']:
   birth=expected[check['role']]
   if check['state']=='mismatch' and birth['platform']=='windows':
    try:current[check['role']]=_receipt_observe(birth['pid'])
    except Exception as error:current[check['role']]={'error':type(error).__name__}
  with Path('scope-inspection-observations.jsonl').open('a',encoding='utf8') as stream:
   stream.write(json.dumps({'result':value,'expected':expected,'current':current})+'\\n')
 return value
_rr.inspection.observe=_diagnosed_observation
'''


async def after_retirement(api,sid,rid,proof,continuation=None):
    report={'passed':False,'checks':[],'crashes':[],'llm_calls':0,'mock_llm_calls':0,
            'database':'postgresql' if api.database_url else 'sqlite'}
    engine=create_async_engine(api.database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    endpoint='/terminal/sessions/'+sid+'/image-cleanup';body={'recovery_id':rid,'confirm':True}
    async def call(method,path,**kwargs):return await asyncio.to_thread(api.call,method,path,**kwargs)
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind})
        print('SCOPE_COMMIT_PASS '+name,flush=True)
    async def state(identifier):
        async with sessions() as db:
            row=await db.get(NativeRequest,identifier)
            child=next(iter(scopes.records.document(row)['children'].values()))
            witness=await db.get(DurableEvent,reconciliation.event_id(identifier))
            return {'phase':row.phase,'version':row.version,'error':row.error,
                'ended_at':row.ended_at.isoformat() if row.ended_at else None,'child':child,'children_json':row.children_json,
                'result_json':row.result_json,'scope':await scopes.evidence(db,row,child),
                'witness':json.loads(witness.payload_json) if witness else None}
    async def restart():
        api.proc=subprocess.Popen(api.proc.args,cwd=api.root,env=api.env,stdout=api.log,stderr=subprocess.STDOUT,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        async with asyncio.timeout(40):
            while True:
                assert api.proc.poll() is None
                try:
                    if (await call('GET','/api/health',api_prefix=False)).get('ok'):return
                except OSError:pass
                await asyncio.sleep(.1)
    try:
        async with sessions() as db:selected=json.loads((await db.get(NativeRequest,rid)).profile_json)
        image=Path(selected['image_root'])/'volume.ext4';archive=Path(selected['root'])/'recovery.zip'
        original=archive.read_bytes()
        stages=('guardian_receipt','native_revoked','before_closed_commit','after_closed_commit',
                'before_reconcile_commit','after_reconcile_commit')
        for stage in stages:
            (api.root/'scope-commit-fault.json').write_text(json.dumps({'stage':stage}))
            before_reconciliation=None
            if 'reconcile' in stage:
                await call('POST',endpoint,body=body,expect=409)
                cancelled=json.loads((api.root/'scope-commit-cancel.json').read_text())
                before_reconciliation=await state(cancelled['request_id'])
                # The cancelled task is the native preparation, not the HTTP
                # request. Its client reports ChildExecutionError to the owner.
                assert before_reconciliation['phase']=='failed' and before_reconciliation['error']=='ChildExecutionError'
                assert before_reconciliation['child']['error']=='CancelledError' and before_reconciliation['ended_at']
                assert before_reconciliation['scope']['closed'] is not None
            try:
                await call('POST',endpoint,body=body)
                raise AssertionError('API did not actually exit at receipt commit boundary')
            except (OSError,ConnectionError):pass
            assert await asyncio.to_thread(api.proc.wait,15)==81
            death=json.loads((api.root/'scope-commit-deaths.jsonl').read_text(encoding='utf8').splitlines()[-1])
            assert death['stage']==stage
            saved=await state(death['request_id']);value=saved['scope'];child=saved['child']
            assert child['identity'] is None and child['stop'] is None
            assert (value['closed'] is not None)==(stage in {'after_closed_commit','before_reconcile_commit','after_reconcile_commit'})
            assert (saved['witness'] is not None)==(stage=='after_reconcile_commit')
            if before_reconciliation is not None:
                assert saved['version']==before_reconciliation['version']+(stage=='after_reconcile_commit')
                assert saved['children_json']==before_reconciliation['children_json']
            preflight=value['admitted']['native_preflight']
            async with asyncio.timeout(30):
                while True:
                    exits={'owner':identity.probe(death['owner']),'host':identity.probe(preflight['host']),
                        'guardian':identity.probe(value['admitted']['guardian']['preparation']['guardian']),
                        'native':await probe_native(preflight['native']['distribution'],preflight['native']['process'])}
                    if all(item['state']=='exited' for item in exits.values()):break
                    await asyncio.sleep(.05)
            actual=await control(value['planned']['distribution'],'observe',{'scope':value['allocated']['result']})
            assert actual['result']['state']==('empty' if stage=='guardian_receipt' else 'absent')
            death.update(persisted_after_death=saved,actual_exits=exits,actual_scope_after_death=actual)
            report['crashes'].append(death)
            await restart()
            assert (await call('GET',endpoint))['status']=='pending'
            assert await state(death['request_id'])==saved,'GET changed an interrupted commit'
            assert image.exists() and archive.read_bytes()==original
            record('actual_'+stage+'_API_death_distinguishes_physical_cleanup_from_committed_proof')
        done=await call('POST',endpoint,body=body)
        assert done['status']=='completed' and done['deleted_this_request'] and not image.exists()
        report['histories']={}
        for death in report['crashes']:
            after=await state(death['request_id']);before=death['persisted_after_death']
            assert after['children_json']==before['children_json'] and after['result_json']==before['result_json']
            assert after['witness'] is not None and after['witness']['normal_stop_receipt_invented'] is False
            assert after['scope']['closed'] is not None
            assert after['version']==before['version']+(before['witness'] is None)
            if death['stage']=='after_reconcile_commit':assert after['witness']==before['witness']
            if before['scope']['closed'] is not None:assert after['scope']['closed']==before['scope']['closed']
            assert after['scope']['closed']['receipt']['result']['state']=='absent'
            report['histories'][death['stage']]=after
        async with sessions() as db:
            witnesses=list(await db.scalars(select(DurableEvent.id).where(DurableEvent.type==reconciliation.EVENT)))
            closures=list(await db.scalars(select(DurableEvent.id).where(DurableEvent.type=='terminal.image_scope_closed')))
            completed=list(await db.scalars(select(DurableEvent.id).where(DurableEvent.type=='terminal.image_cleanup_completed')))
            assert len(witnesses)==6 and len(closures)==7 and len(completed)==1
        assert await call('POST',endpoint,body=body)==done
        assert await call('GET',endpoint)==done and archive.read_bytes()==original
        await call('GET',f'/fixture/{api.case_id}/validation',api_prefix=False)
        await call('GET',f'/fixture/{api.case_id}/admin',api_prefix=False)
        record('six_commit_crashes_recover_once_without_duplicate_proofs_or_original_stop_rewrites')
        if continuation:
            report['llm_calls']=await continuation(api,sid,rid,proof)
            record('real_model_reads_retained_snapshot_after_actual_commit_crashes','real_llm')
        report['passed']=True
        return report['llm_calls']
    finally:
        await engine.dispose()
        diagnostics=api.root/'scope-reconciliation-errors.jsonl'
        if diagnostics.exists():report['reconciliation_errors']=[json.loads(line) for line in diagnostics.read_text(encoding='utf8').splitlines()]
        trace=api.root/'upstream.jsonl'
        if trace.exists():report['llm_calls']=sum(item['kind']=='http' and item['path'].endswith('/chat/completions')
                                               for item in map(json.loads,trace.read_text(encoding='utf8').splitlines()))
        (api.root/'scope-commit-checks.json').write_text(json.dumps(report,indent=2),encoding='utf8')
