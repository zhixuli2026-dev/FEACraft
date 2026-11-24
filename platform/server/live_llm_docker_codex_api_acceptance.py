"""Real native Codex turns and owner recovery through the Docker-backed platform API."""
import argparse
import asyncio
import base64
from contextlib import nullcontext
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import sqlite3
import struct
import subprocess
import time
from uuid import uuid4
from urllib.parse import quote
import zlib

from cryptography.fernet import Fernet

from app.services import codex_docker, docker_acp
from live_llm_acceptance import source
from live_llm_docker_cli_sandbox_acceptance import PROJECT, review
from testsupport import IsolatedServer


def until(probe, timeout=110):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        result = probe()
        if result:
            return result
        time.sleep(.18)
    raise TimeoutError('Docker Codex acceptance checkpoint was not reached')


def image_examples():
    # High-contrast printed digits are less sensitive to color interpretation.
    segments = {'a':(26,18,70,29),'b':(65,24,76,61),'c':(65,59,76,96),
                'd':(26,92,70,103),'e':(21,59,32,96),'f':(21,24,32,61),
                'g':(26,55,70,66)}
    glyphs = {'0':'abcdef','1':'bc','2':'abdeg','3':'abcdg','4':'bcfg',
              '5':'acdfg','6':'acdefg','7':'abc','8':'abcdefg','9':'abcdfg'}
    digits = secrets.SystemRandom().sample(list(glyphs),3)

    def chunk(kind, body):
        return struct.pack('>I',len(body))+kind+body+struct.pack('>I',zlib.crc32(kind+body))

    attachments = []
    for digit in digits:
        width, height = 96, 120
        pixels = [bytearray(b'\xff'*width) for _ in range(height)]
        for segment in glyphs[digit]:
            left,top,right,bottom = segments[segment]
            for row in pixels[top:bottom]:
                row[left:right] = b'\x00'*(right-left)
        scanlines = b''.join(b'\x00'+line for line in pixels)
        content = (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',width,height,8,0,0,0,0))
                   +chunk(b'IDAT',zlib.compress(scanlines))+chunk(b'IEND',b''))
        attachments.append({'name':f'attachment-{uuid4().hex[:10]}.png','mime':'image/png',
                            'data':base64.b64encode(content).decode('ascii')})
    prompt = ('Look at each of the three attachments. Each has one black seven-segment digit on white. '
              'Reply with the three digits in their image order, separated by commas. '
              'Output just the digits and commas. Do not call tools.')
    return attachments,prompt,digits


