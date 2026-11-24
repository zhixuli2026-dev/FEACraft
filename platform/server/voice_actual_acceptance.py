"""Real browser capture and local Whisper inference with generated test audio."""
from __future__ import annotations

import argparse
import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess
from uuid import uuid4
import wave

import httpx

from testsupport import IsolatedServer


def canonical(path):
    with wave.open(str(path),'rb') as source:
        assert source.getparams()[:3]==(1,2,16000)
        frames=source.readframes(source.getnframes())
    output=BytesIO()
    with wave.open(output,'wb') as recording:
        recording.setparams((1,2,16000,0,'NONE','not compressed'))
        recording.writeframes(frames)
    return output.getvalue()


def receipts(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf8').splitlines() if line]


async def cancelled_transcription(url, token, sample, root):
    # A long actual input keeps the real CPU recognition process active long
    # enough to close its HTTP client after admission.
    waveform=sample[44:]
    pcm=(waveform*(1+28*32000//len(waveform)))[:28*32000]
    output=BytesIO()
    with wave.open(output,'wb') as recording:
        recording.setparams((1,2,16000,0,'NONE','not compressed'))
        recording.writeframes(pcm)
    path=root/'voice-process.jsonl'
    count=len(receipts(path))
    async with httpx.AsyncClient(trust_env=False,timeout=32,headers={'Authorization':'Bearer '+token}) as client:
        request=asyncio.create_task(client.put(url,params={'language':'zh'},content=output.getvalue()))
        try:
            async with asyncio.timeout(8):
                while not (launched:=next((item for item in receipts(path)[count:] if item['phase']=='started'),None)):
                    await asyncio.sleep(.03)
            assert not request.done()
            pid=launched['pid']
        finally:
            request.cancel()
            await asyncio.gather(request,return_exceptions=True)
    async with asyncio.timeout(10):
        while not (stopped:=next((item for item in receipts(path)[count:]
                if item['phase']=='exited' and item['pid']==pid),None)):
            await asyncio.sleep(.05)
    assert stopped['code']!=0,stopped
    return stopped


EDGE = """
import json,re,sys
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
cfg=json.load(sys.stdin); out=Path(cfg['out'])
def workstation(context):
    page=context.new_page()
    page.add_init_script('localStorage.setItem("vf_token",'+json.dumps(cfg['token'])+');')
    page.goto(cfg['address'],wait_until='domcontentloaded')
    page.locator('[data-case-name="Offline dictation acceptance"]').click()
    tab=page.locator('.dv-tab:visible').filter(has_text=re.compile('^Agent$'))
    if tab.count():
        tab.click()
    button=page.get_by_test_id('agent-voice')
    expect(button).to_have_attribute('title','录音将发送到平台服务进行转写')
    expect(button).to_be_enabled()
    return page,button
with sync_playwright() as p:
    browser=p.chromium.launch(channel='msedge',headless=True,args=[
        '--use-fake-device-for-media-stream','--use-file-for-fake-audio-capture='+cfg['audio']+'%noloop'])
    denied=browser.new_context(locale='zh-CN',viewport={'width':1440,'height':900})
    page,button=workstation(denied)
    denied_calls=[]
    page.on('request',lambda req:denied_calls.append(req.method) if '/voice/transcription' in req.url else None)
    button.click()
    expect(page.get_by_test_id('agent-voice-error')).to_contain_text('允许麦克风访问')
    assert not denied_calls
    denied.grant_permissions(['microphone'],origin=cfg['address'])
    requests=[]
    page.on('request',lambda req:requests.append(req.method) if '/voice/transcription' in req.url else None)
    draft=page.get_by_test_id('agent-input')
    draft.fill('实验记录')
    button.click()
    expect(button).to_have_text('停止录音')
    assert not requests
    page.wait_for_timeout(3800)
    with page.expect_response(lambda r:'/voice/transcription' in r.url,timeout=35000) as completed:
        button.click()
    assert completed.value.status==200,completed.value.status
    result=completed.value.json()['text']
    assert '管道' in result and ('压力' in result or '壓力' in result),result
    expect(draft).to_have_value('实验记录 '+result)
    expect(button).to_have_text('语音输入')
    assert len(requests)==1
    page.screenshot(path=out/'voice-desktop.png')

    page.set_viewport_size({'width':390,'height':844})
    page.get_by_label('工作面板',exact=True).select_option('agent')
    expect(button).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth+2')
    page.screenshot(path=out/'voice-mobile.png')
    button.click()
    expect(button).to_have_text('停止录音')
    page.reload(wait_until='domcontentloaded')
    page.get_by_label('工作面板',exact=True).select_option('agent')
    expect(button).to_have_text('语音输入')
    assert len(requests)==1
    (out/'voice-browser.json').write_text(json.dumps({'passed':True,'audio_device':'WAV fixture via browser media input',
        'offline_transcriptions':1,'microphone_denied_and_recovered':True,'cancelled_on_navigation':True,
        'draft_preserved':True,'text':result,'llm_calls':0},ensure_ascii=False,indent=2),encoding='utf8')
    denied.close();browser.close()
"""


def run(args):
    api=IsolatedServer('ff-g12-voice',app='voice_fixture:app')
    scratch=api.root/'voice-scratch'
    scratch.mkdir()
    deployed_model=api.root/'speech-model.bin'
    # The read-only fixture model is copied so the failure case can substitute
    # an invalid local file without changing the operator's installed model.
    shutil.copyfile(args.model.resolve(strict=True),deployed_model)
    settings={'VIBEFLOW_VOICE_TRANSCRIBER_PATH':str(args.cli.resolve(strict=True)),
              'VIBEFLOW_VOICE_MODEL_PATH':str(deployed_model)}
    module=('import os,tempfile\nos.environ.update('+repr(settings)+')\n'
        'tempfile.tempdir='+repr(str(scratch))+'\nfrom app.main import app\n'
        'from fastapi.staticfiles import StaticFiles\napp.mount("/",StaticFiles(directory='
        +repr(str(args.dist.resolve(strict=True)))+',html=True))\n')
    module+='CLI='+repr(settings['VIBEFLOW_VOICE_TRANSCRIBER_PATH'])+'\n'
    module+="""
import asyncio,json
from pathlib import Path
original_spawn=asyncio.create_subprocess_exec
receipt_file=Path(__file__).with_name('voice-process.jsonl')

def receipt(process,phase):
    with receipt_file.open('a',encoding='utf8') as stream:
        stream.write(json.dumps({'pid':process.pid,'phase':phase,'code':process.returncode})+'\\n')

async def observed_spawn(*arguments,**options):
    process=await original_spawn(*arguments,**options)
    if arguments and arguments[0]==CLI:
        receipt(process,'started')
        async def observed_exit():
            await process.wait()
            receipt(process,'exited')
        asyncio.create_task(observed_exit())
    return process

asyncio.create_subprocess_exec=observed_spawn
"""
    (api.root/'voice_fixture.py').write_text(module,encoding='utf8')
    report={'passed':False,'llm_calls':0,'mock_llm_calls':0,'checks':[]}
    print('G12_EVIDENCE',api.root,flush=True)
    try:
        with api:
            case=api.create_case('Offline dictation acceptance')
            cid=case['id']
            api.call('POST','/conversations',{'case_id':cid,'title':'Voice acceptance','permission_mode':'ask'})
            assert api.call('GET','/voice/capabilities')['server_transcription']
            url=api.base+f'/api/v1/cases/{cid}/voice/transcription'
            chinese,english=canonical(args.chinese),canonical(args.english)
            with httpx.Client(trust_env=False,timeout=32) as client:
                assert client.put(url,content=chinese).status_code==401
                outsider=api.call('POST','/auth/register',{'email':uuid4().hex+'@example.com',
                    'password':'isolated-test-pass','name':'Independent account'})['token']
                client.headers['Authorization']='Bearer '+outsider
                assert client.put(url,content=chinese).status_code==404
                client.headers['Authorization']='Bearer '+api.token
                assert client.put(url,content=b'not a wave file').status_code==422
                assert client.put(url,content=b'\0'*(961*1024)).status_code==413
                assert client.put(url,content=chinese[:44]+b'\0'*(len(chinese)-44)).status_code==422
                report['checks'].append('authentication, malformed WAV, silence and length guards')
                for language,wave_bytes,expected in [('zh',chinese,'管道'),('en',english,'pressure')]:
                    result=client.put(url,params={'language':language},content=wave_bytes)
                    assert result.status_code==200,(language,result.status_code)
                    recognized=result.json()['text']
                    assert expected in recognized.lower(),(language,recognized)
                    report[language]=recognized
                report['checks'].append('actual offline Chinese and English transcriptions')

                backup=deployed_model.with_suffix('.saved')
                os.replace(deployed_model,backup)
                try:
                    deployed_model.write_bytes(b'broken speech weights')
                    assert client.put(url,params={'language':'zh'},content=chinese).status_code==502
                finally:
                    os.replace(backup,deployed_model)
                assert client.put(url,params={'language':'zh'},content=chinese).status_code==200
                report['checks'].append('native engine failure releases resources and a corrected model recovers')
                report['cancelled_process']=asyncio.run(cancelled_transcription(url,api.token,chinese,api.root))
                assert client.put(url,params={'language':'en'},content=english).status_code==200
                report['checks'].append('actual recognition process stopped on client disconnect, followed by successful retry')
                assert not list(scratch.glob('vf-voice-*'))
            inputs={'out':str(api.root),'address':api.base,'token':api.token,
                    'audio':str(args.chinese.resolve(strict=True))}
            command=[str(args.browser_python),'-c',EDGE]
            subprocess.run(command,input=json.dumps(inputs),text=True,check=True,timeout=110)
            assert not list(scratch.glob('vf-voice-*'))
        report['passed']=True
    finally:
        deployed_model.unlink(missing_ok=True)
        (api.root/'voice-actual.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for option in ('cli','model','dist','chinese','english','browser-python'):
        parser.add_argument('--'+option,type=Path,required=True)
    run(parser.parse_args())
