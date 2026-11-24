"""Exercise TLS, scoped credentials, and native sockets through a disposable nginx/API pair."""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode


PLATFORM = Path(__file__).resolve().parents[1]
STL = ('solid tri\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\n'
       'vertex 0 1 0\nendloop\nendfacet\nendsolid\n')


def docker(*args, check=True):
    return subprocess.run(['docker', *map(str, args)], check=check, capture_output=True, text=True)


def bind(source, target, readonly=True):
    return ['--mount', f'type=bind,source={source},target={target}' + (',readonly' if readonly else '')]


def certificate(root):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'VibeFlow isolated TLS acceptance')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=2))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(key, hashes.SHA256()))
    (root / 'fullchain.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (root / 'privkey.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(public).digest()).decode('ascii')


async def verify_sockets(base, tls, case, other_case, owner, event, cad, terminal, expired, report, http):
    import websockets
    from websockets.exceptions import InvalidStatus

    def connect(route, **query):
        return websockets.connect(base.replace('https://', 'wss://') + route + '?' + urlencode(query),
                                  ssl=tls, proxy=None, open_timeout=30)

    async def denied(label, route, **query):
        try:
            async with connect(route, **query):
                raise AssertionError('Unexpected socket authorization: ' + label)
        except InvalidStatus as error:
            assert error.response.status_code == 403, (label, error.response.status_code)
        report['rejected'].append(label)

    await denied('account JWT in query', '/ws/events', case_id=case, token=owner)
    await denied('event ticket on CAD socket', '/ws/cadscene', case_id=case, token=event)
    await denied('event ticket on same-owner other case', '/ws/events', case_id=other_case, token=event)
    await denied('terminal ticket on other case', '/ws/terminal', case=other_case, token=terminal)
    await denied('expired ticket', '/ws/events', case_id=case, token=expired)
    forged = event.rsplit('.', 1)[0] + '.' + 'A' * 43
    await denied('modified signature', '/ws/events', case_id=case, token=forged)

    for route, ticket in (('/ws/events', event), ('/ws/cadscene', cad)):
        async with connect(route, case_id=case, token=ticket) as ws:
            state = json.loads(await asyncio.wait_for(ws.recv(), 15))
            assert state['type'] == 'ready' and state['case_id'] == case
            report['wss'].append(route)

    async with connect('/ws/terminal', case=case, shell='cmd', token=terminal) as ws:
        ready = json.loads(await asyncio.wait_for(ws.recv(), 40))
        assert ready['type'] == 'ready' and ready['alive'] is True, ready
        session = ready['session']
        try:
            await ws.send(json.dumps({'type': 'input', 'data': "printf 'tls-%s\\n' ticket-ok\r"}))
            output = bytearray()
            while b'tls-ticket-ok' not in output and len(output) < 65536:
                frame = await asyncio.wait_for(ws.recv(), 15)
                if isinstance(frame, bytes):
                    output.extend(frame)
            assert b'tls-ticket-ok' in output
            attach = http('POST', '/api/v1/connection-tickets', owner,
                          {'scope': 'terminal', 'session_id': session}).json()['ticket']
            await denied('case launch ticket on existing terminal', '/ws/terminal', session=session, token=terminal)
            async with connect('/ws/terminal', session=session, token=attach) as reconnected:
                state = json.loads(await asyncio.wait_for(reconnected.recv(), 10))
                assert state['type'] == 'ready' and state['session'] == session
            report['wss'].append('/ws/terminal')
            report['terminal_attach'] = True
        finally:
            endpoint = f'/api/v1/terminal/sessions/{session}'
            http('DELETE', endpoint, owner)
            closed = http('GET', endpoint, owner).json()
            assert closed['stop']['verified'] and closed['stop']['scope_empty'], closed


