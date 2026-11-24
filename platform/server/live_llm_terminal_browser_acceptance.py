"""Check real browser input, PTY output and file readback in an isolated case."""
import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlencode, urlsplit


PTY_OUTPUT = r'''import sys,time
out = sys.stdout.buffer
def write(data):
    out.write(data)
    out.flush()

if sys.argv[1] == 'history':
    for index in range(8):
        time.sleep(.2)
        write(('LIVE_WHILE_SCROLLED_%02d\n' % index).encode())
elif sys.argv[1] == 'colors':
    write(b'\x1b[38;2;40;50;100mTERMINAL_RGB\x1b[0m\n\x1b[4')
    time.sleep(.2)
    write(b'0mTERMINAL_LIGHT\x1b[0m\n\x1b[10')
    time.sleep(.2)
    write(b'0mTERMINAL_LIGHT_GRAY\x1b[0m\n')
else:
    for index in range(33):
        write(b''.join(('LINE-%05d ' % (index*200+row)).encode()+b'a'*510+b'\n' for row in range(200)))
        time.sleep(.018)
    write('TERMINAL_OUTPUT_END_\u4e2d\U0001f642\n'.encode())
'''


async def browser_check(data):
    from playwright.async_api import async_playwright, expect

    root = Path(data['root'])
    checks = []
    input_frames = []
    output_frames = []
    events = []
    headers = {'Authorization': 'Bearer ' + data['token']}
    async with async_playwright() as play:
        browser = await play.chromium.launch(channel=os.getenv('VIBEFLOW_TEST_BROWSER_CHANNEL') or None)
        context = await browser.new_context(viewport={'width':1500, 'height':1200}, locale='zh-CN')
        await context.add_init_script('if (location.origin === '+json.dumps(data['base'])+') localStorage.setItem("vf_token", '+json.dumps(data['token'])+');')
        page = await context.new_page()

        def observe(socket):
            if urlsplit(socket.url).path!='/ws/terminal':
                return
            def from_user(payload):
                if isinstance(payload,str):
                    value=json.loads(payload)
                    if value['type']=='input':input_frames.append(value['data'])
            def from_shell(payload):
                if isinstance(payload, bytes):
                    output_frames.append(payload)
                else:
                    events.append(json.loads(payload))
            socket.on('framesent', from_user)
            socket.on('framereceived', from_shell)
        page.on('websocket', observe)
        terminal = page.get_by_test_id('terminal-panel')
        entry = terminal.locator('.xterm-helper-textarea')
        rows = terminal.locator('.xterm-rows')
        cdp = await context.new_cdp_session(page)
        sid = None

        async def insert(text):
            await cdp.send('Input.insertText', {'text':text})

        async def command(text):
            await entry.focus()
            await insert(text)
            await page.keyboard.press('Enter')

        async def event(kind):
            async with asyncio.timeout(80):
                while not any(e['type']==kind for e in events):
                    assert not any(e['type']=='error' for e in events), events
                    await asyncio.sleep(.05)
                return next(e for e in events if e['type']==kind)

        async def observed_input(predicate):
            async with asyncio.timeout(6):
                while not predicate(input_frames):
                    await asyncio.sleep(.02)

        try:
            await page.goto(data['base'], wait_until='domcontentloaded')
            await page.locator('[data-case-name='+json.dumps(data['case_name'])+']').click(timeout=35000)
            await page.locator('.dv-tab').filter(has_text=re.compile('^终端$')).click(timeout=25000)
            await terminal.get_by_role('button', name='WSL', exact=True).click()
            await terminal.get_by_role('button', name='新建终端', exact=True).click()
            sid = (await event('ready'))['session']
            await event('resize_accepted')
            await expect(rows).to_contain_text('nobody@', timeout=20000)
            await entry.focus()
            await insert("printf '%s' 'DUP-")
            await entry.evaluate('textarea => { const make = () => new InputEvent("beforeinput",{data:"，",bubbles:true,cancelable:true}); textarea.dispatchEvent(make()); textarea.dispatchEvent(new CompositionEvent("compositionend",{data:"，",bubbles:true,cancelable:true})); textarea.dispatchEvent(make()); textarea.dispatchEvent(make()); }')
            await insert("' > Ref/terminal-rapid.txt")
            await page.keyboard.press('Enter')
            assert sum(v=='，' for v in input_frames)==3, input_frames

            await entry.evaluate('textarea => { window.terminalComposition = []; for (const kind of ["compositionstart","compositionupdate","compositionend"]) textarea.addEventListener(kind,event=>window.terminalComposition.push({type:kind,data:event.data,trusted:event.isTrusted}),true); }')
            await insert("printf '%s' 'IME-")
            await observed_input(lambda frames:frames[-1:]==["printf '%s' 'IME-"])
            index = len(input_frames)
            await cdp.send('Input.imeSetComposition', {'text':'，', 'selectionStart':1, 'selectionEnd':1})
            await cdp.send('Input.imeSetComposition', {'text':'中文', 'selectionStart':2, 'selectionEnd':2})
            assert input_frames[index:]==[], input_frames[index:]
            await insert('中文，🙂')
            await page.wait_for_function('window.terminalComposition.some(event => event.type === "compositionend")')
            assert await page.evaluate('window.terminalComposition.some(event=>event.type==="compositionupdate" && event.trusted)')
            await observed_input(lambda frames:'中文，🙂' in ''.join(frames[index:]))
            await insert("-全角Ａ１' > Ref/terminal-composition.txt")
            await page.keyboard.press('Enter')
            checks.append('fallback pairing, rapidly repeated punctuation and actual Chromium composition commit')

            await terminal.get_by_role('button',name=re.compile('快速输入')).click()
            quick = terminal.get_by_placeholder(re.compile('多行命令'))
            await quick.fill("printf '%s' '第一行' > Ref/terminal-quick-a.txt\nprintf '%s' '第二行' > Ref/terminal-quick-b.txt")
            mark = len(input_frames)
            await quick.evaluate('el => el.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",ctrlKey:true,isComposing:true,bubbles:true,cancelable:true}))')
            assert len(input_frames)==mark and (await quick.input_value()).startswith('printf')
            await quick.press('Control+Enter')
            await expect(quick).to_have_value('')
            await terminal.get_by_role('button',name=re.compile('收起输入框')).click()
            checks.append('multiline quick input and Ctrl+Enter guarded during composition')

            await terminal.locator('.terminal-output').evaluate('host => { window.terminalOutputWasActive=false; new MutationObserver(() => { if(host.classList.contains("vf-output-active")) window.terminalOutputWasActive=true; }).observe(host,{attributes:true,attributeFilter:["class"]}); }')
            start = len(output_frames)
            await command('python3 Ref/terminal-output.py large')
            await expect(rows).to_contain_text('TERMINAL_OUTPUT_END_中🙂',timeout=90000)
            await page.wait_for_function('window.terminalOutputWasActive')
            await expect(terminal.locator('.vf-output-active')).to_have_count(0,timeout=20000)
            total = sum(len(chunk) for chunk in output_frames[start:])
            assert total>3*1024*1024, total
            checks.append('over 3 MiB of native PTY output, final Unicode marker and cursor state restored at bottom')

            await command('python3 Ref/terminal-output.py history')
            await expect(rows).to_contain_text('LIVE_WHILE_SCROLLED_00',timeout=15000)
            await page.keyboard.press('Shift+PageUp')
            await page.keyboard.press('Shift+PageUp')
            await expect(rows).not_to_contain_text('LIVE_WHILE_SCROLLED_',timeout=8000)
            visible_lines = await rows.inner_text()
            assert 'LINE-' in visible_lines or 'TERMINAL_OUTPUT_END_' in visible_lines, visible_lines
            async with asyncio.timeout(15):
                while b'LIVE_WHILE_SCROLLED_07' not in b''.join(output_frames[-12:]):
                    await asyncio.sleep(.07)
            await expect(terminal.locator('.vf-output-active')).to_have_count(0,timeout=15000)
            assert (await rows.inner_text())==visible_lines, 'live output moved the user viewport'
            for _ in range(4):
                await page.keyboard.press('Shift+PageDown')
            await expect(rows).to_contain_text('LIVE_WHILE_SCROLLED_07',timeout=10000)
            checks.append('manual history viewport stays in place while native output continues and can return to bottom')
            await terminal.locator('.xterm-screen').hover()
            await page.mouse.wheel(0,-1200)
            await expect(rows).not_to_contain_text('LIVE_WHILE_SCROLLED_07',timeout=8000)
            await page.mouse.wheel(0,1200)
            await expect(rows).to_contain_text('LIVE_WHILE_SCROLLED_07',timeout=10000)
            checks.append('wheel navigation follows the xterm history viewport')

            if await terminal.get_by_test_id('terminal-light-sgr').inner_text()=='浅色 SGR':
                await terminal.get_by_test_id('terminal-light-sgr').click()
            await command('python3 Ref/terminal-output.py colors')
            await expect(rows).to_contain_text('TERMINAL_LIGHT_GRAY',timeout=20000)
            colors = await rows.evaluate('rows => Object.fromEntries(["TERMINAL_RGB","TERMINAL_LIGHT","TERMINAL_LIGHT_GRAY"].map(key => { const el=[...rows.querySelectorAll("span")].find(span=>span.textContent.trim()===key); const style=el && getComputedStyle(el); return [key,style && {color:style.color, background:style.backgroundColor}]; }))')
            assert colors['TERMINAL_RGB']['color']=='rgb(40, 50, 100)', colors
            assert colors['TERMINAL_LIGHT']['background']=='rgb(241, 245, 249)', colors
            assert colors['TERMINAL_LIGHT_GRAY']['background']=='rgb(226, 232, 240)', colors
            assert any(frame.endswith(b'\x1b[4') for frame in output_frames), 'CSI was not split over actual PTY frames'
            checks.append('truecolor preserved and both split ANSI backgrounds rendered in the light theme')
            await terminal.screenshot(path=root/'terminal-browser-light.png')
            await terminal.get_by_test_id('terminal-light-sgr').click()
            await command('vi -n -u NONE Ref/terminal-rapid.txt')
            await expect(rows).to_contain_text('DUP-，，，',timeout=20000)
            await page.keyboard.press('Escape')
            await insert(':q!')
            await page.keyboard.press('Enter')
            await expect(rows).to_contain_text('TERMINAL_LIGHT_GRAY',timeout=20000)
            checks.append('actual vi alternate screen returned to native output history')
            await page.screenshot(path=root/'terminal-browser.png')

            async with asyncio.timeout(65):
                await terminal.locator('[data-session-id='+json.dumps(sid)+']').get_by_role('button',name=re.compile('关闭终端')).click()
                while True:
                    response=await context.request.get(data['base']+'/api/v1/terminal/sessions/'+sid, headers=headers)
                    state=await response.json()
                    if state.get('phase')=='closed':break
                    await asyncio.sleep(.2)
            assert state['cleanup_verified'] is True and state['stop']['verified'] is True
            output = {'browser':browser.version, 'checks':checks, 'session':sid, 'native_output_bytes':total,
                      'rendered_colors':colors, 'verified_stop':True}
            (root/'terminal-browser-result.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf8')
        except Exception:
            await page.screenshot(path=root/'terminal-browser-failure.png')
            print(json.dumps({'checks':checks,'terminal_events':[e for e in events if e['type'] in {'error','exit'}],
                              'last_input_frames':input_frames[-15:]}),flush=True)
            if sid is not None:
                await context.request.delete(data['base']+'/api/v1/terminal/sessions/'+sid,headers=headers,timeout=30000)
            raise
        finally:
            await browser.close()


