"""Real Agent-approved shells, ownership faults and no replay on disposable DBs."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
import aiohttp

from testsupport import IsolatedServer,SERVER
from workflow_migration_smoke import alembic
from viz_execution_smoke import request,until
from app.services import process_identity_native as identity
from app.services.owned_process import probe_native,wsl_path

CONTROLLED='''import asyncio,contextvars,copy,json,uuid
from datetime import timedelta
from pathlib import Path
from fastapi import Depends,HTTPException
from sqlalchemy import select,update,func
from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app
from app.db import get_db,SessionLocal
from app.deps import get_current_user
from app.models import Conversation,ToolCall,ProviderProfile,NativeRequest,Job,Case
from app.routers import agent
from app.services.llm import encrypt_key
from app.services import native_request_records as records
from app.services.owned_process import OwnedProcess
from app.services.durable_events import now
root=Path(__file__).parent;mode='normal';settings={};nonces={};foreign=None
failure=contextvars.ContextVar('agent_commit_fault',default=False)
commit=AsyncSession.commit
async def committing(db):
 if failure.get():raise OSError('isolated Agent receipt failure')
 return await commit(db)
AsyncSession.commit=committing
async def resume(*args,**kwargs):
 yield 'data: '+json.dumps({'type':'stream.done'})+'\\n\\n'
agent._run_loop=resume
create=records.create
async def creating(*args,**kwargs):
 owner=await create(*args,**kwargs);settings[owner.id]=mode;return owner
records.create=creating
register=records.register
async def registering(owner,nonce,identifier,birth,distribution):
 selected=settings.get(owner.id,'normal');nonces[birth['launch_nonce']]=selected
 marker=root/('actual-'+owner.id+'.json');temporary=marker.with_suffix('.writing');temporary.write_text(json.dumps(birth));temporary.replace(marker)
 if selected in {'hold','source_change','args_change','permission_change','expired_lease'}:
  (root/('gated-'+owner.id)).touch()
  while not (root/('release-'+owner.id)).exists():await asyncio.sleep(.02)
 if selected=='bad_birth':
  birth=copy.deepcopy(birth);birth['host']['creation_time_100ns']=str(int(birth['host']['creation_time_100ns'])+1)
 if selected=='foreign_birth':birth={**birth,'host':foreign,'pid':foreign['pid']}
 if selected=='expired_lease':
  async with SessionLocal() as db:
   await db.execute(update(NativeRequest).where(NativeRequest.id==owner.id).values(lease_expires_at=now()-timedelta(seconds=1)));await db.commit()
 token=failure.set(selected=='register_commit')
 try:return await register(owner,nonce,identifier,birth,distribution)
 finally:failure.reset(token)
records.register=registering
finish=records.finish
async def finishing(owner,*args):
 token=failure.set(settings.get(owner.id)=='finish_commit')
 try:return await finish(owner,*args)
 finally:failure.reset(token)
records.finish=finishing
complete=records.complete
async def completing(owner,*args,**kwargs):
 token=failure.set(settings.get(owner.id)=='complete_commit')
 try:return await complete(owner,*args,**kwargs)
 finally:failure.reset(token)
records.complete=completing
stop=OwnedProcess.stop
async def stopping(self,**kwargs):
 result=await stop(self,**kwargs)
 if nonces.get(self.nonce)=='unverified_stop':result['verified']=False;result['error']='OSError'
 return result
OwnedProcess.stop=stopping

@app.post('/fixture/mode/{selected}')
async def selected(selected:str,body:dict={},user=Depends(get_current_user)):
 global mode,foreign
 mode=selected;foreign=body.get('foreign');return {'ok':True}

@app.post('/fixture/tool')
async def tool(body:dict,user=Depends(get_current_user),db=Depends(get_db)):
 profile=ProviderProfile(user_id=user.id,provider='custom',name='local fixture',base_url='http://127.0.0.1:9',key_ciphertext=encrypt_key('fake'),default_model='mock')
 db.add(profile);await db.flush()
 conv=Conversation(case_id=body['case_id'],provider_profile_id=profile.id,permission_mode=body.get('permission','approve_writes'));db.add(conv);await db.flush()
 tc=ToolCall(id=uuid.uuid4().hex,conversation_id=conv.id,tool='run_shell_command',args=json.dumps(body['args']),status='pending');db.add(tc);await db.commit()
 return {'cid':conv.id,'tid':tc.id}

@app.get('/fixture/state')
async def state(user=Depends(get_current_user),db=Depends(get_db)):
 rows=(await db.scalars(select(NativeRequest).where(NativeRequest.kind=='agent.shell',NativeRequest.created_by==user.id).order_by(NativeRequest.created_at))).all()
 tools=(await db.scalars(select(ToolCall))).all()
 return {'executions':[records.public(row) for row in rows],'tools':[{'id':row.id,'status':row.status,'result':row.result} for row in tools],'jobs':await db.scalar(select(func.count()).select_from(Job))}

@app.post('/fixture/change/{identifier}/{selected}')
async def change(identifier:str,selected:str,user=Depends(get_current_user),db=Depends(get_db)):
 row=await db.get(NativeRequest,identifier)
 if row.created_by!=user.id:raise HTTPException(404)
 if selected=='source_change':
  case=await db.get(Case,row.case_id);case.s3_prefix+='rebound/'
 if selected=='args_change':
  tool=await db.get(ToolCall,row.tool_call_id);tool.args=json.dumps({'command':'echo DIFFERENT'})
 if selected=='permission_change':
  conv=await db.get(Conversation,row.conversation_id);conv.permission_mode='plan'
 await db.commit();return {'ok':True}

@app.post('/fixture/expire/{identifier}')
async def expire(identifier:str,user=Depends(get_current_user),db=Depends(get_db)):
 row=await db.get(NativeRequest,identifier)
 if row.created_by!=user.id:raise HTTPException(404)
 row.lease_expires_at=now()-timedelta(seconds=1);await db.commit();return {'ok':True}
'''


def frames(data):return [json.loads(line[5:]) for line in data.decode('utf8').splitlines() if line.startswith('data:')]


async def verify(database_url=None):
    server=IsolatedServer('agent-shell',app='controlled_agent_shell:app',database_url=database_url)
    (server.root/'controlled_agent_shell.py').write_text(CONTROLLED,encoding='utf8');checks=[];births=[]
    sentinel=subprocess.Popen([sys.executable,'-I','-S','-c','import time;time.sleep(600)'],creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    foreign=identity.observe(sentinel.pid)
    try:
        with server:
            server.create_case('owned Agent')
            async def mode(value):return await request(server,'POST','/fixture/mode/'+value,{'foreign':foreign},api=False)
            async def state():return await request(server,'GET','/fixture/state',api=False)
            async def tool(command,runtime='windows',timeout=120):
                return await request(server,'POST','/fixture/tool',{'case_id':server.case_id,'args':{'runtime':runtime,'command':command,'timeout_seconds':timeout}},api=False)
            def endpoint(tool):return f"/conversations/{tool['cid']}/tool-calls/{tool['tid']}:approve"
            async def approve(tool,expect=200):return await request(server,'POST',endpoint(tool),{},raw=True,expect=expect)
            async def record(tool):return await request(server,'GET',f"/conversations/{tool['cid']}/tool-calls/{tool['tid']}/execution")
            async def exited(row):
                path=server.root/('actual-'+row['id']+'.json')
                if path.exists():
                    birth=json.loads(path.read_text());births.append(birth)
                    await until(lambda:asyncio.to_thread(identity.probe,birth['host']),lambda x:x['state']=='exited')
                    if birth['native']:
                        await until(lambda:probe_native(birth['native']['distribution'],birth['native']['process']),lambda x:x['state']=='exited')
            async def closed(tool):
                row=await record(tool);await exited(row);return row

            for runtime,command in (('windows','echo REAL_AGENT_OK真实输出'),('powershell',"Write-Output '真实输出'"),('wsl',"printf '真实输出'")):
                await mode('normal');tc=await tool(command,runtime)
                before=await state();assert not any(row['tool_call_id']==tc['tid'] for row in before['executions'])
                data=frames(await approve(tc));result=next(x for x in data if x['type']=='tool.result')
                assert result['ok'] and ('REAL_AGENT_OK真实输出' if runtime=='windows' else '真实输出') in result['result'],result
                row=await closed(tc);assert row['phase']=='succeeded' and row['cleanup_verified'] is True,row
                await approve(tc,expect=409);assert (await state())['jobs']==0
                checks.append(runtime+'_real_approved_output');print('AGENT_OK '+checks[-1],flush=True)
            prefix='"'+str(Path(getattr(sys,'_base_executable',sys.executable)).resolve())+'" -I -S -X utf8 -c "print(\''
            suffix='\')"';text='中'*(3505-len(prefix)-len(suffix));long_command=prefix+text+suffix
            assert len(long_command)==3505
            tc=await tool(long_command)
            result=next(x for x in frames(await approve(tc)) if x['type']=='tool.result');row=await closed(tc)
            assert result['ok'] is True and text in row['tool_result'] and row['cleanup_verified'] is True,{'ok':result['ok'],'retained_chars':len(row['tool_result']),'unicode_chars':row['tool_result'].count('中'),'cleanup':row['cleanup_verified']}
            checks.append('long_unicode_command_keeps_utf8_transport_bound')
            tc=await tool('[Console]::Write([char]0)','powershell')
            result=next(x for x in frames(await approve(tc)) if x['type']=='tool.result');row=await closed(tc)
            assert result['ok'] is True and '\\0' in row['tool_result'] and '\x00' not in row['tool_result'],row
            checks.append('nul_output_keeps_postgres_text_receipt_valid')
            tc=await tool('exit /b 7');result=next(x for x in frames(await approve(tc)) if x['type']=='tool.result')
            assert result['ok'] is False and 'exit=7' in result['result'],result
            assert (await closed(tc))['cleanup_verified'] is True;checks.append('nonzero_exit_preserved')
            tc=await tool('echo ONCE >> Ref\\once.txt')
            async def racing_approval():
                async with aiohttp.ClientSession() as client:
                    async with client.post(server.base+'/api/v1'+endpoint(tc),json={},headers={'Authorization':'Bearer '+server.token}) as response:
                        return response.status,await response.read()
            responses=await asyncio.gather(racing_approval(),racing_approval())
            assert sorted(status for status,_ in responses)==[200,409],responses
            winner=next(payload for status,payload in responses if status==200)
            assert next(event for event in frames(winner) if event['type']=='tool.result')['ok'] is True
            once=server.call('GET',server.prefix+'/file?path=Ref/once.txt')['content'];assert once.splitlines()==['ONCE '],repr(once)
            checks.append('concurrent_approval_executes_once');await closed(tc)
            await request(server,'DELETE',f"/conversations/{tc['cid']}")
            await request(server,'GET',f"/conversations/{tc['cid']}/tool-calls/{tc['tid']}/execution",expect=404)
            checks.append('verified_conversation_delete_preserves_fk_consistency')
            for selected in ('bad_birth','foreign_birth','register_commit','finish_commit','complete_commit','unverified_stop'):
                await mode(selected);marker=server.root/(selected+'.txt');tc=await tool('echo STARTED> '+str(marker))
                result=next(x for x in frames(await approve(tc)) if x['type']=='tool.result');row=await closed(tc)
                assert result['ok'] is False and row['phase']!='succeeded',(selected,result,row)
                if selected in {'bad_birth','foreign_birth','register_commit'}:assert not marker.exists(),selected
                assert identity.probe(foreign)['state']=='match';checks.append(selected);print('AGENT_FAULT_OK '+selected,flush=True)
                if selected=='complete_commit':await request(server,'DELETE',f"/conversations/{tc['cid']}",expect=409)
            for selected in ('hold','source_change','args_change','permission_change','expired_lease'):
                await mode(selected);marker=server.root/(selected+'.txt');tc=await tool('echo RELEASED> '+str(marker));operation=asyncio.create_task(approve(tc))
                snapshot=await until(state,lambda x:any(row['tool_call_id']==tc['tid'] for row in x['executions']))
                row=next(row for row in snapshot['executions'] if row['tool_call_id']==tc['tid'])
                await until(lambda:asyncio.to_thread((server.root/('gated-'+row['id'])).exists),bool)
                assert not marker.exists();before=time.monotonic()
                await request(server,'PUT',server.prefix+'/file',{'path':'Ref/during-shell.txt','content':'unlocked'})
                await request(server,'GET','/api/health',api=False);assert time.monotonic()-before<2
                await request(server,'DELETE',server.prefix,expect=409)
                if selected in {'source_change','args_change','permission_change'}:await request(server,'POST',f"/fixture/change/{row['id']}/{selected}",api=False)
                (server.root/('release-'+row['id'])).touch();result=next(x for x in frames(await operation) if x['type']=='tool.result')
                row=await closed(tc)
                assert bool(result['ok']) is (selected=='hold'),(selected,result,row)
                assert marker.exists() is (selected=='hold');checks.append(selected);print('AGENT_GATE_OK '+selected,flush=True)
                if selected=='source_change':await asyncio.to_thread(server.create_case,'Agent next case')
            for runtime in ('windows','powershell','wsl'):
                await mode('normal');marker=server.root/(runtime+'-running.txt')
                command=('echo RUNNING> '+str(marker)+' & ping -n 120 127.0.0.1 >nul' if runtime=='windows' else
                    "Set-Content -LiteralPath '"+str(marker)+"' -Value RUNNING; Start-Sleep -Seconds 120" if runtime=='powershell' else
                    'printf RUNNING > '+shlex.quote(wsl_path(marker))+'; sleep 120')
                tc=await tool(command,runtime);operation=asyncio.create_task(approve(tc))
                await until(lambda:asyncio.to_thread(marker.exists),bool)
                before=time.monotonic();cancelled=await request(server,'POST',f"/conversations/{tc['cid']}/messages:cancel")
                assert cancelled['ok'];cancel_frames=frames(await operation)
                assert cancel_frames[-1].get('reason')=='cancelled',cancel_frames
                result=next(x for x in cancel_frames if x['type']=='tool.result')
                row=await closed(tc);assert row['phase']=='cancelled' and row['cleanup_verified'] is True and result['status']=='cancelled',(row,result)
                assert time.monotonic()-before<15;checks.append(runtime+'_cancel_waits_for_scope');print('AGENT_CANCEL_OK '+runtime,flush=True)
            await mode('normal');tc=await tool('Start-Sleep -Seconds 120','powershell',1)
            result=next(x for x in frames(await approve(tc)) if x['type']=='tool.result');row=await closed(tc)
            assert result['ok'] is False and row['cleanup_verified'] is True and 'TimeoutExpired' in result['result'],(result,row)
            checks.append('timeout_verified_cleanup')
            tc=await tool("Write-Output ('x'*1100000)",'powershell')
            result=next(x for x in frames(await approve(tc)) if x['type']=='tool.result');row=await closed(tc)
            assert not result['ok'] and row['cleanup_verified'] is True,(result,row);checks.append('bounded_output_stops_real_scope')
            for runtime,how in (('windows','disconnect'),('wsl','disconnect'),('windows','case_cancel'),('wsl','case_cancel'),('windows','owner_death'),('wsl','owner_death')):
                await mode('normal')
                await asyncio.to_thread(server.create_case,'Agent tree '+runtime+' '+how)
                leaf_file=server.root/(runtime+'-'+how+'-leaf.py');parent_file=server.root/(runtime+'-'+how+'-parent.py');marker=server.root/(runtime+'-'+how+'-leaf.json')
                native=runtime=='wsl'
                leaf_path=wsl_path(leaf_file) if native else str(leaf_file);parent_path=wsl_path(parent_file) if native else str(parent_file)
                marker_path=wsl_path(marker) if native else str(marker)
                identity_path=wsl_path(SERVER/'app/services/process_identity_native.py') if native else str(SERVER/'app/services/process_identity_native.py')
                leaf_file.write_text('import importlib.util,json,os,time\nfrom pathlib import Path\nspec=importlib.util.spec_from_file_location("birth",'+repr(identity_path)+')\nbirth=importlib.util.module_from_spec(spec);spec.loader.exec_module(birth)\nmarker=Path('+repr(marker_path)+')\ntmp=marker.with_suffix(".writing");tmp.write_text(json.dumps(birth.observe(os.getpid())));tmp.replace(marker)\ntime.sleep(300)\n',encoding='utf8')
                parent_file.write_text('import os,subprocess,sys,time\nsubprocess.Popen([sys.executable,"-I","-S",'+repr(leaf_path)+'],**({"start_new_session":True} if os.name!="nt" else {"creationflags":subprocess.DETACHED_PROCESS|subprocess.CREATE_NEW_PROCESS_GROUP}))\ntime.sleep(300)\n',encoding='utf8')
                executable='python3' if native else '"'+str(Path(getattr(sys,'_base_executable',sys.executable)).resolve())+'"'
                tc=await tool(executable+' -I -S '+(shlex.quote(parent_path) if native else '"'+parent_path+'"'),runtime)
                operation=asyncio.create_task(approve(tc));await until(lambda:asyncio.to_thread(marker.exists),bool)
                row=await record(tc);actual=json.loads((server.root/('actual-'+row['id']+'.json')).read_text());leaf=json.loads(marker.read_text())
                if native:
                    def locate():
                        probe=SERVER/'app/services/terminal_identity_probe.py'
                        result=subprocess.run(['wsl.exe','-d',actual['native']['distribution'],'--exec','python3','-I','-S',wsl_path(probe)],
                            input=json.dumps({'root':actual['native']['process'],'reported':leaf}).encode(),capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
                        assert result.returncode==0,result.stderr;return json.loads(result.stdout)
                    leaf=await asyncio.to_thread(locate)
                if how=='disconnect':operation.cancel();await asyncio.gather(operation,return_exceptions=True)
                elif how=='case_cancel':
                    result=await request(server,'POST','/runtime/processes:cancel',{'case_id':server.case_id})
                    assert result['ok'] and result['cancelled']['agent-tool']==1,result
                    await operation
                else:
                    server.proc.kill();server.proc.wait(timeout=10);await asyncio.gather(operation,return_exceptions=True)
                if native:await until(lambda:probe_native(actual['native']['distribution'],leaf),lambda x:x['state']=='exited')
                else:await until(lambda:asyncio.to_thread(identity.probe,leaf),lambda x:x['state']=='exited')
                await exited(row)
                if how=='owner_death':
                    server.proc=subprocess.Popen([sys.executable,'-m','uvicorn',server.app,'--app-dir',str(SERVER),'--host','127.0.0.1','--port',str(server.port),'--no-access-log'],
                        cwd=server.root,env=server.env,stdout=server.log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
                    async def health():
                        try:return await request(server,'GET','/api/health',api=False)
                        except aiohttp.ClientError:return {}
                    await until(health,lambda x:x.get('ok'))
                    await request(server,'POST',f"/fixture/expire/{row['id']}",api=False)
                    unknown=await record(tc);assert unknown['phase']=='recovery_required' and unknown['cleanup_verified'] is False,unknown
                    await approve(tc,expect=409);assert await record(tc)==unknown
                    await request(server,'DELETE',server.prefix,expect=409)
                    checks.append(runtime+'_restart_no_replay')
                else:
                    row=await until(lambda:record(tc),lambda x:x['phase']=='cancelled')
                    assert row['cleanup_verified'] is True,row
                checks.append(runtime+'_detached_'+how);print('AGENT_TREE_OK '+checks[-1],flush=True)
            assert identity.probe(foreign)['state']=='match'
    finally:sentinel.terminate();sentinel.wait(timeout=5)
    value={'passed':True,'count':len(checks),'checks':checks,'actual_births_verified_exited':len(births),'foreign_sentinel_untouched':True,'evidence':str(server.root)}
    (server.root/'agent-result.json').write_text(json.dumps(value,indent=2)+'\n',encoding='utf8');return value


async def main():
    parser=argparse.ArgumentParser();parser.add_argument('--postgres',action='store_true');args=parser.parse_args()
    report=Path(tempfile.mkdtemp(prefix='agent-shell-evidence-',dir=SERVER.parents[1]/'tmp'));results={'sqlite':await verify()}
    if args.postgres:
        import asyncpg
        from sqlalchemy.engine import make_url
        from app.config import Settings
        url=make_url(Settings(_env_file=SERVER/'.env').database_url);assert url.host in {'localhost','127.0.0.1','::1'}
        name='tmp_vf_agentshell_'+uuid.uuid4().hex;assert re.fullmatch('tmp_vf_agentshell_[0-9a-f]{32}',name)
        admin=await asyncpg.connect(host=url.host,port=url.port or 5432,user=url.username,password=url.password,database='postgres',timeout=10);created=False
        try:
            await admin.execute('CREATE DATABASE "'+name+'"');created=True;temporary=url.set(database=name).render_as_string(hide_password=False)
            await asyncio.to_thread(alembic,temporary,report,'upgrade','head');results['postgresql']=await verify(temporary)
        finally:
            if created:
                assert await admin.fetchval('SELECT current_database()')=='postgres';await admin.execute('DROP DATABASE "'+name+'"')
            await admin.close()
        results['postgresql']['temporary_database_removed']=True
    (report/'result.json').write_text(json.dumps(results,indent=2)+'\n',encoding='utf8')
    print('AGENT_SHELL_OK '+json.dumps({'databases':list(results),'evidence':str(report)}),flush=True)


if __name__=='__main__':asyncio.run(main())
