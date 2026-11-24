"""Actual HTTP directory and file downloads against an isolated signed provider."""
import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import threading
import time
from urllib.parse import parse_qs, urlsplit
import zipfile

import httpx

from app.services.scnet import compact_sign
from testsupport import IsolatedServer


PROOF_TEXT = '已有远程文本，雪山🌊\n第二行\t完成'
SETTINGS = '''
import tracemalloc
from fastapi import Depends
from app.main import app
from app.deps import get_current_user
from app.archive_admission import ArchiveAdmission
from app.config import get_settings
from app.services import node_storage_budget

@app.post('/api/v1/test/scnet-download-policy')
def policy(body:dict,user=Depends(get_current_user)):
 if 'limit_mb' in body:get_settings().node_storage_limit_mb=body['limit_mb']
 if 'archive_seconds' in body:ArchiveAdmission.seconds=body['archive_seconds']
 if body.get('measure'):tracemalloc.start();tracemalloc.reset_peak()
 return {'ok':True}

@app.get('/api/v1/test/scnet-download-measurement')
def measurement(user=Depends(get_current_user)):
 value=node_storage_budget.observe()
 if tracemalloc.is_tracing():value['python_peak_bytes']=tracemalloc.get_traced_memory()[1];tracemalloc.stop()
 return value
'''


class Provider:
    def __init__(self, archive):
        self.archive = archive
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()
        self.requests = []
        self.credentials = {'access_key':'isolated-test-access', 'secret_key':'isolated-test-secret', 'token':'isolated-test-token'}

    def __enter__(self):
        provider = self
        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def handle(self):
                try:
                    super().handle()
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
                    pass

            def boundary(self):
                provider.entered.set()
                return provider.release.wait(15)

            def reply(self, status, payload):
                self.send_response(status)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def chunk(self, data):
                self.wfile.write(f'{len(data):x}\r\n'.encode()+data+b'\r\n')
                self.wfile.flush()

            def do_GET(self):
                credentials = provider.credentials
                expected = compact_sign({'accessKey':credentials['access_key'],'token':credentials['token']},credentials['secret_key'])
                assert self.headers['X-Signature']==expected
                assert self.headers['X-Access-Key']==credentials['access_key'] and self.headers['token']==credentials['token']
                url=urlsplit(self.path)
                if url.path.endswith('/jobs'):
                    self.reply(200,b'{"records":[]}');return
                if url.path.endswith('/files'):
                    self.reply(200,'{"fileList":[{"path":"导出/","name":"导出目录"}]}'.encode());return
                assert url.path=='/hpc/openapi/v2/files:download'
                name=parse_qs(url.query)['path'][0];provider.requests.append(name)
                try:
                    if name=='头过大/':
                        self.send_response(200);self.send_header('Content-Length',str(150*1024*1024))
                        self.send_header('Connection','close');self.end_headers();self.close_connection=True
                        return
                    if name=='错误/':
                        self.reply(431,json.dumps({'code':credentials['token'],'message':credentials['secret_key']}).encode());return
                    if name=='坏格式/':
                        self.reply(200,b'{"content":"UEsDBA===bad","format":"zip"}');return
                    if name=='尾部中断/':
                        self.reply(200,b'{"content":"UEsDBA==","format":"zip"');return
                    if name=='坏目录尾/':
                        self.reply(200,b'{"content":"UEsDBA==","format":"zip"}');return
                    if name=='文本.txt':
                        self.reply(200,json.dumps({'content':PROOF_TEXT},ensure_ascii=True).encode());return
                    self.send_response(200);self.send_header('Content-Type','application/json')
                    self.send_header('Transfer-Encoding','chunked');self.end_headers()
                    self.chunk(b'{"content":"')
                    with provider.archive.open('rb') as binary:
                        blocks=0
                        for raw in iter(lambda:binary.read(48*1024), b''):
                            self.chunk(base64.b64encode(raw));blocks+=1
                            if name=='慢速/' and blocks==64 and not self.boundary():return
                    self.chunk(b'","format":"zip"}')
                    self.wfile.write(b'0\r\n\r\n');self.wfile.flush()
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
                    pass
                finally:
                    if name=='慢速/':provider.finished.set()

        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        return self

    def __exit__(self,*_):
        self.release.set();self.server.shutdown();self.server.server_close();self.thread.join(5)


def until(callback, description, timeout=15):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        value=callback()
        if value:return value
        time.sleep(.04)
    raise TimeoutError(description)


