"""Explicit real DeepSeek verification of actual recovered WSL output.

Uses original production sandbox -> API death -> recovery -> retirement ->
confirmed file restoration -> model-chosen read_text_file -> streamed reply.
No model/provider/CLI inference substitution. Credentials never leave the vault
except authorized requests to the configured official DeepSeek service.
"""
import argparse
import asyncio
import json
import sys
import time
import uuid

import httpx

from live_llm_acceptance import FIXTURE,source
from sandbox_retirement_checks import verify
from testsupport import SERVER


def model_read(api,sid,rid,proof,configuration,*,already_restored=False,path='Ref/retirement.txt',expected_content=None):
    result={'passed':False,'simulated_llm':False,'provider':'deepseek','host':'api.deepseek.com',
        'model':configuration['model'],'source_database_modified':False,'session_id':sid,'recovery_id':rid}
    events=[]
    def save():
        trace=api.root/'upstream.jsonl'
        result['upstream']=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []
        result['llm_calls']=sum(row['kind']=='http' and row['path'].endswith('/chat/completions') for row in result['upstream'])
        raw=json.dumps(events,ensure_ascii=False,indent=2)
        assert configuration['key'] not in raw
        (api.root/'live-model-events.json').write_text(raw,encoding='utf8')
        raw=json.dumps(result,ensure_ascii=False,indent=2)
        assert configuration['key'] not in raw
        (api.root/'live-result.json').write_text(raw,encoding='utf8')
    try:
        expected_content = (proof+'\n').encode() if expected_content is None else expected_content
        endpoint='/terminal/sessions/'+sid+'/recoveries/'+rid
        if not already_restored:
            plan=api.call('POST',endpoint+'/restore-plan',{'paths':[path]})
            receipt=api.call('POST',endpoint+'/restore',{'paths':[path],'request_id':str(uuid.uuid4()),
                'plan_sha256':plan['plan_sha256'],'confirm':True,'semantics':'file_contents_only'})
            assert receipt['written_files']==[path]
            result['restoration_request_id']=receipt['request_id']
        else:result['read_existing_restored_file']=True
        assert api.call('GET',api.prefix+'/raw?path='+path,raw=True)==expected_content
        profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Real sandbox recovery acceptance',
            'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
        conversation=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Read actual restored shell output',
            'provider_profile_id':profile['id'],'model':configuration['model'],'permission_mode':'plan'})
        cid=conversation['id'];result['conversation_id']=cid
        with httpx.Client(base_url=api.base+'/api/v1',headers={'Authorization':'Bearer '+api.token},trust_env=False,timeout=180) as client:
            # The random proof is deliberately absent from the prompt. Only the
            # real tool reading the restored shell file can supply its content.
            deadline=time.monotonic()+45
            while True:
                with client.stream('POST',f'/conversations/{cid}/messages',json={
                    'content':f'请调用 read_text_file 读取 {path}，然后原样回复文件里 RETIREMENT_ 开头的完整字符串（不含引号和分号）。仅调用这个工具一次，不修改文件，不猜测；拿到结果后直接回答。',
                    'provider_profile_id':profile['id'],'model':configuration['model']}) as response:
                    if response.is_error:
                        raw=response.read()
                        try:code=json.loads(raw).get('detail',{}).get('code')
                        except (ValueError,AttributeError):code=None
                        if response.status_code==409 and code=='storage_quota_busy' and time.monotonic()<deadline:
                            result.setdefault('admission_rejections',[]).append({'status':409,'code':code})
                            time.sleep(.2)
                            continue
                        detail=raw.decode('utf8','replace').replace(configuration['key'],'[REDACTED]')[:1200]
                        raise RuntimeError('Actual model request rejected ('+str(response.status_code)+'): '+detail)
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if line.startswith('data:'):events.append(json.loads(line[5:]))
                        if len(json.dumps(events))>2*1024*1024:raise ValueError('Live evidence exceeds bound')
                    break
        save()
        failures=[e for e in events if e.get('type') in {'stream.error','request.failed','request.timed_out'}]
        assert not failures,failures
        tools=[e for e in events if e.get('type')=='tool.result']
        assert len(tools)==1 and tools[0]['tool']=='read_text_file' and tools[0]['ok']
        answer=''.join(e.get('text','') for e in events if e.get('type')=='text.delta')
        assert proof in answer
        assert any(e.get('type')=='run.completed' for e in events)
        history=api.call('GET',f'/conversations/{cid}')
        stored=[e['payload'] for e in history['events'] if e['type']=='tool_result'
                and e['payload'].get('tool_call_id')==tools[0]['tool_call_id']]
        assert len(stored)==1 and stored[0]['ok'] and proof in stored[0]['result']
        # SSE intentionally carries a 2000-character UI preview; the model and
        # durable event receive the complete result (including long HTML).
        assert tools[0]['result']==stored[0]['result'][:2000]
        assert api.call('GET',api.prefix+'/raw?path='+path,raw=True)==expected_content
        successful=[e for e in result['upstream'] if e['kind']=='http' and e['path'].endswith('/chat/completions') and e['status']==200]
        assert len(successful)>=2
        result.update(passed=True,answer=answer,tool='read_text_file',persisted=True,restored_source_unchanged=True,
            strict_answer_only_proof=answer.strip()==proof)
        save()
        print('LIVE_RECOVERY_MODEL_PASS '+json.dumps({'model':result['model'],'calls':result['llm_calls'],'evidence':str(api.root/'live-result.json')}),flush=True)
        return result['llm_calls']
    except BaseException as error:
        result['failure']={'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1000]}
        save()
        # Avoid an exception traceback that might contain HTTP request details.
        raise RuntimeError('Real model acceptance failed; see '+str(api.root/'live-result.json')) from None


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--s3',action='store_true')
    parser.add_argument('--source-image-absent',action='store_true')
    group=parser.add_mutually_exclusive_group()
    group.add_argument('--cleanup-original-staging',action='store_true')
    group.add_argument('--staging-restart-matrix',action='store_true')
    group.add_argument('--cleanup-image',action='store_true')
    group.add_argument('--image-restart-matrix',action='store_true');args=parser.parse_args()
    configuration=await source(args.source_profile_id)
    async def callback(api,sid,rid,proof):
        return await asyncio.to_thread(model_read,api,sid,rid,proof,configuration)
    async def acceptance(api,sid,rid,proof):
        if args.source_image_absent:
            from sandbox_snapshot_independence_checks import without_image
            return await without_image(api,sid,rid,proof,continuation=callback)
        return await callback(api,sid,rid,proof)
    fixture = FIXTURE
    if args.image_restart_matrix:
        if args.source_image_absent or args.s3:
            parser.error('--image-restart-matrix currently requires FS with the original image')
        from sandbox_image_restart_checks import after_retirement, FIXTURE as CRASH_FIXTURE
        previous = acceptance
        async def acceptance(api,sid,rid,proof):
            return await after_retirement(api,sid,rid,proof,continuation=previous)
        fixture += CRASH_FIXTURE
    if args.cleanup_image:
        if args.source_image_absent:parser.error('--cleanup-image performs actual deletion; omit --source-image-absent')
        from sandbox_image_cleanup_checks import after_retirement
        previous = acceptance
        async def acceptance(api,sid,rid,proof):
            return await after_retirement(api,sid,rid,proof,continuation=previous)
    if args.cleanup_original_staging or args.staging_restart_matrix:
        if not args.s3:
            parser.error('--cleanup-original-staging requires --s3')
        if args.staging_restart_matrix:
            from sandbox_staging_restart_checks import after_retirement, FIXTURE as CRASH_FIXTURE
        else:
            from sandbox_staging_cleanup_checks import after_retirement, CRASH_FIXTURE
        previous = acceptance
        async def acceptance(api, sid, rid, proof):
            return await after_retirement(api, sid, rid, proof, continuation=previous)
        fixture += CRASH_FIXTURE
    if args.s3:
        from s3testsupport import ObjectStore
        with ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed') as store:
            await verify(store,fixture_prefix=fixture,after_retirement=acceptance)
    else:await verify(fixture_prefix=fixture,after_retirement=acceptance)


if __name__=='__main__':asyncio.run(main())
