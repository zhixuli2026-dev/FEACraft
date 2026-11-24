"""Linux-only real process crashes, locking and bounded tmpfs ENOSPC checks."""
import hashlib,importlib.util,json,os,signal,stat,subprocess,sys,tempfile
from pathlib import Path

HERE=Path(__file__).resolve().parent


def load(name):
    spec=importlib.util.spec_from_file_location('actual_'+name,HERE/'app/services'/name)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def state():return load('codex_wsl_state.py')


def request(root,number,*,large=False):
    return {'root':str(root),'relative':'users/'+str(number)+'/wsl/vibeflow-linux',
        'configuration':('#'+'x'*16382+'\n') if large else '# 测试 interrupted initialization\n'+'# UTF8 中文配置\n'*40,
        'owner':{'schema':'vibeflow.cli-account.v1','user_id':number,'runtime':'wsl','distro':'vibeflow-linux'}}


def snapshot(account):
    result={}
    if not account.exists():return result
    for index,path in enumerate(account.rglob('*')):
        assert index<64
        info=path.lstat();name=path.relative_to(account).as_posix()
        if stat.S_ISLNK(info.st_mode):result[name]={'link':os.readlink(path)}
        elif stat.S_ISREG(info.st_mode):
            assert info.st_size<=32768
            result[name]={'size':info.st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'mode':stat.S_IMODE(info.st_mode)}
        else:assert stat.S_ISDIR(info.st_mode)
    return result


def check_complete(module,value):
    account=Path(value['root'])/value['relative'];home=account/'home'
    configuration,owner=module.initialization_payload(value)
    assert (home/'config.toml').read_bytes()==configuration
    assert (account/'owner.json').read_bytes()==owner
    assert (account/'.initialize.lock').stat().st_size==0
    assert not list(account.glob('.initialize-*'))
    assert (account/'tmp').is_dir()
    for path in (home/'config.toml',account/'owner.json',account/'.initialize.lock'):
        info=path.lstat();assert info.st_uid==65534 and stat.S_IMODE(info.st_mode)==0o600 and info.st_nlink==1
    files=snapshot(account)
    assert sum(v.get('size',0) for v in files.values())==len(configuration)+len(owner)
    return files


