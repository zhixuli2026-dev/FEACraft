"""Actual persistent-volume creation, nobody writes, ENOSPC and SIGKILL."""
import errno
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
import uuid


def implementation():
    path=Path(__file__).parent/'app/services/sandbox_disk_native.py'
    spec=importlib.util.spec_from_file_location('actual_sandbox_disk',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


def run(root,mode):
    disk=implementation()
    if mode=='fill':
        assert os.getuid()==65534 and os.getgid()==65534
        proof=(root.parent.parent/'proof.txt').read_text()
        (root/'nested').mkdir()
        (root/'nested'/'empty').mkdir()
        (root/'nested'/'output.txt').write_text(proof)
        os.chmod(root/'nested'/'output.txt',0o600)
        limited=False
        try:
            with (root/'payload.bin').open('wb',buffering=0) as stream:
                block=b'V'*1024*1024
                for _ in range(129):stream.write(block)
        except OSError as error:
            if error.errno!=errno.ENOSPC:raise
            limited=True
        assert limited,'Filesystem accepted more than its fixed image capacity'
        print(json.dumps({'uid':os.getuid(),'gid':os.getgid(),'enospc':limited,
            'payload_bytes':(root/'payload.bin').stat().st_size}),flush=True)
        return
    if mode=='write-and-die':
        with disk.Image.create(root) as volume:
            mount=volume.attach()
            (mount/'case').mkdir()
            raw=disk.command(['unshare','--user','--map-user=65534','--map-group=65534',
                '--mount','--net','--pid','--fork','--mount-proc',sys.executable,'-I','-S','-u',
                str(Path(__file__).resolve()),str(mount/'case'),'fill'])
            writer=json.loads(raw)
            assert 0<writer['payload_bytes']<disk.IMAGE_BYTES
            (root/'writer.json').write_text(json.dumps(writer))
            (root/'ready.json').write_text(json.dumps({'manifest':volume.manifest,
                'private_mount':os.readlink('/proc/self/ns/mnt'),
                'image_bytes':volume.path.stat().st_size,'payload_bytes':writer['payload_bytes']}))
            os.kill(os.getpid(),signal.SIGKILL)
    elif mode=='recover':
        manifest=json.loads((root/'disk.json').read_text())
        image=root/'volume.ext4'
        before=digest(image)
        checks=[]
        try:disk.Image.create(root)
        except FileExistsError:checks.append('existing_image_never_reformatted')
        else:raise AssertionError('Existing image was overwritten')
        incorrect={**manifest,'uuid':str(uuid.uuid4())}
        with disk.Image.reopen_readonly(root,incorrect) as volume:
            try:volume.attach(readonly=True)
            except ValueError:checks.append('actual_filesystem_uuid_mismatch_rejected')
            else:raise AssertionError('Filesystem UUID mismatch accepted')
        with disk.Image.reopen_readonly(root,manifest) as volume:
            mount=volume.attach(readonly=True)
            assert (mount/'case/nested/output.txt').read_text()==(root/'proof.txt').read_text()
            assert (mount/'case/nested/empty').is_dir()
            assert (mount/'case/nested/output.txt').stat().st_mode&0o777==0o600
            checks.append('actual_sigkill_preserves_file_directory_and_permissions')
            try:(mount/'case/forbidden.txt').write_text('must fail')
            except OSError as error:
                assert error.errno==errno.EROFS
                checks.append('recovery_mount_is_actually_read_only')
            else:raise AssertionError('Recovery mount accepted a write')
            assert (mount/'case/payload.bin').stat().st_size==json.loads((root/'writer.json').read_text())['payload_bytes']
            checks.append('actual_enospc_payload_size_survives_abrupt_scope_exit')
        assert before==digest(image)
        checks.append('read_only_recovery_leaves_image_bytes_unchanged')
        assert not disk.command(['losetup','-j',str(image)]).strip()
        checks.append('private_mount_and_loop_attachment_released')
        (root/'checks.json').write_text(json.dumps({'passed':True,'checks':checks,'image_bytes':disk.IMAGE_BYTES,
            'llm_calls':0,'mock_llm_calls':0,'product_lifecycle_integrated':False},indent=2))
        print('PERSISTENT_VOLUME_OK '+str(root/'checks.json'),flush=True)
    else:raise ValueError(mode)


if __name__=='__main__':run(Path(sys.argv[1]),sys.argv[2])
