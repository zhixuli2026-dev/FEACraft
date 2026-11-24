"""Verify the installed OpenFOAM development environment through owned native probes."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from urllib.request import Request
import uuid

from testsupport import IsolatedServer, SERVER


DISTRO='vibeflow-linux'
INSTALLATION='/usr/lib/openfoam/openfoam2406'
BASHRC=INSTALLATION+'/etc/bashrc'
CONTROL_DICT=INSTALLATION+'/etc/controlDict'
ACCOUNT_PASSWORD='isolated-openfoam-dev-123'


def package_evidence():
    # The installed Debian 1912 binary remains on PATH without sourcing the
    # configured development environment. This is a real competing toolchain.
    command='''printf 'uid=%s\\n' "$(id -u)"
printf 'unsourced_blockmesh=%s\\n' "$(command -v blockMesh)"
dpkg-query -W -f='${Package}=${Version}\\n' openfoam openfoam2406-dev || exit
source /usr/lib/openfoam/openfoam2406/etc/bashrc >/dev/null 2>&1 || exit
printf 'sourced_version=%s\\n' "$WM_PROJECT_VERSION"
printf 'compiler=%s\\n' "$(wmake -show-cxx)"
printf 'foam_etc_api=%s\\n' "$(foamEtcFile -show-api)"
printf 'control_dict=%s\\n' "$(foamEtcFile -mode=o controlDict)"
printf 'icofoam=%s\\n' "$(command -v icoFoam)"
'''
    result=subprocess.run(['wsl.exe','--distribution',DISTRO,'--user','nobody','--exec',
        'bash','--noprofile','--norc','-c',command],text=True,encoding='utf-8',
        capture_output=True,timeout=25)
    assert result.returncode==0,(result.returncode,result.stdout,result.stderr)
    values=dict(line.split('=',1) for line in result.stdout.splitlines())
    assert values['uid']=='65534' and values['unsourced_blockmesh']=='/usr/bin/blockMesh',values
    assert values['openfoam'].startswith('1912.') and values['openfoam2406-dev'].startswith('2406.'),values
    assert values['sourced_version']=='v2406' and values['foam_etc_api']=='2406',values
    assert values['control_dict']==CONTROL_DICT and values['compiler'],values
    assert values['icofoam'].startswith(INSTALLATION+'/') and values['icofoam'].endswith('/icoFoam'),values
    return values


def capabilities(api):
    request=Request(api.base+'/api/v1/runtime/capabilities',headers={'Authorization':'Bearer '+api.token})
    # The product request already applies its own 65-second deadline. The
    # outer client allows the API to return its result or timeout receipt.
    with api.opener.open(request,timeout=90) as response:
        assert response.status==200
        return json.load(response)


def durable_probe(api,probe,bashrc):
    identifier=probe['request_id']
    assert str(uuid.UUID(identifier))==identifier
    with sqlite3.connect((api.root/'test.sqlite').as_uri()+'?mode=ro',uri=True) as db:
        kind,case,phase,profile,receipt,ended_at=db.execute(
            'select kind,case_id,phase,profile_json,children_json,ended_at from native_requests where id=?',
            (identifier,)).fetchone()
    assert kind=='runtime.wsl_probe' and case is None and phase=='succeeded' and ended_at
    assert json.loads(profile)=={'bashrc':bashrc,'distro':DISTRO}
    children=json.loads(receipt)['children']
    assert len(children)==1
    child=next(iter(children.values()))
    identity=child['identity']['native']
    assert identity['distribution']==DISTRO and identity['process']['uid']==65534,identity
    stop=child['stop']
    assert stop['verified'] and stop['scope_empty'] and stop['native_state']=='exited',stop
    return {'id':identifier,'identity':identity,'stop':stop}


def installed(value):
    wsl=value['wsl']
    assert wsl['reachable'] and wsl['distro']==DISTRO and wsl['openfoam_bashrc_loaded'],wsl
    assert wsl['openfoam_version']=='v2406' and wsl['openfoam'] and wsl['wmake'] and wsl['foam_etc_file'],wsl
    assert wsl['openfoam_path'].startswith(INSTALLATION+'/') and wsl['openfoam_path'].endswith('/blockMesh'),wsl
    assert wsl['wmake_path']==INSTALLATION+'/wmake/wmake',wsl
    assert wsl['foam_etc_file_path']==INSTALLATION+'/bin/foamEtcFile' and wsl['foam_etc_control_dict']==CONTROL_DICT,wsl
    assert value['stage_checks']['B']['ok'] and value['stage_checks']['C']['ok']


def write_result(path,report):
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')


def verify(api,report,result):
    api.call('GET','/runtime/capabilities',expect=401)
    admin=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
        'password':ACCOUNT_PASSWORD,'name':'OpenFOAM fixture admin'})
    api.token=admin['token']
    initial=api.call('GET','/runtime/config')
    assert initial['wsl_distro']==DISTRO and initial['openfoam_bashrc']==BASHRC

    member_email=uuid.uuid4().hex+'@example.com'
    member=api.call('POST','/auth/register',{'email':member_email,
        'password':ACCOUNT_PASSWORD,'name':'OpenFOAM fixture member'})
    with sqlite3.connect(api.root/'test.sqlite') as db:
        changed=db.execute("update memberships set role='member' where user_id=(select id from users where email=?)",
            (member_email,)).rowcount
        assert changed==1
        assert db.execute('select count(*) from native_requests').fetchone()==(0,)

    missing='/usr/lib/openfoam/vf-missing-'+uuid.uuid4().hex+'/etc/bashrc'
    api.call('PATCH','/runtime/config',{'openfoam_bashrc':missing},expect=403,
        extra_headers={'Authorization':'Bearer '+member['token']})
    api.call('PATCH','/runtime/config',{'openfoam_bashrc':'/tmp/not-openfoam/etc/bashrc'},expect=400)
    assert api.call('GET','/runtime/config')==initial
    report['checks'].append('anonymous_probe_and_non_admin_or_invalid_setup_rejected')
    write_result(result,report)

    first=capabilities(api)
    installed(first)
    report['initial']={'wsl':first['wsl'],'receipt':durable_probe(api,first['wsl'],BASHRC)}
    report['checks'].append('configured_v2406_tools_and_live_etc_lookup_use_nobody')
    write_result(result,report)
    print('PASS v2406_nonroot_development_environment',flush=True)

    assert api.call('PATCH','/runtime/config',{'openfoam_bashrc':missing})['openfoam_bashrc']==missing
    unavailable=capabilities(api)
    wsl=unavailable['wsl']
    assert wsl['reachable'] and wsl['distro']==DISTRO and not wsl['openfoam_bashrc_loaded'],wsl
    assert not any(wsl[key] for key in ('openfoam','wmake','foam_etc_file')),wsl
    assert all(wsl[key] is None for key in ('openfoam_path','openfoam_version','wmake_path',
        'foam_etc_file_path','foam_etc_control_dict')),wsl
    assert not unavailable['stage_checks']['B']['ok'] and not unavailable['stage_checks']['C']['ok']
    report['missing']={'wsl':wsl,'receipt':durable_probe(api,wsl,missing)}
    report['checks'].append('missing_selected_bashrc_never_uses_installed_1912')
    write_result(result,report)
    print('PASS missing_config_never_falls_back_to_1912',flush=True)

    assert api.call('PATCH','/runtime/config',{'openfoam_bashrc':BASHRC})==initial
    restored=capabilities(api)
    installed(restored)
    assert {key:value for key,value in restored['wsl'].items() if key!='request_id'}=={
        key:value for key,value in first['wsl'].items() if key!='request_id'}
    report['restored']={'wsl':restored['wsl'],'receipt':durable_probe(api,restored['wsl'],BASHRC)}
    with sqlite3.connect((api.root/'test.sqlite').as_uri()+'?mode=ro',uri=True) as db:
        assert db.execute('select count(*) from jobs').fetchone()==(0,)
        assert db.execute('select count(*) from cases').fetchone()==(0,)
    report['checks'].append('restoring_config_recovers_v2406_capabilities_without_case_or_job')


def main():
    server=IsolatedServer('openfoam-v2406-c04',app='openfoam_v2406_fixture:app')
    (server.root/'openfoam_v2406_fixture.py').write_text(
        'import os\nfor key in ("VIBEFLOW_WSL_DISTRO", "VIBEFLOW_OPENFOAM_BASHRC"):\n'
        '    os.environ.pop(key,None)\nfrom app.main import app\n',encoding='utf-8')
    result=server.root/'openfoam-v2406-contract.json'
    report={'passed':False,'real_model_calls':0,'mock_llm_calls':0,'checks':[]}
    try:
        report['installed_packages']=package_evidence()
        with server as api:
            verify(api,report,result)
        report['passed']=report['isolated_api_exited']=server.proc.poll() is not None
        assert report['passed']
        print('OPENFOAM_V2406_CONTRACT_PASS',result,flush=True)
    except BaseException as error:
        report['failure']={'type':type(error).__name__,'message':str(error)}
        raise
    finally:
        write_result(result,report)


if __name__=='__main__':
    sys.path.insert(0,str(SERVER.parent/'tools'))
    from regression_llm_policy import enforce
    enforce([Path(__file__)])
    main()
