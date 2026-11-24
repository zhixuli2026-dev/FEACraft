"""Two independent API processes reserve rendering ports before kernel startup."""
import asyncio
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile

from testsupport import IsolatedServer,SERVER
from viz_execution_smoke import STL,request


async def main():
    with ExitStack() as stack:
        servers=[stack.enter_context(IsolatedServer('viz-concurrent')) for _ in range(2)]
        for index,server in enumerate(servers):
            await asyncio.to_thread(server.create_case,'concurrent-'+str(index));server.put('D-post/cube.stl',STL)
        sessions=await asyncio.gather(*(request(server,'POST',server.prefix+'/viz:sessions',{'file_path':'D-post/cube.stl'}) for server in servers))
        assert sessions[0]['port']!=sessions[1]['port'] and all(row['alive'] is True for row in sessions),sessions
        for server,row in zip(servers,sessions):
            page=await request(server,'GET',f"/viz-proxy/{row['id']}/index.html?token={server.token}",raw=True,api=False)
            assert b'trame' in page.lower()
        await asyncio.gather(*(request(server,'DELETE',f"/viz:sessions/{row['id']}") for server,row in zip(servers,sessions)))
        for server,row in zip(servers,sessions):
            state=await request(server,'GET',f"/viz:sessions/{row['id']}")
            assert state['cleanup_verified'] is True and state['phase']=='closed',state
        report=Path(tempfile.mkdtemp(prefix='viz-concurrency-evidence-',dir=SERVER.parents[1]/'tmp'))
        result={'passed':True,'independent_apis':2,'real_ready_kernels':2,'distinct_ports':[row['port'] for row in sessions],
            'proxy_roundtrip':True,'verified_close':True,'host_local_only':True,'evidence':[str(server.root) for server in servers]}
        (report/'result.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
        print('VIZ_CONCURRENCY_OK '+json.dumps({'evidence':str(report),'ports':result['distinct_ports']}),flush=True)


if __name__=='__main__':asyncio.run(main())
