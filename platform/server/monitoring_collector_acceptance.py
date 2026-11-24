"""Verify central telemetry using two actual API processes and a loopback webhook."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import threading
import time

from testsupport import IsolatedServer


class Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        assert self.path == '/alert'
        assert self.headers.get('Authorization') == 'Bearer '+self.server.delivery_token
        event=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert self.headers['Idempotency-Key']==event['event_id']
        self.server.events.append(event)
        self.send_response(503 if len(self.server.events)==1 else 204)
        self.end_headers()

    def log_message(self,*_):
        pass


def configure(api, values):
    (api.root/'monitor_fixture.py').write_text('import os\nos.environ.update('+repr(values)+')\nfrom app.main import app\n',encoding='utf8')


def wait_closed(port):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        with socket.socket() as connection:
            connection.settimeout(.4)
            if connection.connect_ex(('127.0.0.1',port))!=0:
                return
        time.sleep(.2)
    raise RuntimeError('Disposable peer did not close its listener')


def verify():
    primary=IsolatedServer('ff-j20-monitor-primary',app='monitor_fixture:app')
    replica=IsolatedServer('ff-j20-monitor-peer',app='monitor_fixture:app')
    target=primary.root/'collector.sqlite'
    token=secrets.token_urlsafe(32)
    delivery_token=secrets.token_urlsafe(32)
    canary=secrets.token_hex(20)
    config={'VIBEFLOW_MONITORING_EXPORT_TOKEN':token,'VIBEFLOW_MONITORING_DB_PATH':str(target)}
    configure(primary,config)
    report={'passed':False,'model_calls':0}
    print('J20_EVIDENCE',primary.root,flush=True)

    with ThreadingHTTPServer(('127.0.0.1',0),Receiver) as webhook:
        webhook.delivery_token=delivery_token
        webhook.events=[]
        listener=threading.Thread(target=webhook.serve_forever,daemon=True)
        listener.start()
        try:
            with primary:
                case=primary.create_case('Monitoring replica and job evidence')
                job=primary.call('POST',primary.prefix+'/jobs',{'command':'echo CENTRAL_MONITOR_OK',
                     'runtime':'windows','timeout_seconds':35},extra_headers={'Idempotency-Key':secrets.token_hex(16)})
                ready=primary.wait_job(job['id'],timeout=60)
                assert ready['status']=='succeeded'
                peer_env=config|{key:primary.env[key] for key in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
                configure(replica,peer_env)
                replica.token=primary.token
                nodes={name:{'origin':server.base,'cluster':'shared'} for name,server in (('primary',primary),('peer',replica))}
                env=os.environ|config|{'VIBEFLOW_MONITORING_NODES':json.dumps(nodes),
                    'VIBEFLOW_MONITORING_WEBHOOK':f'http://127.0.0.1:{webhook.server_port}/alert',
                    'VIBEFLOW_MONITORING_WEBHOOK_TOKEN':delivery_token}

                def collect():
                    subprocess.run([sys.executable,'-B','-X','utf8','-m','app.services.monitoring_collector','--once'],
                        cwd=Path(__file__).parent,env=env,timeout=35,check=True)
                    return primary.call('GET','/runtime/monitor/history')

                with replica:
                    for api in (primary,replica):
                        api.call('GET','/runtime/telemetry',expect=401)
                        api.call('GET','/auth/me?canary='+canary)
                    primary.call('GET','/runtime/monitor/history',expect=401,extra_headers={'Authorization':''})
                    initial=collect()
                    assert initial['aggregate']=={'nodes_reachable':2,'nodes_configured':2,
                        'reporting_database_groups':1,'jobs_observed':{'succeeded':1}},initial
                    assert all(row['database']['ok'] and row['storage']['ok'] for row in initial['latest'])
                    logs=primary.call('GET','/runtime/monitor/logs')
                    assert {row['node'] for row in logs['records']}=={'primary','peer'}
                    assert any(row['route']=='/api/v1/auth/me' for row in logs['records'])
                    report.update(initial=initial,central_logs=logs,actual_job_id=job['id'])
                wait_closed(replica.port)

                outage=collect()
                assert len(webhook.events)==1 and webhook.events[0]['phase']=='firing'
                assert outage['aggregate']['nodes_reachable']==1
                assert outage['pending_deliveries']==1
                assert outage['active_alerts']==[{'node':'peer','code':'peer.unreachable','level':'critical'}]
                time.sleep(1.2)
                retry=collect()
                assert len(webhook.events)==2 and webhook.events[0]==webhook.events[1]
                assert retry['pending_deliveries']==0

                with replica:
                    replica.call('GET','/auth/me?canary='+canary)
                    recovered=collect()
                    assert recovered['aggregate']==initial['aggregate'] and recovered['active_alerts']==[]
                    assert len(webhook.events)==3 and webhook.events[-1]['phase']=='recovered'
                    assert webhook.events[-1]['code']=='peer.unreachable'
                    assert webhook.events[-1]['event_id']!=webhook.events[0]['event_id']
                    assert len(recovered['samples'])==8
                    with sqlite3.connect(target) as db:
                        attempts=db.execute('SELECT attempts FROM deliveries ORDER BY created').fetchall()
                        boots=db.execute("SELECT count(DISTINCT boot) FROM node_logs WHERE node='peer'").fetchone()[0]
                    assert attempts==[(2,),(1,)] and boots==2,(attempts,boots)
                    report.update(outage=outage['aggregate'],recovered=recovered['aggregate'],
                         webhook=webhook.events,peer_boots=boots,history_samples=len(recovered['samples']))
                wait_closed(replica.port)
            wait_closed(primary.port)
            report['passed']=True
        finally:
            webhook.shutdown()
            listener.join(timeout=10)
            for api in (primary,replica):
                (api.root/'monitor_fixture.py').unlink(missing_ok=True)
            evidence=json.dumps(report,ensure_ascii=False,indent=2)
            assert not any(secret in evidence for secret in (token,delivery_token,canary))
            (primary.root/'monitoring-result.json').write_text(evidence,encoding='utf8')


if __name__=='__main__':
    verify()