def child(mode,path,parent):
    load('process_gate.py')._parent_death(parent)
    module=state();value=json.loads(Path(path).read_text(encoding='utf8'))
    account=Path(value['root'])/value['relative']
    if mode.startswith('crash-'):
        stage=mode[6:]
        def die():os.kill(os.getpid(),signal.SIGKILL)
        if stage in {'owner_partial','config_partial'}:
            original=os.write
            def write(fd,data):
                name=os.readlink('/proc/self/fd/'+str(fd))
                if '.initialize-' in name and name.endswith('/'+('owner.json' if stage=='owner_partial' else 'config.toml')):
                    cut=max(1,len(data)//2)
                    if stage=='config_partial':cut=data.index('测'.encode('utf8'))+1
                    count=original(fd,data[:cut]);assert count==cut;os.fsync(fd);die()
                return original(fd,data)
            module.os.write=write
        elif stage in {'owner_ready','config_ready'}:
            original=module._resume_file
            def resumed(target,data):
                original(target,data)
                if target.name==('owner.json' if stage=='owner_ready' else 'config.toml'):die()
            module._resume_file=resumed
        elif stage in {'owner_published','home_published'}:
            original=module._publish
            def published(source,target):
                original(source,target)
                if target.name==('owner.json' if stage=='owner_published' else 'home'):die()
            module._publish=published
        elif stage=='pending_created':
            original=module._sync
            def synced(directory):
                original(directory)
                if directory==account and list(account.glob('.initialize-*')):die()
            module._sync=synced
        else:raise ValueError(stage)
    result=module.initialize(value)
    assert mode=='initialize' and result['uid']==65534
    print(json.dumps(result),flush=True)


def start(mode,path):
    return subprocess.Popen([sys.executable,'-I','-S','-u',str(Path(__file__).resolve()),'child',mode,str(path),str(os.getpid())],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE)


def join(process,expected=0):
    try:out,err=process.communicate(timeout=20)
    except BaseException:
        process.kill();process.communicate(timeout=5);raise
    assert len(out)+len(err)<65536
    assert process.returncode==expected,{'returncode':process.returncode,'stderr':err.decode('utf8','replace')[-1500:]}
    return process.returncode


def matrix():
    assert os.getuid()==65534
    root=Path(tempfile.mkdtemp(prefix='vibeflow-init-recovery-',dir='/tmp'));os.chmod(root,0o700)
    module=state();report={'passed':False,'uid':os.getuid(),'root':str(root),'crashes':[],'faults':[],'mock_llm_calls':0}
    stages=('pending_created','owner_partial','owner_ready','config_partial','config_ready','owner_published','home_published')
    for number,stage in enumerate(stages,1001):
        value=request(root,number);path=root/(stage+'.json');path.write_text(json.dumps(value,sort_keys=True),encoding='utf8')
        process=start('crash-'+stage,path);join(process,-signal.SIGKILL)
        account=root/value['relative'];before=snapshot(account)
        expected=sum(map(len,module.initialization_payload(value)))
        assert sum(v.get('size',0) for v in before.values())<=expected
        assert (account/'home').exists()==(stage=='home_published')
        peers=[start('initialize',path) for _ in range(2)]
        try:
            for peer in peers:join(peer)
        finally:
            for peer in peers:
                if peer.poll() is None:peer.kill();peer.wait(timeout=5)
        after=check_complete(module,value)
        assert module.initialize(value)['uid']==65534 and snapshot(account)==after
        report['crashes'].append({'stage':stage,'pid':process.pid,'exit_code':process.returncode,
            'parallel_pids':[p.pid for p in peers],'parallel_exit_codes':[p.returncode for p in peers],
            'initialization_bytes':expected,'before':before,'after':after})
    for number,kind in enumerate(('foreign','different_input','symlink','hardlink'),2001):
        value=request(root,number);path=root/(kind+'.json');path.write_text(json.dumps(value,sort_keys=True),encoding='utf8')
        process=start('crash-config_partial',path);join(process,-signal.SIGKILL)
        account=root/value['relative'];pending=next(account.glob('.initialize-*'));configuration=pending/'home/config.toml'
        canary=root/(kind+'-canary');canary.write_bytes(b'owned-test-canary')
        if kind=='foreign':configuration.write_bytes(b'not the selected initialization')
        elif kind=='different_input':value={**value,'configuration':value['configuration']+'# changed\n'}
        else:
            # Only the file created by this isolated test is replaced.
            assert configuration.resolve().is_relative_to(root) and configuration.is_file()
            configuration.unlink()
            if kind=='symlink':configuration.symlink_to(canary)
            else:os.link(canary,configuration)
        before=snapshot(account);canary_before=canary.read_bytes()
        try:module.initialize(value)
        except (ValueError,OSError):pass
        else:raise AssertionError('Unknown or redirected native initialization was accepted')
        assert snapshot(account)==before and canary.read_bytes()==canary_before
        report['faults'].append({'name':kind,'preserved':True,'kind':'actual_filesystem_fault'})
    value=request(root,3001);account=root/value['relative'];home=account/'home'
    home.mkdir(parents=True,mode=0o700)
    for directory in (account,*account.parents):
        if directory==root.parent:break
        os.chmod(directory,0o700)
    (home/'config.toml').write_bytes(b'legacy-user-content');os.chmod(home/'config.toml',0o600)
    before=(home/'config.toml').read_bytes()
    try:module.initialize(value)
    except (ValueError,OSError):pass
    else:raise AssertionError('Unbound legacy partial home was rebuilt')
    assert (home/'config.toml').read_bytes()==before and not (account/'owner.json').exists()
    report['faults'].append({'name':'legacy_partial_home','preserved':True,'kind':'actual_filesystem_fault'})
    for number,kind in enumerate(('owner','home'),5001):
        value=request(root,number);account=root/value['relative'];observed={}
        original=module._publish
        def competing(source,target):
            if target.name==('owner.json' if kind=='owner' else 'home'):
                if kind=='owner':
                    with target.open('xb') as stream:stream.write(b'foreign-owner')
                    os.chmod(target,0o600)
                else:target.mkdir(mode=0o700)
                observed.update(source=str(source),target=str(target),inode=target.stat().st_ino)
            original(source,target)
        module._publish=competing
        try:
            try:module.initialize(value)
            except OSError as error:assert error.errno==17
            else:raise AssertionError('Atomic native publication replaced an existing target')
        finally:module._publish=original
        source,target=Path(observed['source']),Path(observed['target'])
        assert source.exists() and target.stat().st_ino==observed['inode']
        if kind=='owner':assert target.read_bytes()==b'foreign-owner'
        else:assert target.is_dir() and not list(target.iterdir()) and (source/'config.toml').is_file()
        before=snapshot(account)
        try:module.initialize(value)
        except (ValueError,OSError):pass
        else:raise AssertionError('An unknown raced publication was automatically adopted')
        assert snapshot(account)==before
        report['faults'].append({'name':kind+'_publication_race','preserved':True,'kind':'actual_filesystem_fault'})
    value=request(root,6001);account=root/value['relative'];home=account/'home'
    home.mkdir(parents=True,mode=0o700)
    for directory in (account,*account.parents):
        if directory==root.parent:break
        os.chmod(directory,0o700)
    configuration=value['configuration'].encode('utf8');legacy_marker=json.dumps(value['owner']).encode('utf8')
    assert legacy_marker!=module.initialization_payload(value)[1]
    (home/'config.toml').write_bytes(configuration);(account/'owner.json').write_bytes(legacy_marker)
    os.chmod(home/'config.toml',0o600);os.chmod(account/'owner.json',0o600)
    assert module.initialize(value)['uid']==65534
    assert (home/'config.toml').read_bytes()==configuration and (account/'owner.json').read_bytes()==legacy_marker
    report['legacy_complete']={'passed':True,'original_owner_bytes_preserved':True}
    report['passed']=True;return report


def space_work(root,parent):
    load('process_gate.py')._parent_death(parent)
    assert os.getuid()==65534 and os.getgid()==65534
    module=state();value=request(root,4001,large=True)
    filler=root/'owned-filler.bin'
    with filler.open('xb',buffering=0) as stream:
        assert stream.write(b'F'*(56*1024))==56*1024;os.fsync(stream.fileno())
    before=os.statvfs(root);assert before.f_frsize*before.f_blocks==65536
    try:module.initialize(value)
    except OSError as error:
        import errno
        assert error.errno==errno.ENOSPC;code=error.errno
    else:raise AssertionError('Actual bounded tmpfs did not exhaust capacity')
    account=root/value['relative'];pending=next(account.glob('.initialize-*'))
    fragment=pending/'home/config.toml';actual=fragment.read_bytes();expected=value['configuration'].encode('utf8')
    assert 0<len(actual)<len(expected) and expected.startswith(actual) and not (account/'home').exists()
    # Free only this test's known filler allocation, then retry the unchanged
    # production initializer against the real partial bytes.
    with filler.open('r+b') as stream:stream.truncate(0);stream.flush();os.fsync(stream.fileno())
    module.initialize(value);complete=check_complete(module,value)
    return {'passed':True,'errno':code,'uid':os.getuid(),'filesystem_bytes':65536,
        'partial_config_bytes':len(actual),'expected_config_bytes':len(expected),'after':complete,'mock_llm_calls':0}


def space_inner(outer_namespace):
    assert os.getuid()==0 and os.readlink('/proc/self/ns/mnt')!=outer_namespace
    root=Path(tempfile.mkdtemp(prefix='vibeflow-init-space-',dir='/tmp'));mounted=False
    try:
        subprocess.run(['mount','-t','tmpfs','-o','size=64k,mode=0700,uid=65534,gid=65534,nodev,nosuid','tmpfs',str(root)],check=True,timeout=10)
        mounted=True
        actual=subprocess.check_output(['stat','-f','-c','%T',str(root)],timeout=5).decode().strip();assert actual=='tmpfs'
        result=subprocess.run([sys.executable,'-I','-S','-u',str(Path(__file__).resolve()),'space_work',str(root),str(os.getpid())],
            user=65534,group=65534,extra_groups=[],capture_output=True,timeout=30)
        assert result.returncode==0,result.stderr.decode('utf8','replace')[-2000:]
        report=json.loads(result.stdout);report.update(private_mount_verified=True,filesystem=actual)
    finally:
        if mounted:subprocess.run(['umount',str(root)],check=True,timeout=10)
        assert root.resolve()==root and root.name.startswith('vibeflow-init-space-') and root.parent==Path('/tmp')
        root.rmdir()
    report['private_mount_removed']=True;return report


def space():
    assert os.getuid()==0
    namespace=os.readlink('/proc/self/ns/mnt')
    result=subprocess.run(['unshare','--mount','--pid','--fork','--mount-proc','--kill-child=SIGKILL','--propagation','private',
        sys.executable,'-I','-S','-u',str(Path(__file__).resolve()),'space_inner',namespace],capture_output=True,timeout=60)
    assert result.returncode==0,result.stderr.decode('utf8','replace')[-3000:]
    assert os.readlink('/proc/self/ns/mnt')==namespace
    return json.loads(result.stdout)


if __name__=='__main__':
    mode=sys.argv[1]
    if mode=='child':child(sys.argv[2],sys.argv[3],int(sys.argv[4]))
    elif mode=='space_work':print(json.dumps(space_work(Path(sys.argv[2]),int(sys.argv[3]))),flush=True)
    elif mode=='space_inner':print(json.dumps(space_inner(sys.argv[2])),flush=True)
    else:
        assert mode in {'matrix','space'}
        report=matrix() if mode=='matrix' else space()
        with Path(sys.argv[2]).open('x',encoding='utf8') as stream:json.dump(report,stream,indent=2)
        print('ACTUAL_NATIVE_INITIALIZATION_OK '+mode,flush=True)