def verify(api, provider, expected, size, report):
    bearer={'Authorization':'Bearer '+api.token}
    def payload(path):return {**provider.credentials,'base_url':provider.base,'args':{'path':path}}
    route=api.base+'/api/v1/scnet/files:download'
    def download(path, *, save=False):
        with httpx.Client(timeout=75,trust_env=False,headers=bearer) as client:
            with client.stream('POST',route,json=payload(path)) as response:
                if response.status_code!=200:return response.status_code,json.loads(response.read())['detail']
                digest=hashlib.sha256();total=0
                destination = (api.root/'received.zip').open('wb') if save else nullcontext(None)
                with destination as target:
                    for block in response.iter_bytes(65536):
                        digest.update(block);total+=len(block)
                        if target is not None:target.write(block)
                return 200,{'sha256':digest.hexdigest(),'size':total,'headers':dict(response.headers)}

    def no_claims():return api.call('GET','/test/scnet-download-measurement')['reserved_bytes']==0
    api.call('POST','/test/scnet-download-policy',{'measure':True})
    with ThreadPoolExecutor(max_workers=1) as work:
        pending=work.submit(download,'慢速/',save=True)
        try:
            assert provider.entered.wait(35)
            def staged_claims():
                if pending.done():raise AssertionError(pending.result())
                return list((api.root/'files'/'.node-storage-budget').glob('*.json'))
            claims=until(staged_claims,'API did not admit actual download bytes')
            node_reserved=sum(json.loads(claim.read_text())['bytes'] for claim in claims)
            assert node_reserved>=4*1024*1024
            started=time.monotonic()
            other=api.call('POST','/workspaces',{'name':'Independent write while the remote body is paused'})
            report['independent_db_write_seconds']=round(time.monotonic()-started,3)
            assert other['id']
            rejected=download('文本.txt')
            assert rejected[0]==429 and rejected[1]['code']=='archive_busy',rejected
        finally:provider.release.set()
        status, result=pending.result()
        assert status==200 and result['sha256']==expected and result['size']==size, result
        assert int(result['headers']['content-length'])==size and result['headers']['content-type']=='application/zip'
    memory=api.call('GET','/test/scnet-download-measurement')
    assert memory['python_peak_bytes']<14*1024*1024,memory
    until(no_claims,'Completed response still has a node reservation')
    with zipfile.ZipFile(api.root/'received.zip') as archive:
        assert archive.getinfo('导出/data.bin').file_size==40*1024*1024 and archive.testzip() is None
    report.update(zip_sha256=expected,zip_bytes=size,node_reservation_during_transport=node_reserved,
                  python_peak_bytes=memory['python_peak_bytes'],shared_slot_status=429)

    status,text=download('文本.txt')
    assert status==200 and text['sha256']==hashlib.sha256(PROOF_TEXT.encode()).hexdigest()
    assert "filename*=UTF-8''" in text['headers']['content-disposition']
    assert api.call('POST','/scnet/files.download',payload('文本.txt'))==PROOF_TEXT
    legacy=api.call('POST','/scnet/files.download',payload('导出/'),expect=413)
    assert legacy['detail']['code']=='scnet_download_too_large'
    for name,expected_code in [('头过大/',413),('坏格式/',502),('尾部中断/',502),('坏目录尾/',502)]:
        assert download(name)[0]==expected_code,name
    code, error=download('错误/')
    assert code==502 and error['status']==431
    assert all(secret not in json.dumps(error) for secret in provider.credentials.values())
    api.call('POST','/test/scnet-download-policy',{'limit_mb':2})
    limited=download('导出/')
    assert limited[0]==409 and limited[1]['code']=='node_storage_full',limited
    until(no_claims,'Quota denial retained a node reservation')
    api.call('POST','/test/scnet-download-policy',{'limit_mb':4096})

    provider.entered.clear();provider.release.clear();provider.finished.clear()
    body=json.dumps(payload('慢速/')).encode()
    with socket.create_connection(('127.0.0.1',api.port),timeout=5) as connection:
        headers=('POST /api/v1/scnet/files:download HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n'
                 f'Authorization: {bearer["Authorization"]}\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n')
        connection.sendall(headers.encode()+body)
        assert provider.entered.wait(35)
        connection.shutdown(socket.SHUT_RDWR)
    provider.release.set()
    assert provider.finished.wait(15)
    until(no_claims,'Disconnected download retained node reservation')
    assert download('文本.txt')[0]==200
    provider.entered.clear();provider.release.clear();provider.finished.clear()
    api.call('POST','/test/scnet-download-policy',{'archive_seconds':2})
    with ThreadPoolExecutor(max_workers=1) as work:
        pending=work.submit(download,'慢速/')
        try:
            assert provider.entered.wait(5)
            rejected=pending.result(timeout=12)
            assert rejected[0]==504 and rejected[1]['code']=='archive_timeout',rejected
        finally:provider.release.set()
    api.call('POST','/test/scnet-download-policy',{'archive_seconds':300})
    until(no_claims,'Expired response retained a node reservation')
    assert download('文本.txt')[0]==200
    report.update(legacy_small_and_large=[200,413],bad_archive_statuses=[413,502,502,502],remote_error_redacted=True,
                  node_full_status=409,actual_client_disconnect_drained=True,remaining_reserved_bytes=0,
                  stalled_remote_timeout_status=504)


