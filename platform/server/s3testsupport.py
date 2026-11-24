"""Disposable real SeaweedFS in WSL; never uses the developer bucket or ports."""
import json
import hashlib
import os
from pathlib import Path
from urllib.parse import urlsplit
import socket
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from testsupport import SERVER


# Test-only seed for cases created by releases predating provisioning journals.
# Legacy backends may read/run these cases even though new creation is refused.
LEGACY_CASE_FIXTURE = '''
from fastapi import Depends,HTTPException
from app.db import get_db
from app.deps import get_current_user
from app.models import Case,Workspace
from app import storage
from app.services.casedict import SCAFFOLD_FILES
from app.services.case_events import append_case_event
@app.post('/fixture/legacy-case')
async def legacy_case(body:dict,db=Depends(get_db),user=Depends(get_current_user)):
 workspace=await db.get(Workspace,body['workspace_id'])
 if workspace is None or workspace.user_id!=user.id:raise HTTPException(404)
 case=Case(workspace_id=workspace.id,name=body['name'],s3_prefix='')
 db.add(case);await db.flush()
 case.s3_prefix=f'workspaces/{user.id}/cases/{case.id}'
 for path,content in SCAFFOLD_FILES.items():storage.put_text(case.s3_prefix,path,content)
 append_case_event(case,'case.created','legacy-fixture')
 await db.commit()
 return {'id':case.id,'name':case.name,'workflow_state':case.workflow_state}
'''


def create_legacy_case(api,name):
    from urllib.parse import urlparse
    selected=urlparse(api.base)
    assert selected.hostname=='127.0.0.1' and selected.port not in {8000,5173}
    auth=api.call('POST','/auth/register',{'email':uuid.uuid4().hex+'@example.com',
        'name':name,'password':'isolated-test-123'})
    api.token=auth['token'];workspace=api.call('GET','/workspaces')[0]
    case=api.call('POST','/fixture/legacy-case',{'workspace_id':workspace['id'],'name':name},api_prefix=False)
    api.case_id=case['id'];api.prefix=f'/cases/{api.case_id}'
    return case


