"""Actual organization ACL, queued Windows job, event subscription and DeepSeek turn."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from copy import copy
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode
from uuid import uuid4

from websockets.exceptions import ConnectionClosedError
from websockets.sync.client import connect
from sqlalchemy import func, insert, select

from app.models import AgentEvent
from live_llm_acceptance import FIXTURE, source
from live_llm_terminal_agent_acceptance import disposable_database
from testsupport import IsolatedServer, SERVER
from workflow_migration_smoke import engine_for


def until(check, description, seconds=70):
    deadline = time.monotonic()+seconds
    while time.monotonic()<deadline:
        value = check()
        if value:
            return value
        time.sleep(.25)
    raise TimeoutError(description)


@contextmanager
def redis():
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1',0))
        port = reservation.getsockname()[1]
    name = 'vf-j07-redis-'+uuid4().hex[:12]
    started = subprocess.run(['docker','run','--rm','--pull','never','--detach','--name',name,
        '-p',f'127.0.0.1:{port}:6379','redis:7-alpine'],capture_output=True,text=True,check=True,timeout=30)
    assert started.stdout.strip()
    try:
        until(lambda: subprocess.run(['docker','exec',name,'redis-cli','ping'],capture_output=True,
            text=True,timeout=8).stdout.strip()=='PONG','Isolated Redis did not answer',25)
        yield port
    finally:
        subprocess.run(['docker','stop','--time','5',name],capture_output=True,check=True,timeout=25)


def configure(api, primary, port, dist=None):
    settings = {'VIBEFLOW_JWT_SECRET':primary.shared_secret,'VIBEFLOW_STORAGE_FS_ROOT':str(primary.root/'files'),
                'VIBEFLOW_AUDIT_DIR':str(primary.root/'audit'),'VIBEFLOW_DISTRIBUTED_JOBS':'true',
                'VIBEFLOW_REDIS_HOST':'127.0.0.1','VIBEFLOW_REDIS_PORT':str(port),'VIBEFLOW_REDIS_PASSWORD':''}
    if primary.database_url is None:
        settings['VIBEFLOW_DATABASE_URL']='sqlite+aiosqlite:///'+(primary.root/'test.sqlite').as_posix()
    code='import os\nos.environ.update('+repr(settings)+')\nlive_request_limit=1\n'+ FIXTURE
    code += '\nllm._VAULT_KEY_PATH=Path('+repr(str(primary.root/'.acl-vault'))+')\n'
    if dist is not None:
        code += 'from fastapi.staticfiles import StaticFiles\napp.mount("/",StaticFiles(directory='+repr(str(dist))+',html=True))\n'
    (api.root/'acl_fixture.py').write_text(code,encoding='utf8')
    return settings


async def record_private_detail(database, conversation_id, marker):
    engine = engine_for(database)
    try:
        async with engine.begin() as connection:
            seq = await connection.scalar(select(func.max(AgentEvent.seq)).where(
                AgentEvent.conversation_id == conversation_id))
            await connection.execute(insert(AgentEvent).values(conversation_id=conversation_id,
                seq=(seq or 0)+1,event_id=uuid4().hex,type='acp',
                payload=json.dumps({'kind':'approval_requested','proposed_content':marker})))
    finally:
        await engine.dispose()


EDGE = """
import json,sys
from pathlib import Path
from playwright.sync_api import expect,sync_playwright
cfg=json.load(sys.stdin); out=Path(cfg['root']); wid=str(cfg['workspace'])
def setup(browser,token,width,height):
    context=browser.new_context(viewport={'width':width,'height':height})
    context.add_init_script('localStorage.setItem("vf_token",'+json.dumps(token)+');')
    page=context.new_page()
    page.goto(cfg['address'],wait_until='domcontentloaded')
    if width==390:
        page.get_by_role('button',name='选择算例').click()
    workspace=page.get_by_label('当前工作区',exact=True)
    expect(workspace.locator('option[value="'+wid+'"]')).to_be_attached()
    workspace.select_option(wid)
    selected=page.locator('[data-case-name='+json.dumps(cfg['case'])+']')
    expect(selected).to_be_visible()
    selected.click()
    page.get_by_test_id('workspace-share').click()
    editor=page.get_by_test_id('project-access')
    editor.get_by_label('授权范围').select_option('case')
    return context,page,editor
