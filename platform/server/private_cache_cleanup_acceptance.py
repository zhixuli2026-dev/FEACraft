"""Actual Windows sharing faults, writer drain and exited-owner cache recovery.

Pure filesystem/process protocol checks. No database or model calls.
"""
import ctypes,hashlib,json,os,subprocess,sys,tempfile,threading
from concurrent.futures import ThreadPoolExecutor
from ctypes import wintypes
from pathlib import Path

SERVER=Path(__file__).resolve().parent


def hold(path):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE
    kernel.CloseHandle.argtypes=[wintypes.HANDLE];kernel.CloseHandle.restype=wintypes.BOOL
    handle=kernel.CreateFileW(str(path),0x80000000,0x1|0x2,None,3,0x02000000,None)
    if handle in {None,ctypes.c_void_p(-1).value}:raise ctypes.WinError(ctypes.get_last_error())
    return lambda:kernel.CloseHandle(handle)


def files(root):
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob('*') if p.is_file()}


def child(kind):
    from app.services.private_cache import Cache
    cache=Cache(kind);cache.write('D-post/value.txt',b'real abandoned cache input')
    close=hold(cache.root/'D-post')
    try:
        result=cache.cleanup();assert result['status']=='retained'
        print(json.dumps({'container':str(cache.container),'owner':cache.record['owner'],'cleanup':result}),flush=True)
        sys.stdin.read(1)
    finally:close()


