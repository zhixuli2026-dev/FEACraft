"""Actual SQLite/API/PTY concurrency diagnosis. No model or CLI inference."""
import argparse,asyncio,hashlib,json,sys,time,uuid
from pathlib import Path
import httpx,websockets
from live_llm_shared_index_acceptance import SharedIndexServer,configure,ROOT
from live_llm_opencode_provider_acceptance import DIAGNOSTICS

from sqlite_acceptance_observer import SQL_OBSERVER

async def exercise(api,result):
    headers={'Authorization':'Bearer '+api.token}
    async with httpx.AsyncClient(base_url=api.base+'/api/v1',headers=headers,timeout=75,trust_env=False) as client:
        async def get(path):
            at=time.monotonic()
            r=await client.get(path)
            result['requests'].append({'path':path,'status':r.status_code,'elapsed':round(time.monotonic()-at,3)})
            assert r.status_code==200,(path,r.status_code)
        for round_id in range(3):
            uri = api.base.replace('http:', 'ws:') + f'/ws/terminal?case={api.case_id}&shell=cmd'
            async with websockets.connect(uri, additional_headers={'Authorization': 'Bearer ' + api.token}, proxy=None) as ws:
                ready=json.loads(await asyncio.wait_for(ws.recv(),60));assert ready['type']=='ready',ready
                sid=ready['session'];result['sessions'].append(sid)
                nonce=uuid.uuid4().hex
                await ws.send(json.dumps({'type':'input','data':'echo '+nonce+'\r'}))
                output=''
                while output.count(nonce)<2:
                    msg=await asyncio.wait_for(ws.recv(),30)
                    if isinstance(msg,bytes):output+=msg.decode('utf-8','replace')
                paths=[api.prefix+'/tree',api.prefix+'/index/status',api.prefix+'/job-batches',
                    api.prefix+'/cli-contract','/org/plans','/runtime/capabilities']
                # Actual independent requests; no barriers injected into production.
                requests=[asyncio.create_task(get(p)) for p in paths for _ in range(2)]
                await asyncio.sleep(.5)
                r=await client.delete('/terminal/sessions/'+sid)
                result['closes'].append({'status':r.status_code,'body':r.json()})
                outcomes=await asyncio.gather(*requests,return_exceptions=True)
                assert r.status_code==200,result['closes'][-1]
                assert not any(isinstance(x,BaseException) for x in outcomes),[str(x) for x in outcomes if isinstance(x,BaseException)]
                record=(await client.get('/terminal/sessions/'+sid)).json()
                assert record['phase']=='closed' and record['cleanup_verified']
                print('INFRA_ROUND_OK '+str(round_id+1),flush=True)

def main(review):
    gate=json.loads(review.read_text(encoding='utf-8'))
    assert gate['classification']=='actual_infrastructure_no_llm'
    for p,h in gate['sources'].items():assert hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h,p
    api=SharedIndexServer('sqlite-terminal-concurrency',app='live_fixture:app')
    configure(api,{},False)
    fixture=api.root/'live_fixture.py'
    fixture.write_text(fixture.read_text(encoding='utf-8')+DIAGNOSTICS+SQL_OBSERVER,encoding='utf-8')
    result={'passed':False,'real_llm_calls':0,'mock_llm_calls':0,'sessions':[],'requests':[],'closes':[]}
    print('INFRA_EVIDENCE '+str(api.root),flush=True)
    try:
        with api:
            api.create_case('SQLite terminal concurrency')
            asyncio.run(exercise(api,result))
        result['passed']=True
    except BaseException as e:
        result['failure']=type(e).__name__+': '+str(e)
    finally:
        (api.root/'infra-result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print('INFRA_RESULT '+str(result['passed']),flush=True)
    return 0 if result['passed'] else 1

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--review',type=Path,required=True)
    sys.exit(main(parser.parse_args().review))