with sync_playwright() as p:
    browser=p.chromium.launch(channel='msedge',headless=True)
    owner,page,editor=setup(browser,cfg['owner'],1440,900)
    expect(editor.get_by_label('授权组织编号')).to_have_value(str(cfg['org']))
    editor.get_by_label('授权角色').select_option('viewer')
    editor.get_by_role('button',name='保存授权').click()
    expect(editor.get_by_role('status')).to_contain_text('授权已保存')
    editor.screenshot(path=str(out/'acl-owner-1440.png'))
    owner.close()
    delegate,page,editor=setup(browser,cfg['delegate'],390,844)
    expect(page.locator('[data-case-name='+json.dumps(cfg['sibling'])+']')).to_have_count(0)
    expect(page.locator('button[title="重命名算例"]')).to_be_disabled()
    expect(editor.get_by_label('授权角色').locator('option[value=member]:disabled')).to_have_count(1)
    sizes=editor.evaluate('node=>[node.clientWidth,node.scrollWidth]')
    assert sizes[0]+1>=sizes[1]
    editor.screenshot(path=str(out/'acl-delegate-390.png'))
    editor.get_by_role('checkbox',name='启用授权').uncheck()
    editor.get_by_role('button',name='保存授权').click()
    expect(editor.get_by_role('status')).to_contain_text('授权已撤销')
    with page.expect_response(lambda res: res.url.endswith('/api/v1/workspaces') and res.ok) as workspaces:
        page.reload(wait_until='domcontentloaded')
    assert cfg['workspace'] not in [item['id'] for item in workspaces.value.json()]
    page.get_by_role('button',name='选择算例').click()
    expect(page.get_by_label('当前工作区').locator('option[value="'+wid+'"]')).to_have_count(0)
    (out/'browser.json').write_text(json.dumps({'passed':True,'mobile_width':sizes,'model_calls':0,
        'owner_saved':True,'delegate_revoked':True,'sibling_hidden':True},indent=2),encoding='utf8')
    delegate.close();browser.close()
