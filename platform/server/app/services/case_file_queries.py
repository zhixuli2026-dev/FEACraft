"""Bounded provenance of case-file queries made by actual native processes.

The complete sealed case remains visible. This projection records reads,
existence tests and directory listings, including misses before generation.
Installed runtime files are observed separately, without claiming full closure.
"""
from contextlib import contextmanager
import hashlib,json,os,re,shutil,sys,threading
from pathlib import Path,PurePosixPath

SCHEMA='vibeflow.case-file-queries.v1'
MAX_BYTES=64*1024*1024
RESOURCE_EXIT=98  # Reserved by the registered provenance helpers, never a solver exit.


class TraceBudgetExceeded(SystemExit):
    def __init__(self,observation):
        self.observation={**observation,'max_bytes':MAX_BYTES,'max_files':1024}
        print('Native case query trace exceeded its admitted bound: '+
            json.dumps(self.observation,sort_keys=True),file=sys.stderr,flush=True)
        super().__init__(RESOURCE_EXIT)


def digest(path):
    size=0;value=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1048576),b''):
            size+=len(chunk)
            if size>1024**3:raise ValueError('Case query file exceeds its bound')
            value.update(chunk)
    return {'size':size,'sha256':value.hexdigest()}


def canonical(path):
    if (not isinstance(path,str) or not path or '\\' in path or PurePosixPath(path).is_absolute()
            or '..' in PurePosixPath(path).parts or PurePosixPath(path).as_posix()!=path):
        raise ValueError('Case query path is not canonical')
    return path


def projection(source,policy):
    if policy.get('schema')!=SCHEMA or set(policy)!={'schema','paths','listings','generated','absence_inputs'}:
        raise ValueError('Unknown case query policy')
    for field in ['paths','listings','generated','absence_inputs']:
        values=policy[field]
        if not isinstance(values,list) or values!=sorted(set(values)) or len(values)>10000:
            raise ValueError('Case query policy exceeds its bound')
        for path in values:canonical(path)
    files=source['files'];directories=set(source['directories'])|{'.'}
    generated=set(policy['generated']);absences=set(policy['absence_inputs'])
    def excluded(path):
        return path.split('/')[0]=='.vibeflow' or (path not in absences and any(path==g or path.startswith(g+'/') for g in generated))
    selected={};entries={};listings={}
    for path in policy['paths']:
        if excluded(path):continue
        entries[path]='file' if path in files else 'directory' if path in directories else 'missing'
        selected[path]=files.get(path)
    for directory in policy['listings']:
        if excluded(directory):continue
        prefix='' if directory=='.' else directory+'/'
        children={}
        for path in set(files)|directories:
            if path==directory or not path.startswith(prefix) or excluded(path):continue
            tail=path[len(prefix):]
            if '/' not in tail:children[tail]='file' if path in files else 'directory'
        listings[directory]=dict(sorted(children.items())) if directory in directories else None
    return {'schema':SCHEMA,'policy':policy,'files':dict(sorted(selected.items())),
        'entries':dict(sorted(entries.items())),'listings':dict(sorted(listings.items()))}


class Trace:
    def __init__(self,stage):
        self.directory=stage/'.vibeflow';self.directory.mkdir(exist_ok=True)
        self.executable=shutil.which('strace')
        if self.executable is None:raise ValueError('Actual case provenance requires strace')
        self.tool={'path':self.executable,**digest(Path(self.executable))}

    def command(self,argv,label):
        if not re.fullmatch('[a-z][a-z0-9-]*',label):raise ValueError('Invalid case query phase')
        return [self.executable,'--kill-on-exit','-ff','-ttt','-yy','-xx','-s','4096',
            '-e','trace=%file,%process,getdents64','-o',str(self.directory/('case-query-'+label)),*argv]

    @contextmanager
    def monitor(self,child):
        finished=threading.Event();exceeded=threading.Event()
        observation={}
        def observe():
            while not finished.wait(.05):
                try:
                    files=list(self.directory.glob('case-query-*.*'))
                    size=sum(p.stat().st_size for p in files)
                    over=len(files)>1024 or size>MAX_BYTES
                    if over:observation.update(files=len(files),bytes=size)
                except OSError as error:
                    over=True;observation.update(error=type(error).__name__)
                if over:
                    exceeded.set()
                    try:child.kill()
                    except ProcessLookupError:pass
                    return
        thread=threading.Thread(target=observe,name='case-query-budget');thread.start()
        try:yield
        finally:
            finished.set();thread.join()
            # Callers may raise on the killed child's exit inside this context.
            # Report the actual observed limit instead of masking it with -9.
            if exceeded.is_set():
                raise TraceBudgetExceeded(observation)


