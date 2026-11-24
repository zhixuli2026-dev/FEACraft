"""FF-F10. Observe the real Edge audio graph during three actual DeepSeek requests."""

import argparse
import asyncio
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit

from live_llm_acceptance import FIXTURE, source
from live_llm_agenthost_order_acceptance import OBSERVE, jsonl, ordered
from live_llm_chat_markdown_acceptance import wait_for_api_exit
from live_llm_reasoning_card_acceptance import write_json
from testsupport import IsolatedServer


CASE = 'F10 actual reply sound'
FIRST = 'F10_SOUND_COMPLETE'
SECOND = 'F10_MUTED_COMPLETE'
WORKLET = """
class ReplySoundTap extends AudioWorkletProcessor {
  process(inputs) {
    const waveform = inputs[0]?.[0];
    if (waveform?.some(sample => Math.abs(sample) > 0.00008)) {
      this.port.postMessage({frame: currentFrame, samples: Array.from(waveform)});
    }
    return true;
  }
}
registerProcessor('reply-sound-tap', ReplySoundTap);
"""
INSTALL_AUDIO = """(() => {
  const Audio = window.AudioContext;
  const probe = window.f10Audio = {blocks: [], contexts: 0, connections: 0, error: null};
  window.AudioContext = class extends Audio {
    constructor(...args) {
      super(...args);
      probe.contexts++;
      probe.context = this;
      this.audioWorklet.addModule('/api/v1/fixture/reply-sound-worklet.js').then(() => {
        const tap = new AudioWorkletNode(this, 'reply-sound-tap');
        tap.port.onmessage = event => probe.blocks.push(event.data);
        tap.connect(this.destination);
        probe.tap = tap;
      }).catch(error => { probe.error = String(error); });
    }
    createGain() {
      const node = super.createGain();
      const connect = node.connect.bind(node);
      const context = this;
      node.connect = (destination, ...args) => {
        const result = connect(destination, ...args);
        if (destination === context.destination && probe.tap) {
          connect(probe.tap);
          probe.connections++;
        }
        return result;
      };
      return node;
    }
  };
})()"""
BOUNDARIES = '''
from fastapi.responses import Response

@app.get('/api/v1/fixture/reply-sound-worklet.js')
async def audio_tap():
    return Response(audio_worklet, media_type='application/javascript')

observed_lines = httpx.Response.aiter_lines
response_count = 0

async def speech_boundaries(self, *args, **kwargs):
    global response_count
    if self.request.url.host != 'api.deepseek.com':
        async for line in observed_lines(self, *args, **kwargs):
            yield line
        return
    response_count += 1
    index = response_count
    paused = False
    async for line in observed_lines(self, *args, **kwargs):
        piece = ''
        if line.startswith('data:') and line[5:].strip() != '[DONE]':
            values = json.loads(line[5:])
            choice = (values.get('choices') or [{}])[0]
            delta = choice.get('delta') or {}
            piece = delta.get('content') or delta.get('reasoning_content') or ''
        yield line
        if index in observation_indices and piece and not paused:
            paused = True
            (root/f'reached-{index}.json').write_text(json.dumps({
                'actual_characters':len(piece),
                'lane':'text.delta' if delta.get('content') else 'reasoning.delta',
                'sha256':hashlib.sha256(piece.encode('utf8')).hexdigest()
            }),encoding='utf8')
            timeout = time.monotonic() + 60
            while not (root/f'release-{index}').exists():
                if time.monotonic() >= timeout:
                    raise TimeoutError('Edge did not observe the real reply boundary')
                await asyncio.sleep(.04)

httpx.Response.aiter_lines = speech_boundaries
'''