"""


def verify(config, database, browser_python=None, dist=None):
    primary=IsolatedServer('live-j07-acl-primary',app='acl_fixture:app',database_url=database)
    peer=IsolatedServer('live-j07-acl-peer',app='acl_fixture:app',database_url=database)
    primary.shared_secret=uuid4().hex
    report={'passed':False,'mock_llm_calls':0,'model':config['model'],'checks':[]}
    print('J07_EVIDENCE',primary.root,peer.root,flush=True)
    process=None
    with redis() as port:
        settings=configure(primary,primary,port)
        configure(peer,primary,port,dist)
        try:
            with primary,peer:
                case=primary.create_case('Multi-organization case')
                peer.token=primary.token
                owner_id=primary.call('GET','/auth/me')['id']
                owner_org=primary.call('GET','/org/mine')['org']['id']
                cid=case['id']
                wid=primary.call('GET',f'/cases/{cid}')['workspace_id']
                sibling=primary.call('POST',f'/workspaces/{wid}/cases',{'name':'Owner-only geometry test'})
                cp=f'/cases/{cid}'
                wp=f'/workspaces/{wid}'
                primary.put('Ref/mark.txt','J07 shared case evidence')
                primary.call('PUT',f"/cases/{sibling['id']}/file",{'path':'Ref/hidden.txt','content':'J07 sibling-only datum'})

                def register(label):
                    actor=copy(peer)
                    email=uuid4().hex+'@example.com'
                    actor.token=peer.call('POST','/auth/register',
                        {'email':email,'password':'isolated-test-pass','name':label})['token']
                    return actor,email

                member,email=register('Owner organization member')
                secondary,_=register('Second organization admin')
                outsider,outside_email=register('Independent organization')
                second_org=secondary.call('GET','/org/mine')['org']['id']
                other_org=outsider.call('GET','/org/mine')['org']['id']
                primary.call('POST',f'/org/{owner_org}/members',{'email':email,'role':'member'},expect=201)
                member.call('POST',f'/org/{owner_org}/invitation:accept',{})
                assert {row['id'] for row in member.call('GET','/org/memberships')}.issuperset({owner_org})
                legacy=primary.root/'files'/'workspaces'/str(owner_id)/'workspace-shares'/f'{wid}.json'
                legacy.parent.mkdir(parents=True,exist_ok=True)
                legacy.write_text(json.dumps({'schema':'vibeflow.workspace-share.v1','workspace_id':wid,
                    'org_id':owner_org,'role':'viewer','enabled':True}),encoding='utf8')
                assert member.call('GET',cp+'/tree')
                assert primary.call('GET',wp+'/access-grants')['grants'][0]['version']==0
                assert primary.call('PUT',wp+f'/access-grants/{owner_org}',{'role':'member','enabled':True,
                    'expected_version':0})['enabled']
                primary.call('PUT',wp+f'/access-grants/{other_org}',{'role':'viewer','enabled':True})
                assert member.call('GET',cp)['access_role']=='member'
                assert outsider.call('GET',f"/cases/{sibling['id']}")['access_role']=='viewer'
                primary.call('PUT',wp+f'/access-grants/{other_org}',{'role':'viewer','enabled':False})
                grant=primary.call('PUT',cp+f'/access-grants/{second_org}',
                    {'role':'viewer','enabled':True,'delegated':True})
                assert len(peer.call('GET',wp+'/access-grants')['grants'])==2
                assert any(row['id']==wid and row['case_only'] for row in secondary.call('GET','/workspaces'))
                assert [item['id'] for item in secondary.call('GET',wp+'/cases')]==[cid]
                secondary.call('GET',f"/cases/{sibling['id']}",expect=404)
                secondary.call('GET',wp+'/access-grants',expect=404)
                outsider.call('GET',cp+'/tree',expect=404)
                assert len(secondary.call('GET',cp+'/access-grants')['grants'])==1
                secondary.call('PUT',cp+f'/access-grants/{second_org}',{'role':'member','enabled':True},expect=403)
                secondary.call('PUT',cp+f'/access-grants/{owner_org}',{'role':'viewer','enabled':True},expect=404)
                primary.call('PUT',cp+f'/access-grants/{second_org}',{'role':'viewer','enabled':True,
                    'expected_version':grant['version']-1},expect=409)
                report['checks'].append('legacy file imported, multiple organizations, private sibling and bounded delegation')

                for path in (cp,cp+'/tree',cp+'/file?path=Ref/mark.txt',cp+'/events',cp+'/mpi:status',
                    cp+'/jobs',cp+'/report/outputs',cp+'/ppt/presets',
                    cp+'/latex/outputs',f'/runtime/processes?case_id={cid}',f'/conversations?caseId={cid}'):
                    secondary.call('GET',path)
                secondary.call('PUT',cp+'/file',{'path':'Ref/deny.txt','content':'deny'},expect=404)
                secondary.call('POST',wp+'/cases',{'name':'Denied'},expect=404)
                search=secondary.call('GET',wp+'/search?q=sibling-only')
                assert not search['hits']
                ticket=secondary.call('POST','/connection-tickets',{'scope':'events','case_id':cid})['ticket']
                uri=secondary.base.replace('http:','ws:',1)+'/ws/events?'+urlencode({'case_id':cid,'token':ticket})
                with connect(uri,open_timeout=15) as stream:
                    assert json.loads(stream.recv(timeout=10))['type']=='ready'
                    primary.put('Ref/event-check.txt','AUTHORIZED')
                    until(lambda: json.loads(stream.recv(timeout=4)).get('case_id')==cid,'Missing authorized event',15)
                    secondary.call('PUT',cp+f'/access-grants/{second_org}',{'role':'viewer','enabled':False})
                    try:
                        while True:stream.recv(timeout=4)
                    except ConnectionClosedError as error:
                        assert error.rcvd and error.rcvd.code==4403
                secondary.call('GET',cp+'/tree',expect=404)
                secondary.call('GET',wp+'/cases',expect=404)
                assert member.call('GET',cp+'/tree')
                report['checks'].append('viewer read matrix, scoped workspace search and existing event socket revocation')

                primary.call('PUT',cp+f'/access-grants/{second_org}',{'role':'member','enabled':True,'delegated':True})
                secondary.call('POST',f'/org/{second_org}/members',{'email':outside_email,'role':'viewer'},expect=201)
                outsider.call('POST',f'/org/{second_org}/invitation:accept',{})
                assert outsider.call('GET',cp)['access_role']=='viewer'
                outsider.call('PUT',cp+'/file',{'path':'Ref/reject.txt','content':'deny'},expect=404)
                marker=primary.root/'native-job-ran'
                script=f'from pathlib import Path\nPath({str(marker)!r}).write_text("EXECUTED")\n'
                primary.put('Ref/verify-acl.py',script)
                submitted=secondary.call('POST',cp+'/jobs',{'runtime':'windows','command':'python Ref/verify-acl.py',
                    'input_paths':['Ref/verify-acl.py']},expect=(200,202),extra_headers={'Idempotency-Key':uuid4().hex})
                primary.call('PUT',cp+f'/access-grants/{second_org}',{'role':'member','enabled':False,'delegated':True})
                worker_env={**primary.env,**settings,'PYTHONPATH':str(SERVER)}
                with (primary.root/'redis-worker.log').open('w',encoding='utf8') as log:
                    process=subprocess.Popen([sys.executable,'-m','arq','app.services.distributed_queue.WorkerSettings'],
                        cwd=primary.root,env=worker_env,stdout=log,stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    until(lambda: 'deferred' in (primary.root/'redis-worker.log').read_text(encoding='utf8').lower(),
                          'Redis worker did not reject the revoked requester')
                    assert not marker.exists()
                    assert primary.call('GET',f"/jobs/{submitted['id']}")['status']=='queued'
                    secondary.call('PUT',cp+f'/access-grants/{second_org}',{'role':'member','enabled':True})
                    finished=primary.wait_job(submitted['id'],timeout=75)
                    assert marker.read_text()=='EXECUTED' and finished['status']=='succeeded'
                report['checks'].append('real Redis worker rechecks current ACL and runs the queued attempt after access returns')

                profile=secondary.call('POST','/provider-profiles',{'provider':'deepseek','name':'Editor profile',
                    'base_url':config['base_url'],'default_model':config['model'],'api_key':config['key']})['id']
                conv=secondary.call('POST','/conversations',{'case_id':cid,'provider_profile_id':profile,
                    'model':config['model'],'permission_mode':'ask','title':'Real ACL turn'})['id']
                text='ACL_LIVE_'+uuid4().hex[:12].upper()
                reply=secondary.call('POST',f'/conversations/{conv}/messages',{'content':f'Reply with {text} only.',
                    'provider_profile_id':profile,'model':config['model']},raw=True)
                events=[json.loads(line[6:]) for line in reply.decode().splitlines() if line.startswith('data: {')]
                answer=''.join(event.get('text','') for event in events if event.get('type')=='text.delta')
                assert text in answer
                assert member.call('GET',f'/conversations/{conv}')['id']==conv
                marker='PRIVATE_ACP_DETAIL_'+uuid4().hex
                asyncio.run(record_private_detail(settings.get('VIBEFLOW_DATABASE_URL',database),conv,marker))
                assert marker.encode() not in outsider.call('GET',f'/conversations/{conv}',raw=True)
                assert marker.encode() not in outsider.call('GET',f'/conversations/{conv}/export',raw=True)
                secondary.call('PUT',cp+f'/access-grants/{second_org}',{'role':'member','enabled':False})
                outsider.call('GET',f'/conversations/{conv}',expect=404)
                secondary.call('PUT',cp+f'/access-grants/{second_org}',{'role':'member','enabled':True})
                report['checks'].append('real DeepSeek conversation is visible only within effective case authorization')
                report['checks'].append('viewer membership caps member grant and private ACP detail is excluded from shared history and exports')
                if browser_python is not None:
                    inputs={'address':peer.base,'root':str(primary.root),'owner':primary.token,'delegate':secondary.token,
                        'workspace':wid,'case':case['name'],'org':second_org,'sibling':'Owner-only geometry test'}
                    subprocess.run([str(browser_python),'-c',EDGE],input=json.dumps(inputs),text=True,check=True,timeout=125)
                    report['checks'].append('Edge owner can save and delegated admin can revoke only the selected case')
            trace=peer.root/'upstream.jsonl'
            report['upstream']=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()]
            assert len([entry for entry in report['upstream'] if entry.get('status')==200])==1
            report['passed']=True
        finally:
            if process is not None:
                process.terminate()
                process.wait(timeout=15)
            (primary.root/'.acl-vault').unlink(missing_ok=True)
            trace=peer.root/'upstream.jsonl'
            if trace.exists() and 'upstream' not in report:
                report['upstream']=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()]
            (primary.root/'organization-acl.json').write_text(json.dumps(report,indent=2),encoding='utf8')


async def main(args):
    config=await source(args.source_profile_id)
    async with disposable_database(args.postgres) as database:
        await asyncio.to_thread(verify,config,database,args.browser_python,args.dist.resolve(strict=True) if args.dist else None)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--browser-python',type=Path)
    parser.add_argument('--dist',type=Path)
    args=parser.parse_args()
    if bool(args.browser_python)!=bool(args.dist):
        parser.error('--browser-python and --dist are used together')
    asyncio.run(main(args))
