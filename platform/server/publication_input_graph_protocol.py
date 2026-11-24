"""Pure graph protocol checks; synthetic records, no LLM or native acceptance."""
import asyncio,json,sys,uuid
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from app.services import publication_inputs as inputs,step_handlers
from app.models import ArtifactPublication

def identifier(value):return str(uuid.UUID(int=value))
def ref(value,current=True):return {'publication_id':identifier(value),'paths':None,'require_current':current}

class Graph:
 def __init__(self,edges):
  self.edges=edges;self.calls=Counter();self.file_checks=Counter();self.stale=set()
  self.rows={identifier(key):SimpleNamespace(id=identifier(key),case_id=1,status='committed',
   attempt_id=identifier(key+10000),kind=str(key),generation=1,manifest_sha256=str(key)) for key in edges}
 async def get(self,model,key,**_):
  assert model is ArtifactPublication;return self.rows.get(key)
 async def scalar(self,_):return SimpleNamespace(case_id=1,status='succeeded')
 async def current(self,db,case_id,kind):return None,self.rows[identifier(int(kind))]
 def inspect_files(self,prefix,row,paths):
  self.file_checks[row.id]+=1
  return Path('.'),{'evidence':{'size':1,'sha256':'a'*64}}, {'descriptor':{
   'node':int(row.kind),'source':{'fresh':True},'input_publications':self.edges[int(row.kind)]}}
 def handler(self,descriptor):
  key=descriptor['node'];graph=self
  class Handler:
   def source(self,*_):
    graph.calls[key]+=1;return {'fresh':key not in graph.stale}
  return Handler()
 async def resolve(self,selections):
  original=(inputs.inspect_files,inputs.publication.current,step_handlers.publication_handler,step_handlers.kind_for)
  inputs.inspect_files=self.inspect_files;inputs.publication.current=self.current
  step_handlers.publication_handler=self.handler;step_handlers.kind_for=lambda handler,descriptor:str(descriptor['node'])
  try:return await inputs.resolve(self,SimpleNamespace(id=1,s3_prefix='protocol-only'),selections)
  finally:
   inputs.inspect_files,inputs.publication.current,step_handlers.publication_handler,step_handlers.kind_for=original

async def rejected(graph,selections):
 try:await graph.resolve(selections)
 except inputs.PublicationInputStale:return
 raise AssertionError('unsafe publication graph accepted')

async def main():
 diamond=Graph({1:[],2:[ref(1)],3:[ref(1)],4:[ref(2),ref(3)]})
 await diamond.resolve([ref(4)])
 assert diamond.calls==Counter({1:1,2:1,3:1,4:1})
 assert diamond.file_checks[identifier(1)]==2
 diamond.stale.add(1);await rejected(diamond,[ref(4)])
 # The shared subtree is validated shallowly before it is reached at depth.
 # 1..8 is eight nodes. The second root adds eight or nine nodes above it.
 for count in (8,9):
  edges={key:([ref(key+1)] if key<8 else []) for key in range(1,9)}
  edges.update({key:[ref(key+1)] for key in range(100,100+count-1)})
  edges[100+count-1]=[ref(1)]
  graph=Graph(edges)
  if count==8:await graph.resolve([ref(1),ref(100)])
  else:await rejected(graph,[ref(1),ref(100)])
 # A historical edge is a leaf, but can still point back to an ancestor.
 cycle=Graph({1:[ref(100,False)],100:[ref(1)]})
 await rejected(cycle,[ref(1),ref(100)])
 await rejected(Graph({1:[ref(1)]}),[ref(1)])
 # Historical traversal cannot satisfy a later current freshness check.
 mixed=Graph({1:[ref(100,False)],100:[]});mixed.stale.add(100)
 await rejected(mixed,[ref(1),ref(100)])
 # All references, including reused nodes, still enforce the selected files.
 assert diamond.file_checks[identifier(1)]>=3
 result={'passed':True,'evidence_kind':'pure_protocol_synthetic_graph',
  'llm_calls':0,'mock_llm_calls':0,'native_acceptance':False,
  'checks':['diamond_source_once','files_checked_per_selection','fresh_call_revalidates',
   'cached_subtree_depth_16_allowed','cached_subtree_depth_17_rejected',
   'cached_historical_ancestor_cycle_rejected','direct_cycle_rejected','historical_not_current']}
 path=Path(__file__).resolve().parents[2]/'tmp/publication-input-graph-protocol.json'
 path.write_text(json.dumps(result,indent=2),encoding='utf8');print(json.dumps(result))
if __name__=='__main__':asyncio.run(main())