def analyze_audio(data):
    """Measure the envelope and spectrum of the output tapped from the real gain nodes."""
    import numpy as np

    blocks = data['blocks']
    rate = data['sample_rate']
    start = blocks[0]['frame']
    end = blocks[-1]['frame'] + len(blocks[-1]['samples'])
    waveform = np.zeros(end-start)
    for block in blocks:
        offset = block['frame']-start
        waveform[offset:offset+len(block['samples'])] = block['samples']
    duration = len(waveform)/rate
    assert .18 < duration < .27 and data['connections'] == 2, (duration, data['connections'])
    frequencies, peaks = [], []
    for begin, finish in ((.007,.055),(.135,.185)):
        interval = waveform[round(begin*rate):round(finish*rate)]
        spectrum = abs(np.fft.rfft(interval * np.hanning(len(interval))))
        frequencies.append(round(float(np.fft.rfftfreq(len(interval),1/rate)[np.argmax(spectrum)]),2))
        peaks.append(round(float(np.max(np.abs(interval))),5))
    assert abs(frequencies[0]-880)<35 and abs(frequencies[1]-1318)<35, frequencies
    assert peaks[0] > .025 and peaks[1] > .006, peaks
    return {'duration_ms':round(duration*1000,2),'sample_rate':rate,
            'audio_blocks':len(blocks),'measured_frequencies_hz':frequencies,'window_peaks':peaks,
            'gain_nodes_connected_to_actual_output':data['connections']}


