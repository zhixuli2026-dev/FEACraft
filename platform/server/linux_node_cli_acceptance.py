"""Actual Node/npm and installed CLI checks; no model requests or fake inference."""
import hashlib,json,os,secrets,subprocess,tempfile
from pathlib import Path

def run():
 root=Path(tempfile.mkdtemp(prefix='linux-node-cli-'));result={'passed':False,'real_model_calls':0,'mock_llm_calls':0,'checks':[]}
 def command(args,*,cwd=root,expected=0):
  p=subprocess.run(args,cwd=cwd,capture_output=True,text=True,timeout=60)
  result.setdefault('commands',[]).append({'argv':args,'returncode':p.returncode,'stdout':p.stdout[:5000],'stderr':p.stderr[:2000]})
  assert p.returncode==expected,(args,p.returncode,p.stderr[:200])
  return p.stdout
 try:
  versions={name:command([name,'--version']).strip() for name in ['node','npm','codex','opencode','kimi']}
  assert versions=={'node':'v24.22.3','npm':'11.19.1','codex':'codex-cli 0.169.0','opencode':'opencode v2.0.32','kimi':'2.6.2'},versions
  result['versions']=versions
  from app.services.runtime_registry import environment_registry,stage_check
  registry={v['tool']:v for v in environment_registry()}
  assert all(registry[n]['available'] for n in versions) and stage_check('V',{}, {})['ok']
  result['checks'].append('installed_versions_and_platform_stage_V_discovery')
  # Exercise npm packing, lockfile resolution and offline installation of a real
  # local dependency, then execute its code with Node. No provider is involved.
  module=root/'hash-module';module.mkdir();consumer=root/'consumer';consumer.mkdir()
  (module/'package.json').write_text(json.dumps({'name':'acceptance-hash','version':'1.0.0','type':'module','exports':'./index.js'}))
  (module/'index.js').write_text("import {createHash} from 'node:crypto'; export const hash = value => createHash('sha256').update(value).digest('hex');\n")
  packed=json.loads(command(['npm','pack','--json','--ignore-scripts'],cwd=module));assert len(packed)==1
  tarball=module/packed[0]['filename'];assert tarball.is_file()
  (consumer/'package.json').write_text(json.dumps({'name':'actual-node-consumer','version':'1.0.0','type':'module','scripts':{'build':'node build.mjs'},'dependencies':{'acceptance-hash':'file:'+str(tarball)}}))
  payload=secrets.token_bytes(16384);(consumer/'input.bin').write_bytes(payload)
  (consumer/'build.mjs').write_text("""import {readFileSync,writeFileSync} from 'node:fs';
import {gzipSync,gunzipSync} from 'node:zlib';
import {execFileSync} from 'node:child_process';
import {hash} from 'acceptance-hash';
const input=readFileSync('input.bin');
const restored=gunzipSync(gzipSync(input));
const child=Number(execFileSync(process.execPath,['-p','6*7'],{encoding:'utf8'}).trim());
writeFileSync('output.json',JSON.stringify({sha256:hash(restored),bytes:restored.length,child}));
""")
  command(['npm','install','--offline','--ignore-scripts','--no-audit','--no-fund'],cwd=consumer)
  command(['npm','ci','--offline','--ignore-scripts','--no-audit','--no-fund'],cwd=consumer)
  command(['npm','run','build'],cwd=consumer)
  output=json.loads((consumer/'output.json').read_text())
  assert output=={'sha256':hashlib.sha256(payload).hexdigest(),'bytes':len(payload),'child':42}
  result['node_output']=output;result['checks'].append('real_npm_pack_lock_install_ci_Node_module_crypto_zlib_child_execution')
  for name in ['codex','opencode','kimi']:
   help_text=command([name,'--help']);assert len(help_text)>100
  command(['node','--eval','process.exit(7)'],expected=7)
  result['checks'].extend(['all_three_real_CLI_help_commands','actual_nonzero_Node_exit_propagated'])
  result['passed']=True
 finally:
  (root/'result.json').write_text(json.dumps(result,indent=2)+'\n')
  print('NODE_CLI_EVIDENCE '+str(root),flush=True);print(json.dumps(result),flush=True)
 return result

if __name__=='__main__':run()
