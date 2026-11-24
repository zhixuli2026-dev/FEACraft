"""Real non-root WSL stdio, native ancestry and bounded exit; no model calls."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import subprocess

from testsupport import SERVER
from app.services.codex_stdio import CodexStdioClient
from app.services.codex_environment import create_isolated_codex_environment
from app.services.owned_process import probe_native, wsl_path
from app.services.owned_stdio import IDENTITY, NATIVE_PROBE, packed

FIXTURE = r'''
import json,os,sys,time,subprocess,importlib.util
from pathlib import Path
root=Path(sys.argv[1]);(root/'started').write_text('yes')
spec=importlib.util.spec_from_file_location('birth',sys.argv[2]);birth=importlib.util.module_from_spec(spec);spec.loader.exec_module(birth)
for line in sys.stdin:
    value=json.loads(line);method=value.get('method');rid=value.get('id')
    if method=='initialize':result={'userAgent':'native-wsl-fixture'}
    elif method=='initialized':continue
    elif method=='environment':
        result={'uid':os.getuid(),'pid':os.getpid(),'cwd':os.getcwd(),
            'home':os.environ.get('HOME'),'codex_home':os.environ.get('CODEX_HOME'),
            'tmpdir':os.environ.get('TMPDIR'),'path':os.environ.get('PATH'),
            'credential_present':os.environ.get('VIBEFLOW_CODEX_PROVIDER_TOKEN')=='synthetic-wsl-only-token',
            'inherited_secrets':any(k in os.environ for k in ('OPENAI_API_KEY','ANTHROPIC_API_KEY','VIBEFLOW_DATABASE_URL','WSL_INTEROP','WSLENV','HTTP_PROXY'))}
    elif method=='echo':result=value['params']
    elif method=='spawn':
        child=subprocess.Popen([sys.executable,'-I','-S','-c','import time;time.sleep(120)'],start_new_session=True)
        result={'child':birth.observe(child.pid)}
    elif method=='bad':print('not json',flush=True);continue
    else:result={}
    print(json.dumps({'id':rid,'result':result}),flush=True)
'''


async def main():
    root=Path(tempfile.mkdtemp(prefix='cli-wsl-transport-',dir=SERVER.parents[1]/'tmp'))
    checks=[];receipts=[]
    script=root/'fixture.py';script.write_text(FIXTURE,encoding='utf8')
    async def launch(name,*,root_callback=None,bridge_callback=None):
        folder=root/name;folder.mkdir()
        env=create_isolated_codex_environment(folder/'home')
        env.update(HOME=str(folder),TMPDIR=str(folder),VIBEFLOW_CODEX_PROVIDER_TOKEN='synthetic-wsl-only-token',
            OPENAI_API_KEY='must-not-inherit',HTTP_PROXY='http://must-not-inherit.invalid',
            WSLENV='OPENAI_API_KEY/u:HTTP_PROXY/u',VIBEFLOW_DATABASE_URL='must-not-inherit')
        client=CodexStdioClient(['/usr/bin/python3','-I','-S','-u',wsl_path(script),wsl_path(folder),wsl_path(IDENTITY)],env,
            cwd=folder,distro='vibeflow-linux',before_release=root_callback,before_start=bridge_callback)
        try:
            await client.start()
            return client,folder
        except BaseException:
            await client.close()
            assert client.stop_result['verified']
            receipts.append(client.stop_result)
            raise
    order=[]
    async def registered_root(birth):
        assert birth['runtime']=='wsl' and birth['native']['distribution']=='vibeflow-linux'
        assert birth['native']['process']['uid']!=0
        assert not (root/'normal/started').exists()
        order.append('root')
    async def registered_bridge(birth):
        assert birth['platform']=='linux' and birth['uid']!=0 and order==['root']
        assert not (root/'normal/started').exists()
        order.append('bridge')
    client,folder=await launch('normal',root_callback=registered_root,bridge_callback=registered_bridge)
    try:
        assert order==['root','bridge'] and (folder/'started').exists()
        state=await client.request('environment')
        (root/'environment.json').write_text(json.dumps(state,indent=2),encoding='utf8')
        assert state['uid']!=0 and state['pid']<100 and state['cwd']==wsl_path(folder)
        assert state['home']==state['tmpdir']==wsl_path(folder) and state['codex_home']==wsl_path(folder/'home')
        assert state['credential_present'] and not state['inherited_secrets'] and state['path']=='/usr/bin:/bin'
        assert 'synthetic-wsl-only-token' not in json.dumps(client.transport.profile)
        checks.append('two_registration_callbacks_before_nonroot_native_payload')
        checks.append('explicit_path_exports_credential_environment_only_and_distro_environment_removed')
        content={'text':'中文逐字节往返'*10000}
        assert await client.request('echo',content)==content
        checks.append('bounded_native_duplex_stream_preserves_unicode')
        reported = (await client.request('spawn'))['child']
        def locate_leaf():
            value=subprocess.run(['wsl.exe','-d','vibeflow-linux','--exec','/usr/bin/python3','-I','-S',wsl_path(NATIVE_PROBE)],
                input=packed({'root':client.transport.owned.native['process'],'reported':reported}),
                capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
            assert value.returncode==0
            return json.loads(value.stdout)
        leaf=await asyncio.to_thread(locate_leaf)
        assert leaf['session'] != client.transport.bridge_birth['session']
    finally:
        native=client.transport.owned.native
        await client.close()
        assert client.stop_result['verified'] and client.stop_result['native_state']=='exited'
        assert (await probe_native(native['distribution'],native['process']))['state']=='exited'
        receipts.append(client.stop_result)
    assert (await probe_native(native['distribution'],leaf))['state']=='exited'
    (root/'detached-leaf.json').write_text(json.dumps(leaf,indent=2),encoding='utf8')
    checks.append('closing_native_scope_retires_detached_descendants_and_verifies_exit')
    for boundary in ['root','bridge']:
        async def reject(_):raise ValueError('injected durable registration failure')
        try:
            await launch('reject-'+boundary,root_callback=reject if boundary=='root' else None,
                         bridge_callback=reject if boundary=='bridge' else None)
            raise AssertionError('registration failure ignored')
        except ValueError:pass
        assert not (root/('reject-'+boundary)/'started').exists()
        checks.append(boundary+'_registration_failure_prevents_payload')
    client,folder=await launch('malformed')
    try:
        try:
            await client.request('bad')
            raise AssertionError('malformed native JSON accepted')
        except ValueError:pass
    finally:
        await client.close();assert client.stop_result['verified'];receipts.append(client.stop_result)
    checks.append('malformed_native_rpc_still_closes_owned_scope')
    result={'passed':True,'count':len(checks),'checks':checks,'actual_wsl':True,'actual_codex':False,'evidence':str(root)}
    (root/'result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    (root/'stop-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
    print(json.dumps(result))

if __name__=='__main__':asyncio.run(main())