def browser(settings):
    from playwright.sync_api import expect, sync_playwright

    root, base = Path(settings['root']), settings['base']
    result = {'passed': False, 'notifications': {}, 'model_posts': []}
    with sync_playwright() as driver:
        edge = driver.chromium.launch(channel='msedge')
        result['edge'] = edge.version
        context = edge.new_context(viewport={'width': 1470, 'height': 960}, locale='zh-CN')
        context.add_init_script('if(location.origin===' + json.dumps(base) + ')localStorage.setItem("vf_token",'
                                + json.dumps(settings['token']) + ');')
        context.add_init_script(INSTALL_AUDIO)
        context.on('request', lambda request: result['model_posts'].append(urlsplit(request.url).path)
                   if request.method=='POST' and urlsplit(request.url).path.endswith('/messages') else None)
        page = context.new_page()
        page.set_default_timeout(62000)
        first, muted, cancel = [str(value) for value in settings['conversations']]
        cancellation_only = settings.get('cancel_only', False)
        cancel_index = 1 if cancellation_only else 3

        def enter(tab, conversation):
            row = tab.locator('[data-case-name=' + json.dumps(CASE) + ']')
            expect(row).to_be_visible()
            if row.get_attribute('aria-pressed') != 'true':
                row.click()
            choose = tab.get_by_test_id('agent-conversation')
            if not choose.is_visible():
                tab.locator('.dv-tab:visible').filter(has_text=re.compile(r'^Agent$')).click()
            choose.select_option(conversation)
            expect(tab.get_by_test_id('agent-input')).to_be_enabled()

        def option(tab):
            drawer = tab.locator('details.agent-settings')
            if drawer.get_attribute('open') is None:
                drawer.locator('summary').click()
            return drawer.get_by_role('checkbox', name='回复提示音', exact=True)

        def close_settings(tab):
            drawer = tab.locator('details.agent-settings')
            if drawer.get_attribute('open') is not None:
                drawer.locator('summary').click()

        def wait_audio(tab):
            tab.wait_for_function('window.f10Audio.tap || window.f10Audio.error')
            assert tab.evaluate('window.f10Audio.error') is None
            tab.wait_for_function('window.f10Audio.context.state === "running"')

        def settle_workbench():
            deadline = time.monotonic()+45
            db_path = (root/'test.sqlite').as_uri()+'?mode=ro'
            while time.monotonic()<deadline:
                page.wait_for_timeout(200)
                with closing(sqlite3.connect(db_path,uri=True)) as db:
                    probes = db.execute("SELECT count(*) FROM native_requests WHERE phase='active' "
                        "AND kind IN ('runtime.python_probe','runtime.wsl_probe')").fetchone()[0]
                if probes==0:
                    return
            raise AssertionError('Workstation capability probes did not finish')

        def submit(tab, conversation, prompt):
            composer = tab.get_by_test_id('agent-input')
            composer.fill(prompt)
            route = '/api/v1/conversations/'+conversation+'/messages'
            with tab.expect_response(lambda res: res.request.method=='POST' and
                                     urlsplit(res.url).path==route, timeout=75000) as observed:
                composer.press('Enter')
            return observed.value

        def stream(response, name, terminal):
            assert response.status==200, response.status
            assert 'text/event-stream' in response.headers['content-type']
            frames = [json.loads(line[5:]) for line in response.text().splitlines() if line.startswith('data:')]
            answer = ordered(frames)
            write_json(root/(name+'-wire.json'), [
                {key: frame[key] for key in ('type','run_id','seq','answer_event_id') if key in frame} for frame in frames
            ])
            assert sum(frame['type']==terminal for frame in frames)==1
            return answer, ''.join(frame['text'] for frame in frames if frame['type']=='text.delta')

        def boundary(index):
            until = time.monotonic()+28
            while not (root/f'reached-{index}.json').is_file() and time.monotonic()<until:
                page.wait_for_timeout(100)
            assert (root/f'reached-{index}.json').is_file()

        def silence(tab):
            tab.wait_for_timeout(420)
            assert tab.evaluate('window.f10Audio.blocks.length')==0

        def reset_audio(tab):
            tab.evaluate('() => { window.f10Audio.blocks=[];window.f10Audio.connections=0; }')

        def observe_completion():
            background = submit(page, first, f'只回复验证串 {FIRST}，无需工具。')
            boundary(1)
            enter(page, muted)
            assert page.get_by_test_id('agent-conversation').input_value()==muted
            silence(page)
            (root/'release-1').touch()
            first_run, answer = stream(background, 'background', 'run.completed')
            assert FIRST in answer
            page.wait_for_function('window.f10Audio.blocks.length > 10')
            page.wait_for_timeout(350)
            measured = page.evaluate("""() => ({blocks:window.f10Audio.blocks,
                sample_rate:window.f10Audio.context.sampleRate,connections:window.f10Audio.connections})""")
            write_json(root/'audio-native-samples.json', measured)
            result['notifications']['background_complete'] = {
                'run':first_run, 'selected_different_conversation':True, **analyze_audio(measured)}
            page.wait_for_timeout(420)
            assert page.evaluate('window.f10Audio.blocks.length')==len(measured['blocks'])

            reset_audio(page)
            switch = option(page)
            switch.uncheck()
            close_settings(page)
            disabled = submit(page, muted, f'只回复验证串 {SECOND}，无需工具。')
            muted_run, answer = stream(disabled, 'muted', 'run.completed')
            assert SECOND in answer
            silence(page)
            assert page.evaluate('window.f10Audio.connections')==0
            page.reload(wait_until='domcontentloaded')
            enter(page, muted)
            expect(option(page)).not_to_be_checked()
            result['notifications']['muted_complete'] = {'run':muted_run, 'silent':True,'persisted_after_reload':True}

        def observe_cancellation():
            enter(page, cancel)
            settle_workbench()
            cancelling = submit(page,cancel,'请用三条简短中文列出办公室交接注意事项，无需工具。')
            boundary(cancel_index)
            other_tab = context.new_page()
            other_tab.goto(base, wait_until='domcontentloaded')
            enter(other_tab, cancel)
            other_tab.bring_to_front()
            rejected = submit(other_tab,cancel,'同一会话已有工作时，请拒绝这次并发提交。无需工具。')
            assert rejected.status==409, rejected.status
            rejection = rejected.json()['detail']
            assert rejection['code']=='state_conflict' and rejection['message']=='Conversation already has an execution owner', rejection
            page.bring_to_front()
            stop_route = '/api/v1/conversations/'+cancel+'/messages:cancel'
            with page.expect_response(lambda res: res.request.method=='POST' and
                                      urlsplit(res.url).path==stop_route) as stopped:
                page.get_by_role('button',name='停止',exact=True).click()
            assert stopped.value.ok and stopped.value.json()['ok']
            stopped_run, _ = stream(cancelling,'cancelled','run.cancelled')
            silence(page)
            assert page.evaluate('window.f10Audio.connections')==0
            result['notifications']['cancelled'] = {'run':stopped_run,'silent':True}
            other_tab.bring_to_front()
            wait_audio(other_tab)
            silence(other_tab)
            assert other_tab.evaluate('window.f10Audio.connections')==0
            result['notifications']['actual_rejected_request'] = {'http':409,'code':rejection['code'],'silent':True}
            other_tab.close()

        try:
            page.goto(base, wait_until='domcontentloaded')
            enter(page, first)
            switch = option(page)
            expect(switch).not_to_be_checked()
            switch.check()
            wait_audio(page)
            close_settings(page)
            settle_workbench()
            if not cancellation_only:
                observe_completion()
                option(page).check()
                wait_audio(page)
                close_settings(page)
            observe_cancellation()

            page.reload(wait_until='domcontentloaded')
            enter(page, first)
            expect(page.get_by_test_id('chat-markdown').filter(has_text=FIRST)).to_be_visible()
            expect(option(page)).to_be_checked()
            silence(page)
            assert page.evaluate('window.f10Audio.contexts')==0
            result['notifications']['history_reload'] = {'persisted_enabled':True,'silent':True}
            page.screenshot(path=str(root/'reply-sound-settings.png'))
            assert len(result['model_posts'])==(2 if cancellation_only else 4)
            result['passed'] = True
        finally:
            for index in (1,3):
                (root/f'release-{index}').touch()
            if not result['passed']:
                page.screenshot(path=str(root/'reply-sound-failure.png'))
            write_json(root/'reply-sound-browser.json',result)
            edge.close()


