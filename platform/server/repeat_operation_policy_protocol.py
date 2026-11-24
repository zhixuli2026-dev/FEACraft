"""Catalog/public-operation policy only; synthetic state, no native/LLM proof."""
import asyncio,json
from pathlib import Path
from types import SimpleNamespace
from app.models import JobExecutionHead,CaseEventCounter
from app.services import job_repeat,step_handlers

async def main():
 catalog=step_handlers.registered();rows=[]
 class Session:
  async def get(self,model,key):
   if model is JobExecutionHead:return SimpleNamespace(active_attempt_id=state['head'])
   assert model is CaseEventCounter;return SimpleNamespace(next_seq=1)
 state={'handler':None,'head':'current','status':'failed'}
 async def source(*_):
  return SimpleNamespace(id=1,dict_revision=1),SimpleNamespace(id=1,type=state['handler'].job_type,status=state['status']),SimpleNamespace(
   id='current',version=1,root_job_id=1,phase=state['status']),SimpleNamespace()
 async def trusted(*_,**__):return {},state['handler']
 original=job_repeat.source,job_repeat.trusted,job_repeat.job_delivery.occupying
 job_repeat.source=source;job_repeat.trusted=trusted;job_repeat.job_delivery.occupying=lambda *_:False
 try:
  for (key,version),handler in sorted(catalog.items()):
   state.update(handler=handler,head='current',status='failed')
   options=await job_repeat.options(Session(),1,None)
   expected=bool(handler.recovery.get('resume'))
   assert options['retry']['available'] and options['rerun']['available'] and options['resume']['available']==expected
   assert not options['reprepare']['available']
   if not expected:assert options['resume']['reason']['code']=='checkpoint_required'
   state['head']='retired';options=await job_repeat.options(Session(),1,None)
   assert not options['retry']['available'] and not options['resume']['available'] and options['rerun']['available']
   state.update(head='current',status='running');options=await job_repeat.options(Session(),1,None)
   assert not any(options[name]['available'] for name in ('retry','rerun','resume','reprepare'))
   state['status']='succeeded';options=await job_repeat.options(Session(),1,None)
   assert not options['retry']['available'] and not options['resume']['available'] and options['rerun']['available']
   rows.append({'key':key,'version':version,'resume':expected,'four_state_contracts_checked':True})
 finally:job_repeat.source,job_repeat.trusted,job_repeat.job_delivery.occupying=original
 assert len(rows)==26 and {row['key'] for row in rows if row['resume']}=={'solve.openfoam','solve.openfoam.resume'}
 report={'passed':True,'scope':'pure_protocol_synthetic_state_against_registered_policy; ownership/drain/native not claimed',
  'llm_calls':0,'mock_llm_calls':0,'native_acceptance':False,'rows':rows,'catalog_handlers':26,'state_checks':104}
 path=Path(__file__).resolve().parents[2]/'tmp/repeat-operation-policy-protocol.json'
 path.write_text(json.dumps(report,indent=2),encoding='utf8');print(json.dumps({key:report[key] for key in ['passed','scope','catalog_handlers','state_checks','llm_calls','mock_llm_calls']}))
if __name__=='__main__':asyncio.run(main())
