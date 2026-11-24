"""CLI file discovery and explicit WSL callbacks; disposable capability API."""
import json
from unittest.mock import patch

from app.services.cli_discovery import discover_cli,version_sort_key
from testsupport import IsolatedServer


def main():
    api=IsolatedServer('cli-discovery')
    root=api.root/'selection';root.mkdir()
    checks=[]
    versions=root/'versions'
    for name,suffix in (('0.150.0','cmd'),('0.151.0-rc.2','cmd'),('0.151.0-beta.1','cmd'),
                        ('0.151.0','cmd'),('0.149.0','bat')):
        folder=versions/name/'bin';folder.mkdir(parents=True)
        (folder/('codex.'+suffix)).write_text('rem fixture',encoding='utf8')
    selected=discover_cli('codex',env={'VIBEFLOW_CODEX_DIR':str(versions)},path_dirs=[])
    assert selected['source']=='version_dir' and selected['version']=='0.151.0' and selected['path'].endswith('codex.cmd')
    assert version_sort_key('0.151.0')>version_sort_key('0.151.0-rc.2')>version_sort_key('0.151.0-beta.1')>version_sort_key('0.151.0-alpha.1')
    checks.append('version_directory_and_stable_prerelease_order_preserved')
    explicit=root/'custom/codex.bat';explicit.parent.mkdir();explicit.write_text('rem fixture')
    selected=discover_cli('codex',env={'VIBEFLOW_CODEX':str(explicit),'VIBEFLOW_CODEX_DIR':str(versions)},path_dirs=[])
    assert selected['source']=='explicit' and selected['path']==str(explicit)
    checks.append('explicit_file_precedes_version_directory')
    directory=root/'path';directory.mkdir();(directory/'codex.cmd').write_text('rem fixture')
    selected=discover_cli('codex',env={},path_dirs=[str(directory)])
    assert selected['source']=='path' and selected['path'].endswith('codex.cmd')
    try:discover_cli('codex',env={'VIBEFLOW_CODEX':str(root/'missing.cmd')},path_dirs=[str(directory)])
    except FileNotFoundError:pass
    else:raise AssertionError('An explicit missing program fell back to PATH')
    checks.append('path_suffix_and_missing_explicit_file_contract_preserved')
    calls=[]
    def runner(argv):
        calls.append(argv)
        return {'returncode':0,'stdout':'v22.14.0\n' if 'node' in argv[-1] else '/fixture/bin/codex\n'}
    with patch('subprocess.run',side_effect=AssertionError('Implicit CLI process execution')):
        selected=discover_cli('codex',env={},path_dirs=[],wsl_runner=runner,distro='vibeflow-linux')
        assert selected['runtime']=='wsl' and selected['node_version']=='v22.14.0'
        assert len(calls)==2 and all('vibeflow-linux' in call for call in calls)
        checks.append('explicit_wsl_callback_preserves_cli_and_node_discovery')
        assert discover_cli('codex',env={},path_dirs=[]) is None
        for invalid in (False,0,'',True):
            try:discover_cli('codex',env={},path_dirs=[],wsl_runner=invalid)
            except ValueError:pass
            else:raise AssertionError('A non-callable WSL runner was accepted')
        checks.append('absent_or_invalid_callback_never_selects_an_implicit_runner')
        class FalseCallback:
            def __bool__(self):return False
            def __call__(self,argv):return runner(argv)
        calls.clear()
        assert discover_cli('codex',env={},path_dirs=[],wsl_runner=FalseCallback())['runtime']=='wsl'
        assert len(calls)==2
        checks.append('false_boolean_callable_still_uses_the_explicit_callback')
    with api:
        api.create_case('Discovery capability fixture')
        capabilities=api.call('GET','/runtime/capabilities')
        assert 'codex' in capabilities['cli_agent_details']
        checks.append('disposable_authenticated_capability_api_returns_discovery_details')
    result={'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root),
            'developer_login_not_used':True,'actual_cli_inference':False}
    (api.root/'discovery-result.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result))


if __name__=='__main__':main()