def main():
    assert os.name=='nt','This explicit entry requires real Win32 sharing semantics'
    root=Path(tempfile.mkdtemp(prefix='private-cache-cleanup-',dir=SERVER.parents[1]/'tmp')).resolve()
    os.environ.update(VIBEFLOW_STORAGE_FS_ROOT=str(root/'files'),VIBEFLOW_CODEX_STATE_ROOT=str(root/'cli'),
        VIBEFLOW_CODEX_WSL_STATE_ROOT='',VIBEFLOW_NODE_STORAGE_LIMIT_MB='1',VIBEFLOW_NODE_STORAGE_MIN_FREE_MB='0')
    from app.services import private_cache as caches,node_storage_budget as budget,process_identity_native as identity
    from app.services.case_write import CaseWriteConflict
    checks=[]
    def record(value):
        checks.append(value);(root/'progress.json').write_text(json.dumps(checks,indent=2),encoding='utf8')
        print('CACHE_CLEANUP_CHECK '+value['check'],flush=True)
    for kind in sorted(caches.KINDS):
        for target in ['data/D-post','.','owner.json','files.jsonl']:
            cache=caches.Cache(kind);cache.write('D-post/value.txt',b'real source bytes');cache.write('Ref/keep.txt',b'other input')
            owner=(cache.container/'owner.json').read_bytes();journal=(cache.container/'files.jsonl').read_bytes()
            close=hold(cache.container/target)
            try:
                result=cache.cleanup();assert result=={'status':'retained','reason':'sharing_violation','winerror':32}
                assert (cache.container/'owner.json').read_bytes()==owner and (cache.container/'files.jsonl').read_bytes()==journal
                assert next(v for v in caches.recover() if v['name']==cache.container.name)['status']=='active'
                try:cache.write('late.txt',b'must refuse')
                except ValueError:pass
                else:raise AssertionError('Closed cache accepted a new writer')
                retained_bytes=sum(p.stat().st_size for p in cache.container.rglob('*') if p.is_file());assert retained_bytes>0
            finally:close()
            assert cache.cleanup()['status']=='removed' and not cache.container.exists()
            assert cache.cleanup()['status']=='removed'
            record({'check':kind+':actual_lock:'+target,'actual_winerror':32,'owner_and_journal_preserved':True,
                'retained_physical_bytes':retained_bytes,'active_owner_not_reaped':True,'removed_after_handle_close':True})
        for mode in ['changed-input','foreign-file','foreign-directory','changed-owner','truncated-journal']:
            cache=caches.Cache(kind);cache.write('input.txt',b'original input')
            original_owner=(cache.container/'owner.json').read_bytes();original_journal=(cache.container/'files.jsonl').read_bytes()
            if mode=='changed-input':(cache.root/'input.txt').write_bytes(b'changed bytes')
            elif mode=='foreign-file':(cache.root/'foreign.txt').write_bytes(b'retain unknown bytes')
            elif mode=='foreign-directory':(cache.root/'unknown').mkdir()
            elif mode=='changed-owner':
                value=json.loads(original_owner);value['owner']['host_id']='0'*64;(cache.container/'owner.json').write_text(json.dumps(value))
            else:(cache.container/'files.jsonl').write_bytes(original_journal+b'{')
            before=files(cache.container)
            try:cache.cleanup()
            except ValueError:pass
            else:raise AssertionError('Unknown or changed cache contents were removed')
            assert files(cache.container)==before
            if mode=='changed-input':(cache.root/'input.txt').write_bytes(b'original input')
            elif mode=='foreign-file':(cache.root/'foreign.txt').unlink()
            elif mode=='foreign-directory':(cache.root/'unknown').rmdir()
            elif mode=='changed-owner':(cache.container/'owner.json').write_bytes(original_owner)
            else:(cache.container/'files.jsonl').write_bytes(original_journal)
            assert cache.cleanup()['status']=='removed'
            record({'check':kind+':unknown_retained:'+mode,'all_bytes_preserved_before_explicit_restore':True})
        cache=caches.Cache(kind);started=threading.Event();release=threading.Event();cleaned=threading.Event()
        source=root/'real-reader-input.txt';source.write_bytes(b'actual reader bytes')
        def reader():started.set();assert release.wait(10);return source.read_bytes()
        def writer():cache.fetch_many([('read.txt',source.stat().st_size,reader)])
        def cleanup():
            try:return cache.cleanup()
            finally:cleaned.set()
        with ThreadPoolExecutor(max_workers=2) as pool:
            writing=pool.submit(writer);assert started.wait(5);cleaning=pool.submit(cleanup)
            try:assert not cleaned.wait(.15),'Cleanup passed a still-active cache writer'
            finally:release.set()
            writing.result();assert cleaning.result()['status']=='removed'
        assert not cache.container.exists();source.unlink()
        record({'check':kind+':actual_reader_pool_drained','cleanup_waited_for_writer':True})
        process=subprocess.Popen([sys.executable,'-X','utf8','-B',__file__,'--child',kind],cwd=SERVER,env=dict(os.environ),
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,encoding='utf8',creationflags=subprocess.CREATE_NO_WINDOW)
        line=process.stdout.readline();assert line
        abandoned=json.loads(line);container=Path(abandoned['container'])
        assert container.resolve().is_relative_to(root)
        assert next(v for v in caches.recover() if v['name']==container.name)['status']=='active'
        stdout,stderr=process.communicate('x',timeout=20);assert process.returncode==0,stderr
        probe=identity.probe(abandoned['owner']);assert probe['state']=='exited'
        assert next(v for v in caches.recover() if v['name']==container.name)['status']=='removed' and not container.exists()
        record({'check':kind+':actual_exited_owner_recovery','actual_owner_exit_probe':probe,'metadata_survived_until_recovery':True})
    cache=caches.Cache('.case-read-views');cache.write('large.bin',b'A'*(900*1024));close=hold(cache.root/'large.bin')
    try:
        assert cache.cleanup()['status']=='retained';observed=budget.observe();assert observed['bytes']>=900*1024
        try:
            with budget.reserve(200*1024):raise AssertionError('Retained cache did not consume actual node capacity')
        except CaseWriteConflict as error:assert error.code=='node_storage_full'
    finally:close()
    assert cache.cleanup()['status']=='removed'
    with budget.reserve(200*1024):pass
    record({'check':'actual_retained_cache_charges_node_budget','actual_measured_bytes':observed['bytes'],'extra_200KiB_refused_until_cleanup':True})
    report={'passed':True,'model_calls':0,'mock_llm_calls':0,'checks':checks,'source_sha256':hashlib.sha256((SERVER/'app/services/private_cache.py').read_bytes()).hexdigest()}
    (root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8');print(json.dumps({'root':str(root),'checks':len(checks),'passed':True}))


if __name__=='__main__':
    if '--child' in sys.argv:child(sys.argv[2])
    else:main()
