"""Cut a real Agent approval around its file SQL commit and restart the API."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from functools import partial
import hashlib
import json
import sys
import uuid

import httpx

from live_llm_acceptance import FIXTURE as LIVE, source
from live_llm_agent_recovery_acceptance import stream
from live_llm_approval_acceptance import postgres
from live_llm_failure_fallback_acceptance import kill_owned_api, wait_for
from s3testsupport import ObjectStore
from testsupport import IsolatedServer, SERVER
from app.services.memory import MEMORY_PATH


PATH = '0-caseDict/caseDict'
ORDINARY_PATH = 'Ref/approved-recovery.txt'
CUTS = ('before-commit', 'after-commit')
FIXTURE = '''
import asyncio
import hashlib
import os
import uuid
from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app import storage
from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import AgentEvent, Case, Conversation, ConversationExecution, ToolCall
from app.routers import agent
from app.routers.workspaces import get_owned_case
from app.services.case_events import CASE_EVENTS_PATH
from app.services.casedict import CASE_DICT_PATH
from app.services.memory import MEMORY_PATH
from app.services.provenance import PROVENANCE_PATH

def file_hashes(case, path):
    paths = (CASE_DICT_PATH, CASE_EVENTS_PATH, PROVENANCE_PATH) if path==CASE_DICT_PATH else (path,CASE_DICT_PATH)
    return {path:hashlib.sha256(storage.get_bytes(case.s3_prefix,path)).hexdigest() for path in paths}

async def snapshot(db, cid, tid):
    conv = await db.get(Conversation,cid)
    case = await db.get(Case,conv.case_id)
    tool = await db.get(ToolCall,tid)
    owner = await db.get(ConversationExecution,cid)
    path = MEMORY_PATH if tool.tool=='update_memory' else json.loads(tool.args)['path']
    events = await db.scalars(select(AgentEvent.payload).where(
        AgentEvent.conversation_id==cid,AgentEvent.type=='tool_result'))
    results = [event for raw in events if (event:=json.loads(raw)).get('tool_call_id')==tid.split(':',1)[-1]]
    return {'revision':case.dict_revision,'tool_status':tool.status,'result':tool.result,
            'results':results,'owner':owner.phase,'hashes':await asyncio.to_thread(file_hashes,case,path)}

@app.get('/api/v1/fixture/dictionary-recovery/{cid}/{tid}')
async def read_recovery(cid:int,tid:str,user=Depends(get_current_user),db=Depends(get_db)):
    conv = await db.get(Conversation,cid)
    tool = await db.get(ToolCall,tid)
    assert tool.conversation_id==cid
    await get_owned_case(conv.case_id,user,db,write=False)
    return {**await snapshot(db,cid,tid),'startup':app.state.agent_recovery}

if os.environ.get('VIBEFLOW_DICT_CRASH')=='writer':
    original_store = agent._store_tool_result
    original_commit = AsyncSession.commit

    async def hold(boundary):
        async with SessionLocal() as db:
            value = await snapshot(db,boundary['cid'],boundary['tid'])
        value.update(pid=os.getpid(),cut=boundary['cut'],tool_id=boundary['tid'])
        target = root/(boundary['cut']+'-witness.json')
        partial = target.with_name('.'+uuid.uuid4().hex)
        partial.write_text(json.dumps(value),encoding='utf8')
        partial.replace(target)
        async with asyncio.timeout(80):
            while not (root/(boundary['cut']+'-release')).exists():
                await asyncio.sleep(.03)

    async def result(db,call,model_call_id,args,result,status,**details):
        await original_store(db,call,model_call_id,args,result,status,**details)
        boundary = json.loads((root/'selected.json').read_text(encoding='utf8'))
        if not result['ok'] or call.conversation_id!=boundary['cid']:
            return
        if boundary.get('memory'):
            if call.tool!='update_memory':
                return
            boundary['tid'] = call.id
        elif call.id!=boundary['tid']:
            return
        if boundary['cut']=='before-commit':
            await hold(boundary)
        else:
            db.info['dictionary_crash_after_commit'] = boundary
    agent._store_tool_result = result

    async def commit(db):
        boundary = db.info.pop('dictionary_crash_after_commit',None)
        await original_commit(db)
        if boundary is not None:
            await hold(boundary)
    AsyncSession.commit = commit
'''


def write_fixture(api, shared_root, overrides):
    live = LIVE.replace('root=Path(__file__).parent', f'root=Path({str(shared_root)!r})')
    assert live != LIVE
    location = api.root/'dictionary_fixture.py'
    location.write_text('import os\nos.environ.update('+repr(overrides)+')\n'+live+FIXTURE,encoding='utf8')
    return location


def peer(host, store, role):
    api = IsolatedServer('dictionary-recovery-'+role,app='dictionary_fixture:app')
    shared = {key:value for key,value in host.env.items() if key.startswith('VIBEFLOW_')}
    if store is not None:
        shared.update(store.settings())
    shared['VIBEFLOW_DICT_CRASH'] = role
    write_fixture(api,host.root,shared)
    api.token = host.token
    api.case_id = host.case_id
    api.prefix = host.prefix
    return api


def execute_cut(host, store, configuration, cut, report, *, ordinary=False, memory=False):
    host.create_case('Real file approval '+cut)
    profile = host.call('POST','/provider-profiles',{
        'provider':'deepseek','name':'Dictionary approval exit','base_url':configuration['base_url'],
        'default_model':configuration['model'],'api_key':configuration['key']})
    initial = host.call('GET',host.prefix+'/caseDict')
    old = 'ORIGINAL_'+uuid.uuid4().hex
    new = 'REPLACED_'+uuid.uuid4().hex
    if ordinary or memory:
        path, text = MEMORY_PATH if memory else ORDINARY_PATH, old+'\n'
        paths = (path,PATH)
        seeded = initial
        host.put(path,text)
    else:
        path, text = PATH, initial['content']+'\n// '+old+'\n'
        paths = (path,'0-caseDict/case-events.jsonl','0-caseDict/provenance.jsonl')
        seeded = host.call('PUT',host.prefix+'/caseDict',{'content':text,'expected_revision':initial['dict_revision']})
    before = {path:hashlib.sha256(host.call('GET',host.prefix+'/raw?path='+path,raw=True)).hexdigest()
              for path in paths}
    conv = host.call('POST','/conversations',{'case_id':host.case_id,'title':'Dictionary exit '+cut,
        'model':configuration['model'],'provider_profile_id':profile['id'],'permission_mode':'approve_writes'})
    cid = conv['id']
    prompt = (f'请仅调用一次 propose_text_replacement，对 {path} 将唯一的 {old} 替换为 {new}。'
              '保持所有实际参数原样并等待用户批准，不要调用别的工具。')
    def append_payload():
        return {'content':f'请仅调用一次 update_memory，action=append，把 {new} 原样作为 content 写入当前算例记忆。'
                          '请直接调用工具。收到结果后只简短回答，不再次调用。',
                'provider_profile_id':profile['id'],'model':configuration['model']}

    if memory:
        tid = None
        write_route = f'/conversations/{cid}/messages'
        payload = append_payload()
    else:
        events = stream(host,f'/conversations/{cid}/messages',{'content':prompt,
            'provider_profile_id':profile['id'],'model':configuration['model']},cut+'-model',configuration)
        proposal, = [event['tool_call'] for event in events if event['type']=='approval.requested']
        args = proposal['args']
        assert (proposal['tool'],args['path'],args['old_text'],args['new_text'],args['baseline_sha256']) == (
            'propose_text_replacement',path,old,new,before[path])
        tid = proposal['id']
        write_route = f'/conversations/{cid}/tool-calls/{tid}:approve'
        payload = {}
    notice = host.root/(cut+'-witness.json')
    release = host.root/(cut+'-release')
    (host.root/'selected.json').write_text(json.dumps({'cid':cid,'tid':tid,'cut':cut,'memory':memory}),encoding='utf8')
    expected_commit = cut=='after-commit'
    revision = seeded['dict_revision']+int(expected_commit and not (ordinary or memory))
    expected_content = old+'\n\n'+new+'\n' if memory else text.replace(old,new,1)
    expected_hash = hashlib.sha256(expected_content.encode()).hexdigest()
    crash = peer(host,store,'writer')
    with crash, ThreadPoolExecutor(max_workers=1) as threads:
        pending = threads.submit(stream,crash,write_route,payload,cut+'-interrupted',configuration)
        try:
            def boundary():
                if pending.done():
                    raise AssertionError(('Approval did not reach the cut',pending.result()))
                return notice.is_file()
            wait_for(boundary,'File write did not reach the selected commit position',seconds=55)
            witness = json.loads(notice.read_text(encoding='utf8'))
            assert witness['hashes'][path]==expected_hash,witness
            assert witness['revision']==revision,witness
            if ordinary or memory:
                assert witness['hashes'][PATH]==before[PATH]
            assert witness['tool_status']==('executed' if expected_commit else 'running'),witness
            assert len(witness['results'])==int(expected_commit),witness
            launcher = kill_owned_api(crash,witness['pid'])
            report.update(witness=witness,verified_launcher_pid=launcher,original_hashes=before)
            try:
                pending.result(timeout=15)
            except (httpx.RemoteProtocolError,httpx.ReadError,httpx.ConnectError):
                report['real_http_terminated'] = True
            else:
                raise AssertionError('Approval stream completed despite actual API exit')
        finally:
            release.touch()

    tid = witness['tool_id']
    recovery = peer(host,store,'cold')
    with recovery:
        recovered = recovery.call('GET',f'/fixture/dictionary-recovery/{cid}/{tid}')
        report['cold'] = recovered
        assert recovered['startup']['recovered']>=1 and not recovered['startup']['failed'],recovered
        assert recovered['owner']=='idle' and recovered['revision']==revision
        assert recovered['tool_status']==('executed' if expected_commit else 'interrupted'),recovered
        assert recovered['hashes']==(witness['hashes'] if expected_commit else before),recovered
        result, = recovered['results']
        assert result['ok']==expected_commit
        assert result['status']==('completed' if expected_commit else 'interrupted')
        published = recovery.call('GET',recovery.prefix+'/caseDict')
        assert published['dict_revision']==recovered['revision']
        assert hashlib.sha256(published['content'].encode()).hexdigest()==recovered['hashes'][PATH]
        if ordinary or memory:
            written = recovery.call('GET',recovery.prefix+'/raw?path='+path,raw=True)
            assert hashlib.sha256(written).hexdigest()==recovered['hashes'][path]
        recovery.call('POST',f'/conversations/{cid}/tool-calls/{tid}:approve',{},expect=409)
        if memory and not expected_commit:
            events = stream(recovery,f'/conversations/{cid}/messages',append_payload(),cut+'-retry',configuration)
            tool, = [event for event in events if event['type']=='tool.result' and event['tool']=='update_memory']
            assert tool['tool']=='update_memory' and tool['ok'] and tool['tool_call_id']!=tid.split(':',1)[1]
            assert any(event['type']=='run.completed' for event in events)
            raw = recovery.call('GET',recovery.prefix+'/raw?path='+path,raw=True)
            assert raw.decode()==expected_content and raw.decode().count(new)==1
            report['retried_new_tool_once'] = True
    report['passed'] = True


def verify(configuration, cuts, store=None, database_url=None, *, ordinary=False, memory=False):
    label = 'live-agent-memory-recovery' if memory else ('live-agent-file-recovery' if ordinary else 'live-agent-dictionary-recovery')
    host = IsolatedServer(label,app='dictionary_fixture:app',database_url=database_url)
    write_fixture(host,host.root,store.settings() if store else {})
    report = {'passed':False,'backend':'s3' if store else 'fs','simulated_model_calls':0,'cases':{},
              'ordinary_file':ordinary,'memory':memory}
    destination = host.root/'live-result.json'
    print('LIVE_DICTIONARY_RECOVERY_EVIDENCE',host.root,flush=True)
    try:
        with host:
            for cut in cuts:
                run = report['cases'].setdefault(cut,{})
                execute_cut(host,store,configuration,cut,run,ordinary=ordinary,memory=memory)
                print('LIVE_DICTIONARY_RECOVERY_PASS',cut,flush=True)
        report['passed'] = True
    except Exception as error:
        report['failure'] = {'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1800]}
        print('LIVE_DICTIONARY_RECOVERY_FAILED',report['failure'],flush=True)
    finally:
        trace = host.root/'upstream.jsonl'
        upstream = [json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.is_file() else []
        report['upstream'] = upstream
        report['actual_model_calls'] = sum(row['kind']=='http' for row in upstream)
        if report['passed']:
            expected = len(cuts)+2*int(memory and 'before-commit' in cuts)
            assert report['actual_model_calls']>=expected if memory else report['actual_model_calls']==expected
            assert all(row['status']==200 for row in upstream if row['kind']=='http')
        destination.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    return 0 if report['passed'] else 1


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--cut',choices=(*CUTS,'both'),default='both')
    parser.add_argument('--s3',action='store_true')
    parser.add_argument('--postgres',action='store_true')
    files = parser.add_mutually_exclusive_group()
    files.add_argument('--ordinary',action='store_true')
    files.add_argument('--memory',action='store_true')
    arguments = parser.parse_args()
    configuration = asyncio.run(source(arguments.source_profile_id))
    cuts = CUTS if arguments.cut=='both' else (arguments.cut,)
    native = SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed'
    context = ObjectStore(executable=native,volume_max=12,authenticated=True) if arguments.s3 else nullcontext()
    verifier = partial(verify,ordinary=arguments.ordinary,memory=arguments.memory)
    with context as store:
        code = (asyncio.run(postgres(configuration,cuts,store,verifier=verifier)) if arguments.postgres
                else verifier(configuration,cuts,store))
    sys.exit(code)