def clone_completed_history(original, target):
    previous = json.loads((original/'live-result.json').read_text(encoding='utf8'))
    assert previous['host']=='api.deepseek.com' and previous['model_calls']==3
    report = json.loads((original/'reply-sound-browser.json').read_text(encoding='utf8'))
    audible, muted = (report['notifications'][key] for key in ('background_complete','muted_complete'))
    assert audible['gain_nodes_connected_to_actual_output']==2 and muted['silent']
    port = urlsplit(json.loads((original/'result.json').read_text(encoding='utf8'))['api']).port
    cleanup = wait_for_api_exit(original, port)
    db_uri = (original/'test.sqlite').as_uri()+'?mode=ro'
    with closing(sqlite3.connect(db_uri,uri=True)) as db, closing(sqlite3.connect(target/'test.sqlite')) as clone:
        events = [(cid,typ,json.loads(payload)) for cid,typ,payload in db.execute(
            "SELECT conversation_id,type,payload FROM agent_events WHERE type IN ('message','run_state') ORDER BY id")]
        stored = {}
        for index, (expected, run) in enumerate(((FIRST,audible['run']),(SECOND,muted['run'])),start=1):
            cid, body = next((cid,payload['content']) for cid,typ,payload in events if typ=='message'
                             and payload.get('role')=='assistant' and payload.get('run_id')==run)
            assert expected in body
            assert [payload['status'] for event_cid,typ,payload in events if event_cid==cid and
                    typ=='run_state' and payload.get('run_id')==run][-1]=='completed'
            lane, = [value for value in jsonl(original/'upstream-lanes.jsonl') if value['index']==index]
            assert lane['done'] and [value['sha256'] for value in lane['groups'] if value['type']=='text.delta']==[
                hashlib.sha256(body.encode('utf8')).hexdigest()]
            stored[expected] = cid
        cid = stored[FIRST]
        case_id, = db.execute('SELECT case_id FROM conversations WHERE id=?',(cid,)).fetchone()
        email, = db.execute('SELECT email FROM users ORDER BY id LIMIT 1').fetchone()
        assert list(db.execute("SELECT phase FROM conversation_executions WHERE phase!='idle'"))==[]
        db.backup(clone)
    shutil.copytree(original/'files',target/'files',dirs_exist_ok=True)
    return case_id, email, stored[FIRST], stored[SECOND], cleanup


