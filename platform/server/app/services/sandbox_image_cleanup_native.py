"""Fixed image deletion in the registered OUTER Linux process gate.

All original births are observed before any private PID namespace. The native
request already registered this gate and released its exact fixed command.
Only volume.ext4 is removed; the original metadata and snapshots stay intact.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

from importlib import util

SCHEMA = 'vibeflow.sandbox-image-cleanup.v1'
MAX_CONFIG = 256 * 1024
CODE = ('sandbox_image_cleanup_native.py','sandbox_disk_native.py','sandbox_recovery_native.py',
        'sandbox_export_native.py','process_identity_native.py','process_stop.py','process_gate.py')


def module(name):
    spec = util.spec_from_file_location('image_cleanup_'+name,Path(__file__).with_name(name+'.py'))
    result = util.module_from_spec(spec);spec.loader.exec_module(result)
    return result


def code():
    return {name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in CODE}


def read(path, maximum):
    return module('sandbox_export_native').read(path,maximum)


def validate(value):
    expected={'schema','root','image_root','session_id','recovery_id','intent_id','nonce','disk','image_sha256','processes','code'}
    if not isinstance(value,dict) or set(value)!=expected or value['schema']!=SCHEMA or value['code']!=code():
        raise ValueError('Image cleanup settings or code changed')
    for key in ('session_id','recovery_id','intent_id'):
        if str(uuid.UUID(value[key]))!=value[key]:raise ValueError('Invalid cleanup identity')
    for key in ('nonce','image_sha256'):
        if not isinstance(value[key],str) or not re.fullmatch('[0-9a-f]{64}',value[key]):raise ValueError('Invalid cleanup digest')
    disk=module('sandbox_disk_native');image_root=disk.root_path(value['image_root']);root=Path(value['root'])
    if (root.parent!=image_root.parent.parent/'.terminal-image-cleanup' or root.resolve()!=root
            or not re.fullmatch('[0-9a-f]{32}',root.name)):
        raise ValueError('Cleanup output is outside private node storage')
    for path in (root,*root.parents):disk.ordinary(path,directory=True)
    if json.loads(read(image_root/'disk.json',16384))!=value['disk']:raise ValueError('Original disk manifest changed')
    if not isinstance(value['processes'],list) or not 1<=len(value['processes'])<=256:
        raise ValueError('Missing original process identities')
    identity=module('process_identity_native')
    for birth in value['processes']:
        if identity.validate(birth)['platform']!='linux':raise ValueError('Native identity is not Linux')
    return root,image_root


def prepare(arguments):
    """Run ONLY after the registered outer gate receives its release."""
    if (len(arguments)!=9 or arguments[:4]!=['python3','-I','-S','-u']
            or Path(arguments[4])!=Path(__file__).resolve() or arguments[5]!='--config' or arguments[7]!='--sha256'):
        raise ValueError('Cleanup requires its fixed invocation')
    if os.getpid()==1 or os.geteuid()!=0:
        raise ValueError('Cleanup requires the trusted outer Linux gate')
    raw=read(arguments[6],MAX_CONFIG)
    if hashlib.sha256(raw).hexdigest()!=arguments[8]:raise ValueError('Cleanup configuration changed')
    value=json.loads(raw);root,image_root=validate(value)
    identity=module('process_identity_native');observations=[]
    for birth in value['processes']:
        observed=identity.probe(birth)
        if observed['state']!='exited':raise ValueError('Original native scope is not verifiably exited')
        observations.append({'birth':birth,'observation':observed})
    disk=module('sandbox_disk_native');target=image_root/'volume.ext4';deleted=False
    if os.path.lexists(target):
        with disk.Image.reopen_readonly(image_root,value['disk']) as image:
            if disk.command(['losetup','-j',str(target)]).strip():raise ValueError('Image has a live loop attachment')
            identifier=disk.command(['blkid','-p','-s','UUID','-o','value',str(target)]).decode('ascii').strip()
            if identifier!=value['disk']['uuid']:raise ValueError('Image filesystem UUID changed')
            digest,size=module('sandbox_recovery_native').digest(target,disk.IMAGE_BYTES,time.monotonic()+120)
            if digest!=value['image_sha256'] or size!=disk.IMAGE_BYTES:raise ValueError('Image differs from the retained snapshot')
            disk.inspect_image(target,image.fd,value['disk']['image'])
            if disk.command(['losetup','-j',str(target)]).strip():raise ValueError('Image became attached before unlink')
            # Only this fixed basename, under the already checked original root.
            parent=os.open(image_root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            try:
                actual=os.fstat(parent);expected=image_root.lstat()
                if (actual.st_dev,actual.st_ino)!=(expected.st_dev,expected.st_ino):raise ValueError('Image root changed')
                os.unlink('volume.ext4',dir_fd=parent);deleted=True
                os.fsync(parent)
            finally:os.close(parent)
    if os.path.lexists(target):raise ValueError('Image reappeared during cleanup')
    receipt={'schema':SCHEMA,'session_id':value['session_id'],'recovery_id':value['recovery_id'],
        'intent_id':value['intent_id'],'nonce':value['nonce'],'image_sha256':value['image_sha256'],
        'image_absent':True,'deleted_this_request':deleted,'image_verified':deleted,
        'observations':observations,'outer_gate':identity.observe(os.getpid())}
    module('sandbox_export_native').write(root,'complete.json',json.dumps(receipt,sort_keys=True).encode())
    # The receipt file is fsynced before rename; persist the directory entry
    # before the inner verifier may acknowledge completion. A sync failure must
    # fail this request even if the renamed receipt is currently readable.
    directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(directory)
    finally:os.close(directory)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--sha256',required=True)
    args=parser.parse_args();raw=read(args.config,MAX_CONFIG)
    if hashlib.sha256(raw).hexdigest()!=args.sha256:raise ValueError('Cleanup configuration changed')
    value=json.loads(raw);root,image_root=validate(value)
    result=json.loads(read(root/'complete.json',MAX_CONFIG))
    if (result['nonce']!=value['nonce'] or result['intent_id']!=value['intent_id']
            or result['image_absent'] is not True or os.path.lexists(image_root/'volume.ext4')):
        raise ValueError('Outer gate did not complete image cleanup')
    print('IMAGE_CLEANUP_RECEIPT_VERIFIED',flush=True)


if __name__=='__main__':main()
