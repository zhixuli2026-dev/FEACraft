"""Run a serial Code_Aster export in the owned Linux job scope."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys


MAX_OUTPUT = 128 * 1024 * 1024


def run(*argv):
    subprocess.run(argv,check=True)


def readonly(source, root):
    target = root/source.lstrip('/')
    target.mkdir(parents=True)
    run('/usr/bin/mount','--bind',source,str(target))
    run('/usr/bin/mount','-o','remount,bind,ro',str(target))


def result_file(root, member):
    parts = ('case',*member.split('/'))
    current = root
    for part in parts[:-1]:
        current = current/part
        if not stat.S_ISDIR(current.lstat().st_mode):
            raise ValueError('Code_Aster output has a non-directory parent: '+member)
    file = current/parts[-1]
    info = file.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or not 0<info.st_size<=MAX_OUTPUT:
        raise ValueError('Code_Aster output is not an admitted regular file: '+member)
    return file


def create_root(runtime):
    run('/usr/bin/mount','-t','tmpfs','-o','size=512m,nosuid,nodev','tmpfs','/tmp')
    root = Path('/tmp/vibeflow-code-aster')
    for name in ('etc','proc','dev','tmp','case'):
        (root/name).mkdir(parents=True)
    for path in ('/usr/bin','/usr/sbin','/usr/lib/x86_64-linux-gnu','/usr/lib64',runtime):
        readonly(path,root)
    for name in ('bin','sbin','lib','lib64'):
        (root/name).symlink_to('usr/'+name,target_is_directory=True)
    for name in ('null','zero','urandom'):
        target = root/'dev'/name
        target.touch()
        run('/usr/bin/mount','--bind','/dev/'+name,str(target))
    shutil.copyfile('/etc/ld.so.cache',root/'etc/ld.so.cache')
    (root/'etc/passwd').write_text('solver:x:0:0:Code_Aster:/tmp:/bin/sh\n',encoding='ascii')
    (root/'etc/group').write_text('solver:x:0:\n',encoding='ascii')
    return root


def main():
    parser = argparse.ArgumentParser()
    for option in ('root','config','executable'):
        parser.add_argument('--'+option,required=True)
    parser.add_argument('--state-sha256')
    args = parser.parse_args()
    workspace = Path(args.root)
    config = json.loads(Path(args.config).read_text(encoding='utf8'))
    plan = config['source']['plan']
    base = plan.get('base',{})
    state = None
    if base:
        helper = Path(__file__).resolve().with_name('code_aster_state.py')
        if hashlib.sha256(helper.read_bytes()).hexdigest()!=args.state_sha256:
            raise ValueError('Code_Aster native state helper changed')
        sys.path.insert(0,str(helper.parent))
        import code_aster_state as state
    runtime = Path(args.executable).parents[1].as_posix()
    root = create_root(runtime)
    for name in (plan['input'],*plan['inputs']):
        target = root/'case'/name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(workspace/name,target)
    for name in plan['outputs']:
        (root/'case'/name).parent.mkdir(parents=True,exist_ok=True)
    if 'input' in base:
        original = workspace/config['restart']['checkpoint_path']
        state.validate(original,config['engine_sha256'],root/'case'/base['input'])
    if 'output' in base:
        (root/'case'/base['output']).parent.mkdir(parents=True,exist_ok=True)

    # The inner PID namespace drains every solver descendant before the
    # trusted parent opens results. The outer owned scope handles cancellation.
    command = ['/usr/bin/env','-i','PATH='+runtime+'/bin:/usr/bin:/bin','LANG=C.UTF-8',
               'TMPDIR=/tmp','PYTHONNOUSERSITE=1','OMP_NUM_THREADS=1','OPENBLAS_NUM_THREADS=1',
               '/usr/bin/unshare','--pid','--fork','--kill-child=SIGKILL','--mount-proc='+str(root/'proc'),
               '/usr/sbin/chroot',str(root),'/usr/bin/setpriv','--no-new-privs','--bounding-set=-all',
               '--inh-caps=-all','--ambient-caps=-all','/usr/bin/prlimit','--as=4294967296:4294967296',
               '--fsize=134217728:134217728','/bin/sh','-c','cd /case && exec "$1" "$2"',
               'code-aster',args.executable,plan['input']]
    status = subprocess.run(command,stdin=subprocess.DEVNULL).returncode
    if status:
        return status
    files = {name:result_file(root,name) for name,kind in plan['outputs'].items() if kind!='base'}
    if 'output' in base:
        directory = base['output']
        selected = {name:result_file(root,directory+'/'+name) for name in state.MEMBERS}
        if {path.name for path in (root/'case'/directory).iterdir()}!=set(state.MEMBERS):
            raise ValueError('Code_Aster produced an unsupported native base layout')
        archive = Path('/tmp/code-aster-base.zip')
        state.pack(selected,archive,config['engine_sha256'])
        files[directory+'_base.zip'] = archive
    for name,file in files.items():
        target = workspace/name
        target.parent.mkdir(parents=True,exist_ok=True)
        with file.open('rb') as incoming, target.open('xb') as outgoing:
            shutil.copyfileobj(incoming,outgoing,length=1024*1024)
    return 0


if __name__=='__main__':
    raise SystemExit(main())