def verify(args, config):
    endpoint = urlsplit(config['base_url'])
    assert endpoint.scheme=='https' and endpoint.hostname=='api.deepseek.com'
    dist = args.dist.resolve(strict=True)
    assert (dist/'index.html').is_file()
    python = args.browser_python.resolve(strict=True)
    original = args.cancel_from.resolve(strict=True) if args.cancel_from else None
    budget = 1 if original else 3
    api = IsolatedServer('live-f10-reply-sound', app='reply_sound_fixture:app')
    guard = "if calls>8:raise RuntimeError('Live acceptance exceeded its eight-request limit')"
    assert FIXTURE.count(guard)==1
    fixture = FIXTURE.replace(guard,f"if calls>{budget}:raise RuntimeError('FF-F10 acceptance exceeded its actual request limit')")
    mount = '\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/",StaticFiles(directory=' + repr(str(dist)) + ',html=True))\n'
    indices = '(1,)' if original else '(1, 3)'
    (api.root/'reply_sound_fixture.py').write_text(fixture+'\nprobe_sparse=False\n'+OBSERVE+f'\nobservation_indices={indices}\n'+
        '\naudio_worklet='+repr(WORKLET)+'\n'+BOUNDARIES+mount,encoding='utf8')
    result = {'passed':False,'provider':'deepseek','host':endpoint.hostname,'model':config['model'],
              'maximum_actual_requests':budget,'mock_llm_calls':0}
    print('LIVE_F10_EVIDENCE',api.root,flush=True)
    try:
        if original:
            case_id, email, first, muted, cleanup = clone_completed_history(original, api.root)
            result['retained_completed_history'] = {'source':str(original), 'cleanup':cleanup}
        with api:
            if original:
                account = api.call('POST','/auth/login',{'email':email,'password':'isolated-test-123'})
                api.token = account['token']
            else:
                case_id = api.create_case(CASE)['id']
            profile = api.call('POST','/provider-profiles',{'provider':'deepseek','name':'F10 authorized DeepSeek',
                'base_url':config['base_url'],'default_model':config['model'],'api_key':config['key']})
            def create_conversation(title):
                return api.call('POST','/conversations',{'case_id':case_id,'title':title,'model':config['model'],
                    'permission_mode':'ask','provider_profile_id':profile['id']})['id']
            if not original:
                first, muted = create_conversation('声响活动'), create_conversation('用户关闭')
            ids = [first,muted,create_conversation('用户取消提示音验证')]
            temp=api.root/'edge-tmp'
            temp.mkdir()
            subprocess.run([str(python),str(Path(__file__).resolve()),'--browser'],cwd=api.root,
                input=json.dumps({'root':str(api.root),'base':api.base,'token':api.token,'conversations':ids,
                                  'cancel_only':original is not None}),
                encoding='utf8',env=dict(os.environ,PYTHONIOENCODING='utf8',TEMP=str(temp),TMP=str(temp)),
                check=True,timeout=405)
            report = json.loads((api.root/'reply-sound-browser.json').read_text(encoding='utf8'))
            assert report['passed']
            result['browser']=report
            terminals = [(ids[-1],'cancelled','cancelled')]
            if not original:
                terminals[:0] = [(first,'background_complete','completed'),(muted,'muted_complete','completed')]
            for cid, label, status in terminals:
                run = report['notifications'][label]['run']
                history = api.call('GET',f'/conversations/{cid}')['events']
                assert [event['payload']['status'] for event in history if event['type']=='run_state'
                        and event['payload'].get('run_id')==run][-1]==status
                assert api.call('GET',f'/fixture/stream-owner/{cid}/{run}')=={
                    'phase':'idle','registered':False,'sequence_tracked':False}
            result['durable_terminal_status']=[status for _,_,status in terminals]
        result['cleanup']=wait_for_api_exit(api.root,api.port)
        calls=[row for row in jsonl(api.root/'upstream.jsonl') if row['kind']=='http']
        assert len(calls)==budget and all(row['status']==200 for row in calls)
        result['passed']=True
    except BaseException as error:
        result['failure']={'type':type(error).__name__,'detail':str(error).replace(config['key'],'[REDACTED]')[:450]}
        raise
    finally:
        result['upstream']=jsonl(api.root/'upstream.jsonl')
        result['model_calls']=sum(row['kind']=='http' for row in result['upstream'])
        assert config['key'] not in json.dumps(result)
        write_json(api.root/'live-result.json',result)
    print('LIVE_F10_PASS',api.root/'live-result.json',flush=True)


if __name__=='__main__':
    if sys.argv[1:]==['--browser']:
        browser(json.load(sys.stdin))
    else:
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--source-profile-id',type=int,required=True)
        parser.add_argument('--cancel-from',type=Path)
        parser.add_argument('--dist',type=Path,required=True)
        parser.add_argument('--browser-python',type=Path,required=True)
        options=parser.parse_args()
        verify(options,asyncio.run(source(options.source_profile_id)))
