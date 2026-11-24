"""Real DeepSeek file proposals during a paused baseline read, stop, and deadline."""
import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
import hashlib
import json
import sys
import time
import uuid

from file_tree_lock_acceptance import writer_available
from live_llm_acceptance import FIXTURE, source
from live_llm_agent_recovery_acceptance import stream
from live_llm_failure_fallback_acceptance import wait_for
from live_llm_terminal_agent_acceptance import disposable_database
from testsupport import IsolatedServer, SERVER


PROBE = '''
from contextlib import asynccontextmanager, closing
from contextvars import ContextVar
from app.services import case_read_capacity
from app.services.agent_file_reads import stop_signal
from app.services.file_read import FileRead
from app.services.tools import ToolExecutor

selection = ContextVar('baseline_fixture', default=None)
baseline = ToolExecutor.prepare_file_proposal
def observe_baseline(self, name, arguments):
    path = root/'baseline-selected.json'
    config = json.loads(path.read_text(encoding='utf8')) if path.exists() else None
    token = selection.set(config if config and arguments.get('path') == config['path'] else None)
    try:
        return baseline(self, name, arguments)
    finally:
        selection.reset(token)
ToolExecutor.prepare_file_proposal = observe_baseline

chunks = FileRead.chunks
def observe_chunks(self, *args, **kwargs):
    selected = selection.get()
    if selected is None:
        yield from chunks(self, *args, **kwargs)
        return
    assert stop_signal.get() is not None, 'Proposal read bypassed the cancellable reader'
    mode = selected['mode']
    seen = root/(mode+'-read.jsonl')
    ready = root/(mode+'-ready')
    released = root/(mode+'-release')
    number = 0
    try:
        with closing(chunks(self,*args,**kwargs)) as body:
            for block in body:
                number += 1
                if number == 1 and not ready.exists():
                    ready.write_text(str(len(block)), encoding='utf8')
                    stop_at = time.monotonic() + 24
                    while not released.exists():
                        if time.monotonic()>=stop_at:
                            raise TimeoutError('Actual baseline body was never released')
                        time.sleep(.03)
                yield block
    finally:
        with seen.open('a', encoding='utf8') as trace:
            trace.write(json.dumps({'blocks':number,'file_bytes':self.size})+'\\n')
FileRead.chunks = observe_chunks

deadline = case_read_capacity.file_read_deadline
@asynccontextmanager
async def limit(code, label, *, seconds=None):
    if code == 'proposal_baseline' and (root/'short-baseline-deadline').exists():
        seconds = 6
    async with deadline(code,label,seconds=seconds):
        yield
case_read_capacity.file_read_deadline = limit
'''