def verify(configuration, *, bundle=None, browser_python=None, writes=False, images=False, storage_env=None):
    api = IsolatedServer('docker-codex-api', app='docker_fixture:app')
    fixture = api.root/'docker_fixture.py'
    vault = api.root/'docker-vault.key'
    vault.write_bytes(Fernet.generate_key())
    deployment = uuid4()
    settings = {'VIBEFLOW_ACP_DOCKER_IMAGE':'vibeflow/api:cli-discovery-20261118',
                'VIBEFLOW_ACP_DOCKER_DEPLOYMENT':str(deployment),
                'VIBEFLOW_VAULT_KEY_PATH':str(vault),'VIBEFLOW_JWT_SECRET':uuid4().hex+uuid4().hex,
                'VIBEFLOW_RATE_LIMIT_BACKEND':'memory'}
    settings.update(storage_env or {})
    fixture_content = 'import os\nos.environ.update('+repr(settings)+')\nfrom app.main import app\n'
    if bundle is not None:
        fixture_content += ('from fastapi.staticfiles import StaticFiles\n'
                            f'app.mount("/", StaticFiles(directory={str(bundle.resolve(strict=True))!r}, html=True))\n')
    fixture.write_text(fixture_content,encoding='utf8')
    db_path = api.root/'test.sqlite'
    initial = docker_acp.command('ps','--no-trunc','--format','{{.ID}} {{.Names}}').splitlines()
    report = {'passed':False,'simulated_model_calls':0,'upstream':[],'checks':[],
              'storage':'s3' if storage_env else 'fs'}
    evidence = api.root/'codex-docker-result.json'
    print('DOCKER_CODEX_EVIDENCE='+str(evidence),flush=True)
    proof = 'CODEX_DOCKER_'+secrets.token_hex(12)
    user_token = None

    def save():
        data = json.dumps(report,indent=2)+'\n'
        assert configuration['key'] not in data and proof not in data
        evidence.write_text(data,encoding='utf8')

    def record(name):
        report['checks'].append(name)
        save()
        print('DOCKER_CODEX_PASS='+name,flush=True)

    def history(turn):
        raw = api.call('GET',f"/codex/turns/{turn['id']}/events",raw=True).decode('utf8')
        assert configuration['key'] not in raw
        return [json.loads(line[5:]) for line in raw.splitlines() if line.startswith('data:')]

    def native_session(cid, *, regular=False):
        runtime = {'runtime':'docker','provider_profile_id':profile,'provider_wire_api':'responses'}
        if regular:
            startup = api.call('POST',f'/codex/conversations/{cid}/session-requests',
                               {**runtime,'purpose':'chat','client_request_id':uuid4().hex})
            def ready():
                current = api.call('GET','/codex/session-requests/'+startup['id'])
                if current['phase'] in ('admitted','starting'):
                    return None
                assert current['phase']=='completed',current
                return current['session']
            session = until(ready)
        else:
            session = api.call('POST','/codex/sessions',{**runtime,'case_id':case['id'],'conversation_id':cid})
        assert session['runtime']=='docker' and session['phase']=='active',session
        assert set(session['process']['containers']) == {'worker','relay'}
        return session

    def submit(session,text,*,resume=False,attachments=None):
        payload = {'content':text,'client_message_id':uuid4().hex,'timeout_seconds':140,
                   'resume':resume,'model_options':{'effort':'low'}}
        if attachments:
            payload['images'] = attachments
        turn = api.call('POST',f"/codex/sessions/{session['id']}/turns",payload)
        return turn,payload

    def settled(turn, phase, *, require_upstream=True):
        def terminal():
            current = api.call('GET',f"/codex/turns/{turn['id']}")
            if current['phase'] in {'admitted','starting','running','awaiting_input','finalizing'}:
                return None
            assert current['phase']==phase and current['cleanup_verified'],current
            return current
        current = until(terminal,155)
        session = api.call('GET',f"/codex/sessions/{current['session_id']}")
        stop = session['stop']
        assert stop['scope_empty'] and stop['verified']
        posts = [event for event in stop['upstream'] if event['method']=='POST']
        assert (posts or not require_upstream) and all(event['status']==200 for event in posts)
        report['upstream'].extend(posts)
        return current,history(current)

    def receipt_turns():
        with sqlite3.connect(db_path) as conn:
            return conn.execute("SELECT id,created_by,profile_json FROM native_cli_sessions "
                                "WHERE runtime='docker'").fetchall()

    def file(path):
        return api.call('GET',f"/cases/{case['id']}/file?path="+quote(path,safe=''))['content']

    def put(path, content):
        return api.call('PUT',f"/cases/{case['id']}/file",{'path':path,'content':content})

    def image_turn():
        assets,prompt,expected = image_examples()
        cid = api.call('POST','/conversations',{**payload,'title':'Docker native images'})['id']
        session = native_session(cid,regular=True)
        mounts = json.loads(docker_acp.command('inspect','--format','{{json .Mounts}}',
                                              session['process']['containers']['worker']))
        mounted = next(m for m in mounts if m['Destination']=='/inputs')
        assert mounted['Type']=='bind' and not mounted['RW']
        turn,_ = submit(session,prompt,attachments=assets)
        first,events = settled(turn,'completed')
        answer = ''.join(e['payload']['text'] for e in events if e['type']=='text.delta').strip()
        assert [digit.strip() for digit in answer.split(',')]==expected, (answer,expected)
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute("SELECT payload FROM agent_events WHERE conversation_id=? AND type='message'",
                                (cid,)).fetchall()
        attachments = next(value['images'] for row, in rows if (value:=json.loads(row)).get('images'))
        assert len(attachments)==3
        for meta,asset in zip(attachments,assets,strict=True):
            content = base64.b64decode(asset['data'])
            assert meta['name']==asset['name'] and meta['sha256']==sha256(content).hexdigest()
            link = f"/cases/{case['id']}/raw?path="+quote(meta['path'],safe='')
            assert api.call('GET',link,raw=True)==content
            api.call('GET',link,expect=404,extra_headers=foreign)
        resumed_session = native_session(cid,regular=True)
        resumed_mounts = json.loads(docker_acp.command('inspect','--format','{{json .Mounts}}',
                                                      resumed_session['process']['containers']['worker']))
        assert next(m for m in resumed_mounts if m['Destination']=='/inputs')==mounted
        turn,_ = submit(resumed_session,'Repeat only the three digits previously shown in image order. '
                        'Separate them with commas and do not call tools.',resume=True)
        resumed,events=settled(turn,'completed')
        recalled=''.join(e['payload']['text'] for e in events if e['type']=='text.delta').strip()
        assert [digit.strip() for digit in recalled.split(',')]==expected, (recalled,expected)
        assert resumed['thread_id']==first['thread_id'] and resumed['session_id']!=first['session_id']
        report['images'] = {'matched':True,'count':len(assets),'raw_bytes_verified':True,
                            'outsider_denied':True,'read_only_mount':True,'resumed':True}
        record('actual_responses_recognizes_three_native_images_and_resumes_thread')

    def write_case_shadow(mode, *, conflict=False):
        target, second = 'ref-codex-docker-output.txt','ref-codex-docker-extra.txt'
        put(target,'original')
        put(second,'original second')
        cid = api.call('POST','/conversations',{**payload,'title':'Docker case stage '+mode,
                       'permission_mode':mode})['id']
        work = native_session(cid,regular=True)
        staging = f"/codex/sessions/{work['id']}/staging"
        api.call('GET',staging,expect=409)
        assert file(target)=='original' and file(second)=='original second'
        if conflict:
            put(target,'parallel editor update')
        command = f'cp ref-codex-docker.txt {target} && cp ref-codex-docker.txt {second}'
        turn,_ = submit(work,f'Use the shell tool to execute this concrete command in /workspace: {command}. '
                             'You must actually run the command, with default sandbox permissions. '
                             'Never ask for escalation. Briefly confirm only after execution.')
        row,events = settled(turn,'failed' if conflict else 'completed')
        assert any(e['type']=='item.completed' and e['payload'].get('type')=='commandExecution'
                   and e['payload'].get('exitCode')==0 for e in events), [
                       (e['type'],e['payload'].get('type'),e['payload'].get('exitCode')) for e in events]
        expected_receipt = 'parallel editor update' if conflict else proof
        assert file(target)==expected_receipt and file(second)==('original second' if conflict else proof)
        observed=api.call('GET',staging)
        assert observed['retained'] is conflict
        if conflict:
            assert {e['path'] for e in observed['changes']}=={target,second}
            assert api.call('GET',staging+'?path='+target)['content']==proof
            blocked=api.call('GET','/codex/sessions/'+work['id'])
            assert blocked['error']=='DockerCasePublicationConflict',blocked['error']
        api.call('GET',staging,expect=404,extra_headers=foreign)
        with sqlite3.connect(db_path) as conn:
            published = conn.execute("SELECT count(*) FROM durable_events WHERE type='codex.docker.case_published' "
                "AND json_extract(payload_json,'$.request_id')=?",(row['id'],)).fetchone()[0]
        assert published==int(not conflict)
        return {'mode':mode,'native_tool_executed':True,'published':not conflict,'retained':conflict,
                'parallel_file_preserved':conflict,'no_partial_publication':conflict}

    def interrupt_writer():
        target = 'ref-codex-docker-interrupted.txt'
        put(target,'live source unchanged')
        cid = api.call('POST','/conversations',{**payload,'title':'Stopped writable container',
                       'permission_mode':'full_access'})['id']
        session = native_session(cid)
        with sqlite3.connect(db_path) as conn:
            selected = json.loads(conn.execute('SELECT profile_json FROM native_cli_sessions WHERE id=?',
                                               (session['id'],)).fetchone()[0])
        copy = Path(selected['cwd'])/target
        assert copy.read_text(encoding='utf8')=='live source unchanged'
        turn,_ = submit(session,f'In /workspace run this exact command with the shell tool: '
                         f'cp ref-codex-docker.txt {target} && sleep 35. Wait for the command to finish.')
        def confirm_copy():
            with sqlite3.connect(db_path) as conn:
                phase=conn.execute('SELECT phase FROM native_cli_turns WHERE id=?',(turn['id'],)).fetchone()[0]
            assert phase in {'admitted','starting','running'},phase
            return phase=='running' and copy.read_text(encoding='utf8')==proof
        until(confirm_copy,65)
        assert file(target)=='live source unchanged'
        original = set(session['process']['containers'].values())
        assert original <= set(docker_acp.command('ps','-q','--no-trunc').splitlines())
        api.proc.kill()
        api.proc.wait(timeout=10)
        return turn,session,original,target

    try:
        with api:
            case = api.create_case('Docker Codex actual Responses')
            user_token = api.token
            profile = api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Authorized official API',
                'base_url':'https://api.deepseek.com/v1','default_model':configuration['model'],
                'api_key':configuration['key']})['id']
            payload = {'case_id':case['id'],'provider_profile_id':profile,
                       'model':configuration['model'],'permission_mode':'ask'}
            conversation = api.call('POST','/conversations',{**payload,'title':'Native Docker case'})['id']
            path = 'ref-codex-docker.txt'
            api.call('PUT',f"/cases/{case['id']}/file",{'path':path,'content':proof})
            session = native_session(conversation,regular=True)
            outsider = api.call('POST','/auth/register',{'email':uuid4().hex+'@example.com',
                'name':'Outside account','password':'isolated-test-123'})['token']
            foreign = {'Authorization':'Bearer '+outsider}
            api.call('GET','/codex/sessions/'+session['id'],expect=404,extra_headers=foreign)
            api.call('POST','/codex/sessions/'+session['id']+':reconcile-scope',expect=404,extra_headers=foreign)
            api.call('POST','/codex/sessions/'+session['id']+':reconcile-scope',expect=409)
            record('normal_startup_account_boundary_and_live_owner_fence')
            turn,request = submit(session,f'Call vibeflow_read_case_file exactly once with path {path}. '
                                          'Use no other tools. Reply with the exact reference it returns.')
            first,events = settled(turn,'completed')
            assert proof in ''.join(e['payload']['text'] for e in events if e['type']=='text.delta')
            assert any(e['type']=='item.completed' and e['payload'].get('type')=='dynamicToolCall'
                       and e['payload'].get('tool')=='vibeflow_read_case_file' and e['payload'].get('success')
                       for e in events)
            assert api.call('POST',f"/codex/sessions/{session['id']}/turns",request)['id']==first['id']
            record('actual_responses_sse_case_read_and_submit_replay')
            if images:
                image_turn()
            if writes:
                report['writeback'] = [write_case_shadow('approve_writes'),write_case_shadow('full_access',conflict=True)]
                record('writable_case_publication_and_whole_batch_version_conflict')
        with api:
            api.token = user_token
            session = native_session(conversation)
            turn,_ = submit(session,'Repeat the exact first reference from our conversation. Use no tools.',resume=True)
            resumed,events = settled(turn,'completed')
            assert resumed['thread_id']==first['thread_id'] and resumed['session_id']!=first['session_id']
            assert proof in ''.join(e['payload']['text'] for e in events if e['type']=='text.delta')
            record('native_thread_restored_in_a_new_container_after_restart')
            interrupted = api.call('POST','/conversations',{**payload,'title':'Interrupted Docker Codex'})['id']
            live = native_session(interrupted)
            run,_ = submit(live,'Write an 800-line list of engineering report section suggestions. Use no tools.')
            def observe():
                with sqlite3.connect(db_path) as conn:
                    phase = conn.execute('SELECT phase FROM native_cli_turns WHERE id=?',(run['id'],)).fetchone()[0]
                assert phase in {'admitted','starting','running'},phase
                return phase=='running'
            until(observe,50)
            original = set(live['process']['containers'].values())
            assert original <= set(docker_acp.command('ps','-q','--no-trunc').splitlines())
            api.proc.kill()
            api.proc.wait(timeout=10)
        with api:
            api.token = user_token
            recovered = api.call('POST',f"/codex/turns/{run['id']}:reconcile")
            assert recovered['execution_lock_recovered'] and recovered['turn']['phase']=='interrupted',recovered
            assert original.isdisjoint(docker_acp.command('ps','-aq','--no-trunc').splitlines())
            _,_ = settled(run,'interrupted',require_upstream=False)
            record('actual_api_owner_exit_removes_original_scope_and_releases_conversation')
            session = native_session(interrupted)
            fresh = 'AFTER_'+secrets.token_hex(10)
            turn,_ = submit(session,'Reply with only this reference. Use no tools. '+fresh)
            _,events = settled(turn,'completed')
            assert fresh in ''.join(e['payload']['text'] for e in events if e['type']=='text.delta')
            record('fresh_real_turn_after_proven_cleanup')
            if writes:
                stopped_writer = interrupt_writer()
        with api if writes or bundle is not None else nullcontext():
            api.token = user_token
            if writes:
                turn,session,container_ids,target = stopped_writer
                recovered = api.call('POST',f"/codex/turns/{turn['id']}:reconcile")
                assert recovered['execution_lock_recovered']
                assert container_ids.isdisjoint(docker_acp.command('ps','-aq','--no-trunc').splitlines())
                settled(turn,'interrupted')
                assert file(target)=='live source unchanged'
                assert api.call('GET',f"/codex/sessions/{session['id']}/staging?path={target}")['content']==proof
                record('writable_owner_exit_preserves_private_work_without_publication')
            if bundle is not None:
                plan = api.call('POST','/conversations',{**payload,'title':'Docker workbench plan',
                    'permission_mode':'plan'})['id']
                data = {'base':api.base,'artifacts':str(api.root),'conversation':plan,
                    'token':api.token,'case':'Docker Codex actual Responses','proof':proof,
                    'case_id':case['id'],'writes':writes}
                if images:
                    image_cid = api.call('POST','/conversations',{**payload,'title':'Docker workbench images'})['id']
                    assets,prompt,expected = image_examples()
                    data['images'] = {'conversation':image_cid,'assets':assets,'prompt':prompt,'expected':expected}
                subprocess.run([str(browser_python.resolve(strict=True)),'-B','-X','utf8',
                        str(Path(__file__).with_name('docker_codex_browser.py'))],
                    input=json.dumps(data),text=True,encoding='utf8',check=True,timeout=310,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                browser = json.loads((api.root/'codex-docker-edge.json').read_text(encoding='utf8'))
                turns = api.call('GET',f'/codex/conversations/{plan}/turns')
                assert browser['passed']
                row = next(item for item in turns['data'] if item['session_id']==browser['startup_id'])
                plan_turn, events = settled(row,'completed')
                assert any(e['type']=='item.completed' and e['payload'].get('tool')=='vibeflow_read_case_file'
                           and e['payload'].get('success') for e in events)
                if writes:
                    write_row=next(item for item in turns['data'] if item['session_id']==browser['write_startup_id'])
                    continued,_=settled(write_row,'completed')
                    assert continued['thread_id']==plan_turn['thread_id'] and browser['continued_plan_write']
                if images:
                    image_turns = api.call('GET',f'/codex/conversations/{image_cid}/turns')
                    image_row = next(item for item in image_turns['data']
                                     if item['session_id']==browser['images_startup_id'])
                    settled(image_row,'completed')
                    assert browser['images_visible_after_reload']
                report['edge'] = browser
                record('edge_native_plan_reads_case_and_restores_history')
        report['passed'] = True
    finally:
        try:
            if db_path.is_file():
                with sqlite3.connect(db_path) as conn:
                    exists=conn.execute("SELECT 1 FROM sqlite_master WHERE name='native_cli_sessions'").fetchone()
                if exists:
                    sessions = receipt_turns()
                    with sqlite3.connect(db_path) as conn:
                        unfinished = conn.execute("SELECT id FROM native_cli_sessions WHERE runtime='docker' "
                                                  "AND stop_json IN ('{}','null')").fetchall()
                    if unfinished and user_token and not report['passed']:
                        with api:
                            api.token = user_token
                            for identifier, in unfinished:
                                api.call('POST',f'/codex/sessions/{identifier}:reconcile-scope')
                    volumes = set(docker_acp.command('volume','ls','--format','{{.Name}}').splitlines())
                    seen = set()
                    for _, uid, selected in sessions:
                        selection = json.loads(selected)
                        state = codex_docker.state(codex_docker.runtime_from(selection['docker_runtime']),
                                                  uid,selection['credential'])
                        if state.name in volumes and state.name not in seen:
                            state.remove(docker_acp.command)
                            seen.add(state.name)
        finally:
            fixture.unlink(missing_ok=True)
            vault.unlink(missing_ok=True)
            original_match = set(initial)==set(docker_acp.command('ps','--no-trunc','--format','{{.ID}} {{.Names}}').splitlines())
            report['original_containers_match']=original_match
            report['passed'] = report['passed'] and original_match
            save()
    assert report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('review',type=Path)
    parser.add_argument('--bundle',type=Path)
    parser.add_argument('--browser-python',type=Path)
    parser.add_argument('--writes',action='store_true')
    parser.add_argument('--images',action='store_true')
    parser.add_argument('--rustfs',type=Path)
    args=parser.parse_args()
    if bool(args.bundle)!=bool(args.browser_python):
        parser.error('--bundle and --browser-python are used together')
    manifest=json.loads(args.review.read_text(encoding='utf8'))
    assert sha256(Path(__file__).read_bytes()).hexdigest()==manifest['sources'][Path(__file__).relative_to(PROJECT).as_posix()]
    review(args.review)
    store = nullcontext()
    if args.rustfs is not None:
        from live_llm_s3_publication_maintenance_acceptance import RustFS
        store = RustFS(args.rustfs)
    config = asyncio.run(source(26))
    with store as selected:
        verify(config,bundle=args.bundle,browser_python=args.browser_python,writes=args.writes,images=args.images,
               storage_env=selected.settings() if selected else None)