def scenario(api, **_):
    from live_llm_job_repeat_acceptance import put

    put(api,'Ref/terminal-output.py',PTY_OUTPUT)
    case = api.call('GET',api.prefix)
    args={'root':str(api.root),'base':api.base,'token':api.token,'case_name':case['name']}
    subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'],'-B','-X','utf8',str(Path(__file__).resolve()),'--browser'],
                   input=json.dumps(args),text=True,timeout=400,check=True)
    expected={'terminal-rapid.txt':'DUP-，，，','terminal-composition.txt':'IME-中文，🙂-全角Ａ１',
              'terminal-quick-a.txt':'第一行','terminal-quick-b.txt':'第二行'}
    for name, value in expected.items():
        item=api.call('GET',api.prefix+'/file?'+urlencode({'path':'Ref/'+name}))
        assert item['content']==value, (name,item['content'])
    report=json.loads((api.root/'terminal-browser-result.json').read_text(encoding='utf8'))
    outsider=api.call('POST','/auth/register',{'email':'terminal-outsider@example.com','name':'Terminal outsider',
                                             'password':'isolated-terminal-visitor-123'})
    api.call('GET','/terminal/sessions/'+report['session'],expect=404,
             extra_headers={'Authorization':'Bearer '+outsider['token']})
    report['file_readback']=expected
    report['foreign_session_denied']=True
    (api.root/'terminal-browser-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return report


if __name__=='__main__':
    if sys.argv[1:]==['--browser']:
        asyncio.run(browser_check(json.load(sys.stdin)))
    else:
        from live_llm_job_repeat_acceptance import main

        bundle=Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
        assert (bundle/'index.html').is_file()
        fixture='\nfrom fastapi.staticfiles import StaticFiles\napp.mount("/", StaticFiles(directory='+repr(str(bundle))+', html=True))\n'
        asyncio.run(main(selected_override=scenario,fixture_override=fixture))
