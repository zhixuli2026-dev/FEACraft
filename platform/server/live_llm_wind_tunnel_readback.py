"""Verify a real DeepSeek tool turn against a previously published engineering report."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import time

import httpx

from live_llm_acceptance import FIXTURE, source
from testsupport import IsolatedServer


def published_report(root):
    receipt = json.loads((root / 'wind-provider.json').read_text(encoding='utf-8'))
    assert receipt['passed'] and receipt['official_data']['http_status'] == 200
    publication = root / 'files/workspaces/1/cases/1/.vibeflow/publications' / receipt['report_publication']
    manifest = json.loads((publication / 'manifest.json').read_text(encoding='utf-8'))
    path, = (name for name in manifest['files'] if name.endswith('/report.md'))
    archive = (publication / 'files').resolve(strict=True)
    draft = (archive / path).resolve(strict=True)
    assert draft.is_relative_to(archive)
    body = draft.read_bytes()
    digest = hashlib.sha256(body).hexdigest()
    expected = receipt['alignment']['source']['sha256']
    assert manifest['files'][path]['sha256'] == digest and expected.encode('ascii') in body
    return body.decode('utf-8'), expected, digest, receipt['report_publication']


def readback(configuration, root):
    body, expected, checksum, publication = published_report(root)
    api = IsolatedServer('live-wind-tunnel', app='fixture:app')
    (api.root / 'fixture.py').write_text(FIXTURE + '\nlive_request_limit=3\n', encoding='utf-8')
    summary = {'passed':False, 'provider':'deepseek', 'host':'api.deepseek.com', 'model':configuration['model'],
               'mock_llm_calls':0, 'source_publication':publication, 'report_sha256':checksum}
    filename = api.root / 'live-result.json'
    print('LIVE_WIND_TUNNEL_EVIDENCE', api.root, flush=True)
    try:
        with api:
            api.create_case('Public wind tunnel validation report readback')
            path = 'E-report/wind-tunnel.md'
            stored = api.put(path, body)
            assert stored['content_sha256'] == checksum
            profile = api.call('POST', '/provider-profiles', {'provider':'deepseek', 'name':'Live wind tunnel readback',
                'base_url':configuration['base_url'], 'default_model':configuration['model'], 'api_key':configuration['key']})
            conversation = api.call('POST','/conversations',{'case_id':api.case_id,'title':'Read the official dataset digest',
                'permission_mode':'plan','provider_profile_id':profile['id'],'model':configuration['model']})
            prompt = ('请用 read_text_file 读取 E-report/wind-tunnel.md 一次。找到“原始观测”一行，'
                      '严格原样返回该行的 SHA-256，其余文字省略，文件保持原样。')
            events = []
            start = time.monotonic()
            with httpx.Client(base_url=api.base+'/api/v1',timeout=150,trust_env=False,
                              headers={'Authorization':'Bearer '+api.token}) as client:
                with client.stream('POST',f'/conversations/{conversation["id"]}/messages',json={
                    'content':prompt,'provider_profile_id':profile['id'],'model':configuration['model']}) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if line.startswith('data:'):
                            events.append(json.loads(line[5:]))
                        if time.monotonic()-start > 150 or len(events) > 20000:
                            raise TimeoutError('Live report readback exceeded its limit')
            answer = ''.join(event.get('text','') for event in events if event['type']=='text.delta').strip()
            tool, = (event for event in events if event['type']=='tool.result')
            assert tool['tool']=='read_text_file' and tool['ok']
            history = api.call('GET',f'/conversations/{conversation["id"]}')
            saved, = (event['payload'] for event in history['events'] if event['type']=='tool_result')
            assert saved['tool']=='read_text_file' and saved['ok'] and expected in saved['result']
            summary['strict_readback'] = answer == expected
            assert summary['strict_readback'] and any(event['type']=='run.completed' for event in events)
            assert not any(event['type'] in {'stream.error','request.failed','request.timed_out'} for event in events)
            summary.update(passed=True, observed_source_sha256=answer, stored_tool_result_verified=True)
    except BaseException as error:
        summary['failure'] = {'type':type(error).__name__,
                              'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1000]}
    finally:
        trace = api.root / 'upstream.jsonl'
        summary['upstream'] = [json.loads(line) for line in trace.read_text(encoding='utf-8').splitlines()] if trace.is_file() else []
        summary['llm_calls'] = sum(event.get('status')==200 and event['path'].endswith('/chat/completions')
                                   for event in summary['upstream'] if event.get('kind')=='http')
        summary['passed'] = summary['passed'] and summary['llm_calls']==2
        summary['api_exited'] = api.proc is not None and api.proc.poll() is not None
        encoded = json.dumps(summary,ensure_ascii=False,indent=2)
        assert configuration['key'] not in encoded
        filename.write_text(encoded+'\n',encoding='utf-8')
    if not summary['passed']:
        raise RuntimeError('Live report readback failed; see '+str(filename))
    print('LIVE_WIND_TUNNEL_OK', filename,flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--evidence',type=Path,required=True)
    args=parser.parse_args()
    readback(asyncio.run(source(args.source_profile_id)),args.evidence.resolve(strict=True))
