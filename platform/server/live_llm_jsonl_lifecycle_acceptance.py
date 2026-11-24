"""Read back real multi-request turns, disconnect an HTTP client and import overlapping legacy IDs."""
import asyncio
from collections import defaultdict
from functools import partial
import hashlib
import json
import uuid

import httpx

from live_llm_agent_recovery_acceptance import stream
from live_llm_failure_fallback_acceptance import wait_for
from live_llm_job_repeat_acceptance import main, put


def scenario(config, api, **_):
    api.create_case('JSONL lifecycle acceptance')
    marker = 'READBACK_'+uuid.uuid4().hex
    put(api,'Ref/scope.txt',marker)
    profile = api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Lifecycle scope',
        'base_url':config['base_url'],'default_model':config['model'],'api_key':config['key']})
    cid = api.call('POST','/conversations',{'case_id':api.case_id,'title':'Requests in a real run',
        'permission_mode':'plan','provider_profile_id':profile['id'],'model':config['model']})['id']
    route = f'/conversations/{cid}'
    def body(prompt):
        return {'content':prompt,'provider_profile_id':profile['id'],'model':config['model']}
    frames = stream(api,route+'/messages',body('请调用一次 read_text_file 读取 Ref/scope.txt。只回复文件中的完整标识。'),
                    'multi-request',config)
    assert any(e['type']=='run.completed' for e in frames)
    assert any(e['type']=='tool.result' and e['tool']=='read_text_file' and e['ok'] for e in frames)
    assert marker in ''.join(e['text'] for e in frames if e['type']=='text.delta')
    raw = api.call('GET',route+'/export',raw=True)
    archive = [json.loads(line) for line in raw.splitlines()]
    assert archive[0]['schema']=='vibeflow.agent.session.v1'
    assert all(e['schema']=='vibeflow.agent.event.v1' and e['session_id']==cid for e in archive[1:])
    assert [e['seq'] for e in archive[1:]]==sorted(set(e['seq'] for e in archive[1:]))
    completed_run=next(e['payload']['run_id'] for e in archive[1:] if e['type']=='run_state')
    requests=defaultdict(list)
    for e in archive[1:]:
        if e['type']=='request_state':
            p=e['payload']
            assert p['run_id']==completed_run
            requests[p['request_id']].append(p)
    assert len(requests)>=2
    for i, pair in enumerate(requests.values()):
        assert [p['status'] for p in pair]==['started','completed']
        assert all(p['request_index']==i for p in pair)
        assert pair[0]['started_at']==pair[1]['started_at'] and pair[1]['ended_at']>=pair[1]['started_at']
        assert pair[1]['duration_ms']==round((pair[1]['ended_at']-pair[1]['started_at'])*1000)

    headers={'Authorization':'Bearer '+api.token}
    with httpx.Client(base_url=api.base+'/api/v1',headers=headers,timeout=120,trust_env=False) as client:
        with client.stream('POST',route+'/messages',json=body('直接用约800字解释有限元法，不需要工具。')) as response:
            response.raise_for_status()
            active_run=None
            for line in response.iter_lines():
                if line.startswith('data:'):
                    e=json.loads(line[5:])
                    if e['type'] in ('reasoning.delta','text.delta') and e.get('text'):
                        active_run=e['run_id']
                        break
            assert active_run and active_run!=completed_run
        def disconnected():
            history=api.call('GET',route)['events']
            terminal={kind:[e['payload'] for e in history if e['type']==kind and e['payload'].get('run_id')==active_run]
                      for kind in ('run_state','request_state')}
            return terminal if all(len(v)>1 for v in terminal.values()) else None
        terminal=wait_for(disconnected,'Disconnected lifecycle did not finish',30)
        for pair in terminal.values():
            assert [p['status'] for p in pair]==['started','interrupted'],pair
            assert all(isinstance(pair[-1].get(k),(int,float)) for k in ('started_at','ended_at','duration_ms'))
        def import_lines(lines, *, expected=200):
            content=b'\n'.join(json.dumps(v,ensure_ascii=False).encode() for v in lines)
            response=client.post('/conversations:import',params={'case_id':api.case_id},content=content)
            assert response.status_code==expected,response.text[:512]
            return response.json()
        clone=import_lines(archive)
        cloned=api.call('GET','/conversations/'+str(clone['id']))
        assert [e['type'] for e in cloned['events']]==[e['type'] for e in archive[1:]]
        assert any(e['type']=='request_state' and e['payload'].get('reported_usage') for e in archive[1:])
        for original, imported in zip(archive[1:], cloned['events']):
            p=original['payload']
            if original['type'] in ('tool_state','tool_result') and p.get('status') in ('queued','running','pending','awaiting_approval'):
                p={**p,'imported_status':p['status'],'status':'interrupted'}
            if original['type']=='request_state':
                # An imported archive cannot attest that its token counts came from a provider.
                p={key:value for key,value in p.items() if key!='reported_usage'}
            actual=imported['payload']
            assert actual==p,{'type':original['type'],
                'added':sorted(set(actual)-set(p)), 'removed':sorted(set(p)-set(actual)),
                'changed':sorted(key for key in set(actual)&set(p) if actual[key]!=p[key])}
        assert not cloned['pending_tools'] and clone['provider_profile_id'] is None

        # Only lifecycle metadata is changed to represent a legacy request ID reused by two runs.
        shared=next(iter(requests))
        lifecycle=[{'type':kind,'payload':{**pair[0]}} for kind,pair in terminal.items()]
        lifecycle[1]['payload']['request_id']=shared
        for e in archive[1:]:
            if e['type']=='run_state' or (e['type']=='request_state' and e['payload']['request_id']==shared):
                lifecycle.append({'type':e['type'],'payload':e['payload']})
        restored=import_lines(lifecycle)
        history=api.call('GET','/conversations/'+str(restored['id']))['events']
        for kind in ('run_state','request_state'):
            for rid,end in [(active_run,'interrupted'),(completed_run,'completed')]:
                pair=[e['payload'] for e in history if e['type']==kind and e['payload']['run_id']==rid]
                assert [p['status'] for p in pair]==['started',end],pair
                assert pair[1]['started_at']==pair[0]['started_at'] and pair[1]['duration_ms']>=0
                if kind=='request_state':
                    assert pair[0]['request_id']==pair[1]['request_id']==shared
                if rid==active_run:
                    assert pair[1]['imported_status']=='started'
        import_lines([{'type':'request_state','payload':{'run_id':active_run}}],expected=400)
    translation=api.call('POST','/llm/translate',{'text':'Heat transfer coefficient','target':'zh',
                                                'provider_profile_id':profile['id']})
    assert translation['ok'] and '传热' in translation['translation'] and '系数' in translation['translation']
    report={'source_conversation':cid,'completed_run':completed_run,'disconnected_run':active_run,
            'distinct_request_indices':[v[0]['request_index'] for v in requests.values()],
            'source_jsonl_sha256':hashlib.sha256(raw).hexdigest(),'actual_http_client_disconnected':True,
            'independent_request_and_run_timing':True,'shared_legacy_request_id_scoped_to_run':True,
            'real_turn_roundtrip_payload_count':len(archive)-1,'disconnected_states':terminal,
            'imported_usage_unattested':True,'actual_non_streaming_translation':translation['translation']}
    api.create_case('Independent lifecycle proof readback')
    for owned in (cid,clone['id'],restored['id']):
        api.call('GET','/conversations/'+str(owned),expect=404)
    (api.root/'jsonl-lifecycle.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    return report


if __name__=='__main__':
    asyncio.run(main(selected_factory=lambda config:partial(scenario,config)))
