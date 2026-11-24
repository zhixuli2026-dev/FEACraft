"""Probe the installed Linux CLI filesystem sandbox with harmless local writes."""
import asyncio
import json
from pathlib import Path
import tempfile

from testsupport import SERVER
from codex_wsl_test_support import NativeStateFixture
from app.services.codex_sessions import native_command
from app.services.codex_wsl_state import selection, windows_home
from app.services.codex_environment import wsl_codex_environment
from app.services.owned_stdio import OwnedStdio
from app.services.owned_process import wsl_path


async def main():
    root=Path(tempfile.mkdtemp(prefix='cli-wsl-sandbox-',dir=SERVER.parents[1]/'tmp'))
    workspace=root/'workspace';workspace.mkdir()
    script=root/'probe.py'
    script.write_text('''import json,errno
from pathlib import Path
root=Path(__file__).parent
(root/'workspace/allowed.txt').write_text('owned fixture')
try:
    (root/'outside.txt').write_text('unexpected')
    denied=False
except OSError as error:
    denied=error.errno in (errno.EACCES,errno.EPERM,errno.EROFS)
print(json.dumps({'inside_write':True,'outside_write_denied':denied}),flush=True)
''',encoding='utf8')
    with NativeStateFixture() as state:
        prepared=selection(state.root,'probe','cli_auth_credentials_store = "file"\n',{'fixture':'sandbox'})
        home=windows_home(prepared,'vibeflow-linux')
        env=wsl_codex_environment({'CODEX_HOME':str(home),'HOME':str(home.parent),'TMPDIR':str(home.parent/'tmp')},
                                  'vibeflow-linux',native_state=prepared)
        # Windows loader still needs its usual OS environment; only the three
        # explicit Linux paths survive the fixed bridge's environment reset.
        import os
        from app.services.codex_environment import OS_ENVIRONMENT
        env.update({k:v for k,v in os.environ.items() if k.upper() in OS_ENVIRONMENT})
        program,command=native_command(distro='vibeflow-linux')
        argv=command[:-3]+['sandbox','--include-managed-config','--permission-profile',':workspace','--',
            '/usr/bin/python3','-I','-S',wsl_path(script)]
        transport=OwnedStdio(argv,cwd=workspace,env=env,distro='vibeflow-linux',native_state=prepared)
        result=None
        try:
            await transport.start()
            result=json.loads(await asyncio.wait_for(transport.readline(),30))
            assert result=={'inside_write':True,'outside_write_denied':True},result
            assert (workspace/'allowed.txt').is_file() and not (root/'outside.txt').exists()
        finally:
            await transport.close()
            evidence={'passed':result=={'inside_write':True,'outside_write_denied':True},'probe':result,
                'stderr':transport.stderr_tail.decode('utf8',errors='replace'),'stop':transport.stop_result,
                'actual_cli':True,'network_isolation_tested':False,'evidence':str(root)}
            (root/'result.json').write_text(json.dumps(evidence,indent=2),encoding='utf8')
            print(json.dumps(evidence))
            assert transport.stop_result['verified']


if __name__=='__main__':asyncio.run(main())
