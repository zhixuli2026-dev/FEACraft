"""Owned real API + Vite dev UI for legacy upload ownership browser checks."""
import argparse,hashlib,json,os,shutil,socket,subprocess,time
from contextlib import ExitStack
from pathlib import Path
from testsupport import IsolatedServer,SERVER
from upload_owner_reconciliation_acceptance import FIXTURE
from app.services import process_identity_native as identity

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--s3',action='store_true');a=parser.parse_args()
    api=IsolatedServer('upload-owner-ui',app='upload_owner_ui_app:app')
    report={'passed':False,'model_calls':0,'mock_llm_calls':0,'storage':'s3' if a.s3 else 'fs'}
    print('UPLOAD_OWNER_UI_ROOT '+str(api.root),flush=True)
    try:
        with ExitStack() as stack:
            settings={};store=None
            if a.s3:
                from s3testsupport import ObjectStore
                store=ObjectStore(executable=SERVER.parents[1]/'third_party/seaweed-4.52-runtime/weed');stack.enter_context(store);settings=store.settings();report['store_root']=str(store.root)
            (api.root/'upload_owner_ui_app.py').write_text('import os\nos.environ.update('+repr(settings)+')\n'+FIXTURE,encoding='utf8')
            stack.enter_context(api);api.create_case('Owned upload ownership UI bootstrap')
            report['API_birth']=identity.observe(api.call('GET','/testing/owner-pid')['pid'])
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            web=SERVER.parent/'web';log=stack.enter_context((api.root/'vite.log').open('w',encoding='utf8'))
            process=subprocess.Popen([shutil.which('node'),str(web/'node_modules/vite/bin/vite.js'),'--host','127.0.0.1','--port',str(port),'--strictPort'],cwd=web,env=dict(os.environ,VITE_API_TARGET=api.base),stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
            report['Vite_birth']=identity.observe(process.pid)
            def stop():
                if process.poll() is None:process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            stack.callback(stop)
            report['url']='http://127.0.0.1:'+str(port)
            (api.root/'ready.json').write_text(json.dumps({'url':report['url'],'root':str(api.root),'storage':report['storage']},indent=2)+'\n',encoding='utf8')
            print('UPLOAD_OWNER_UI_URL '+report['url'],flush=True)
            until=time.monotonic()+1800
            while not (api.root/'stop').exists():
                assert process.poll() is None and time.monotonic()<until;time.sleep(.2)
            assert (api.root/'browser-result.json').is_file()
            evidence=json.loads((api.root/'browser-result.json').read_text(encoding='utf8'));report['browser_passed']=evidence['passed'];assert evidence['passed'];report['passed']=True
    finally:
        for name in ['API','Vite']:
            if name+'_birth' in report:
                until=time.monotonic()+10
                while True:
                    probe=identity.probe(report[name+'_birth'])
                    if probe['state']=='exited' or time.monotonic()>=until:break
                    time.sleep(.05)
                report[name+'_exit_probe']=probe
        if a.s3 and 'store_root' in report:report['store_stopped']=store.proc is not None and store.proc.poll() is not None
        (api.root/'ui-result.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf8')
    print('UPLOAD_OWNER_UI_DONE '+str(api.root),flush=True)

if __name__=='__main__':main()