def exercise(base, tls, secret, pin, root, browser_python, report):
    import httpx
    import jwt

    with httpx.Client(base_url=base, verify=tls, trust_env=False, timeout=90) as client:
        def http(method, path, bearer=None, payload=None, status=200, **extra):
            headers = extra.pop('headers', {})
            if bearer:
                headers = {'Authorization': 'Bearer ' + bearer, **headers}
            response = client.request(method, path, headers=headers, json=payload, **extra)
            assert response.status_code == status, (method, path.split('?')[0], response.status_code, status)
            return response

        html = http('GET', '/')
        assert "frame-ancestors 'self'" in html.headers['content-security-policy']
        assert html.headers['x-frame-options'] == 'SAMEORIGIN' and html.headers['referrer-policy'] == 'no-referrer'
        assert html.headers['x-content-type-options'] == 'nosniff'
        health = http('GET', '/api/health')
        assert "default-src 'none'" in health.headers['content-security-policy'] and health.headers['permissions-policy']
        for origin, status in ((base, 200), ('https://foreign.example.com', 400)):
            answer = http('OPTIONS', '/api/v1/connection-tickets', status=status, headers={
                'Origin': origin, 'Access-Control-Request-Method': 'POST',
                'Access-Control-Request-Headers': 'authorization,content-type'})
            assert answer.headers.get('access-control-allow-origin') == (origin if status == 200 else None)
        report['nginx_csp_api_headers_cors'] = True

        def account(name):
            response = http('POST', '/api/v1/auth/register', payload={
                'email': name + '@example.com', 'password': secrets.token_hex(20), 'name': name})
            return response.json()['token']

        owner, outsider = account('ticket-owner'), account('ticket-outsider')
        workspace = http('GET', '/api/v1/workspaces', owner).json()[0]['id']
        cases = [http('POST', f'/api/v1/workspaces/{workspace}/cases', owner, {'name': name}).json()['id']
                 for name in ('TLS connection acceptance', 'Other owner case')]
        case = cases[0]
        for scope in ('events', 'cadscene', 'terminal'):
            http('POST', '/api/v1/connection-tickets', outsider, {'scope': scope, 'case_id': case}, status=404)

        def issue(scope):
            response = http('POST', '/api/v1/connection-tickets', owner, {'scope': scope, 'case_id': case})
            assert response.headers['cache-control'] == 'no-store' and response.json()['expires_in'] == 60
            return response.json()['ticket']

        event, cad, terminal = [issue(scope) for scope in ('events', 'cadscene', 'terminal')]
        http('GET', '/api/v1/auth/me', event, status=401)
        claims = jwt.decode(event, options={'verify_signature': False})
        claims['iat'] -= 120
        claims['exp'] -= 120
        signing = hmac.new(secret.encode(), b'vibeflow-connection', hashlib.sha256).digest()
        expired = jwt.encode(claims, signing, algorithm='HS256')
        asyncio.run(verify_sockets(base, tls, case, cases[1], owner, event, cad, terminal, expired, report, http))

        http('PUT', f'/api/v1/cases/{case}/file', owner, {'path': 'D-post/tls.stl', 'content': STL})
        renderer = http('POST', f'/api/v1/cases/{case}/viz:sessions', owner, {'file_path': 'D-post/tls.stl'}).json()
        sid = renderer['id']
        try:
            assert renderer['ready'] and renderer['phase'] == 'active'
            auth = {'scope': 'viz', 'session_id': str(sid)}
            loader = f'/viz-proxy/{sid}/index.html'
            http('GET', loader + '?' + urlencode({'token': owner}), status=401)
            http('GET', loader, outsider, status=404)
            http('POST', '/api/v1/connection-tickets', outsider, auth, status=404)
            response = http('POST', '/api/v1/connection-tickets', owner, auth)
            cookie = response.headers['set-cookie']
            assert all(flag.lower() in cookie.lower() for flag in ('HttpOnly', 'Secure', 'SameSite=strict',
                       f'Path=/viz-proxy/{sid}/', 'Max-Age=300'))
            assert response.json() == {'url': loader, 'expires_in': 300}
            scoped = client.cookies.get(f'vf_viz_{sid}')
            assert scoped and '?' not in loader and http('GET', loader).text
            http('GET', f'/viz-proxy/{sid + 1}/index.html?' + urlencode({'token': scoped}), status=401)
            http('GET', '/api/v1/auth/me', scoped, status=401)
            report['viz_scoped_secure_cookie'] = True
            settings = {'base': base, 'owner': owner, 'sid': sid, 'case': case, 'pin': pin, 'root': str(root)}
            process = subprocess.run([str(browser_python), '-B', '-X', 'utf8', __file__, '--browser'],
                                     input=json.dumps(settings), text=True, timeout=180)
            process.check_returncode()
            report['browser'] = json.loads((root / 'browser.json').read_text(encoding='utf8'))
        finally:
            endpoint = f'/api/v1/viz:sessions/{sid}'
            http('DELETE', endpoint, owner)
            closed = http('GET', endpoint, owner).json()
            assert closed['stop']['verified'] and closed['stop']['scope_empty'], closed

        query_canary = secrets.token_hex(24)
        http('GET', '/?' + urlencode({'token': query_canary}), headers={'Referer': base + '/?token=' + query_canary})
        return [owner, outsider, event, cad, terminal, expired, scoped, query_canary]


