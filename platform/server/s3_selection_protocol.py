"""Synthetic storage-listing protocol only; no LLM or native solver evidence."""
import json
from pathlib import Path
from types import SimpleNamespace
from app.services import attempt_objects as objects

BASE='workspaces/1/cases/2/'
class Listing:
 def __init__(self,paths):self.paths=paths;self.queries=[]
 def get_paginator(self,name):assert name=='list_objects_v2';return self
 def paginate(self,*,Bucket,Prefix):
  assert Bucket=='protocol-only';self.queries.append(Prefix)
  rows=[{'Key':BASE+path,'Size':record[0],'ETag':record[1]} for path,record in self.paths.items() if (BASE+path).startswith(Prefix)]
  # Deliberately paginate to exercise exact-file and nested-prefix selection.
  for offset in range(0,len(rows),2):yield {'Contents':rows[offset:offset+2]}

def selected(paths,required,optional=()):
 listing=Listing(paths);original=objects.storage.s3,objects.get_settings
 objects.storage.s3=lambda:listing;objects.get_settings=lambda:SimpleNamespace(s3_bucket='protocol-only')
 try:result=objects.selection(BASE.rstrip('/'),required,False,optional_paths=optional)
 finally:objects.storage.s3,objects.get_settings=original
 return result,listing.queries

def refuse(paths,required,optional=()):
 try:selected(paths,required,optional)
 except ValueError:return
 raise AssertionError('invalid input selection accepted')

def main():
 paths={'0-caseDict/caseDict':(1,'"dict"'),'C-run/a/system/controlDict':(2,'"ctl"'),
  'C-run/a/system/fvSchemes':(3,'"schemes"'),'C-run/a/system-old/wrong':(4,'"wrong"'),
  'C-run/a/0/T':(5,'"initial"'),'.vibeflow/attempts/history/huge':(99,'"old"')}
 result,queries=selected(paths,['0-caseDict/caseDict','C-run/a/system'],['C-run/a/0','missing'])
 assert set(result)=={'0-caseDict/caseDict','C-run/a/system/controlDict','C-run/a/system/fvSchemes','C-run/a/0/T'}
 assert result['0-caseDict/caseDict']=={'size':1,'etag':'dict'}
 assert queries and BASE not in queries and all('.vibeflow' not in query for query in queries)
 overlap,queries=selected(paths,['C-run/a/system','C-run/a/system/controlDict'])
 assert len(queries)==1 and len(overlap)==2
 refuse(paths,['C-run/a/missing'])
 refuse(paths,['C-run/a/system-o'])
 empty,queries=selected(paths,[],['absent']);assert empty=={}
 assert selected({'empty':(0,'"e"')},['empty'])[0]['empty']['size']==0
 refuse({'A/file':(1,'"a"'),'A/File':(1,'"b"')},['A'])
 refuse({'A/file':(1,'"a"'),'A/file/child':(1,'"b"')},['A'])
 refuse({'A/bad.':(1,'"a"')},['A'])
 refuse({'A/file':(1,'')},['A'])
 refuse({'A/file':(-1,'"a"')},['A'])
 refuse({'A/file':(objects.workspace.MAX_BYTES+1,'"a"')},['A'])
 refuse({'A/one':(objects.workspace.MAX_BYTES,'"a"'),'A/two':(1,'"b"')},['A'])
 refuse(paths,['C-run/../a'])
 # A fresh call must see added optional inputs, removals and ETag changes.
 changed=dict(paths);changed['missing/new']=(7,'"new"');changed['C-run/a/0/T']=(5,'"changed"')
 current,_=selected(changed,['C-run/a/system'],['missing','C-run/a/0'])
 assert current['missing/new']['etag']=='new' and current['C-run/a/0/T']['etag']=='changed'
 del changed['C-run/a/system/controlDict'];del changed['C-run/a/system/fvSchemes']
 refuse(changed,['C-run/a/system'])
 report={'passed':True,'evidence_kind':'pure_protocol_synthetic_object_listing','llm_calls':0,
  'mock_llm_calls':0,'native_acceptance':False,'checks':['exact_file','directory_and_pagination',
  'prefix_sibling_excluded','required_missing_refused','optional_missing_allowed','overlaps_coalesced',
  'case_alias_refused','file_directory_collision_refused','nonportable_refused','etag_required',
  'negative_and_oversize_refused','total_size_limit','fresh_optional_addition_and_etag','removed_required_refused',
  'unrelated_archives_not_listed','zero_byte_file']}
 target=Path(__file__).resolve().parents[2]/'tmp/s3-selection-protocol.json'
 target.write_text(json.dumps(report,indent=2),encoding='utf8');print(json.dumps(report))
if __name__=='__main__':main()
