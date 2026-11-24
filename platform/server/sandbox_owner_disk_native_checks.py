"""Read actual product sandbox files after its recorded owner processes died."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys


def sha(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):result.update(block)
    return result.hexdigest()


def main():
    root=Path(sys.argv[1]);expected=sys.argv[2]
    assert len(expected)==64
    path=Path(__file__).parent/'app/services/sandbox_disk_native.py'
    spec=importlib.util.spec_from_file_location('sandbox_disk_under_test',path)
    disk=importlib.util.module_from_spec(spec);spec.loader.exec_module(disk)
    manifest=json.loads((root/'disk.json').read_text())
    initial=sha(root/'volume.ext4')
    with disk.Image.reopen_readonly(root,manifest) as volume:
        mount=volume.attach(readonly=True)
        target=mount/'case/Ref/recovery-output.txt'
        assert sha(target)==expected
        assert target.stat().st_mode&0o777==0o600
        assert (mount/'case/Ref/recovery-empty').is_dir()
    assert initial==sha(root/'volume.ext4')
    assert not disk.command(['losetup','-j',str(root/'volume.ext4')]).strip()
    (root/'owner-death-read.json').write_text(json.dumps({'actual_file_sha256':expected,
        'permissions_preserved':True,'empty_directory_preserved':True,'image_bytes_unchanged':True,
        'loop_released':True,'user_command_replayed':False}))


if __name__=='__main__':main()
