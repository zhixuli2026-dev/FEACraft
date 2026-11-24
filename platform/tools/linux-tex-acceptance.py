"""Actual LaTeX recipes, bibliography, PDF downloads and owned SyncTeX queries; no LLM."""
import argparse,importlib.util,io,json,secrets,sys,uuid
from pathlib import Path
import httpx
from pypdf import PdfReader
R=Path(__file__).resolve().parents[2];sys.path.insert(0,str(R/'platform/server'))
from testsupport import IsolatedServer
spec=importlib.util.spec_from_file_location('native_cae',Path(__file__).with_name('linux-cae-acceptance.py'))
base=importlib.util.module_from_spec(spec);spec.loader.exec_module(base)

class Run(base.Run):
 def __init__(self,review,url,windows):
  self.root=R/'tmp/linux-tex-20261118'/('windows-' if windows else 'linux-');self.root=self.root.with_name(self.root.name+secrets.token_hex(5))
  (self.root/'private').mkdir(parents=True);self.windows=windows
  self.result={'passed':False,'checks':[],'jobs':[],'flows':[],'latex_jobs':[],'recipes':{},'review':review,'real_model_calls':0,'mock_llm_calls':0,'windows':windows}
  self.client=httpx.Client(base_url=url+'/api/v1',trust_env=False,timeout=180);self.save();print('TEX_EVIDENCE '+str(self.root),flush=True)

 def setup(self):
  if self.windows:
   self.register();case=self.api('POST',f'/workspaces/{self.workspace}/cases',json={'name':'Actual standalone native TeX'})
   self.case_id=case['id'];self.result['document_context']='standalone'
  else:
   prior=R/'tmp/linux-gmsh-20261118/linux-gmsh-7686a43294';evidence=json.loads((prior/'result.json').read_text('utf8'));assert evidence['passed']
   account=json.loads((prior/'private/account.json').read_text('utf8'));base.write(self.root/'private/account.json',account)
   self.client.headers['Authorization']='Bearer '+self.api('POST','/auth/login',json={'email':account['email'],'password':account['password']})['token']
   self.case_id=evidence['cases'][0];self.result['document_context']='actual_registered_Gmsh_thermal_postprocess'
  self.prefix='/cases/'+str(self.case_id);self.result['cases']=[self.case_id];self.save()

 def compile(self,recipe):
  folder='E-report/tex-'+recipe;main=folder+'/main.tex';marker='TEX'+secrets.token_hex(8)
  packages='\\usepackage{amsmath}\n'
  if recipe=='xelatex':packages+='\\usepackage{fontspec}\n\\setmainfont{Noto Serif CJK SC}\n\\usepackage[backend=biber,style=numeric]{biblatex}\n\\addbibresource{refs.bib}\n'
  if recipe=='lualatex':packages+='\\usepackage{fontspec}\n\\setmainfont{Latin Modern Roman}\n'
  ending=('\\printbibliography\n' if recipe=='xelatex' else '\\bibliographystyle{plain}\n\\bibliography{refs}\n' if recipe=='pdflatex' else '')
  content='\\documentclass{article}\n'+packages+'\\begin{document}\n\\section{Introduction}\nActual native document. $E=mc^2$.\\newpage\n\\section{Results}\n\\input{section}\n'+ending+'\\end{document}\n'
  section=marker+' and actual document verification.\n'+('温度验证。\n' if recipe=='xelatex' else '')+('Citation \\cite{thermal}.\n' if recipe!='lualatex' else '')+'\\par\n'
  self.put(main,content);self.put(folder+'/section.tex',section)
  if recipe!='lualatex':self.put(folder+'/refs.bib','@article{thermal,author={Doe, Jane},title={Native thermal verification},journal={Engineering Notes},year={2026}}\n')
  submitted=self.api('POST',self.prefix+'/latex:build',json={'main':main,'recipe':recipe},headers={'Idempotency-Key':str(uuid.uuid4())})
  self.result['latex_jobs'].append(submitted['job_id']);self.save();job=self.job(submitted['job_id'],timeout=420)
  document=self.api('GET',self.prefix+'/latex/results',params={'publication_id':submitted['publication_id']})
  assert not document['publication']['stale'] and document['publication']['aliases_applied']
  assert document['toolchain']['distro']==('vibeflow-linux' if self.windows else 'native-linux')
  assert bool(document['manifest']['input_publications']) is (not self.windows)
  base.write(self.root/(recipe+'-document.json'),document)
  children=list(job['attempt']['child_processes']['children'].values());suffix='' if self.windows else '_linux'
  assert {c['purpose'] for c in children}=={'latex_probe'+suffix,'latex_compile'+suffix}
  pdf=self.client.get(self.prefix+'/latex/artifact',params={'publication_id':submitted['publication_id'],'path':submitted['pdf']});pdf.raise_for_status()
  assert base.sha(pdf.content)==document['manifest']['artifacts'][submitted['pdf']]['sha256']
  reader=PdfReader(io.BytesIO(pdf.content));text='\n'.join(p.extract_text() for p in reader.pages)
  assert len(reader.pages)==2 and marker in text
  if recipe=='xelatex':assert '温度' in text
  if recipe!='lualatex':assert 'Native thermal verification' in text
  for path,item in document['manifest']['artifacts'].items():
   if '/latex-history/' in path:continue
   response=self.client.get(self.prefix+'/latex/artifact',params={'publication_id':submitted['publication_id'],'path':path});response.raise_for_status()
   assert base.sha(response.content)==item['sha256'];(self.root/(recipe+'-'+Path(path).name)).write_bytes(response.content)
  # The second page's first results paragraph has an actual source mapping.
  location=self.api('POST',self.prefix+'/synctex:query',json={'pdf':submitted['pdf'],'publication_id':submitted['publication_id'],'page':2,'x':134,'y':157})
  assert location['file'] in [main,folder+'/section.tex'] and location['line']>0 and location['source_current_matches']
  source=self.client.get(self.prefix+'/latex/source',params={'publication_id':submitted['publication_id'],'path':folder+'/section.tex'});source.raise_for_status();assert source.content==section.encode()
  # Read a genuine publication range with its server-provided version fence.
  params={'publication_id':submitted['publication_id'],'path':submitted['pdf']}
  head=self.client.head(self.prefix+'/latex/artifact',params=params);head.raise_for_status()
  part=self.client.get(self.prefix+'/latex/artifact',params=params,headers={'Range':'bytes=0-99','X-Vibeflow-File-Version':head.headers['x-vibeflow-file-version']})
  assert part.status_code==206 and part.content==pdf.content[:100]
  base.write(self.root/(recipe+'-document.json'),document);base.write(self.root/(recipe+'-synctex.json'),location)
  self.result['recipes'][recipe]={'job_id':job['id'],'publication_id':submitted['publication_id'],'pdf':submitted['pdf'],'main':main,'marker':marker}
  self.check('actual_'+recipe+'_PDF_bibliography_sources_SyncTeX_and_ranges',job_id=job['id'],pages=2,owned_children=len(children))

 def close(self):
  for jid in self.result['latex_jobs']:
   job=self.api('GET','/jobs/'+str(jid))
   if job['status'] in ['queued','running']:self.api('POST','/jobs/'+str(jid)+':cancel')
   self.job(jid,expected=None)
  self.client.close()

