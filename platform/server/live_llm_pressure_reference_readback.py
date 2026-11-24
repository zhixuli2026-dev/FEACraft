"""Read a real OpenFOAM reference-pressure publication in an actual DeepSeek Agent turn."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time
from uuid import UUID

import httpx

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer


def published_receipt(root):
    receipt=json.loads((root/'pressure-reference.json').read_bytes())
    assert receipt['passed'] and receipt['backend']=='fs' and receipt['confirmed']==['B','C','D']
    publication=receipt['publication']
    assert str(UUID(publication))==publication
    archive=root/'files/workspaces/1/cases/1/.vibeflow/publications'/publication
    assert archive.resolve(strict=True).is_relative_to(root)
    manifest=json.loads((archive/'manifest.json').read_bytes())
    descriptor=manifest['descriptor']
    gauge=descriptor['configuration']['pressure_reference']
    moment={'icoFoam':'0.5','pimpleFoam':'0.2'}[receipt['solver']]
    name=f"{descriptor['run_dir']}/{moment}/p"
    output=(archive/'files'/name).resolve(strict=True)
    assert output.is_relative_to(archive/'files')
    digest=hashlib.sha256(output.read_bytes()).hexdigest()
    assert digest==receipt['pressure_sha256']==manifest['files'][name]['sha256']
    text=(f'OpenFOAM 发布 {publication}\n原生压力场摘要 {digest}\n'
          f"参考单元 {gauge['cell']} 的运动学压力为 {gauge['pressure_m2_s2']:g} m²/s²。\n")
    return publication,digest,text


def verify(configuration,root):
    publication,digest,content=published_receipt(root)
    api=IsolatedServer('live-pressure-reference',app='live_fixture:app')
    (api.root/'live_fixture.py').write_text(FIXTURE+'\nlive_request_limit=3\n',encoding='utf-8')
    result={'passed':False,'provider':'deepseek','host':'api.deepseek.com','model':configuration['model'],
            'mock_llm_calls':0,'source_publication':publication,'pressure_sha256':digest}
    destination=api.root/'live-result.json'
    print('LIVE_PRESSURE_REFERENCE_EVIDENCE',api.root,flush=True)
    try:
        with api:
            api.create_case('Native OpenFOAM pressure reference evidence')
            name='Ref/pressure-reference.txt'
            before=api.put(name,content)['content_sha256']
            profile=api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Pressure result readback',
                'base_url':configuration['base_url'],'default_model':configuration['model'],'api_key':configuration['key']})
            cid=api.call('POST','/conversations',{'case_id':api.case_id,'title':'Read the sealed OpenFOAM pressure field digest',
                'permission_mode':'plan','provider_profile_id':profile['id'],'model':configuration['model']})['id']
            prompt=(f'请用 read_text_file 读取 {name} 一次。找到“原生压力场摘要”一行，'
                    '原样返回该行的 SHA-256，其余文字省略，文件保持原样。')
            events=[]
            deadline=time.monotonic()+150
            with httpx.Client(base_url=api.base+'/api/v1',timeout=150,trust_env=False,
                              headers={'Authorization':'Bearer '+api.token}) as client:
                with client.stream('POST',f'/conversations/{cid}/messages',json={'content':prompt,
                        'provider_profile_id':profile['id'],'model':configuration['model']}) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if line.startswith('data:'):
                            events.append(json.loads(line[5:]))
                        if time.monotonic()>deadline or len(events)>20000:
                            raise TimeoutError('Live pressure readback exceeded its limit')
            tool,=(event for event in events if event['type']=='tool.result')
            answer=''.join(event.get('text','') for event in events if event['type']=='text.delta').strip()
            assert tool['tool']=='read_text_file' and tool['ok'] and digest in tool['result'] and answer==digest
            history=api.call('GET',f'/conversations/{cid}')
            saved,=(event['payload'] for event in history['events'] if event['type']=='tool_result')
            assert saved['ok'] and saved['tool']=='read_text_file' and digest in saved['result']
            assert api.call('GET',api.prefix+'/file?path='+name)['content_sha256']==before
            assert any(event['type']=='run.completed' for event in events)
            assert not any(event['type'] in {'stream.error','request.failed','request.timed_out'} for event in events)
            result.update(passed=True,readback=answer,stored_tool_result_verified=True,proof_file_unmodified=True)
    except BaseException as error:
        result['failure']={'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:800]}
    finally:
        trace=api.root/'upstream.jsonl'
        result['upstream']=[json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()] if trace.is_file() else []
        result['actual_model_calls']=sum(item.get('status')==200 and item['path'].endswith('/chat/completions')
                                         for item in result['upstream'] if item.get('kind')=='http')
        result['passed']=result['passed'] and result['actual_model_calls']==2
        result['api_exited']=api.proc is not None and api.proc.poll() is not None
        encoded=json.dumps(result,ensure_ascii=False,indent=2)
        assert configuration['key'] not in encoded
        destination.write_text(encoded+'\n',encoding='utf-8')
    if not result['passed']:
        raise RuntimeError('Live pressure reference readback failed; see '+str(destination))
    print('LIVE_PRESSURE_REFERENCE_OK',destination,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    args=parser.parse_args()
    verify(asyncio.run(source(args.source_profile_id)),args.evidence.resolve(strict=True))
