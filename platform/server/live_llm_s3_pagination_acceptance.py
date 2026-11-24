"""Actual multi-page S3 enumeration and cursor checks; real model hash readback."""
import asyncio,json
from live_llm_job_repeat_acceptance import main

FIXTURE=r'''
import asyncio,uuid
from fastapi import Depends
from app import storage
from app.config import get_settings
from app.db import get_db
from app.deps import get_current_user
from app.routers.workspaces import get_owned_case
from app.services.s3_live_store import Client

def pagination_probe(prefix):
 client=storage.s3();assert isinstance(client,Client)
 bucket=get_settings().s3_bucket;requested=prefix+'/'
 baseline=client.list_objects_v2(Bucket=bucket,Prefix=requested)
 assert not baseline['IsTruncated']
 expected={row['Key'] for row in baseline['Contents']}
 own=uuid.uuid4().hex;private=[prefix+'/.vibeflow/attempts/'+own+'/listing/'+str(i).zfill(4)+'.bin' for i in range(1025)]
 for name in private:client.raw.put_object(Bucket=bucket,Key=name,Body=b'actual private protocol member')
 expected.update(private)
 logical=[prefix+'/Ref/pagination-'+own+'/'+str(i).zfill(2)+'.txt' for i in range(12)]
 for name in logical:client.put_object(Bucket=bucket,Key=name,Body=b'actual logical member')
 client.delete_object(Bucket=bucket,Key=logical[-1]);expected.update(logical[:-1])
 raw_calls=[];original=client._raw_pages
 def counted(*args,**kwargs):raw_calls.append(args);yield from original(*args,**kwargs)
 client._raw_pages=counted
 try:
  pages=list(client.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix=requested,PaginationConfig={'PageSize':73}))
 finally:client._raw_pages=original
 observed=[row['Key'] for page in pages for row in page['Contents']]
 assert observed==sorted(expected) and len(observed)==len(set(observed))
 assert len(pages)>14 and len(raw_calls)==1,(len(pages),len(raw_calls))
 assert all(page['KeyCount']==len(page['Contents']) and len(page['Contents'])<=73 for page in pages)
 assert all(page['IsTruncated'] for page in pages[:-1]) and not pages[-1]['IsTruncated']
 first=client.list_objects_v2(Bucket=bucket,Prefix=requested,MaxKeys=73)
 following=client.list_objects_v2(Bucket=bucket,Prefix=requested,MaxKeys=73,ContinuationToken=first['NextContinuationToken'])
 assert [row['Key'] for row in following['Contents']]==sorted(expected)[73:146]
 after=client.list_objects_v2(Bucket=bucket,Prefix=requested,MaxKeys=73,StartAfter=first['Contents'][-1]['Key'])
 assert after==following
 # A single iterator retains its assembled listing; a later one observes the
 # real intervening HEAD transition. Readers still recheck versions separately.
 iterator=client.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix=requested,PaginationConfig={'PageSize':73})
 snapshot=[next(iterator)]
 added=prefix+'/Ref/pagination-'+own+'/added-after-first-page.txt'
 client.put_object(Bucket=bucket,Key=added,Body=b'actual concurrent logical addition')
 snapshot.extend(iterator)
 assert [row['Key'] for page in snapshot for row in page['Contents']]==sorted(expected)
 current=[row['Key'] for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix=requested) for row in page['Contents']]
 assert current==sorted(expected|{added})
 subset=client.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix=prefix+'/Ref/pagination-'+own+'/',PaginationConfig={'PageSize':3})
 assert [row['Key'] for page in subset for row in page['Contents']]==sorted(set(logical[:-1])|{added})
 empty=list(client.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix=prefix+'/Ref/nonexistent-'+own+'/'))
 assert len(empty)==1 and empty[0]=={'Contents':[],'KeyCount':0,'IsTruncated':False}
 for options in ({'PageSize':0},{'PageSize':1001},{'MaxItems':5}):
  try:list(client.get_paginator('list_objects_v2').paginate(Bucket=bucket,Prefix=requested,PaginationConfig=options))
  except ValueError:pass
  else:raise AssertionError('Unsupported or invalid pagination options were accepted')
 return {'passed':True,'protocol_only':True,'actual_s3':True,'private_members':len(private),
  'merged_visible_members':len(expected),'logical_pages':len(pages),'physical_inventory_passes':len(raw_calls),
  'ordered_no_duplicates_or_omissions':True,'tombstone_hidden':True,'continuation_and_start_after_equal':True,
  'one_iterator_keeps_assembled_listing':True,'next_iterator_observes_real_mutation':True,
  'prefix_and_empty_listing_verified':True,'invalid_options_refused':True,'mock_llm_calls':0}

@app.post('/api/v1/testing/pagination/{case_id}')
async def probe_pagination(case_id:int,user=Depends(get_current_user),db=Depends(get_db)):
 case=await get_owned_case(case_id,user,db,write=True);prefix=case.s3_prefix;await db.rollback()
 return await asyncio.to_thread(pagination_probe,prefix)
'''


def scenario(api,*,store=None,**_):
    assert store is not None,'Actual owned S3 is required'
    report=api.call('POST','/testing/pagination/'+str(api.case_id),{})
    assert report['passed']
    (api.root/'s3-pagination.json').write_text(json.dumps(report,indent=2),encoding='utf8');return report


if __name__=='__main__':asyncio.run(main(selected_override=scenario,fixture_override=FIXTURE))
