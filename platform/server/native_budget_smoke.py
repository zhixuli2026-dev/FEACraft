"""The single Job deadline stops actual worker and finalizer TeX children."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

from testsupport import IsolatedServer,SERVER
from native_execution_smoke import CONTROLLED,BUSY,NORMAL,wait_compiler,wait_job,inspect_native

ADMISSION='''
from app.services.latex_handler import LatexHandler
prepare_latex=LatexHandler.prepare
async def preparing_latex(self,*args,budget,**kwargs):
 return await prepare_latex(self,*args,budget={**budget,**BUDGET_OVERRIDES},**kwargs)
LatexHandler.prepare=preparing_latex
'''

SLOW_NOISY=(r'\documentclass{article}\begin{document}\newcount\budgettick'
    r'\loop\budgettick=\pdfelapsedtime\advance\budgettick by 19661 '
    r'{\loop\ifnum\pdfelapsedtime<\budgettick\repeat}'
    r'\typeout{OWNED-OUTPUT-'+('x'*1024)+r'}\iftrue\repeat\end{document}')


def verify(url=None,resource='timeout'):
    api=IsolatedServer('native-budget',app='controlled_native_budget:app',database_url=url)
    busy=SLOW_NOISY if resource=='log_limit' else BUSY
    limits={'max_log_bytes':65536} if resource=='log_limit' else {'timeout_seconds':45}
    (api.root/'controlled_native_budget.py').write_text('BUSY='+repr(busy)+'\n'+CONTROLLED+
        ADMISSION.replace('BUDGET_OVERRIDES',repr(limits)),encoding='utf8')
    checks=[];receipts=[]
    marker='import sys;print("READY",flush=True);sys.stdin.buffer.read(1)'
    sentinel=subprocess.Popen(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-u','-c',marker],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        assert sentinel.stdout.readline().strip()==b'READY'
        with api:
            for caller in ('worker','finalizer'):
                api.create_case('Whole attempt timeout '+caller)
                # Reuse only the real busy-compiler fixture; do not call cancel
                # and do not reduce the native child's independent timeout.
                (api.root/f'mode-{api.case_id}').write_text(caller+'_cancel')
                source=busy if caller=='worker' else NORMAL
                main='E-report/latex/main.tex';api.put(main,source)
                root=next((api.root/'files/workspaces').glob(f'*/cases/{api.case_id}'))
                before=hashlib.sha256((root/main).read_bytes()).hexdigest()
                submitted=api.call('POST',api.prefix+'/latex:build',{'main':main,'recipe':'pdflatex'})
                running,child,members=wait_compiler(api,submitted['job_id'],caller)
                final=wait_job(api,submitted['job_id'],'failed')
                assert final['attempt']['reason_code']=='resource_limit',final['attempt']['reason_code']
                assert final['attempt']['process_stop']['verified']
                children=final['attempt']['child_processes']['children']
                # A timed-out child is failed with a verified stop receipt;
                # require_stopped is the stricter successful-publication gate.
                for record in children.values():
                    assert record['phase'] in {'stopped','failed'} and record['stop']['verified']
                    assert record['parent_launch_nonce']==final['attempt']['process_identity']['launch_nonce']
                    assert record['identity']['attempt_id']==final['attempt']['id'] and record['identity']['job_id']==final['id']
                    assert inspect_native({'probe':[record['identity']['native']['process']]})[0]['state']=='exited'
                assert any(c['caller']==caller and c['purpose']=='latex_compile' and c['stop']['reason']==resource for c in children.values())
                assert all(v['state']=='exited' for v in inspect_native({'probe':[m['identity'] for m in members]}))
                assert final['attempt']['outputs']['publication']['status']=='failed'
                assert not (root/submitted['pdf']).exists()
                assert hashlib.sha256((root/main).read_bytes()).hexdigest()==before and sentinel.poll() is None
                checks.append(caller+'_compiler_stopped_by_whole_attempt_'+resource+'_without_publication')
                receipts.append({'caller':caller,'captured_members':members,'final':final,'external_sentinel_alive':True})
                (api.root/'budget-native-receipts.json').write_text(json.dumps(receipts,indent=2),encoding='utf8')
                print('PASS '+checks[-1],flush=True)
    finally:sentinel.communicate(b'x',timeout=10)
    return {'passed':True,'count':len(checks),'checks':checks,'evidence':str(api.root)}


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true')
    parser.add_argument('--resource',choices=('timeout','log_limit'),default='timeout');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='native-budget-evidence-',dir=SERVER.parents[1]/'tmp'))
    results={'sqlite':await asyncio.to_thread(verify,resource=args.resource)}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        from workflow_migration_smoke import alembic
        url=make_url(Settings(_env_file=SERVER/'.env').database_url)
        assert url.drivername=='postgresql+asyncpg' and url.host in {'127.0.0.1','localhost','::1'}
        name='tmp_vf_nativebudget_'+uuid.uuid4().hex;assert re.fullmatch(r'tmp_vf_nativebudget_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10)
        created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True
            target=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,target,report,'upgrade','head')
            results['postgresql']=await asyncio.to_thread(verify,target,args.resource)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres'
                await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('NATIVE_BUDGET_OK '+json.dumps({'evidence':str(report),'counts':{k:v['count'] for k,v in results.items()}}))


if __name__=='__main__':asyncio.run(main())
