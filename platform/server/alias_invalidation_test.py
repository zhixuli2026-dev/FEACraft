"""Test-only persisted publication bundle for alias invalidation boundaries."""
FIXTURE = '''
from app.models import ArtifactPublication,ArtifactPublicationHead,JobAttempt,Job
from app.services import artifact_publication,attempt_objects
from app.services.durable_events import json_text
import hashlib,uuid
@app.post('/api/v1/fixture/alias/{case_id}')
async def seed_alias(case_id:int,db=Depends(get_db),user=Depends(get_current_user)):
 case=await get_owned_case(case_id,user,db)
 job=Job(case_id=case.id,type='shell',command='persisted publication IO fixture',status='succeeded')
 db.add(job);await db.flush()
 attempt=JobAttempt(job_id=job.id,number=1,phase='succeeded',
  publication_json=json_text({'descriptor':{'output':'alias.txt'}}))
 db.add(attempt);await db.flush()
 identifier=str(uuid.uuid4());kind='pdf-number:alias-fixture'
 record={'size':3,'sha256':hashlib.sha256(b'old').hexdigest(),'executable':False}
 manifest={'schema':artifact_publication.SCHEMA,'publication_id':identifier,'case_id':case.id,
  'attempt_id':attempt.id,'kind':kind,'generation':1,'storage':attempt_objects.binding(case.s3_prefix),
  'files':{'alias.txt':record},'descriptor':{'output':'alias.txt','generated_paths':['generated.txt']}}
 encoded=json_text(manifest).encode();folder='.vibeflow/publications/'+identifier
 row=ArtifactPublication(id=identifier,case_id=case.id,attempt_id=attempt.id,kind=kind,generation=1,
  status='committed',immutable_path=folder,manifest_sha256=hashlib.sha256(encoded).hexdigest())
 db.add(row);await db.flush()
 db.add(ArtifactPublicationHead(case_id=case.id,kind=kind,next_generation=2,version=2,active_publication_id=row.id))
 await db.commit()
 storage._s3_put(case.s3_prefix,folder+'/manifest.json',encoded)
 storage._s3_put(case.s3_prefix,folder+'/files/alias.txt',b'old')
 storage._s3_put(case.s3_prefix,'alias.txt',b'old')
 return {'publication_id':row.id,'attempt_id':attempt.id,'kind':kind}
@app.get('/api/v1/fixture/alias/{case_id}')
async def alias_state(case_id:int,db=Depends(get_db),user=Depends(get_current_user)):
 await get_owned_case(case_id,user,db)
 row=await db.get(ArtifactPublicationHead,(case_id,'pdf-number:alias-fixture'))
 return {'publication_id':row.active_publication_id,'version':row.version}
'''

