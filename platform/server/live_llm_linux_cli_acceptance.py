"""Host launcher; credentials travel via stdin, never Docker argv or saved config."""
import argparse,asyncio,hashlib,json,secrets,subprocess
from pathlib import Path
from live_llm_acceptance import source
ROOT=Path(__file__).resolve().parents[2]
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('review',type=Path);p.add_argument('tool',choices=['codex','opencode','kimi']);p.add_argument('--image',default='vibeflow/api:node-cli-v1-20261118');a=p.parse_args()
 review=json.loads(a.review.read_text('utf8'));assert review['classification']=='real_llm'
 for n,h in review['sources'].items():assert hashlib.sha256((ROOT/n).read_bytes()).hexdigest()==h,n
 cfg=asyncio.run(source(26));assert cfg['provider']=='deepseek' and 'api.deepseek.com' in cfg['base_url']
 folder=ROOT/'tmp'/('live-linux-cli-'+a.tool+'-'+secrets.token_hex(5));folder.mkdir()
 name='vf-'+folder.name;print('LIVE_CLI_EVIDENCE '+str(folder),flush=True)
 mount='type=bind,source='+str(ROOT/'platform/server/live_llm_linux_cli_runner.py')+',target=/app/live_cli.py,readonly'
 proc=subprocess.run(['docker','run','-i','--name',name,'--mount',mount,a.image,'python','/app/live_cli.py',a.tool],input=json.dumps(cfg),capture_output=True,text=True,encoding='utf8',timeout=270)
 assert cfg['key'] not in proc.stdout+proc.stderr
 (folder/'process-stderr.txt').write_text(proc.stderr,encoding='utf8');assert proc.returncode==0,'Container process failed'
 result=json.loads(proc.stdout)
 state=json.loads(subprocess.check_output(['docker','inspect','--format','{{json .State}}',name],text=True));assert state['Status']=='exited' and not state['Running']
 result.update(container=name,container_exit=state['ExitCode'],image=a.image)
 (folder/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
 print(json.dumps({k:result.get(k) for k in ['passed','tool','returncode','failure','upstream','container_exit']}))
 assert result['passed']
