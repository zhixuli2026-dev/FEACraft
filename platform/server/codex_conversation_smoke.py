"""Native chat discovery/access and context handoff through the product API."""
import json
import uuid
from testsupport import IsolatedServer
from codex_turn_api_smoke import WORKER, CONTROLLED
from codex_session_smoke import wait_for

EXTRA = '''
@app.post('/api/v1/fixture/history/{cid}')
async def history(cid:int):
    from app.routers.agent import _append_event
    async with SessionLocal() as db:
        await _append_event(db,cid,'message',{'role':'user','content':'API_USER_CONTEXT'})
        await _append_event(db,cid,'message',{'role':'assistant','content':'API_ANSWER_CONTEXT'})
        await db.commit()
    return {'ok':True}
'''

api=IsolatedServer('codex-conversation',app='conversation_fixture:app')
(api.root/'cli.py').write_text(WORKER,encoding='utf8')
(api.root/'conversation_fixture.py').write_text(CONTROLLED+EXTRA,encoding='utf8')
checks=[]
with api:
    case=api.create_case('Native discovery');provider=api.call('POST','/fixture/profile')['id']
    conversation=api.call('POST','/conversations',{'case_id':case['id'],'provider_profile_id':provider})
    cid=conversation['id'];prefix=f'/codex/conversations/{cid}'
    settings={'provider_profile_id':provider,'model':'fixture-model','permission_mode':'approve_writes'}
    assert api.call('PATCH',prefix+'/settings',settings)['model']=='fixture-model'
    def session():
        return api.call('POST','/codex/sessions',{'case_id':case['id'],'conversation_id':cid,
            'provider_profile_id':provider,'provider_wire_api':'responses'})
    first=session();key=uuid.uuid4().hex
    turn=api.call('POST',f"/codex/sessions/{first['id']}/turns",{'content':'normal','client_message_id':key})
    wait_for(lambda:api.call('GET','/codex/turns/'+turn['id']),lambda value:value['phase']=='completed')
    request_count=len((api.root/'requests.log').read_text().splitlines())
    for _ in range(3):
        assert api.call('GET',prefix+'/turns?client_message_id='+key)['data'][0]['id']==turn['id']
        assert api.call('GET',prefix+'/activity')['phase']=='idle'
    assert len((api.root/'requests.log').read_text().splitlines())==request_count
    checks.append('response_loss_discovery_does_not_repeat_execution')
    owner=api.token
    other=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','password':'isolated-test-123','name':'Other'})
    api.token=other['token']
    try:
        api.call('GET',prefix+'/turns',expect=404)
        api.call('GET',prefix+'/activity',expect=404)
        api.call('PATCH',prefix+'/settings',settings,expect=404)
    finally:api.token=owner
    checks.append('discovery_and_settings_deny_another_account')
    api.call('POST',f'/fixture/history/{cid}')
    second=session()
    stale=api.call('POST',f"/codex/sessions/{second['id']}/turns",{
        'content':'next','client_message_id':uuid.uuid4().hex,'resume':True},expect=409)
    assert stale['detail']['code']=='input_stale'
    assert len((api.root/'requests.log').read_text().splitlines())==request_count
    assert api.call('GET',prefix+'/activity')['phase']=='idle'
    checks.append('intervening_api_history_rejects_resume_before_admission')
    turn=api.call('POST',f"/codex/sessions/{second['id']}/turns",{'content':'normal','client_message_id':uuid.uuid4().hex})
    wait_for(lambda:api.call('GET','/codex/turns/'+turn['id']),lambda value:value['phase']=='completed')
    prompt=json.loads((api.root/'requests.log').read_text().splitlines()[-1])['kind']
    assert 'API_USER_CONTEXT' in prompt and 'API_ANSWER_CONTEXT' in prompt
    events=api.call('GET','/fixture/turn/'+turn['id'])['events']
    assert any(e['type']=='context.handoff' for e in events)
    checks.append('new_thread_handoff_contains_api_history_after_rejected_resume')
    assert api.call('GET',prefix+'/turns')['data'][0]['id']==turn['id']
    checks.append('rejected_resume_leaves_no_phantom_turn')
result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}
(api.root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
print(json.dumps(result))