def main():
 p=argparse.ArgumentParser();p.add_argument('review');p.add_argument('--windows',action='store_true');p.add_argument('--recipes',nargs='+',choices=['pdflatex','xelatex','lualatex'],default=['pdflatex','xelatex','lualatex']);args=p.parse_args()
 review=json.loads((R/args.review).read_text('utf8'));assert review['classification']=='no_llm'
 for name,digest in review['sources'].items():assert base.sha((R/name).read_bytes())==digest,name
 server=IsolatedServer('native-tex-windows') if args.windows else None;run=None
 try:
  if server:server.__enter__()
  run=Run(args.review,server.base if server else 'http://127.0.0.1:57700',args.windows)
  if server:run.result['isolated_API']=str(server.root);run.save()
  run.setup()
  for recipe in args.recipes:run.compile(recipe)
  run.result['functional_passed']=True
 except BaseException as error:
  if run:run.result['failure']={'type':type(error).__name__,'message':str(error)[:2500]}
  raise
 finally:
  try:
   if run:run.close();run.result['all_native_stops_verified']=True
  finally:
   if server and server.proc is not None:
    server.__exit__(*sys.exc_info())
    if run:run.result['isolated_API_exited']=server.proc.poll() is not None
   if run:run.result['passed']=bool(run.result.get('functional_passed') and run.result.get('all_native_stops_verified'));run.save()

if __name__=='__main__':main()
