"""Actually kill the retained guardian; verify native cleanup and refusal, no LLM substitution."""
import asyncio
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.models import NativeRequest, DurableEvent
from app.services import sandbox_image_scope as scopes, sandbox_image_launch as launch
from app.services import process_identity_native as identity
from app.services.native_scope_control import control

FIXTURE = '''
import asyncio,json,os
from pathlib import Path
from app.services.owned_process import OwnedProcess as _DeadGuardianOwned
from app.services.process_identity_native import observe as _dead_guardian_observe
from app.services import sandbox_image_scope as _dead_guardian_scopes
_selected_guardian = None
_dead_guardian_prepare = _DeadGuardianOwned.prepare_guardian
async def _prepare_dead_guardian(self,root):
 global _selected_guardian
 await _dead_guardian_prepare(self,root)
 _selected_guardian = self.guardian
_DeadGuardianOwned.prepare_guardian = _prepare_dead_guardian
_dead_guardian_admit = _dead_guardian_scopes.admit
async def _admit_before_guardian_death(owner,child_id,**kwargs):
 await _dead_guardian_admit(owner,child_id,**kwargs)
 armed = Path('kill-selected-guardian')
 if armed.exists():
  armed.unlink()
  birth = _selected_guardian.handle.identity()
  assert kwargs['guardian']['preparation']['guardian']==birth
  _selected_guardian.proc.kill()
  code = await asyncio.to_thread(_selected_guardian.proc.wait,10)
  _selected_guardian.handle.wait_exit(0)
  Path('killed-selected-guardian.json').write_text(json.dumps({'guardian':birth,'exit_code':code,
   'owner':_dead_guardian_observe(os.getpid()),'request':_selected_guardian.request,'preparation':_selected_guardian.prepared,
   'native_preflight':kwargs['native_preflight']}))
  raise ValueError('Actual guardian was killed after admission before native handoff')
_dead_guardian_scopes.admit = _admit_before_guardian_death
'''


async def after_retirement(api,sid,rid,proof,continuation=None):
    report={'passed':False,'checks':[],'llm_calls':0,'mock_llm_calls':0,'cleanup_completed':False}
    engine=create_async_engine('sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    endpoint='/terminal/sessions/'+sid+'/image-cleanup'
    body={'recovery_id':rid,'confirm':True}
    async def call(method,path,**kwargs):return await asyncio.to_thread(api.call,method,path,**kwargs)
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind})
        print('IMAGE_GUARDIAN_FAILURE_PASS '+name,flush=True)
    try:
        (api.root/'kill-selected-guardian').write_text('kill only the retained creation handle')
        await call('POST',endpoint,body=body,expect=409)
        death=json.loads((api.root/'killed-selected-guardian.json').read_text())
        assert death['exit_code']!=0 and identity.probe(death['guardian'])['state']=='exited'
        from app.services.owned_process import probe_native
        native=death['native_preflight']['native']
        assert (await probe_native(native['distribution'],native['process']))['state']=='exited'
        assert identity.probe(death['native_preflight']['host'])['state']=='exited'
        assert identity.probe(death['owner'])['state']=='match'
        assert not (Path(death['request']['root']['path'])/'receipt.json').exists()
        async with sessions() as db:
            rows=list(await db.scalars(select(NativeRequest).where(NativeRequest.kind=='terminal.cleanup')))
            assert len(rows)==1
            row=rows[0]
            child=next(iter(scopes.records.document(row)['children'].values()))
            value=await scopes.evidence(db,row,child)
            assert row.phase=='failed' and row.ended_at is not None
            assert child['identity'] is None and child['stop'] is None
            assert not (await launch.evidence(db,row,child))[1]
            assert value['admitted']['guardian']['preparation']==death['preparation']
            assert value['closed'] is None
            selected=json.loads(row.profile_json)
            original_children=row.children_json
        image=Path(selected['image_root'])/'volume.ext4'
        assert image.exists()
        observed=await control(value['planned']['distribution'],'observe',{'scope':value['allocated']['result']})
        assert observed['result']['state']=='absent'
        report.update(death=death,scope_history=value,actual_native_scope_after_failure=observed)
        record('actual_guardian_kill_exits_known_bootstrap_births_and_still_revokes_native_allocation')
        await call('POST',endpoint,body=body,expect=409)
        assert (await call('GET',endpoint))['status']=='pending' and image.exists()
        await call('GET',f'/fixture/{api.case_id}/validation',api_prefix=False,expect=409)
        async with sessions() as db:
            current=await db.get(NativeRequest,row.id)
            assert current.children_json==original_children
            assert not list(await db.scalars(select(DurableEvent.id).where(DurableEvent.type=='terminal.image_cleanup_abandoned')))
        record('missing_guardian_proof_keeps_retry_and_case_admission_blocked_without_forged_stop')
        if continuation:
            report['llm_calls']=await continuation(api,sid,rid,proof)
            record('real_model_reads_preserved_snapshot_without_claiming_cleanup_complete','real_llm')
        report['passed']=True
        return report['llm_calls']
    finally:
        await engine.dispose()
        trace=api.root/'upstream.jsonl'
        if trace.exists():report['llm_calls']=sum(item['kind']=='http' and item['path'].endswith('/chat/completions')
                                               for item in map(json.loads,trace.read_text(encoding='utf8').splitlines()))
        (api.root/'image-guardian-failure-checks.json').write_text(json.dumps(report,indent=2),encoding='utf8')
