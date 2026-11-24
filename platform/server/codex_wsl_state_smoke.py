"""Actual Linux permission and symlink checks for the private CLI state root."""
import json
from pathlib import Path
import subprocess
import tempfile

from testsupport import SERVER
from codex_wsl_test_support import NativeStateFixture
from app.services import codex_wsl_state as state
from app.services.owned_process import wsl_path

PROBE = r'''
import importlib.util,json,sys,os
from pathlib import Path
spec=importlib.util.spec_from_file_location('native_state',sys.argv[1]);state=importlib.util.module_from_spec(spec);spec.loader.exec_module(state)
root=Path(sys.argv[2]);checks=[]
request={'root':str(root),'relative':'users/1/wsl/vibeflow-linux','configuration':'cli_auth_credentials_store = "file"\n','owner':{'user_id':1}}
def reject(label,changes=None):
    try:state.initialize({**request,**(changes or {})})
    except (ValueError,OSError):checks.append(label)
    else:raise AssertionError(label)
home=Path(state.initialize(request)['home'])
assert state.initialize(request)['home']==str(home) and home.stat().st_uid==65534 and home.stat().st_mode&0o777==0o700
checks.append('idempotent_same_account_with_real_0700_nonroot_ownership')
reject('parent_traversal',{'relative':'../outside'})
reject('windows_mount_root',{'root':'/mnt/d/fixture'})
reject('broad_tmp_root',{'root':'/tmp'})
reject('changed_configuration',{'configuration':'changed'})
reject('different_owner',{'owner':{'user_id':2}})
os.chmod(home,0o777)
try:reject('insecure_home_permissions')
finally:os.chmod(home,0o700)
marker=home.parent/'owner.json';backup=marker.read_bytes();marker.unlink()
try:reject('existing_home_without_owner_marker')
finally:marker.write_bytes(backup);os.chmod(marker,0o600)
file=home/'config.toml';saved=file.read_bytes();file.unlink();target=root/'unrelated';target.write_bytes(saved);file.symlink_to(target)
try:reject('configuration_symlink')
finally:file.unlink();file.write_bytes(saved);os.chmod(file,0o600)
(root/'alias').symlink_to(root/'users',target_is_directory=True)
reject('symlink_ancestor',{'relative':'alias/1/wsl/vibeflow-linux'})
os.chmod(root,0o777)
try:reject('insecure_state_root')
finally:os.chmod(root,0o700)
other=Path(state.initialize({**request,'relative':'users/2/wsl/vibeflow-linux','owner':{'user_id':2}})['home'])
assert other!=home and json.loads((home.parent/'owner.json').read_text())=={'user_id':1}
checks.append('separate_accounts_do_not_adopt_each_others_state')
print(json.dumps({'passed':True,'count':len(checks),'checks':checks}))
'''


def main():
    root=Path(tempfile.mkdtemp(prefix='cli-wsl-state-',dir=SERVER.parents[1]/'tmp'))
    script=root/'probe.py';script.write_text(PROBE,encoding='utf8')
    with NativeStateFixture() as fixture:
        result=subprocess.run(['wsl.exe','-d','vibeflow-linux','--user','nobody','--exec','/usr/bin/python3','-I','-S',
            wsl_path(script),wsl_path(Path(state.__file__).resolve()),fixture.root],capture_output=True,timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode==0,result.stderr.decode('utf8',errors='replace')
        value=json.loads(result.stdout)
    try:state.mounted_path('\\\\wsl.localhost\\foreign\\tmp\\x','vibeflow-linux')
    except ValueError:value['checks'].append('cross_distribution_unc_path_rejected')
    else:raise AssertionError('Cross-distribution state mapping accepted')
    value.update(count=len(value['checks']),actual_wsl=True,evidence=str(root))
    (root/'result.json').write_text(json.dumps(value,indent=2),encoding='utf8')
    print(json.dumps(value))


if __name__=='__main__':main()
