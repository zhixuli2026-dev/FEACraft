"""Actual native handoff exits, real independent deadlines and invalid permits."""
import asyncio,hashlib,json,socket,subprocess,tempfile,time
from pathlib import Path
import aiohttp
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine,async_sessionmaker
from app.models import NativeRequest,DurableEvent
from app.services import sandbox_image_scope as scopes,process_identity_native as identity
from app.services.owned_process import probe_native
from testsupport import SERVER

FAULT_HELPER='''
def _handoff_fault(stage,birth):
    root=Path(__file__).parents[2]
    armed=root/'native-handoff-fault.json'
    if not armed.exists() or json.loads(armed.read_text())['stage']!=stage:return
    armed.unlink()
    with (root/('native-handoff-'+stage+'.json')).open('x') as stream:
        stream.write(json.dumps({'stage':stage,'birth':birth}));stream.flush();os.fsync(stream.fileno())
    os._exit(83)

'''

FIXTURE='''
import asyncio,json
from pathlib import Path
from app.services import sandbox_image_launch as _launch,native_process_preflight as _flight
from app.services.windows_scope_guard import Guardian as _Guardian
_guardian_init=_Guardian.__init__
def _deadline_guardian(self,scope,root,**kwargs):
 armed=Path('handoff-deadline.json')
 if armed.exists() and json.loads(armed.read_text())['stage']=='guardian_deadline':
  kwargs['max_seconds']=20
 return _guardian_init(self,scope,root,**kwargs)
_Guardian.__init__=_deadline_guardian
_handoff_observed=_launch.observed
async def _deadline_observed(owner,child_id,value):
 result=await _handoff_observed(owner,child_id,value)
 armed=Path('handoff-deadline.json')
 if value['stage']=='host_ready' and armed.exists():
  stage=json.loads(armed.read_text())['stage'];armed.unlink()
  pending=Path('handoff-paused.pending')
  pending.write_text(json.dumps({'stage':stage,'request_id':owner.id,'child_id':child_id}))
  pending.replace('handoff-paused.json')
  async with asyncio.timeout(100):
   while not Path('handoff-resume').exists():await asyncio.sleep(.02)
 return result
_launch.observed=_deadline_observed
_correct_permit=_flight.permit
def _invalid_permit(nonce,birth):
 value=_correct_permit(nonce,birth)
 armed=Path('wrong-native-permit')
 if armed.exists():
  armed.unlink();value['birth_sha256']='0'*64
 return value
_flight.permit=_invalid_permit
'''

def source_copy():
    root=Path(tempfile.mkdtemp(prefix='native-handoff-source-',dir=SERVER.parents[1]/'tmp'))
    destination=root/'app/services';destination.mkdir(parents=True)
    original=SERVER/'app/services';hashes={}
    for path in original.rglob('*'):
        relative=path.relative_to(original)
        if not path.is_file() or '__pycache__' in relative.parts or path.suffix not in {'.py','.sh'}:continue
        target=destination/relative;target.parent.mkdir(parents=True,exist_ok=True)
        raw=path.read_bytes();target.write_bytes(raw);hashes[relative.as_posix()]=hashlib.sha256(raw).hexdigest()
    preflight=destination/'native_process_preflight.py';code=preflight.read_text(encoding='utf8')
    def replace(before,after):
        nonlocal code
        assert code.count(before)==1,before
        code=code.replace(before,after)
    replace('def main():',FAULT_HELPER+'def main():')
    replace('    # Read one line without read-ahead;',"    _handoff_fault('before_permit',birth)\n    # Read one line without read-ahead;")
    replace("    if identities['observe'](os.getpid())!=birth:","    _handoff_fault('after_permit',birth)\n    if identities['observe'](os.getpid())!=birth:")
    compile(code,str(preflight),'exec');preflight.write_text(code,encoding='utf8')
    gate=destination/'process_gate.py';code=gate.read_text(encoding='utf8')
    replace('def main():',FAULT_HELPER+'def main():')
    replace('        enter_native_scope(args.native_scope,args.nonce)',
        "        enter_native_scope(args.native_scope,args.nonce)\n        _handoff_fault('after_join',identity_module().observe(os.getpid()))")
    compile(code,str(gate),'exec');gate.write_text(code,encoding='utf8')
    manifest={'original':hashes,'instrumented':{name:hashlib.sha256((destination/name).read_bytes()).hexdigest() for name in hashes},
        'production_modified_by_fixture':False,'fault_helper':FAULT_HELPER}
    assert {name for name in hashes if hashes[name]!=manifest['instrumented'][name]}=={'native_process_preflight.py','process_gate.py'}
    assets={}
    for path in (SERVER/'skills').rglob('*'):
        if not path.is_file() or path.is_symlink() or path.suffix not in {'.md','.yaml','.yml','.json'}:continue
        relative=path.relative_to(SERVER);target=root/relative;target.parent.mkdir(parents=True,exist_ok=True)
        raw=path.read_bytes();target.write_bytes(raw);assets[relative.as_posix()]=hashlib.sha256(raw).hexdigest()
    manifest['shipped_skill_assets']=assets
    (root/'source-manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf8')
    return 'import app.services\napp.services.__path__.insert(0,'+repr(str(destination))+')\n',root