class ObjectStore:
    def __init__(self, *, executable=None, volume_max=3, restore_from=None, authenticated=False):
        if type(volume_max) is not int or not 1 <= volume_max <= 128:
            raise ValueError('Invalid disposable object-store volume limit')
        self.authenticated=authenticated
        self.volume_max=volume_max
        self.root=Path(tempfile.mkdtemp(prefix='s3-attempt-store-',dir=SERVER.parents[1]/'tmp'))
        self.access_key=uuid.uuid4().hex if authenticated else 'test'
        self.secret_key=uuid.uuid4().hex if authenticated else 'test'
        self.bucket='test-attempt-'+uuid.uuid4().hex
        self.restore_from=Path(restore_from).resolve() if restore_from is not None else None
        self.restore_records=None
        self.restore_port=None
        if self.restore_from is not None:
            workspace=(SERVER.parents[1]/'tmp').resolve()
            if not self.restore_from.is_relative_to(workspace) or self.restore_from==workspace:
                raise ValueError('Disposable restore source must be an explicitly owned workspace test store')
            saved=json.loads((self.restore_from/'store.json').read_text());self.bucket=saved['bucket']
            if not re.fullmatch('test-attempt-[0-9a-f]{32}',self.bucket):raise ValueError('Only an isolated test bucket can be restored')
            endpoint=urlsplit(saved['endpoint'])
            if endpoint.scheme!='http' or endpoint.hostname!='127.0.0.1' or endpoint.port in {8000,5173,5432,6380}:
                raise ValueError('Restored test namespace requires its original isolated loopback endpoint')
            self.restore_port=endpoint.port
        self.proc=None;self.log=None
        self.runtime=Path(executable) if executable else SERVER.parents[1]/'third_party/seaweed-runtime/weed'

    def _restore_inventory(self):
        source=self.restore_from/'data';result={};total=0
        for path in source.rglob('*'):
            if path.is_symlink() or not path.resolve().is_relative_to(source):raise ValueError('Restore source is redirected')
            if path.is_file():
                total+=path.stat().st_size
                if len(result)>=10000 or total>1024*1024*1024:raise ValueError('Disposable restore exceeds its bounds')
                result[path.relative_to(source).as_posix()]=hashlib.sha256(path.read_bytes()).hexdigest()
        return result

    def __enter__(self):
        import boto3
        from botocore.config import Config
        self.linux='/mnt/'+self.root.drive[0].lower()+self.root.as_posix()[2:]
        executable=self.runtime.resolve()
        self.executable='/mnt/'+executable.drive[0].lower()+executable.as_posix()[2:]
        sockets=[socket.socket() for _ in range(8)]
        try:
            # WSL consumes its ephemeral ports for outbound S3/proxy traffic
            # after the Windows reservation is released. Pick below that range
            # so a request cannot claim the selected listener port during boot.
            candidates=iter(random.SystemRandom().sample(range(20000,30000),200))
            for index,sock in enumerate(sockets):
                if self.restore_port is not None and index==6:
                    # Immutable publication namespaces include their endpoint.
                    # Reserve the old isolated address; never remap the receipt.
                    sock.bind(('127.0.0.1',self.restore_port));continue
                for candidate in candidates:
                    try:sock.bind(('127.0.0.1',candidate));break
                    except OSError:continue
                else:raise RuntimeError('No disposable test listener ports available')
            ports=[sock.getsockname()[1] for sock in sockets]
        finally:
            for sock in sockets:sock.close()
        self.endpoint='http://127.0.0.1:'+str(ports[6])
        if self.restore_from is None:(self.root/'data').mkdir()
        else:
            probe="""import json,sys;from pathlib import Path
r=Path(sys.argv[1]);o=json.loads((r/'owner.json').read_text());p=Path('/proc')/str(o['pid'])
if p.exists():
 s=(p/'stat').read_text().rsplit(')',1)[1].split()
 if s[19]==o['start'] and p.stat().st_uid==o['uid']:raise RuntimeError('Original test store may still be writing')
"""
            old='/mnt/'+self.restore_from.drive[0].lower()+self.restore_from.as_posix()[2:]
            subprocess.run(['wsl.exe','-d','vibeflow-linux','--exec','python3','-I','-S','-c',probe,old],
                check=True,capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
            self.restore_records=self._restore_inventory()
            shutil.copytree(self.restore_from/'data',self.root/'data',copy_function=shutil.copyfile)
            for name,digest in self.restore_records.items():
                if hashlib.sha256((self.root/'data'/name).read_bytes()).hexdigest()!=digest:raise ValueError('Restored object-store bytes differ')
            if self._restore_inventory()!=self.restore_records:raise ValueError('Original object-store data changed during copy')
            (self.root/'restore.json').write_text(json.dumps({'source':str(self.restore_from),'records':self.restore_records,'original_unchanged':True}),encoding='utf8')
        (self.root/'master.toml').write_text(
            '[master.volume_growth]\ncopy_1 = 1\ncopy_2 = 1\ncopy_3 = 1\ncopy_other = 1\nallocation_deferral = "0s"\n',encoding='utf8')
        args=[self.executable,'server','-dir='+self.linux+'/data','-ip=127.0.0.1','-ip.bind=127.0.0.1',
            '-master.port='+str(ports[0]),'-master.port.grpc='+str(ports[1]),
            '-volume.port='+str(ports[2]),'-volume.port.grpc='+str(ports[3]),
            '-filer.port='+str(ports[4]),'-filer.port.grpc='+str(ports[5]),
            '-s3','-s3.port='+str(ports[6]),'-s3.port.grpc='+str(ports[7]),'-master.volumeSizeLimitMB=32','-volume.max='+str(self.volume_max)]
        if self.authenticated:
            config = {'identities':[{'name':'test-owner','credentials':[{'accessKey':self.access_key,
                'secretKey':self.secret_key}],'actions':['Admin','Read','Write','List','Tagging']}]}
            (self.root/'s3-identity.json').write_text(json.dumps(config),encoding='utf8')
            args.append('-s3.config='+self.linux+'/s3-identity.json')
        launch="import os,json,pathlib,sys; p=os.getpid(); s=pathlib.Path('/proc/self/stat').read_text().rsplit(')',1)[1].split(); pathlib.Path('owner.json').write_text(json.dumps({'pid':p,'start':s[19],'uid':os.getuid()})); os.execv(sys.argv[1],sys.argv[1:])"
        self.log=(self.root/'weed.log').open('w',encoding='utf8')
        self.proc=subprocess.Popen(['wsl.exe','-d','vibeflow-linux','-u','nobody','--cd',self.linux,
            '--exec','/usr/bin/python3','-c',launch,*args],stdout=self.log,stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        self.client=boto3.client('s3',endpoint_url=self.endpoint,aws_access_key_id=self.access_key,aws_secret_access_key=self.secret_key,
            region_name='us-east-1',config=Config(connect_timeout=1,read_timeout=15,retries={'max_attempts':0},
                s3={'addressing_style':'path'}))
        self.raw_client=self.client
        from app.services.s3_live_store import Client
        self.client=Client(self.raw_client)
        try:
            deadline=time.monotonic()+40
            while time.monotonic()<deadline:
                if self.proc.poll() is not None:raise RuntimeError('Isolated SeaweedFS exited; see '+str(self.root/'weed.log'))
                try:self.client.list_buckets();break
                except Exception:time.sleep(.2)
            else:raise TimeoutError('Isolated SeaweedFS did not become ready')
            if self.restore_from is None:self.client.create_bucket(Bucket=self.bucket)
            elif self.bucket not in [v['Name'] for v in self.client.list_buckets()['Buckets']]:
                raise ValueError('Copied filer does not contain its original bucket')
            (self.root/'store.json').write_text(json.dumps({'endpoint':self.endpoint,'bucket':self.bucket,'ports':ports})+'\n')
            return self
        except BaseException:
            self.__exit__(*sys.exc_info());raise

    def __exit__(self,*_):
        try:
            owner=self.root/'owner.json'
            if owner.exists() and self.proc and self.proc.poll() is None:
                stop="""import json,os,pathlib,signal,sys
r=pathlib.Path(sys.argv[1]);o=json.loads((r/'owner.json').read_text());p=pathlib.Path('/proc')/str(o['pid'])
if p.exists():
 s=(p/'stat').read_text().rsplit(')',1)[1].split()
 assert s[19]==o['start'] and p.stat().st_uid==o['uid']
 assert (p/'cwd').resolve()==r.resolve()
 assert (p/'exe').resolve()==pathlib.Path(sys.argv[2]).resolve()
 os.kill(o['pid'],signal.SIGTERM)
"""
                subprocess.run(['wsl.exe','-d','vibeflow-linux','-u','nobody','--exec','/usr/bin/python3','-c',
                    stop,self.linux,self.executable],check=True,capture_output=True,timeout=10)
                self.proc.wait(timeout=40)
        finally:
            if self.log:self.log.close()
            if self.authenticated:
                (self.root/'s3-identity.json').unlink(missing_ok=True)
            if self.restore_from is not None and self.restore_records is not None:
                unchanged=self._restore_inventory()==self.restore_records
                (self.root/'restore-final.json').write_text(json.dumps({'original_unchanged':unchanged}),encoding='utf8')
                if not unchanged:raise ValueError('Original disposable store was modified')

    def settings(self):
        return {'VIBEFLOW_STORAGE_BACKEND':'s3','VIBEFLOW_ALLOW_PARTIAL_S3':'true',
            'VIBEFLOW_S3_ENDPOINT':self.endpoint,'VIBEFLOW_S3_BUCKET':self.bucket,
            'VIBEFLOW_S3_ACCESS_KEY':self.access_key,'VIBEFLOW_S3_SECRET_KEY':self.secret_key}
