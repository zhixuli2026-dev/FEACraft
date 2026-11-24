"""Actual file reads stop between chunks at the inherited request deadline."""
import asyncio,hashlib,json,tempfile,time,zipfile
from pathlib import Path
from app.services import sandbox_recovery_execution as recovery,request_deadline
from app.services.async_ops import finish_before_cancel
from testsupport import SERVER


async def verify():
    root=Path(tempfile.mkdtemp(prefix='archive-read-deadline-',dir=SERVER.parents[1]/'tmp'))
    path=root/'recovery.zip'
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_STORED) as bundle:
        bundle.writestr('proof.txt',b'actual archive bytes\n'*(128*1024))
    raw=path.read_bytes()
    expected={'archive_bytes':len(raw),'archive_sha256':hashlib.sha256(raw).hexdigest()}
    report={'passed':False,'llm_calls':0,'mock_llm_calls':0,'checks':[]}
    stats={'read_calls':0,'closed':False,'worker_error':None};original=Path.open
    class ObservedRead:
        def __init__(self,stream):self.stream=stream
        def __getattr__(self,name):return getattr(self.stream,name)
        def read(self,*args):
            value=self.stream.read(*args);stats['read_calls']+=1
            if stats['read_calls']==1:time.sleep(.25)
            return value
        def close(self):
            self.stream.close();stats['closed']=self.stream.closed
    def opened(self,*args,**kwargs):
        value=original(self,*args,**kwargs)
        return ObservedRead(value) if self==path else value
    def work():
        try:
            stream=recovery.download({'root':str(root)},expected)
            stream.close()
        except BaseException as error:
            stats['worker_error']=type(error).__name__;raise
    try:
        Path.open=opened
        try:
            try:
                async with request_deadline.scope(.15):await finish_before_cancel(asyncio.to_thread(work))
            except TimeoutError:pass
            else:raise AssertionError('Inherited archive deadline did not expire')
        finally:Path.open=original
        assert stats=={'read_calls':1,'closed':True,'worker_error':'TimeoutError'},stats
        report['checks'].append('actual_inflight_read_drains_but_no_second_read_starts_after_expiry')
        with recovery.download({'root':str(root)},expected) as stream:assert stream.read()==raw
        assert path.read_bytes()==raw
        report['checks'].append('ordinary_no_parent_deadline_download_preserves_actual_bytes')
        report['passed']=True
    finally:
        report['read_observations']=stats
        (root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
        print('ARCHIVE_DEADLINE_EVIDENCE '+str(root),flush=True)
    return root