def browser(data):
    from playwright.sync_api import expect, sync_playwright

    root, base = Path(data['root']), data['base']
    sockets = []
    with sync_playwright() as playwright:
        edge = playwright.chromium.launch(channel='msedge', args=[
            '--ignore-certificate-errors-spki-list=' + data['pin']])
        context = edge.new_context(viewport={'width': 1500, 'height': 1000})
        context.add_init_script(f"if (location.origin === {json.dumps(base)}) localStorage.setItem('vf_token', {json.dumps(data['owner'])});")
        page = context.new_page()
        page.on('websocket', lambda ws: sockets.append(ws.url.split('?')[0].removeprefix(base.replace('https', 'wss'))))
        try:
            page.goto(base, wait_until='domcontentloaded')
            assert page.evaluate('window.isSecureContext')
            page.locator('[data-case-name="TLS connection acceptance"]').click(timeout=30000)
            page.locator('.dv-tab').get_by_text('终端', exact=True).click()
            terminal = page.get_by_test_id('terminal-panel')
            terminal.get_by_role('button', name='Windows cmd', exact=True).click()
            terminal.get_by_role('button', name='新建终端', exact=True).click()
            active = terminal.locator('.terminal-session-tab').filter(has=page.locator('button[aria-pressed="true"]'))
            expect(active).to_have_count(1, timeout=30000)
            terminal_id = active.get_attribute('data-session-id')
            assert terminal_id
            with page.expect_response(lambda response: response.request.method == 'DELETE' and
                                      response.url.split('?')[0].endswith('/terminal/sessions/' + terminal_id)) as stopped:
                active.get_by_role('button', name='关闭终端 ' + terminal_id[:8]).click()
            assert stopped.value.status in (200, 202), stopped.value.status
            endpoint = '/api/v1/terminal/sessions/' + terminal_id
            for _ in range(50):
                closed = page.evaluate('''async ({path, owner}) => {
                    const response = await fetch(path, {headers: {Authorization: 'Bearer ' + owner}});
                    return await response.json();
                }''', {'path': endpoint, 'owner': data['owner']})
                stop = closed.get('stop') or {}
                if stop.get('verified') and stop.get('scope_empty'):
                    break
                page.wait_for_timeout(200)
            else:
                raise AssertionError('Browser terminal close was not verified')
            tab = page.locator('.dv-tab').filter(has_text='可视化')
            tab.click()
            loader = f'/viz-proxy/{data["sid"]}/index.html'
            with page.expect_response(lambda response: response.url == base + loader, timeout=30000) as loaded:
                page.get_by_role('button', name='打开', exact=True).click()
            assert loaded.value.status == 200
            iframe = page.frame_locator('iframe[title="可视化"]')
            expect(iframe.locator('.v-navigation-drawer')).to_be_visible(timeout=30000)
            assert set(('/ws/events', '/ws/cadscene', '/ws/terminal', f'/viz-proxy/{data["sid"]}/ws')).issubset(sockets), sockets
            page.screenshot(path=root / 'tls-viz.png')
            (root / 'browser.json').write_text(json.dumps({'edge': edge.version, 'sockets': sockets,
                                                           'renderer_drawer_visible': True}), encoding='utf8')
        finally:
            edge.close()


