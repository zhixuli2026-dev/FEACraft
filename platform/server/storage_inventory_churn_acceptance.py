"""Actual atomic filesystem replacement during retained-byte inventory; no LLM."""
import argparse,hashlib,json,os,tempfile,time
from pathlib import Path


def main():
 parser=argparse.ArgumentParser();parser.add_argument('--expect-rejection',action='store_true');args=parser.parse_args()
 root=Path(tempfile.mkdtemp(prefix='storage-inventory-churn-',dir=Path(__file__).resolve().parents[2]/'tmp'))
 os.environ.update(VIBEFLOW_STORAGE_BACKEND='fs',VIBEFLOW_STORAGE_FS_ROOT=str(root/'files'),
  VIBEFLOW_INDEX_ROOT=str(root/'index'),VIBEFLOW_CODEX_STATE_ROOT=str(root/'cli-state'),VIBEFLOW_CODEX_WSL_STATE_ROOT='')
 from app.config import get_settings
 from app.services import storage_usage as usage,cli_private_usage
 settings=get_settings();base=root/'files/workspaces/1/cases/1/.vibeflow/native'
 base.mkdir(parents=True);target=base/'result.bin';temporary=base/'pending.tmp'
 target.write_bytes(b'r'*37);temporary.write_bytes(b'p'*83)
 original=usage._ordinary;observed=[];mode='once'
 def actual_replacement(path):
  if path.parent==base and ((mode=='once' and path==temporary) or (mode=='continual' and path.is_file())):
   destination=target if mode=='once' else base/('renamed-'+str(len(observed))+'.bin')
   path.replace(destination)
   proof={'source':path.name,'destination':destination.name,'source_gone':not path.exists(),'destination_size':destination.stat().st_size}
   observed.append(proof)
   try:return original(path)
   except FileNotFoundError:proof['actual_lstat_FileNotFoundError']=True;raise
  return original(path)
 def measure():
  deadline=time.monotonic()+10;inventory,entries=cli_private_usage.inventory(settings,deadline)
  assert inventory=={}
  return usage._measure(settings,1,deadline,None,(inventory,{},entries))
 report={'passed':False,'classification':'Actual filesystem/protocol fault acceptance, no model behavior',
  'llm_calls':0,'mock_llm_calls':0,'source_sha256':hashlib.sha256(Path(usage.__file__).read_bytes()).hexdigest()}
 print('STORAGE_INVENTORY_CHURN '+str(root),flush=True)
 try:
  usage._ordinary=actual_replacement
  try:result=measure()
  except usage.MeasurementUnavailable as error:
   report['one_atomic_replacement']={'allowed':False,'error':type(error).__name__,'cause':type(error.__cause__).__name__,'actual_operations':list(observed)}
   assert args.expect_rejection
  else:
   report['one_atomic_replacement']={'allowed':True,'bytes':result.bytes,'complete':result.complete,'actual_operations':list(observed)}
   assert not args.expect_rejection and result.bytes==83 and result.complete and len(observed)==1
  assert observed and all(p['actual_lstat_FileNotFoundError'] and p['source_gone'] for p in observed)
  assert target.read_bytes()==b'p'*83 and not temporary.exists()
  if not args.expect_rejection:
   observed.clear();mode='continual'
   try:measure()
   except usage.MeasurementUnavailable as error:
    report['continual_replacements_refused']={'error':type(error).__name__,'cause':type(error.__cause__).__name__,'actual_operations':list(observed)}
   else:raise AssertionError('An inventory with continuous disappearance was accepted')
   assert len(observed)==3 and all(p['actual_lstat_FileNotFoundError'] for p in observed)
   mode='normal';settings.storage_measurement_max_entries=1
   try:measure()
   except usage.MeasurementUnavailable:report['entry_limit_preserved']=True
   else:raise AssertionError('Retained entry limit was bypassed')
   settings.storage_measurement_max_entries=1000000
   stable=measure();assert stable.bytes==83 and stable.complete
   report['stable_after_failure']={'bytes':stable.bytes,'complete':stable.complete}
  report['passed']=True
 finally:
  usage._ordinary=original
  (root/'result.json').write_text(json.dumps(report,indent=2),encoding='utf8')
 print(json.dumps(report),flush=True)


if __name__=='__main__':main()
