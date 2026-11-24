"""Actual retained node claim plus real API termination and restart."""
from cli_initialization_checks import FIXTURE as BASE,verify as initialization

FIXTURE=BASE+r'''
from app.services import cli_node_capacity as node_capacity,node_storage_budget as node_budget
from app import storage
native_config=Path(__file__).parent/'node-native-root.json'
if native_config.exists():get_settings().codex_wsl_state_root=json.loads(native_config.read_text(encoding='utf8'))['root']

@app.post('/api/v1/test/cli-node-retained-marker/{action}')
async def retained_marker(action:str,user=Depends(get_current_user)):
 evidence=json.loads((Path(__file__).parent/'cli-initialization-checks.json').read_text(encoding='utf8'))
 identifier=evidence['prepared_credit']['session_id']
 async with SessionLocal() as db:
  row=await db.get(NativeCliSession,identifier);credit=await db.get(CliInitializationReservation,identifier)
  assert row.created_by==user.id and row.phase=='closed' and credit.state=='settled'
  selected=capacity.ticket(credit,row)
 if action=='retain':
  # Recreate the exact on-disk marker representing interrupted node cleanup.
  # This is an explicit filesystem fault, not evidence of an actual API crash.
  await asyncio.to_thread(node_capacity.acquire,selected)
  assert await asyncio.to_thread(node_capacity.outstanding,selected)
 elif action=='settle':
  await capacity.closed(user.id)
  assert not await asyncio.to_thread(node_capacity.outstanding,selected)
 else:raise ValueError('Unknown marker scenario')
 return {'ok':True,'filesystem_fault':True}

@app.post('/api/v1/test/cli-node-prepare')
async def node_prepare(user=Depends(get_current_user)):
 settings=get_settings()
 native_config.write_text(json.dumps({'root':settings.codex_wsl_state_root}),encoding='utf8')
 session=sessions.Session(None,None,user.id,runtime='wsl');await session.prepare()
 assert session.client.transport.owned.proc is None
 async with SessionLocal() as db:
  credit=await db.get(CliInitializationReservation,session.id)
  row=await db.get(NativeCliSession,session.id)
  ticket=capacity.ticket(credit,row)
  assert credit.state=='active' and credit.remaining_bytes==credit.size
 root,identifier,encoded=node_capacity.encoded(ticket)
 claim=root/node_budget.DIRECTORY/(identifier+'.json')
 assert claim.read_bytes()==encoded
 observed=await asyncio.to_thread(node_budget.observe,private=True)
 assert observed['reserved_bytes']==ticket['bytes']
 report={'passed':False,'session_id':session.id,'ticket':ticket,'claim_sha256':hashlib.sha256(encoded).hexdigest(),
  'before':observed,'process_was_not_created':True,'mock_llm_calls':0}
 (Path(__file__).parent/'cli-node-initialization-checks.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 return report

@app.post('/api/v1/test/cli-node-after-restart')
async def node_restarted(body:dict,user=Depends(get_current_user)):
 path=Path(__file__).parent/'cli-node-initialization-checks.json';report=json.loads(path.read_text(encoding='utf8'))
 assert report['ticket']['user_id']==user.id and report['session_id']==body['session_id']
 assert identity.probe(report['ticket']['owner'])['state']=='exited'
 await capacity.closed(user.id)
 observed=await asyncio.to_thread(node_budget.observe,private=True)
 async with SessionLocal() as db:
  credit=await db.get(CliInitializationReservation,report['session_id'])
  row=await db.get(NativeCliSession,report['session_id'])
  assert credit.state=='active' and credit.remaining_bytes==credit.size
  assert row.phase=='starting' and json.loads(row.stop_json)=={}
  report['retained_session']=records.public(row)
 root,identifier,encoded=node_capacity.encoded(report['ticket'])
 actual=(root/node_budget.DIRECTORY/(identifier+'.json')).read_bytes()
 assert actual==encoded and hashlib.sha256(actual).hexdigest()==report['claim_sha256']
 assert observed['reserved_bytes']==report['ticket']['bytes']
 report.update(passed=True,api_owner_exited=True,after=observed,unknown_credit_retained=True)
 report['checks']=[{'name':name,'kind':'actual_infrastructure'} for name in (
  'native_node_claim_committed_before_process_creation',
  'real_api_owner_exit_does_not_release_native_node_claim',
  'independent_restarted_api_preserves_unknown_user_and_node_credit',
  'normal_prepared_and_verified_stop_paths_remove_node_claims')]
 path.write_text(json.dumps(report,indent=2),encoding='utf8');return report
'''


def verify(api,**_):
    import shutil,httpx,json
    conversation=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Retained native node history','permission_mode':'ask'})
    with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},timeout=240,trust_env=False) as client:
        response=client.post('/test/cli-initialization',json={'case_id':api.case_id,'conversation_id':conversation['id']})
        response.raise_for_status();result=response.json()
    assert result['passed'] and len(result['checks'])==7
    api.call('POST','/test/cli-node-retained-marker/retain')
    rejected=api.call('DELETE','/conversations/'+str(conversation['id']),expect=409)
    assert '初始化额度' in rejected['detail']
    api.call('POST','/test/cli-node-retained-marker/settle')
    api.call('DELETE','/conversations/'+str(conversation['id']))
    result=api.call('GET','/test/cli-initialization-deleted')
    assert len(result['checks'])==8
    assert not list((api.root/'files/.node-storage-budget').glob('*.json'))
    user=api.call('GET','/auth/me')
    pending=api.call('POST','/test/cli-node-prepare')
    # Windows TerminateProcess ends the actual API, without synthesizing a CLI
    # stop receipt. No native process was created by this prepare-only scenario.
    api.__exit__(RuntimeError,None,None)
    assert api.proc.poll() is not None
    shutil.copyfile(api.root/'api.log',api.root/'api-before-node-restart.log')
    api.__enter__()
    api.token=api.call('POST','/auth/login',{'email':user['email'],'password':'isolated-test-123'})['token']
    observation=api.call('POST','/test/cli-node-after-restart',{'session_id':pending['session_id']})
    assert observation['passed'] and len(observation['checks'])==4
    observation['retained_marker_deletion_guard']={'passed':True,'kind':'actual_filesystem_fault','rejected_status':409}
    (api.root/'cli-node-initialization-checks.json').write_text(json.dumps(observation,indent=2),encoding='utf8')
    print('CLI_NODE_INITIALIZATION_PASS '+str(api.root),flush=True)
    return {'initialization':result,'node':observation}