def run(args):
    root = Path(tempfile.mkdtemp(prefix='ff-j18-tls-', dir=PLATFORM.parent / 'tmp'))
    print('CONNECTION_TICKET_EVIDENCE ' + str(root), flush=True)
    prefix = 'vf-j18-' + secrets.token_hex(5)
    api, nginx, network, state = prefix + '-api', prefix + '-web', prefix, prefix + '-state'
    certs = root / 'certs'
    certs.mkdir()
    pin = certificate(certs)
    secret = secrets.token_hex(32)
    env = root / 'api.env'
    with socket.socket() as port_reservation:
        port_reservation.bind(('127.0.0.1', 0))
        port = port_reservation.getsockname()[1]
    base = f'https://127.0.0.1:{port}'
    env.write_text('\n'.join(['VIBEFLOW_DATABASE_URL=sqlite+aiosqlite:////stage/test.sqlite',
        'VIBEFLOW_STORAGE_BACKEND=fs', 'VIBEFLOW_STORAGE_FS_ROOT=/stage/files', 'VIBEFLOW_INDEX_ROOT=/stage/index',
        'VIBEFLOW_AUDIT_DIR=/stage/audit', 'VIBEFLOW_RUNTIME_CONFIG=/stage/runtime.json',
        'VIBEFLOW_STORAGE_MAINTENANCE_ENABLED=false', 'VIBEFLOW_RATE_LIMIT_API_PER_MINUTE=5000',
        'VIBEFLOW_DISTRIBUTED_JOBS=false', 'VIBEFLOW_JWT_SECRET=' + secret, 'VIBEFLOW_CORS_ORIGINS=' + base]),
        encoding='ascii')
    tls = ssl.create_default_context(cafile=str(certs / 'fullchain.pem'))
    report = {'passed': False, 'wss': [], 'rejected': [], 'model_calls': 0}
    markers = []
    try:
        docker('network', 'create', network)
        docker('volume', 'create', state)
        app_mount = bind(PLATFORM / 'server/app', '/app/app') if args.mounted_source else []
        web_mount = bind(PLATFORM / 'web/dist', '/usr/share/nginx/html') if args.mounted_source else []
        conf_mount = bind(PLATFORM / 'infra/nginx-web.conf', '/etc/nginx/conf.d/default.conf') if args.mounted_source else []
        if args.mounted_source:
            conf_mount += bind(PLATFORM / 'infra/nginx-upstream-default', '/etc/nginx/vibeflow-upstreams')
        docker('run', '--pull=never', '-d', '--name', api, '--network', network, '--network-alias', 'api',
               '--security-opt', 'seccomp=unconfined', '--ulimit', 'core=0',
               '--env-file', env, *app_mount,
               '--mount', f'type=volume,source={state},target=/stage', args.api_image)
        docker('run', '--pull=never', '-d', '--name', nginx, '--network', network, '-p', f'127.0.0.1:{port}:443',
               *conf_mount, *bind(certs, '/etc/nginx/certs'),
               *bind(PLATFORM / 'infra/nginx-tls.conf.example', '/etc/nginx/vibeflow-tls/acceptance.conf'),
               *web_mount, args.web_image)
        report['image_ids'] = {label: docker('image', 'inspect', '--format', '{{.Id}}', image).stdout.strip()
                               for label, image in (('api', args.api_image), ('web', args.web_image))}
        report['mounted_source'] = args.mounted_source
        import httpx
        with httpx.Client(verify=tls, trust_env=False, timeout=4) as client:
            for _ in range(45):
                try:
                    if client.get(base + '/api/health').status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(1)
            else:
                raise AssertionError('Isolated TLS proxy did not become healthy')
        with socket.create_connection(('127.0.0.1', port), timeout=10) as sock, tls.wrap_socket(sock, server_hostname='127.0.0.1') as secure:
            report['tls'] = secure.version()
            assert report['tls'] in ('TLSv1.2', 'TLSv1.3')
        markers = exercise(base, tls, secret, pin, root, args.browser_python, report)
        report['passed'] = True
    except BaseException as error:
        report['error_type'] = type(error).__name__
        raise
    finally:
        try:
            render_logs = docker('exec', api, 'find', '/stage', '-name', 'viz-*.log',
                                 '-type', 'f', '-exec', 'cat', '{}', ';', check=False)
            (root / 'renderer.log').write_text(render_logs.stdout, encoding='utf8')
            for label, container in (('nginx', nginx), ('api', api)):
                log = docker('logs', container, check=False)
                combined = log.stdout + log.stderr
                found = set(re.findall(r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', combined))
                found.update(marker for marker in markers if marker in combined)
                report[label + '_credential_leaks'] = len(found)
                for marker in found:
                    combined = combined.replace(marker, '[redacted]')
                (root / (label + '.log')).write_text(combined, encoding='utf8')
            assert not report['nginx_credential_leaks'] and not report['api_credential_leaks']
        finally:
            docker('rm', '-f', nginx, api, check=False)
            docker('network', 'rm', network, check=False)
            docker('volume', 'rm', state, check=False)
            env.unlink(missing_ok=True)
            (certs / 'privkey.pem').unlink(missing_ok=True)
            (root / 'acceptance.json').write_text(json.dumps(report, indent=2), encoding='utf8')


if __name__ == '__main__':
    if sys.argv[1:] == ['--browser']:
        browser(json.load(sys.stdin))
    else:
        p = argparse.ArgumentParser(description=__doc__)
        p.add_argument('--api-image', required=True)
        p.add_argument('--web-image', required=True)
        p.add_argument('--browser-python', type=Path, required=True)
        p.add_argument('--mounted-source', action='store_true')
        run(p.parse_args())