def browser(api, provider, expected):
    from playwright.sync_api import expect, sync_playwright

    result={}
    with sync_playwright() as playwright:
        edge=playwright.chromium.launch(channel='msedge',downloads_path=str(api.root))
        context=edge.new_context(viewport={'width':730,'height':970},accept_downloads=True,locale='zh-CN')
        context.add_init_script('if(location.origin==='+json.dumps(api.base)+')localStorage.setItem("vf_token",'
                                +json.dumps(api.token)+');')
        page=context.new_page()
        page.goto(api.base,wait_until='domcontentloaded')
        page.get_by_label('工作面板',exact=True).select_option('scnet')
        page.get_by_test_id('scnet-base-url').fill(provider.base)
        page.get_by_label('Access Key',exact=True).fill(provider.credentials['access_key'])
        page.get_by_test_id('scnet-secret').fill(provider.credentials['secret_key'])
        page.get_by_label('会话令牌',exact=True).fill(provider.credentials['token'])
        page.get_by_test_id('scnet-connect').click()
        expect(page.get_by_test_id('scnet-status')).to_contain_text('jobs.list 完成')
        page.get_by_test_id('scnet-tab-files').click()
        row=page.get_by_test_id('scnet-table').get_by_text('导出目录',exact=True).locator('..')
        with page.expect_download(timeout=75000) as download:
            row.get_by_role('button',name='下载',exact=True).click()
        completed=download.value
        assert completed.suggested_filename=='导出.zip'
        path=api.root/'browser-directory.zip';completed.save_as(path)
        with path.open('rb') as stream:assert hashlib.file_digest(stream,'sha256').hexdigest()==expected
        expect(page.get_by_test_id('scnet-status')).to_contain_text('文件已交给浏览器保存')
        expect(page.get_by_test_id('scnet-download-result')).to_have_count(0)
        page.screenshot(path=str(api.root/'browser-download.png'))
        result.update(edge=edge.version,name=completed.suggested_filename,sha256=expected,bytes=path.stat().st_size)
        edge.close()
    return result


def run(args, database_url=None):
    api=IsolatedServer('scnet-download-acceptance',app='scnet_download_fixture:app',database_url=database_url)
    fixture=SETTINGS
    if args.dist:
        dist=args.dist.resolve(strict=True);assert (dist/'index.html').is_file()
        fixture+='\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/",StaticFiles(directory='+repr(str(dist))+',html=True))\n'
    (api.root/'scnet_download_fixture.py').write_text(fixture,encoding='utf8')
    archive_path=api.root/'provider.zip'
    block=(bytes(range(256))*4096)
    with zipfile.ZipFile(archive_path,'w',zipfile.ZIP_STORED) as archive:
        with archive.open('导出/data.bin','w') as target:
            for _ in range(40):target.write(block)
    with archive_path.open('rb') as source:expected=hashlib.file_digest(source,'sha256').hexdigest()
    report={'passed':False,'model_calls':0,'simulated_llm_calls':0,'database':'postgresql' if database_url else 'sqlite'}
    print('SCNET_DOWNLOAD_EVIDENCE',api.root,flush=True)
    try:
        with Provider(archive_path) as provider, api:
            api.create_case('Actual SCNET binary transport')
            verify(api,provider,expected,archive_path.stat().st_size,report)
            if args.dist:report['browser']=browser(api,provider,expected)
        def closed():
            with socket.socket() as probe:
                probe.settimeout(1);return probe.connect_ex(('127.0.0.1',api.port))!=0
        until(closed,'Isolated SCNET API still listening')
        assert not provider.thread.is_alive()
        report.update(passed=True,isolated_api_closed=True,isolated_provider_closed=True)
    finally:
        (api.root/'download-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    return 0


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist',type=Path)
    parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args()
    if args.postgres:
        from mixed_read_capacity_checks import postgres
        return asyncio.run(postgres(None,'scnetdownload',lambda _store,url,_group:run(args,url)))
    return run(args)


if __name__=='__main__':
    raise SystemExit(main())
