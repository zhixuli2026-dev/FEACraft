"""Real S3 input selection/version checks, followed by real model hash readback.

Synthetic file contents exercise the storage protocol only; this does not claim
native solver execution or substitute any model/provider response.
"""
import asyncio,json
from pathlib import Path

from cae_fixtures import cavity_2d
from live_llm_job_repeat_acceptance import main

FIXTURE=r'''
import asyncio
from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from app import storage
from app.db import get_db
from app.deps import get_current_user
from app.models import User
from app.routers.workspaces import get_owned_case
from app.services.case_write import CaseWriteConflict
from app.services.mesh_publication import inspect_inputs,_source_snapshot

@app.post('/api/v1/cases/{case_id}/testing/input-view')
async def input_view(case_id:int,body:dict,user:User=Depends(get_current_user),db:AsyncSession=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=True)
 def run():
  directory='C-run/case-1'
  if body.get('seed_outputs'):
   for number in range(1,41):
    storage.put_bytes(case.s3_prefix,directory+'/'+str(number/100)+'/uniform/functionObjects/functionObjectProperties',b'protocol-only output')
  def read(root,run_dir):
   value={'source':_source_snapshot(root,run_dir),'materialized_files':sorted(p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file())}
   if body.get('mutate_optional'):
    storage.put_bytes(case.s3_prefix,directory+'/0.orig/U',b'actual concurrent optional-input addition')
   if body.get('mutate_output'):
    storage.put_bytes(case.s3_prefix,directory+'/0.4/uniform/functionObjects/functionObjectProperties',b'actual concurrent output edit')
   return value
  original=None
  if body.get('remove_required'):
   original=storage.get_bytes(case.s3_prefix,'0-caseDict/caseDict');storage.delete(case.s3_prefix,'0-caseDict/caseDict')
  try:return inspect_inputs(case.s3_prefix,read,directory)
  except CaseWriteConflict as failure:return {'refused':failure.code}
  except ValueError as failure:return {'refused_type':type(failure).__name__,'reason':str(failure)}
  finally:
   if original is not None:storage.put_bytes(case.s3_prefix,'0-caseDict/caseDict',original)
 return await asyncio.to_thread(run)
'''


def scenario(api,*,store=None,**_):
    assert store is not None,'This acceptance requires the actual owned S3 fixture'
    files=cavity_2d()
    for name,content in files.items():api.put(name,content)
    dictionary=api.call('GET',api.prefix+'/raw?path=0-caseDict/caseDict',raw=True)
    root=api.root/'reference-inputs';root.mkdir()
    for name,content in {**files,'0-caseDict/caseDict':dictionary}.items():
        path=root/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(content.encode() if isinstance(content,str) else content)
    from app.services.mesh_publication import _source_snapshot
    expected=_source_snapshot(root,'C-run/case-1')
    result=api.call('POST',api.prefix+'/testing/input-view',{'seed_outputs':True})
    assert result['source']==expected,'Narrowed S3 input view must equal the filesystem input fingerprint'
    assert not any('/uniform/functionObjects/' in name for name in result['materialized_files'])
    expected_files={name for name in files if any(name.startswith('C-run/case-1/'+part+'/') for part in ('system','constant','0','0.orig'))}
    assert set(result['materialized_files'])==expected_files|{'0-caseDict/caseDict'}
    unchanged=api.call('POST',api.prefix+'/testing/input-view',{'mutate_output':True})
    assert unchanged['source']==expected,'Generated output changes are not semantic input changes'
    conflict=api.call('POST',api.prefix+'/testing/input-view',{'mutate_optional':True})
    assert conflict=={'refused':'input_stale'},conflict
    changed=api.call('POST',api.prefix+'/testing/input-view',{})
    assert 'C-run/case-1/0.orig/U' in changed['source']['files'] and changed['source']!=expected
    missing=api.call('POST',api.prefix+'/testing/input-view',{'remove_required':True})
    assert missing=={'refused_type':'ValueError','reason':'Selected S3 input does not exist'},missing
    assert not list((api.root/'files/.case-read-views').glob('owned-*')),'Input-view cleanup retained an owned directory'
    report={'passed':True,'protocol_only':True,'actual_native_compilation_claimed':False,'mock_llm_calls':0,
        'actual_s3':True,'filesystem_fingerprint_equal':True,'irrelevant_outputs_excluded':40,
        'concurrent_output_edit_allowed':True,'concurrent_optional_input_addition_refused':True,
        'optional_input_included_on_next_read':True,'missing_required_input_refused':True,
        'all_owned_views_removed':True,'input_files':len(result['materialized_files'])}
    (api.root/'s3-input-view.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE))
