"""Real HTTP uploads and real temporary files; no model calls or inference doubles."""
import io
import json
import socket
import sys
import time
import zipfile
from pathlib import Path

import httpx
from testsupport import IsolatedServer, SERVER

FIXTURE = '''
import contextvars,json
from pathlib import Path
from starlette import formparsers
from app.main import app
from app.archive_admission import ArchiveAdmission
root=Path(__file__).parent
current=contextvars.ContextVar('spools',default=None)
original=formparsers.SpooledTemporaryFile
def observed(*args,**kwargs):
 stream=original(*args,**kwargs)
 record={'rolled':False,'closed':False}
 items=current.get()
 if items is not None:items.append((stream,record))
 close=stream.close
 def finish():
  if not stream.closed:
   record['rolled']=stream._rolled
   stream.seek(0,2);record['size']=stream.tell()
  close();record['closed']=stream.closed
 stream.close=finish
 return stream
formparsers.SpooledTemporaryFile=observed
class Observe:
 def __init__(self,inner):self.inner=inner
 async def __call__(self,scope,receive,send):
  if scope['type']!='http':return await self.inner(scope,receive,send)
  tag=dict(scope.get('headers',[])).get(b'x-test-id',b'').decode('ascii')
  items=[];token=current.set(items);received=0;status=None
  ArchiveAdmission.seconds=1 if (root/'short-deadline').exists() else 300
  async def incoming():
   nonlocal received
   event=await receive();received+=len(event.get('body',b''));return event
  async def outgoing(event):
   nonlocal status
   if event['type']=='http.response.start':status=event['status']
   await send(event)
  try:await self.inner(scope,incoming,outgoing)
  finally:
   if tag:
    for stream,record in items:record['closed']=stream.closed
    (root/('request-'+tag+'.json')).write_text(json.dumps({'received':received,'status':status,'spools':[r for s,r in items]}))
   current.reset(token)
app=Observe(app)
'''


def main():
    sys.path.insert(0, str(SERVER.parent / 'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    api = IsolatedServer('multipart-body', app='multipart_fixture:app')
    (api.root / 'multipart_fixture.py').write_text(FIXTURE, encoding='utf8')
    report = {'passed': False, 'real_model_calls': 0, 'mock_llm_calls': 0, 'checks': []}

    def evidence(tag, *, spool=True, rolled=False):
        path = api.root / ('request-' + tag + '.json')
        end = time.monotonic() + 15
        while not path.exists():
            if time.monotonic() > end:
                raise TimeoutError('Missing request evidence: ' + tag)
            time.sleep(.03)
        value = json.loads(path.read_text())
        assert all(s['closed'] for s in value['spools']), value
        assert bool(value['spools']) == spool, value
        if rolled:
            assert any(s['rolled'] for s in value['spools']), value
        report['checks'].append(dict(tag=tag, **value))
        return value

    def socket_request(path, tag, length, body=b'', disconnect=False):
        with socket.create_connection(('127.0.0.1', api.port), timeout=15) as sock:
            head = (f'POST /api/v1{path} HTTP/1.1\r\nHost: localhost\r\n'
                    f'Authorization: Bearer {api.token}\r\nX-Test-Id: {tag}\r\n'
                    f'Content-Type: multipart/form-data; boundary=probe\r\n'
                    f'Content-Length: {length}\r\nConnection: close\r\n\r\n')
            sock.sendall(head.encode('ascii') + body)
            if disconnect:
                return
            reply = b''
            while b'\r\n\r\n' not in reply:
                chunk = sock.recv(4096)
                if not chunk:
                    raise AssertionError('Response closed before headers')
                reply += chunk
            return int(reply.split(b' ', 2)[1])

    entered = False
    try:
        api.__enter__(); entered = True
        api.create_case('Multipart acceptance')
        ws = api.call('GET', '/workspaces')[0]['id']
        endpoints = [('/workspaces:restore', 101), (f'/workspaces/{ws}/cases:import', 51),
                     (api.prefix + '/upload?path=Ref/probe.bin', 17), ('/skills/install', 3)]
        with httpx.Client(base_url=api.base + '/api/v1', headers={'Authorization': 'Bearer ' + api.token},
                          trust_env=False, timeout=40) as client:
            payload = b'actual upload\x00\xff' * 100000
            r = client.post(endpoints[2][0], files={'file': ('probe.bin', payload)}, headers={'X-Test-Id': 'success-upload'})
            assert r.status_code == 200, r.text[:500]
            assert client.get(api.prefix + '/raw', params={'path': 'Ref/probe.bin'}).content == payload
            evidence('success-upload', rolled=True)
            package = io.BytesIO()
            with zipfile.ZipFile(package, 'w') as archive:
                archive.writestr('upload-check/SKILL.md', '---\nname: upload-check\ndescription: upload acceptance\n---\nActual upload test.\n')
            r = client.post('/skills/install', files={'file': ('skill.zip', package.getvalue())}, headers={'X-Test-Id': 'success-skill'})
            assert r.status_code == 200, r.text[:500]
            evidence('success-skill')
            # Actual chunked uploads have no declared length: count bytes while receiving.
            prefix = b'--probe\r\nContent-Disposition: form-data; name="file"; filename="probe.zip"\r\n\r\n'
            for index, (path, mib) in enumerate(endpoints):
                tag = 'declared-' + str(index)
                assert socket_request(path, tag, mib * 1024 * 1024 + 1) == 413
                assert evidence(tag, spool=False)['received'] == 0
                def chunks():
                    yield prefix
                    for _ in range(mib * 16 + 1):
                        yield b'x' * 65536
                    yield b'\r\n--probe--\r\n'
                tag = 'chunked-' + str(index)
                r = client.post(path, content=chunks(), headers={'Content-Type': 'multipart/form-data; boundary=probe', 'X-Test-Id': tag})
                assert r.status_code == 413, (tag, r.status_code, r.text[:300])
                observed = evidence(tag, rolled=True)
                assert sum(s['size'] for s in observed['spools']) <= mib * 1024 * 1024, observed
            partial = prefix + b'x' * (2 * 1024 * 1024)
            assert socket_request(endpoints[0][0], 'incomplete', len(partial), partial) == 400
            evidence('incomplete', rolled=True)
            socket_request(endpoints[0][0], 'disconnect', len(partial) + 1000, partial, disconnect=True)
            assert evidence('disconnect', rolled=True)['status'] == 400
            (api.root / 'short-deadline').touch()
            assert socket_request(endpoints[0][0], 'timeout', len(partial) + 1000, partial) == 504
            evidence('timeout', rolled=True)
            (api.root / 'short-deadline').unlink()
            r = client.post(endpoints[2][0], files={'file': ('probe.bin', payload)}, headers={'X-Test-Id': 'reuse'})
            assert r.status_code == 200, r.text[:500]
            evidence('reuse', rolled=True)
        report['passed'] = True
    except BaseException as error:
        report['failure'] = {'type': type(error).__name__, 'message': str(error)[:2000]}
    finally:
        if entered:
            api.__exit__(None if report['passed'] else AssertionError, None, None)
        report['API_exit_verified'] = api.proc is not None and api.proc.poll() is not None
        report['passed'] = report['passed'] and report['API_exit_verified']
        (api.root / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
        print('MULTIPART_RESULT ' + str(api.root / 'acceptance.json'), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
