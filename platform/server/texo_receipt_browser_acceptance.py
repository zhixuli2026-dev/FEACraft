"""Production formula panel, packaged FormulaNet, and browser storage recovery."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid


def run_browser(args):
    from playwright.sync_api import expect, sync_playwright

    root = Path(args['root'])
    report = {'passed':False,'checks':[],'actual_formulanet_runs':0,'llm_calls':0,'simulated_recognition':False}
    save = lambda: (root/'texo-browser.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf8')
    with sync_playwright() as play:
        browser = play.chromium.launch(channel='msedge' if args['edge'] else None)
        context = browser.new_context(viewport={'width':1580,'height':900},locale='zh-CN')
        page = context.new_page()
        context.add_init_script("if(location.origin === "+json.dumps(args['base'])+" && !localStorage.getItem('vf_token'))"+
                                "localStorage.setItem('vf_token',"+json.dumps(args['token'])+");")
        model_assets = []
        page.on('response',lambda response:model_assets.append({'path':response.url.split('/texo/',1)[-1],'status':response.status})
                if '/texo/model/' in response.url else None)
        errors = []
        page.on('pageerror',lambda error:errors.append(str(error)))
        reports = []
        page.on('console',lambda msg:reports.append(msg.text.split(':',3)[1]) if msg.text.startswith('VIBEFLOW_TEXO:') else None)

        def panel():
            page.locator('.dv-tab').get_by_text('公式识别',exact=True).click(timeout=25000)
            worker = page.locator('iframe[title="texo-worker"]')
            expect(worker).to_be_attached()
            view = worker.locator('..')
            child = worker.element_handle().content_frame()
            child.wait_for_function("typeof window.vfTexoRecognize === 'function'",timeout=20000)
            return view,child

        def mark_worker(frame, gate=False):
            frame.evaluate('''(gate) => {
              const actual = window.vfTexoRecognize;
              window.actualCalls = 0;
              const proof = gate ? new Promise(resolve => { window.releaseOcr = resolve }) : Promise.resolve();
              window.vfTexoRecognize = async (...args) => {
                window.actualCalls++;
                await proof;
                return actual(...args);
              };
            }''',gate)

        def image(equation):
            return page.evaluate('''equation => {
              const canvas = document.createElement('canvas');
              canvas.width = 340; canvas.height = 125;
              const ctx = canvas.getContext('2d');
              ctx.fillStyle = '#fff'; ctx.fillRect(0,0,340,125);
              ctx.fillStyle = '#000'; ctx.font = 'italic 52px Cambria Math';
              ctx.fillText(equation,25,78);
              return canvas.toDataURL('image/png');
            }''',equation)

        def select_local(view,url):
            view.locator('input[type=file]').set_input_files({'name':'formula.png','mimeType':'image/png',
                                                             'buffer':base64.b64decode(url.split(',',1)[1])})
            expect(view.get_by_alt_text('待识别')).to_have_attribute('src',url)

        def button(view):
            return view.get_by_role('button',name='识别公式',exact=True)

        def status(view):
            return view.get_by_test_id('texo-ocr-status')

        def value(view):
            return view.get_by_role('textbox',name='识别结果')

        def entry(owner):
            return page.evaluate('''async owner => {
              const local = JSON.parse(localStorage.getItem(`vf_texo_receipts_v2_${owner}`) || '[]');
              const cache = await caches.open('vf-texo-receipts-v2');
              const response = await cache.match(`/texo/receipts/v2/${owner}`);
              return {local, remote:response ? await response.json() : []};
            }''',owner)

        def formula(text):
            normalized = ''.join(text.replace(r'\!', '').split())
            return re.fullmatch(r'E=mc\^(?:\{2\}|2)',normalized) is not None

        try:
            page.goto(args['base'],wait_until='domcontentloaded')
            page.locator('[data-case-name='+json.dumps(args['case_name'])+']').click(timeout=25000)
            view,frame = panel()
            mark_worker(frame,gate=True)
            original, replacement = image('E = mc²'), image('x + 1 = 3')
            original_hash = hashlib.sha256(original.encode()).hexdigest()
            replacement_hash = hashlib.sha256(replacement.encode()).hexdigest()
            select_local(view,original)
            expect(status(view)).to_contain_text('图片已就绪')
            button(view).click()
            frame.wait_for_function('window.actualCalls === 1')
            select_local(view,replacement)
            expect(status(view)).to_contain_text('图片已就绪')
            frame.evaluate('window.releaseOcr()')
            expect(button(view)).to_be_enabled(timeout=90000)
            assert value(view).count()==0 and reports.count('result')==1
            expect(view.get_by_alt_text('待识别')).to_have_attribute('src',replacement)
            page.wait_for_function('''async owner => {
              const local = localStorage.getItem(`vf_texo_receipts_v2_${owner}`);
              const cache = await caches.open('vf-texo-receipts-v2');
              const result = await cache.match(`/texo/receipts/v2/${owner}`);
              return local && result && local === await result.text();
            }''',arg=args['owner'])
            saved = entry(args['owner'])
            assert saved['local']==saved['remote'] and len(saved['local'])==1, saved
            recognized = saved['local'][0]
            assert recognized['hash']==original_hash and recognized['hash']!=replacement_hash
            report['actual_formulanet_runs'] += 1
            assert formula(recognized['latex']), recognized['latex']
            report['checks'].append('actual late FormulaNet result stayed linked to original SHA-256')

            select_local(view,original)
            expect(status(view)).to_contain_text('缓存命中')
            expect(value(view)).to_have_value(recognized['latex'])
            button(view).click()
            expect(status(view)).to_contain_text('缓存命中')
            assert frame.evaluate('window.actualCalls')==1
            report['checks'].append('repeat recognition reused cached output')
            page.screenshot(path=root/'texo-cache-hit.png')

            case_path = 'Ref/ocr-formula.png'
            file_route = f'/api/v1/cases/{args["case_id"]}'
            posted = page.evaluate('''async ({fileRoute,path,original}) => {
              const bytes = Uint8Array.from(atob(original.split(',')[1]), c => c.charCodeAt(0));
              const blob = new Blob([bytes],{type:'image/png'});
              const body = new FormData();
              body.append('file',blob,'ocr-formula.png');
              return (await fetch(`${fileRoute}/upload?path=${encodeURIComponent(path)}`,{
                method:'POST',body,headers:{Authorization:'Bearer '+localStorage.getItem('vf_token')},
              })).status;
            }''',{'fileRoute':file_route,'path':case_path,'original':original})
            assert posted==200, posted
            page.reload(wait_until='domcontentloaded')
            page.locator('[data-case-name='+json.dumps(args['case_name'])+']').click(timeout=25000)
            view,frame = panel()
            mark_worker(frame)
            picker = view.locator('select').first
            raw_route = lambda url: file_route+'/raw?path=Ref%2Focr-formula.png' in url
            page.route(raw_route,lambda route:route.fulfill(status=503,body='storage unavailable for this check'))
            picker.select_option(case_path)
            expect(status(view)).to_contain_text('无法加载算例图片')
            expect(view.get_by_alt_text('待识别')).to_have_count(0)
            assert value(view).count()==0
            page.unroute(raw_route)
            picker.select_option('')
            picker.select_option(case_path)
            expect(status(view)).to_contain_text('缓存命中')
            expect(value(view)).to_have_value(recognized['latex'])
            assert frame.evaluate('window.actualCalls')==0
            report['checks'].append('case image download failure clears prior output and retry finds correct receipt')

            page.evaluate('owner => localStorage.setItem(`vf_texo_receipts_v2_${owner}`, "{bad")',args['owner'])
            page.reload(wait_until='domcontentloaded')
            view,frame = panel()
            mark_worker(frame)
            select_local(view,original)
            expect(status(view)).to_contain_text('缓存命中')
            expect(value(view)).to_have_value(recognized['latex'])
            assert frame.evaluate('window.actualCalls')==0
            repaired = entry(args['owner'])
            assert repaired['local']==repaired['remote']==[recognized]
            report['checks'].append('corrupt local snapshot repaired from CacheStorage')

            aged = page.evaluate('''async ({owner,recognized}) => {
              const history = Array.from({length:25},(_,i) => ({ ...recognized,
                hash:(i+1).toString(16).padStart(64,'0'),created_at:recognized.created_at-i-1 }));
              const cache = await caches.open('vf-texo-receipts-v2');
              await cache.delete(`/texo/receipts/v2/${owner}`);
              localStorage.setItem(`vf_texo_receipts_v2_${owner}`,JSON.stringify(history));
              const legacy = await caches.open('vf-texo-receipts-v1');
              for (const row of [recognized,...history]) {
                localStorage.setItem(`vf_texo_receipt_${row.hash}`,JSON.stringify(row));
                await legacy.put(`/receipts/${row.hash}`,new Response(JSON.stringify(row)));
              }
              localStorage.setItem('vf_texo_receipt_index',JSON.stringify([recognized.hash,...history.map(x=>x.hash)]));
              return history.at(-1).hash;
            }''',{'owner':args['owner'],'recognized':recognized})
            page.reload(wait_until='domcontentloaded')
            view,frame = panel()
            mark_worker(frame)
            select_local(view,original)
            expect(status(view)).to_contain_text('缓存命中')
            expect(value(view)).to_have_value(recognized['latex'])
            trimmed = entry(args['owner'])
            assert len(trimmed['local'])==20 and trimmed['local']==trimmed['remote']
            assert any(row['hash']==original_hash for row in trimmed['local']) and all(row['hash']!=aged for row in trimmed['local'])
            assert await_legacy_deleted(page,original_hash,aged)
            assert frame.evaluate('window.actualCalls')==0
            report['checks'].append('legacy selected image migrated; old v1 and v2 history trimmed')

            page.evaluate('token => localStorage.setItem("vf_token",token)',args['guest_token'])
            page.reload(wait_until='domcontentloaded')
            view,frame = panel()
            mark_worker(frame)
            page.evaluate('''() => {
              const set = Storage.prototype.setItem;
              Storage.prototype.setItem = function(key,value) {
                if (key.startsWith('vf_texo_receipts_v2_')) throw new DOMException('Test storage limit','QuotaExceededError');
                return set.call(this,key,value);
              };
              const open = CacheStorage.prototype.open;
              CacheStorage.prototype.open = function(name) {
                return name.startsWith('vf-texo-receipts') ? Promise.reject(new DOMException('Test CacheStorage unavailable','SecurityError'))
                  : open.call(this,name);
              };
            }''')
            select_local(view,original)
            expect(status(view)).to_contain_text('图片已就绪')
            assert value(view).count()==0
            button(view).click()
            expect(status(view)).to_have_text('识别完成',timeout=90000)
            assert formula(value(view).input_value())
            report['actual_formulanet_runs'] += 1
            button(view).click()
            expect(status(view)).to_contain_text('缓存命中')
            assert frame.evaluate('window.actualCalls')==1
            report['checks'].append('second account isolated; disabled persistence falls back to live OCR and session cache')
            assert sum(e['status']==200 and e['path'].endswith('.onnx') for e in model_assets)>=2
            assert not errors, errors
            report.update(passed=True,chromium=browser.version,latex=recognized['latex'],model_assets=model_assets)
        except Exception:
            report.update(console_signals=reports,page_errors=errors,model_assets=model_assets)
            page.screenshot(path=root/'texo-failure.png',timeout=5000)
            raise
        finally:
            save()
            browser.close()


def await_legacy_deleted(page, migrated, expired):
    return page.evaluate('''async ([migrated,expired]) => {
      const cache = await caches.open('vf-texo-receipts-v1');
      return (await Promise.all([migrated,expired].map(async hash =>
        !localStorage.getItem(`vf_texo_receipt_${hash}`) && !(await cache.match(`/receipts/${hash}`))))).every(Boolean);
    }''',[migrated,expired])


def main():
    from testsupport import IsolatedServer

    bundle = Path(os.environ['VIBEFLOW_TEST_WEB_DIST']).resolve(strict=True)
    assert (bundle/'texo/model/onnx/encoder_model.onnx').is_file()
    api = IsolatedServer('texo-ocr-browser',app='texo_fixture:app')
    (api.root/'texo_fixture.py').write_text('from app.main import app\nfrom fastapi.staticfiles import StaticFiles\n'
        'app.mount("/", StaticFiles(directory='+repr(str(bundle))+',html=True))\n',encoding='utf8')
    with api:
        name = 'Formula recognition '+uuid.uuid4().hex[:8]
        api.create_case(name)
        owner = api.call('GET','/auth/me')['id']
        guest = api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com','name':'Other account',
            'password':'isolated-texo-123'})
        args = {'base':api.base,'root':str(api.root),'token':api.token,'case_name':name,'case_id':api.case_id,'owner':owner,
                'guest_token':guest['token'],'edge':'--edge' in sys.argv}
        subprocess.run([os.environ['VIBEFLOW_TEST_BROWSER_PYTHON'],str(Path(__file__).resolve()),'--browser'],
                       input=json.dumps(args),text=True,timeout=240,check=True)
    print('TEXO_OCR_BROWSER_OK '+str(api.root/'texo-browser.json'),flush=True)


if __name__ == '__main__':
    run_browser(json.load(sys.stdin)) if sys.argv[1:] == ['--browser'] else main()
