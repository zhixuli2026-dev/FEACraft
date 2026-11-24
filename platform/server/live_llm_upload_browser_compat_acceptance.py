"""Run the three existing Chromium upload regressions against one disposable API."""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request

from live_llm_job_repeat_acceptance import main
from testsupport import SERVER


RUN = '''
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const [driver,origin,scripts]=process.argv.slice(1);
(async()=>{
  const browser=await require(driver).chromium.launch();
  try {
    const context=await browser.newContext({viewport:{width:1600,height:1000}});
    const page=await context.newPage();
    await page.goto(origin);
    const results=[];
    for(const filename of ['playwright-upload-resume.js','playwright-upload-resume-storage.js','playwright-upload-resume-completion.js']){
      const run=vm.runInThisContext(fs.readFileSync(path.join(scripts,filename),'utf8'),{filename});
      let result;
      try {
        result=await run(page);
      } catch (error) {
        const completed=await page.evaluate(()=>window.__uploadResumeEvidence?.checks || []);
        fs.writeFileSync('upload-browser-failure.json',JSON.stringify({filename,completed,error:error.message},null,2));
        throw error;
      }
      if(!result.passed)throw Error(filename+' was not successful');
      results.push({filename,...result});
      fs.writeFileSync('upload-browser-compat.json',JSON.stringify(results,null,2));
    }
  } finally {await browser.close();}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
'''


def scenario(api, **_):
    web=SERVER.parent/'web'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        port=sock.getsockname()[1]
    origin=f'http://127.0.0.1:{port}'
    python=os.environ['VIBEFLOW_TEST_BROWSER_PYTHON']
    driver=subprocess.check_output([python,'-c',"import playwright,pathlib;print(pathlib.Path(playwright.__file__).parent/'driver/package/index.js')"],text=True).strip()
    node=shutil.which('node')
    (api.root/'tmp/mainline-N2-browser-upload-evidence').mkdir(parents=True)
    with (api.root/'vite.log').open('w',encoding='utf8') as log:
        vite=subprocess.Popen([node,str(web/'node_modules/vite/bin/vite.js'),'--host','127.0.0.1','--port',str(port),'--strictPort'],
            cwd=web,env={**os.environ,'VITE_API_TARGET':api.base},stdout=log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline=time.monotonic()+30
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            while time.monotonic()<deadline and vite.poll() is None:
                try:
                    with opener.open(origin+'/api/health',timeout=2) as response:
                        if json.load(response)['ok']:break
                except OSError:time.sleep(.1)
            else:raise TimeoutError('Disposable browser frontend did not start')
            subprocess.run([node,'-e',RUN,driver,origin,str(SERVER.parent/'tools')],cwd=api.root,check=True,
                           timeout=600,creationflags=subprocess.CREATE_NO_WINDOW)
        finally:
            if vite.poll() is None:vite.terminate()
            vite.wait(timeout=15)
    checks=json.loads((api.root/'upload-browser-compat.json').read_text(encoding='utf8'))
    return {'suites':len(checks),'checks':[len(s['checks']) for s in checks], 'vite_exited':vite.poll() is not None}


if __name__=='__main__':
    asyncio.run(main(selected_override=scenario))
