"""Verify SCNET endpoint completion and fault diagnostics over isolated HTTP."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import parse_qs, urlsplit

import httpx

from app.services.scnet import compact_sign, safe_link, service_url
from testsupport import IsolatedServer


ACCESS = 'diagnostic-sample-ak'
SECRET = 'diagnostic-sample-sk'
TOKEN = 'diagnostic-session-token'
UNKNOWN = 'other-service-credential'


class ProtocolPeer(BaseHTTPRequestHandler):
    def do_GET(self):
        expected = compact_sign({'accessKey': ACCESS, 'token': TOKEN}, SECRET)
        assert self.headers.get('X-Access-Key') == ACCESS and self.headers.get('X-Signature') == expected
        assert self.headers.get('token') == TOKEN
        route = urlsplit(self.path)
        self.server.routes.append(route.path)
        fault = int(parse_qs(route.query).get('fault', [0])[0])
        if fault == 400:
            payload = f'Authorization: Bearer {UNKNOWN} token={TOKEN}'.encode()
        elif fault == 431:
            payload = json.dumps({'error': {'code':10009, 'msg': f'Bad header: {SECRET}'}}).encode()
        elif fault == 494:
            payload = json.dumps({'message': f'Large header for {ACCESS}', 'code': 'header_size'}).encode()
        elif fault == 200:
            payload = json.dumps({'msg':'Session rejected: '+TOKEN,'code':'10009'}).encode()
        else:
            payload = b'{"data":{"records":[{"id":"reference"}]}}'
        self.send_response(fault or 200)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_):
        pass


def verify():
    roots = [('https://compute.example.com', 'hpc', '/hpc'), ('https://compute.example.com/', 'ai', '/ai'),
             ('https://compute.example.com/hpc', 'hpc', '/hpc'), ('https://compute.example.com/custom', 'ai', '/custom')]
    for url, service, expected in roots:
        normalized = service_url(url, service)
        assert urlsplit(normalized).path == expected and service_url(normalized, service) == normalized
    assert safe_link('https://compute.example.com/hpc', '/desktop?ticket=example') == 'https://compute.example.com/desktop?ticket=example'

    with ThreadingHTTPServer(('127.0.0.1',0),ProtocolPeer) as server:
        server.routes = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        api = IsolatedServer('ff-i20-scnet-http')
        report = {'model_calls':0, 'passed':False, 'error_responses':[]}
        print('I20_EVIDENCE', api.root, flush=True)
        try:
            with api, httpx.Client(trust_env=False, timeout=20) as app:
                api.create_case('SCNET diagnostics')
                endpoint = api.base+'/api/v1/scnet/'
                base = f'http://127.0.0.1:{server.server_port}'
                access = {'Authorization':'Bearer '+api.token}

                def call(operation='jobs.list', *, root=base, ai=None, args=None, headers=access, status=200):
                    response=app.post(endpoint+operation,headers=headers,json={'base_url':root,'access_key':ACCESS,
                        'secret_key':SECRET,'token':TOKEN,'args':args or {},'ai_base_url':ai})
                    assert response.status_code==status,(operation,response.status_code,status)
                    assert not any(value in response.text for value in (ACCESS,SECRET,TOKEN,UNKNOWN))
                    return response.json()

                call(headers={},status=401)
                for bad in ('https:///missing-host', 'https://@example.com', 'https://example.com:99999',
                            'https://example.com:non-numeric', 'http://compute.example.com', 'https://example.com?a=b'):
                    call(root=bad,status=400)
                assert not server.routes
                assert call()==[{'id':'reference'}]
                assert call(root=base+'/custom')==[{'id':'reference'}]
                assert call('containers.list')==[{'id':'reference'}]
                assert call('notebooks.list', root=base+'/hpc', ai=base+'/ai')==[{'id':'reference'}]
                for fault in (400,431,494,200):
                    detail=call(args={'query':{'fault':fault,'token':TOKEN}},status=502)['detail']
                    assert detail['status']==fault and '?' not in detail['url']
                    assert bool(detail['hint']) == (fault != 200)
                    counts=detail['header_diagnostics']
                    assert counts['token_bytes']==len(TOKEN) and counts['total_bytes']==sum(counts['field_lengths'].values())
                    if fault==431:
                        assert detail['code']==10009 and 'Bad header' in detail['message']
                    elif fault==494:
                        assert detail['code']=='header_size' and 'Large header' in detail['message']
                    elif fault==200:
                        assert detail['code']=='10009' and 'Session rejected' in detail['message']
                    report['error_responses'].append(detail)
                assert call()==[{'id':'reference'}]
                call('jobs.detail',args={'id':'resource?#1'})
                assert server.routes[0]=='/hpc/openapi/v2/jobs' and server.routes[1]=='/custom/openapi/v2/jobs'
                assert server.routes[2:4]==['/ai/openapi/v2/containers','/ai/openapi/v2/notebooks']
                assert server.routes[-1]=='/hpc/openapi/v2/jobs/resource%3F%231'
                report.update(passed=True,routes=server.routes.copy(),invalid_roots_rejected=6,unauthenticated_status=401)
        finally:
            server.shutdown()
            thread.join(timeout=10)
            (api.root/'scnet-diagnostics.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')


if __name__=='__main__':
    verify()
