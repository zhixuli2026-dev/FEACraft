"""Missing native ownership must fail before creating a process or work copy."""
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

from app.services import native_execution_client as client,pandoc_execution,latex_artifacts,report_worker
from testsupport import SERVER


def main():
    root=Path(tempfile.mkdtemp(prefix='native-channel-',dir=SERVER.parents[1]/'tmp'))
    case=root/'case';case.mkdir();(case/'main.tex').write_text('Fixture source',encoding='utf8')
    original={p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}
    checks=[]
    def denied(label,action,expected=client.ChildExecutionError):
        try:action()
        except expected:pass
        else:raise AssertionError('Missing native owner was accepted: '+label)
        assert {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*') if p.is_file()}==original
        assert sorted(p.name for p in root.iterdir())==['case']
        checks.append(label);print('PASS '+label,flush=True)
    with ExitStack() as stack:
        stack.enter_context(patch('subprocess.run',side_effect=AssertionError('Unowned subprocess.run')))
        stack.enter_context(patch('subprocess.Popen',side_effect=AssertionError('Unowned subprocess.Popen')))
        connection=stack.enter_context(patch('socket.create_connection',side_effect=AssertionError('Unexpected channel connection')))
        stack.enter_context(patch.dict('os.environ',{client.ADDRESS:'127.0.0.1:9',client.CAPABILITY:'1'*64}))
        invoke=lambda:client.run_native('latex_probe',{'distro':'vibeflow-linux','recipe':'pdflatex'},timeout=1)
        denied('environment_without_explicit_scope_is_not_authority',invoke)
        with patch.dict('os.environ',{client.ADDRESS:'',client.CAPABILITY:''}),client.require_supervision():
            denied('registered_worker_without_channel_fails_closed',invoke)
        with client.context('remote.invalid:123','1'*64):
            denied('non_loopback_channel_rejected',invoke)
        with client.context('127.0.0.1:0','1'*64):
            denied('invalid_channel_port_rejected',invoke)
        with client.context('127.0.0.1:123','wrong'):
            denied('invalid_channel_capability_rejected',invoke)
        denied('context_reset_does_not_restore_implicit_execution',invoke)
        for purpose,kwargs in (('pandoc_version',{}),('pandoc_parse',{'markdown':b'# Owned document'}),
                ('pandoc_compile',{'ast':case/'document.json','output':case/'report.html','extension':'html'})):
            denied(purpose+'_requires_channel_before_work_copy',
                lambda p=purpose,k=kwargs:pandoc_execution.run(p,sys.executable,case,**k))
        denied('latex_probe_cannot_spawn_without_owner',lambda:latex_artifacts.probe('vibeflow-linux','pdflatex'),ValueError)
        def compile_latex():
            with latex_artifacts.compile_document(case,'main.tex','pdflatex','vibeflow-linux',0,4096,{},{}):
                raise AssertionError('Compilation yielded without an owned channel')
        denied('latex_compile_requires_channel_before_work_copy',compile_latex)
        with patch.object(sys,'argv',['report_worker.py','--work-dir',str(case),'--pandoc',sys.executable]):
            try:report_worker.main()
            except SystemExit as error:assert 'registered report build' in str(error)
            else:raise AssertionError('Retired report worker executed')
        checks.append('retired_report_worker_has_no_process_or_output_path')
        assert connection.call_count==0
        connection.side_effect=ConnectionRefusedError('Fixture refused owned channel')
        with client.context('127.0.0.1:9','1'*64):
            denied('unavailable_explicit_channel_never_falls_back',invoke,ConnectionRefusedError)
        with client.require_supervision():
            denied('unavailable_registered_worker_channel_never_falls_back',invoke,ConnectionRefusedError)
        assert connection.call_count==2
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(root),
            'process_creation_forbidden':True,'input_and_directory_inventory_unchanged':True}
    (root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result))


if __name__=='__main__':main()