def _decode(value):
    if value and not re.fullmatch(r'(?:\\x[0-9a-fA-F]{2})+',value):raise ValueError('Case query path encoding was truncated')
    return bytes.fromhex(value.replace('\\x','')).decode('utf8')


def _arguments(value):
    """Split syscall arguments, retaining annotated fd paths and nested structs."""
    parts=[];start=0;depth=0;quoted=False;escaped=False
    for i,char in enumerate(value):
        if quoted:
            if escaped:escaped=False
            elif char=='\\':escaped=True
            elif char=='"':quoted=False
        elif char=='"':quoted=True
        elif char in '([{<':depth+=1
        elif char in ')]}>':depth-=1
        elif char==',' and depth==0:parts.append(value[start:i].strip());start=i+1
    parts.append(value[start:].strip());return parts


def collect(stage,cwd,source,translate,*,forced_paths=(),derived_inputs=None,initial_cwds=None):
    stage=stage.resolve();frozen=source['files'];directories=set(source['directories'])|{'.'}
    derived_inputs=derived_inputs or {}
    for path,record in derived_inputs.items():
        canonical(path)
        if path not in frozen or set(record)!={'size','sha256'}:
            raise ValueError('Derived native control is not an original sealed input')
    traces=sorted((stage/'.vibeflow').glob('case-query-*.*'))
    if not traces:raise ValueError('Actual case query traces are missing')
    if len(traces)>1024:raise TraceBudgetExceeded({'files':len(traces)})
    events=[];size=0;records=[];locations={}
    for trace in traces:
        if trace.is_symlink() or not trace.is_file():raise ValueError('Case query trace is not a regular private file')
        size+=trace.stat().st_size
        if size>MAX_BYTES:raise TraceBudgetExceeded({'bytes':size,'files':len(traces)})
        pid=int(trace.name.rsplit('.',1)[1]);pending=None;count=0
        if initial_cwds is not None:
            phase=trace.name.rsplit('.',1)[0].removeprefix('case-query-')
            initial=Path(initial_cwds[phase]).resolve()
            if not initial.is_relative_to(stage):raise ValueError('Case query initial cwd escapes stage')
            locations[pid]=[str(initial)]
        with trace.open(encoding='ascii') as stream:
            for raw in stream:
                count+=1
                if len(raw)>1048576:raise ValueError('Case query event exceeds its bound')
                match=re.match(r'(\d+\.\d+) (.*)',raw.rstrip('\n'))
                if not match:raise ValueError('Case query event has no timestamp')
                stamp,body=match.groups()
                if '<unfinished ...>' in body:
                    if pending is not None:raise ValueError('Case query trace contains nested unfinished calls')
                    pending=(stamp,body.replace('<unfinished ...>',''));continue
                resumed=re.match(r'<\.\.\. (\w+) resumed>(.*)',body)
                if resumed:
                    if pending is None:raise ValueError('Case query trace resumes an unknown call')
                    stamp,prefix=pending;body=prefix+resumed.group(2);pending=None
                events.append((float(stamp),pid,body))
                if len(events)>500000:raise ValueError('Case query event limit exceeded')
        if pending is not None:raise ValueError('Case query trace ended in an incomplete call')
        records.append({'path':'.vibeflow/'+trace.name,'pid':pid,'events':count,**digest(trace)})
    queries=set(forced_paths);enumerated=set();generated=set();written=set();external=set();exclusive=set()
    reads={'open','openat','openat2','stat','lstat','newfstatat','statx','access','faccessat','faccessat2','link','linkat',
        'readlink','readlinkat','execve','execveat','statfs','statfs64','getxattr','lgetxattr','listxattr','llistxattr'}
    at_calls={'openat','openat2','newfstatat','statx','faccessat','faccessat2','readlinkat','execveat',
        'mkdirat','unlinkat','mknodat','fchmodat','fchownat','utimensat','renameat','renameat2','linkat'}
    writes={'unlink','unlinkat','mkdir','mkdirat','rmdir','truncate','chmod','fchmodat','chown','lchown','fchownat','utime','utimes','utimensat','mknod','mknodat'}
    quoted=r'"((?:\\x[0-9a-fA-F]{2})*)"';fd=r'<((?:\\x[0-9a-fA-F]{2})+)>'
    def relative(path):
        value=Path(os.path.normpath(str(path)))
        return translate(value.relative_to(stage).as_posix(),reverse=True) if value.is_relative_to(stage) else None
    def resolve(value,location,dirfd=None):
        path=Path(value)
        if not path.is_absolute():
            if dirfd is not None:
                found=re.search(fd,dirfd)
                if found:location=_decode(found.group(1))
                elif not dirfd.startswith('AT_FDCWD'):raise ValueError('Case query has an unresolved relative dirfd')
            path=Path(location)/path
        return path
    def write(path):
        p=relative(path)
        if p is not None:
            written.add(p)
            if p not in frozen and p not in directories:generated.add(p)
    for _,pid,body in sorted(events):
        match=re.match(r'(\w+)\((.*)\)\s+=\s+(.+)',body)
        if not match:continue
        call,args,result=match.groups();location=locations.setdefault(pid,[str(cwd)])
        decoded_cwd=re.search(r'AT_FDCWD'+fd,args)
        if decoded_cwd:location[0]=_decode(decoded_cwd.group(1))
        success=not result.startswith('-1 ')
        if call in {'clone','clone3','fork','vfork'} and re.match(r'\d+(?:\s|$)',result):
            locations[int(result.split()[0])]=location if 'CLONE_FS' in args else [location[0]];continue
        strings=re.findall(quoted,args)
        if call=='getcwd' and success and strings:location[0]=_decode(strings[0]);continue
        if call=='fchdir' and success:
            found=re.search(fd,args)
            if not found:raise ValueError('Case query fchdir has no decoded path')
            location[0]=_decode(found.group(1));continue
        if call=='getdents64' and success:
            found=re.match(r'\d+'+fd,args)
            if not found:raise ValueError('Case query enumeration has no decoded directory')
            p=relative(_decode(found.group(1)))
            if p is not None:enumerated.add(p)
            continue
        if not strings:continue
        parts=_arguments(args);path=resolve(_decode(strings[0]),location[0],parts[0] if call in at_calls else None)
        p=relative(path)
        if call=='chdir':
            if p is not None and not any(p==g or p.startswith(g+'/') for g in generated):queries.add(p)
            if success:location[0]=os.path.normpath(str(path))
            continue
        if call in {'execve','execveat'} and success:locations[pid]=[location[0]]
        if call in {'open','openat','openat2'} and success and any(flag in args for flag in ['O_WRONLY','O_RDWR','O_CREAT']):
            if p is not None and 'O_EXCL' in args and p not in frozen and p not in directories:exclusive.add(p)
            write(path)
        if call in writes and success:write(path)
        if call in {'rename','renameat','renameat2','link','linkat','symlink','symlinkat'} and success and len(strings)>=2:
            if call in {'rename','renameat','renameat2'}:
                if p in directories:raise ValueError('Native query provenance cannot follow a renamed initial directory')
                if p in frozen:queries.add(p)
                write(path)
            destination_fd=parts[2] if call in {'renameat','renameat2','linkat'} else parts[1] if call=='symlinkat' else None
            write(resolve(_decode(strings[1]),location[0],destination_fd))
        if call in reads and (call not in {'open','openat','openat2'} or 'O_WRONLY' not in args):
            if p is not None:
                resolved=relative(path.resolve(strict=False));candidate=resolved if resolved is not None else p
                if candidate in frozen or not any(candidate==g or candidate.startswith(g+'/') for g in generated):queries.add(candidate)
            elif success:external.add(str(path))
    absence=exclusive|{p for p in queries if p not in frozen and p not in directories and any(p==g or p.startswith(g+'/') for g in generated)}
    queries.update(exclusive)
    def ignored(p):return p.split('/')[0]=='.vibeflow' or (p not in absence and any(p==g or p.startswith(g+'/') for g in generated))
    queries={canonical(p) for p in queries if not ignored(p)}
    present=queries&set(frozen)
    queries={p for p in queries if p not in directories or not any(p=='.' or q.startswith(p+'/') for q in present)}
    # Existing files legitimately written by the observed native process retain
    # their sealed initial value as a conservative dependency. Untouched inputs
    # must still match the exact seal after execution.
    for path in present-written:
        expected=derived_inputs.get(path,{k:frozen[path][k] for k in ['size','sha256']})
        if digest(stage/translate(path))!=expected:
            raise ValueError('Queried native input changed without an observed write: '+path)
    policy={'schema':SCHEMA,'paths':sorted(queries),'listings':sorted(p for p in enumerated if not ignored(p)),
        'generated':sorted(generated),'absence_inputs':sorted(p for p in absence if not p.startswith('.vibeflow/'))}
    return {'source':projection(source,policy),'evidence':{'records':records,'bytes':size,'events':len(events),
        'external_paths_observed':sorted(external),'external_dependency_closure':False}}
