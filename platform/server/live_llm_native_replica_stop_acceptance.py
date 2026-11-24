"""Ask a peer API to stop a real Windows Codex turn held at the official response boundary."""
import argparse
import asyncio
import json
from pathlib import Path
import secrets
import shutil
import time

from live_llm_acceptance import source
from live_llm_chat_markdown_acceptance import wait_for_api_exit
from live_llm_native_reply_sound_acceptance import FIXTURE, ResponsesBudget, windows_relay
from testsupport import IsolatedServer


def fixture(api, values, dist):
    path=api.root/'peer_stop_fixture.py'
    prefix=f'import os\nos.environ.update({values!r})\ndist={str(dist)!r}\nread_only_mode=False\nno_turn_mode=False\naudio_worklet=""\n'
    path.write_text(prefix+FIXTURE,encoding='utf8')
    return path


def verify(config, dist):
    primary=IsolatedServer('live-f34-native-owner',app='peer_stop_fixture:app')
    peer=IsolatedServer('live-f34-native-peer',app='peer_stop_fixture:app')
    token=secrets.token_hex(32)
    budget=ResponsesBudget(config,primary.root,per_runtime={'windows':1})
    program=Path(shutil.which('codex.exe')).resolve(strict=True)
    paths=[fixture(primary,{'VIBEFLOW_CODEX':str(program)},dist)]
    report={'passed':False,'mock_llm_calls':0,'model':config['model']}
    print('LIVE_F34_NATIVE_EVIDENCE',primary.root,flush=True)
    try:
        with windows_relay(budget,token) as port:
            try:
                with primary:
                    values={k:primary.env[k] for k in ('VIBEFLOW_DATABASE_URL','VIBEFLOW_STORAGE_FS_ROOT','VIBEFLOW_JWT_SECRET')}
                    paths.append(fixture(peer,values,dist))
                    with peer:
                        case=primary.create_case('Native cancellation through peer API')
                        peer.token=primary.token
                        profile=primary.call('POST','/provider-profiles',{'provider':'custom','name':'Official response relay',
                           'api_key':token,'base_url':f'http://127.0.0.1:{port}','default_model':config['model']})['id']
                        cid=primary.call('POST','/conversations',{'case_id':case['id'],'title':'Windows native remote stop',
                           'provider_profile_id':profile,'model':config['model'],'permission_mode':'ask'})['id']
                        session=primary.call('POST','/codex/sessions',{'case_id':case['id'],'conversation_id':cid,
                           'provider_profile_id':profile,'provider_wire_api':'responses','runtime':'windows'})['id']
                        turn=primary.call('POST',f'/codex/sessions/{session}/turns',{'client_message_id':secrets.token_hex(16),
                           'content':'Reply with the short plain text NATIVE_CROSS_API_STOP and use no tools.', 'timeout_seconds':100})['id']
                        deadline=time.monotonic()+75
                        while not budget.receipts and time.monotonic()<deadline:
                            time.sleep(.1)
                        assert len(budget.receipts)==1 and budget.receipts[0]['status']==200,budget.receipts
                        path=f'/codex/turns/{turn}'
                        outsider=primary.call('POST','/auth/register',{'email':'f34-native-other@example.com',
                            'password':'no-live-outside-123','name':'Outsider'})
                        peer.call('POST',path+':cancel',expect=404,extra_headers={'Authorization':'Bearer '+outsider['token']})
                        requested=peer.call('POST',path+':cancel')
                        assert requested['cancelling'] is True
                        now=time.monotonic()
                        while time.monotonic()-now<36:
                            state=primary.call('GET',path)
                            if state['phase']=='cancelled':
                                break
                            time.sleep(.2)
                        assert state['phase']=='cancelled' and state['cleanup_verified'] is True,state
                        stopped=primary.call('GET',f'/codex/sessions/{session}')
                        assert stopped['stop']['verified'] and stopped['stop']['scope_empty'],stopped
                        assert peer.call('GET',f'/codex/conversations/{cid}/activity')['phase']=='idle'
                        report.update(peer_cancel=True,foreign_status=404,phase=state['phase'],scope_empty=True,
                                      elapsed_ms=round((time.monotonic()-now)*1000))
                report['api_exits']=[wait_for_api_exit(api.root,api.port) for api in (primary,peer)]
                report['passed']=True
            finally:
                (primary.root/'release-windows').touch()
    finally:
        for path in paths:
            path.unlink(missing_ok=True)
        report['official_receipts']=budget.receipts
        evidence=json.dumps(report,indent=2)
        assert config['key'] not in evidence and token not in evidence
        (primary.root/'native-peer-stop-result.json').write_text(evidence,encoding='utf8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-profile-id',type=int,required=True)
    parser.add_argument('--dist',type=Path,required=True)
    args=parser.parse_args()
    verify(asyncio.run(source(args.source_profile_id)),args.dist.resolve(strict=True))
