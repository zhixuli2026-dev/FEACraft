"""Exercise archived report draft downloads while pausing the actual body read."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlencode

import httpx


FIXTURE = '''
import threading
import time
from app.services import artifact_publication, file_read

def pause_report_body(path):
    marker = root/'compose-download-target'
    if not marker.is_file() or not str(path).endswith(marker.read_text(encoding='utf-8')):
        return
    entered = root/'compose-download-entered'
    entered.write_text(threading.current_thread().name, encoding='utf-8')
    limit = time.monotonic()+15
    while not (root/'compose-download-release').exists():
        assert time.monotonic()<limit, 'Test never released the selected body read'
        time.sleep(.025)

original_read_file = artifact_publication.read_file
def read_file(prefix,row,path):
    pause_report_body(path)
    return original_read_file(prefix,row,path)
artifact_publication.read_file = read_file

original_chunks = file_read.ArchivedMemberRead.chunks
def chunks(self):
    pause_report_body(self.path)
    yield from original_chunks(self)
file_read.ArchivedMemberRead.chunks = chunks
'''


def scenario(api, *, baseline, **_):
    from live_llm_remote_post_report_acceptance import scenario as run_report
    from sqlalchemy.engine import make_url

    selected = make_url(api.database_url) if api.database_url else None
    result = asyncio.run(run_report(api, selected=selected))
    output = 'E-report/report.md'
    draft = api.call('GET', api.prefix+'/report/compose-result?'+urlencode({'output':output}))
    path = draft['source_path']
    original = (api.root/'report.md').read_bytes()
    assert original==draft['content'].encode('utf-8')
    query = urlencode({'publication_id':draft['publication_id'],'path':path,'output':output})
    route = api.prefix+'/report/compose-artifact?'+query
    base = api.base + '/api/v1'
    headers = {'Authorization':'Bearer '+api.token}
    workspace_id = api.call('GET', api.prefix)['workspace_id']
    other = api.call('POST', f'/workspaces/{workspace_id}/cases', {'name':'Report download unrelated write'})
    marker = api.root/'compose-download-target'
    marker.write_text(path, encoding='utf-8')
    entered = api.root/'compose-download-entered'
    release = api.root/'compose-download-release'
    with ThreadPoolExecutor(max_workers=1) as pool, httpx.Client(headers=headers, timeout=45, trust_env=False) as client:
        download = pool.submit(client.get, base+route)
        try:
            deadline = time.monotonic()+10
            while not entered.exists():
                if download.done():
                    response = download.result()
                    raise AssertionError(f'Download completed before reaching the held read: {response.status_code} {response.text[:180]}')
                assert time.monotonic()<deadline, 'Download did not reach the actual body read'
                time.sleep(.025)
            reader_thread = entered.read_text(encoding='utf-8')
            try:
                health = client.get(api.base+'/api/health', timeout=1)
                assert health.status_code==200 and health.json()['ok']
                health_responsive = True
            except httpx.ReadTimeout:
                health_responsive = False
            assert health_responsive is not baseline
            if not baseline:
                write = client.put(base+f"/cases/{other['id']}/file", json={'path':'Ref/during-download.txt','content':'released SQL before read\n'}, timeout=8)
                assert write.status_code==200, write.text
                assert not download.done()
        finally:
            release.touch()
        response = download.result(timeout=45)
        assert response.status_code==200 and response.content==original
        assert response.headers['content-type'].startswith('text/markdown')
        assert response.headers['content-disposition']=='attachment; filename="report.md"'
        if not baseline:
            version = response.headers['x-vibeflow-file-version']
            segment = client.get(base+route,headers={'Range':'bytes=1-25','X-Vibeflow-File-Version':version})
            assert segment.status_code==206 and segment.content==original[1:26]
            assert segment.headers['content-range']==f'bytes 1-25/{len(original)}'
            head = client.head(base+route)
            assert head.status_code==200 and not head.content and int(head.headers['content-length'])==len(original)
            wrong_range = client.get(base+route, headers={'Range':f'bytes={len(original)}-'})
            assert wrong_range.status_code==416 and not wrong_range.content
            assert client.get(base+route, headers={'X-Vibeflow-File-Version':'0'*64}).status_code==409
            wrong_output = route.replace(urlencode({'output':output}),urlencode({'output':'E-report/report.generated.md'}))
            assert client.get(base+wrong_output).status_code==404
            assert client.get(base+route.replace(f'/cases/{api.case_id}/', f"/cases/{other['id']}/")).status_code==404
            implicit = api.prefix+'/report/compose-artifact?'+urlencode({'path':path})
            assert client.get(base+implicit).content==original
            derivative = api.prefix+'/report/derivative-artifact?'+urlencode({'publication_id':draft['publication_id'],'path':path})
            assert client.get(base+derivative).content==original
            archive = verify_large_previous_draft(api, client, base, output)
    outcome = {'passed':True, 'baseline':baseline,'health_responsive_during_read':health_responsive,
               'independent_write_completed_during_read':not baseline,'reader_thread':reader_thread,
               'actual_report_bytes':len(original),'sha256':hashlib.sha256(original).hexdigest(),
               'actual_temperature_k':result['mean_temperature_k'], 'http_ranges_verified':not baseline,
               'large_previous_draft':None if baseline else archive}
    (api.root/'compose-download-capacity.json').write_text(json.dumps(outcome,indent=2)+'\n',encoding='utf-8')
    return result


def verify_large_previous_draft(api, client, base, output):
    block = b'An archived report draft.\n' * 1000
    repeats = 340
    generated = ("from pathlib import Path\n"
                 f"block = {block[:26]!r} * 1000\n"
                 f"with Path({output!r}).open('wb') as draft:\n"
                 f"    for _ in range({repeats}):\n"
                 "        draft.write(block)\n")
    assert block[:26]*1000 == block
    api.call('PUT',api.prefix+'/file',{'path':'Ref/large-draft.py','content':generated})
    seed = api.call('POST',api.prefix+'/jobs',{'type':'convert','runtime':'windows','command':'python Ref/large-draft.py'})
    api.wait_job(seed['id'], timeout=120)
    compose = api.call('POST',api.prefix+'/report:compose?wait=false',{'output':output,'overwrite':True},expect=(200,202))
    job = api.wait_job(compose['job_id'], timeout=180)
    publication_id = job['attempt']['outputs']['publication']['id']
    trace = api.call('GET',api.prefix+'/provenance/artifacts/'+publication_id)
    path = next(name for name in trace['files'] if name.endswith('/previous.md'))
    query = urlencode({'publication_id':publication_id,'path':path,'output':output})
    route = base+api.prefix+'/report/compose-artifact?'+query
    with client.stream('GET',route,timeout=90) as response:
        assert response.status_code==200, response.status_code
        assert response.headers['content-disposition']=='attachment; filename="previous.md"'
        count, received = 0, hashlib.sha256()
        for data in response.iter_bytes():
            count += len(data)
            received.update(data)
    expected = hashlib.sha256()
    for _ in range(repeats):
        expected.update(block)
    assert count==len(block)*repeats and received.hexdigest()==expected.hexdigest()
    return {'bytes':count,'sha256':received.hexdigest()}


if __name__ == '__main__':
    from live_llm_job_repeat_acceptance import main
    from s3testsupport import ObjectStore, SERVER

    baseline = '--baseline' in sys.argv
    if baseline:
        sys.argv.remove('--baseline')
    binary = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    asyncio.run(main(selected_override=partial(scenario,baseline=baseline),fixture_override=FIXTURE,
                     store_factory=lambda:ObjectStore(executable=binary,volume_max=8,authenticated=True)))