def exercise(api, case, profile, configuration, mode, *, remote):
    path = f'Ref/baseline-{mode}.txt'
    original = 'original version\n' + 'measurement line\n' * 63_000
    latest = 'externally saved version\n'
    replacement = 'SELECTED_' + uuid.uuid4().hex
    api.put(path, original)
    endpoint = api.prefix + '/file?path=' + path
    cid = api.call('POST', '/conversations', {'case_id':case['id'], 'title':mode,
        'permission_mode':'approve_writes', 'provider_profile_id':profile['id'], 'model':configuration['model']})['id']
    message = {'content':f'{path} 是我刚写入的一次性测试文件。首行是 original version，后面只有 63000 行 '
                         f'measurement line。我需要完整覆盖它。请直接调用一次 propose_file_update，'
                         f'把 content 精确设为 {replacement}，reason 写“核对基线”，并等待我批准。'
                         '旧内容已知，不需要读取或搜索。收到工具结果后只报告结果，不重试，不再调用工具。',
               'provider_profile_id':profile['id'], 'model':configuration['model']}
    route = f'/conversations/{cid}'
    (api.root/'baseline-selected.json').write_text(json.dumps({'path':path,'mode':mode}),encoding='utf8')
    short = api.root/'short-baseline-deadline'
    if mode=='timeout':
        short.touch()
    ready, release = api.root/(mode+'-ready'), api.root/(mode+'-release')
    with ThreadPoolExecutor(max_workers=1) as work:
        pending = work.submit(stream,api,route+'/messages',message,mode+'-proposal',configuration)
        try:
            def reached():
                if pending.done():
                    raise AssertionError(('Proposal completed before reaching the native read', pending.result()))
                return ready.exists()
            wait_for(reached,'Actual baseline read did not start',seconds=22)
            assert int(ready.read_text(encoding='utf8')) == 65_536
            assert writer_available(api,case['id']), 'Baseline read blocked the same-case database write lock'
            if mode=='cancel':
                assert api.call('POST',route+'/messages:cancel',{})['ok']
            elif mode=='timeout':
                time.sleep(6.2)
            else:
                api.put(path if remote else 'Ref/baseline-sibling.txt',latest)
            assert not pending.done(), 'The worker must drain before reporting completion'
        finally:
            release.touch()
        events = pending.result(timeout=120)
    short.unlink(missing_ok=True)
    assert not any(row['type'] in {'request.failed','stream.error','request.timed_out'} for row in events)
    reads = [json.loads(line) for line in (api.root/(mode+'-read.jsonl')).read_text(encoding='utf8').splitlines()]
    recorded = api.call('GET',route)
    proposals = [row['tool_call'] for row in events if row['type']=='approval.requested']
    if mode=='fresh':
        proposal, = proposals
        assert proposal['tool']=='propose_file_update' and proposal['args']['content']==replacement
        expected = latest if remote else original
        assert proposal['args']['baseline_sha256']==hashlib.sha256(expected.encode()).hexdigest()
        assert len(reads)==2 and api.call('GET',endpoint)['content']==expected
        approval = route+'/tool-calls/'+proposal['id']+':approve'
        approved = stream(api,approval,{},mode+'-approved',configuration)
        tool, = [row for row in approved if row['type']=='tool.result']
        assert tool['ok'] and tool['tool']=='propose_file_update'
        assert api.call('GET',endpoint)['content']==replacement
        api.call('POST',approval,{},expect=409)
    else:
        assert not proposals and not recorded['pending_tools']
        tool, = [row for row in events if row['type']=='tool.result']
        assert tool['tool']=='propose_file_update' and not tool['ok']
        assert tool['status']==('cancelled' if mode=='cancel' else 'error')
        assert any(row['type']==('run.cancelled' if mode=='cancel' else 'run.completed') for row in events)
        if mode=='timeout':
            assert tool['code']=='proposal_baseline_timeout'
        durable, = [row['payload'] for row in recorded['events'] if row['type']=='tool_result']
        assert durable['tool_call_id']==tool['tool_call_id'] and durable['status']==tool['status']
        assert len(reads)==1 and reads[0]['blocks']==1
        assert api.call('GET',endpoint)['content']==original
    return {'passed':True,'database_write_available':True,'native_reads':reads,'approved':mode=='fresh',
            'status':tool['status'], 'conversation_id':cid}


def verify(configuration, modes, storage, database_url):
    api = IsolatedServer('live-proposal-baseline', app='baseline_fixture:app', database_url=database_url)
    settings = storage.settings() if storage else {}
    (api.root/'baseline_fixture.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE+PROBE,
                                              encoding='utf8')
    report = {'passed':False,'simulated_model_calls':0,'database':'postgresql' if database_url else 'sqlite',
              'backend':'s3' if storage else 'fs','checks':{}}
    print('LIVE_PROPOSAL_BASELINE_EVIDENCE',api.root,flush=True)
    try:
        with api:
            case = api.create_case('Actual Agent proposal baseline')
            profile = api.call('POST','/provider-profiles',{'provider':'deepseek','name':'Actual proposal baseline',
                'base_url':configuration['base_url'],'api_key':configuration['key'],'default_model':configuration['model']})
            for mode in modes:
                report['checks'][mode]=exercise(api,case,profile,configuration,mode,remote=bool(storage))
                print('LIVE_PROPOSAL_BASELINE_PASS',mode,flush=True)
        report['passed']=True
    except Exception as error:
        report['failure']={'type':type(error).__name__,'detail':str(error).replace(configuration['key'],'[REDACTED]')[:1200]}
        print('LIVE_PROPOSAL_BASELINE_FAILED',report['failure'],flush=True)
    finally:
        trace=api.root/'upstream.jsonl'
        report['upstream']=[json.loads(line) for line in trace.read_text(encoding='utf8').splitlines()] if trace.exists() else []
        (api.root/'live-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return 0 if report['passed'] else 1


async def run(options):
    configuration = await source(options.source_profile_id)
    store = nullcontext()
    if options.s3:
        from s3testsupport import ObjectStore
        store = ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed',authenticated=True)
    async with disposable_database(options.postgres) as database_url:
        with store as active:
            return await asyncio.to_thread(verify,configuration,options.modes.split(','),active,database_url)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--modes',default='fresh,cancel,timeout')
    parser.add_argument('--s3',action='store_true')
    parser.add_argument('--postgres',action='store_true')
    args=parser.parse_args()
    selected=args.modes.split(',')
    if len(selected)!=len(set(selected)) or not set(selected)<={'fresh','cancel','timeout'}:
        parser.error('Select unique fresh, cancel, and timeout modes')
    sys.exit(asyncio.run(run(args)))