async def after_retirement(api,sid,rid,proof,continuation=None,source_root=None):
    report={'passed':False,'checks':[],'cases':[],'llm_calls':0,'mock_llm_calls':0,'source_copy':str(source_root),
        'database':'postgresql' if api.database_url else 'sqlite'}
    engine=create_async_engine(api.database_url or 'sqlite+aiosqlite:///'+(api.root/'test.sqlite').as_posix())
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    worker=None;first=None;log=(api.root/'handoff-peer-api.log').open('w',encoding='utf8')
    async def rows():
        async with sessions() as db:
            result={}
            for row in await db.scalars(select(NativeRequest).where(NativeRequest.kind=='terminal.cleanup')):
                child=next(iter(scopes.records.document(row)['children'].values()))
                result[row.id]={'phase':row.phase,'error':row.error,'children_json':row.children_json,'result_json':row.result_json,
                    'owner':json.loads(row.owner_identity_json),'profile':json.loads(row.profile_json),
                    'child':child,'scope':await scopes.evidence(db,row,child)}
            return result
    def record(name,kind='actual_infrastructure'):
        report['checks'].append({'name':name,'kind':kind});print('NATIVE_HANDOFF_PASS '+name,flush=True)
    try:
        async with sessions() as db:selected=json.loads((await db.get(NativeRequest,rid)).profile_json)
        image=Path(selected['image_root'])/'volume.ext4';archive=Path(selected['root'])/'recovery.zip';retained=archive.read_bytes()
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        args=list(api.proc.args);args[args.index('--port')+1]=str(port)
        worker=subprocess.Popen(args,cwd=api.root,env=api.env,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        peer='http://127.0.0.1:'+str(port);report['peer']=peer
        async with aiohttp.ClientSession(headers={'Authorization':'Bearer '+api.token},timeout=aiohttp.ClientTimeout(total=210)) as client:
            async with asyncio.timeout(40):
                while True:
                    assert worker.poll() is None
                    try:
                        async with client.get(peer+'/api/health') as response:
                            if response.status==200:break
                    except aiohttp.ClientError:pass
                    await asyncio.sleep(.1)
            endpoint='/api/v1/terminal/sessions/'+sid+'/image-cleanup'
            async def submit(host):
                async with client.post(host+endpoint,json={'recovery_id':rid,'confirm':True}) as response:
                    return response.status,await response.text()
            for index,stage in enumerate(('before_permit','after_permit','after_join','wrong_permit','preflight_deadline','guardian_deadline')):
                before=await rows();paused=None;elapsed=None;deadline_evidence=None
                owner_host,other=(api.base,peer) if index%2==0 else (peer,api.base)
                if stage.endswith('_deadline'):
                    for filename in ('handoff-resume','handoff-paused.json'):(api.root/filename).unlink(missing_ok=True)
                    (api.root/'handoff-deadline.json').write_text(json.dumps({'stage':stage}))
                    first=asyncio.create_task(submit(owner_host))
                    async with asyncio.timeout(60):
                        while not (api.root/'handoff-paused.json').exists():
                            assert not first.done(),await first if first.done() else 'Deadline checkpoint not reached'
                            await asyncio.sleep(.02)
                    marker=json.loads((api.root/'handoff-paused.json').read_text());paused=(await rows())[marker['request_id']]
                    assert paused['phase']=='active' and identity.probe(paused['owner'])['state']=='match'
                    assert (await submit(other))[0]==409 and image.exists()
                    admission=paused['scope']['admitted'];native=admission['native_preflight']['native']
                    started=time.monotonic()
                    async with asyncio.timeout(65):
                        while True:
                            probe=await probe_native(native['distribution'],native['process'])
                            if probe['state']=='exited':break
                            assert probe['state']=='match',probe
                            await asyncio.sleep(.5)
                    elapsed=time.monotonic()-started
                    if stage=='guardian_deadline':
                        request=admission['guardian']['request']
                        deadline_evidence=json.loads((Path(request['root']['path'])/'receipt.json').read_text())
                        assert request['max_seconds']==20 and deadline_evidence['reason']=='deadline'
                        assert identity.probe(admission['guardian']['preparation']['guardian'])['state']=='exited'
                    else:assert 30<elapsed<65,'Actual 45-second preflight expiry not observed in expected window'
                    assert not first.done() and (await submit(other))[0]==409
                    assert (await rows())[marker['request_id']]['phase']=='active'
                    (api.root/'handoff-resume').write_text('resume after actual independent expiry')
                    code,raw=await first
                else:
                    if stage=='wrong_permit':(api.root/'wrong-native-permit').write_text('send wrong birth binding once')
                    else:(source_root/'native-handoff-fault.json').write_text(json.dumps({'stage':stage}))
                    code,raw=await submit(owner_host)
                assert code==409,(stage,code,raw)
                after=await rows();created=set(after)-set(before);assert len(created)==1
                identifier=created.pop();row=after[identifier]
                report['cases'].append({'stage':stage,'request_id':identifier,'persisted':row,'paused':paused,
                    'elapsed_after_pause':elapsed,'deadline_receipt':deadline_evidence})
                assert row['phase']=='failed' and row['scope']['closed'] is not None
                exits={}
                if stage in {'before_permit','after_permit','after_join'}:
                    fault=json.loads((source_root/('native-handoff-'+stage+'.json')).read_text())
                    exits['interrupted_native']=await probe_native(row['scope']['planned']['distribution'],fault['birth'])
                    report['cases'][-1]['fault']=fault
                if row['scope']['admitted'] is not None:
                    flight=row['scope']['admitted']['native_preflight']
                    exits.update(host=identity.probe(flight['host']),native=await probe_native(flight['native']['distribution'],flight['native']['process']))
                assert exits and all(v['state']=='exited' for v in exits.values()),exits
                report['cases'][-1]['actual_exits']=exits
                assert image.exists() and archive.read_bytes()==retained
                record(stage+'_fails_without_business_release','protocol_fault' if stage=='wrong_permit' else 'actual_infrastructure')
            original=await rows();code,raw=await submit(peer);assert code==200,(code,raw);done=json.loads(raw)
            assert done['status']=='completed' and done['deleted_this_request'] and not image.exists()
            for host in (api.base,peer):
                status,raw=await submit(host);assert status==200 and json.loads(raw)==done
                for mode in ('validation','admin'):
                    async with client.get(host+f'/fixture/{api.case_id}/{mode}') as response:assert response.status==200
            final=await rows()
            for identifier,row in original.items():
                assert final[identifier]['children_json']==row['children_json'] and final[identifier]['result_json']==row['result_json']
            async with sessions() as db:
                completed=list(await db.scalars(select(DurableEvent.id).where(DurableEvent.type=='terminal.image_cleanup_completed')))
                assert len(completed)==1
            assert archive.read_bytes()==retained;report['completion']=done
            record('both_workers_finish_once_after_all_failed_handoffs')
        if continuation:
            report['llm_calls']=await continuation(api,sid,rid,proof);record('real_model_reads_snapshot_after_handoff_failures','real_llm')
        report['passed']=True;return report['llm_calls']
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'detail':str(error)[:1200]};raise
    finally:
        (api.root/'handoff-resume').write_text('drain actual preparation')
        if first is not None and not first.done():await asyncio.gather(first,return_exceptions=True)
        if worker is not None:
            if worker.poll() is None:worker.terminate()
            await asyncio.to_thread(worker.wait,10);report['peer_exited']=worker.returncode is not None
        log.close();await engine.dispose()
        trace=api.root/'upstream.jsonl'
        if trace.exists():report['llm_calls']=sum(v['kind']=='http' and v['path'].endswith('/chat/completions') for v in map(json.loads,trace.read_text(encoding='utf8').splitlines()))
        (api.root/'native-handoff-checks.json').write_text(json.dumps(report,indent=2),encoding='utf8')
