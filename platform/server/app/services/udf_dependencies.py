"""Data-only provenance of real GNU compiler/make/linker case inputs.

Native execution supplies actual compiler .d files, make traces and linker
traces. Case records always originate in the sealed input inventory. Installed
runtime paths are identified separately; full runtime closure is not claimed.
"""
import hashlib,json,os,re,shlex,subprocess
from pathlib import Path,PurePosixPath

SCHEMA='vibeflow.udf-compiler-inputs.v1'
QUERY_SCHEMA='vibeflow.udf-file-queries.v2'


def file_digest(path):
    size=0;value=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1048576),b''):
            size+=len(chunk)
            if size>1073741824:raise ValueError('UDF input exceeds sealed file limit')
            value.update(chunk)
    return {'size':size,'sha256':value.hexdigest()}


def _parents(files):
    return {'.'}|{'/'.join(PurePosixPath(p).parts[:i]) for p in files for i in range(1,len(PurePosixPath(p).parts))}


def query_projection(files,directories,name,policy):
    paths=policy['direct_paths'];enumerated=policy['enumerated_directories'];generated=set(policy['generated_paths'])
    absence_dependencies=set(policy.get('absence_dependencies',[]))
    if len(paths)>10000 or len(enumerated)>10000 or len(generated)>10000:raise ValueError('UDF file-query limit exceeded')
    for path in [*paths,*enumerated,*generated,*absence_dependencies]:
        if path!='.' and (PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or '\\' in path):
            raise ValueError('UDF file-query path escaped the case')
    directories=set(directories)|{'.'};entries={};selected={};listings={}
    def excluded(p):
        return (p.split('/')[0]=='.vibeflow' or compiler_cache(p,name) or
            (p not in absence_dependencies and any(p==g or p.startswith(g+'/') for g in generated)))
    for p in paths:
        if excluded(p):continue
        entries[p]='file' if p in files else 'directory' if p in directories else 'missing'
        selected[p]=files.get(p)
    for directory in enumerated:
        if excluded(directory):continue
        prefix='' if directory=='.' else directory+'/'
        children={}
        for p in set(files)|directories:
            if p==directory or not p.startswith(prefix) or excluded(p):continue
            tail=p[len(prefix):]
            if '/' not in tail:children[tail]='file' if p in files else 'directory'
        listings[directory]=dict(sorted(children.items())) if directory in directories else None
    return {'schema':SCHEMA,'policy':policy,'files':dict(sorted(selected.items())),
        'entries':dict(sorted(entries.items())),'listings':dict(sorted(listings.items()))}


def _hex_path(value):
    if not value:return ''
    if not re.fullmatch(r'(?:\\x[0-9a-fA-F]{2})+',value):raise ValueError('Truncated or unsupported strace path encoding')
    return bytes.fromhex(value.replace('\\x','')).decode('utf8')


def collect_file_queries(stage,udf,config,translate):
    """Interpret bounded actual strace data, including fork/cwd and misses."""
    stage=stage.resolve();frozen=config['source']['files'];dirs=set(config['source']['directories'])|{'.'}
    traces=sorted((stage/'.vibeflow').glob('compiler-query.*'))
    if not traces or len(traces)>1024:raise ValueError('Actual compiler process traces missing or excessive')
    events=[];size=0;records=[]
    for trace in traces:
        if trace.is_symlink() or not trace.is_file():raise ValueError('Compiler trace is not a private regular file')
        size+=trace.stat().st_size
        if size>67108864:raise ValueError('UDF compiler trace exceeds 64 MiB')
        pid=int(trace.name.rsplit('.',1)[1]);pending=None;count=0
        with trace.open(encoding='ascii') as stream:
            for raw in stream:
                count+=1
                if len(raw)>1048576:raise ValueError('Compiler trace event exceeds its bound')
                match=re.match(r'(\d+\.\d+) (.*)',raw.rstrip('\n'))
                if not match:raise ValueError('Compiler trace has no event timestamp')
                stamp,body=match.groups()
                if '<unfinished ...>' in body:
                    if pending is not None:raise ValueError('Compiler trace has nested unfinished calls')
                    pending=(stamp,body.replace('<unfinished ...>',''));continue
                resumed=re.match(r'<\.\.\. (\w+) resumed>(.*)',body)
                if resumed:
                    if pending is None:raise ValueError('Compiler trace resumes an unknown call')
                    stamp,prefix=pending;body=prefix+resumed.group(2);pending=None
                events.append((float(stamp),pid,body))
        if pending is not None:raise ValueError('Compiler trace ended with an incomplete call')
        records.append({'pid':pid,'path':'.vibeflow/'+trace.name,'bytes':trace.stat().st_size,
            'sha256':file_digest(trace)['sha256'],'events':count})
    if len(events)>500000:raise ValueError('Compiler file-query event count exceeds its bound')
    cwd={};queries=set();enumerated=set();writes=set();external=set();exclusive_absences=set()
    reads={'open','openat','openat2','stat','lstat','newfstatat','statx','access','faccessat','faccessat2',
        'readlink','readlinkat','execve','execveat','statfs','statfs64','getxattr','lgetxattr','listxattr','llistxattr'}
    at_calls={'openat','openat2','newfstatat','statx','faccessat','faccessat2','readlinkat','execveat',
        'mkdirat','unlinkat','mknodat','fchmodat','fchownat','utimensat'}
    writing={'unlink','unlinkat','mkdir','mkdirat','rmdir','truncate','chmod','fchmodat','chown','lchown',
        'fchownat','utime','utimes','utimensat','mknod','mknodat'}
    hex_string=r'"((?:\\x[0-9a-fA-F]{2})*)"';fd_path=r'<((?:\\x[0-9a-fA-F]{2})+)>'
    def relative(path):
        path=Path(os.path.normpath(str(path)))
        if path.is_relative_to(stage):return translate(path.relative_to(stage).as_posix(),reverse=True)
        return None
    for _,pid,body in sorted(events):
        matched=re.match(r'(\w+)\((.*)\)\s+=\s+(.+)',body)
        if not matched:continue  # signal/exit markers contain no file query.
        call,args,result=matched.groups();location=cwd.setdefault(pid,[str(udf)])
        at=re.search(r'AT_FDCWD'+fd_path,args)
        if at:location[0]=_hex_path(at.group(1))
        success=not result.startswith('-1 ')
        if call in {'clone','clone3','fork','vfork'} and re.match(r'\d+(?:\s|$)',result):
            child=int(result.split()[0]);cwd[child]=location if 'CLONE_FS' in args else [location[0]];continue
        strings=re.findall(hex_string,args)
        if call=='getcwd' and success and strings:location[0]=_hex_path(strings[0]);continue
        if call=='fchdir' and success:
            found=re.search(fd_path,args)
            if found:location[0]=_hex_path(found.group(1))
            else:raise ValueError('Compiler changed directory without decoded identity')
            continue
        if call=='getdents64' and success:
            found=re.match(r'\d+'+fd_path,args)
            if found:
                p=relative(_hex_path(found.group(1)))
                if p is not None:enumerated.add(p)
            else:raise ValueError('Compiler directory query has no decoded path')
            continue
        if not strings:continue
        value=_hex_path(strings[0]);path=Path(value)
        if not path.is_absolute():
            base=location[0]
            if call in at_calls:
                first=args.split(',',1)[0];found=re.search(fd_path,first)
                if found:base=_hex_path(found.group(1))
                elif not first.startswith('AT_FDCWD'):raise ValueError('Compiler relative query has an unknown dirfd')
            path=Path(base)/path
        if call=='chdir':
            p=relative(path)
            if p is not None and not any(p==g or p.startswith(g+'/') for g in writes):queries.add(p)
            if success:location[0]=os.path.normpath(str(path))
            continue
        if call in {'execve','execveat'} and success:cwd[pid]=[location[0]]
        p=relative(path)
        if call in {'open','openat','openat2'} and success and ('O_WRONLY' in args or 'O_RDWR' in args or 'O_CREAT' in args):
            if p is not None and 'O_EXCL' in args and p not in frozen and p not in dirs:exclusive_absences.add(p)
            if p is not None and p not in frozen and p not in dirs:writes.add(p)
        if call in writing and success:
            if p is not None and p not in frozen and p not in dirs:writes.add(p)
        if call in {'rename','renameat','renameat2','link','linkat','symlink','symlinkat'} and success:
            # Their destination is a derived result. Initial-case files are
            # still validated if later consumed; generated aliases are resolved.
            if len(strings)>=2:
                dest=Path(_hex_path(strings[1]));dest=dest if dest.is_absolute() else Path(location[0])/dest
                destination=relative(dest)
                if destination is not None and destination not in frozen and destination not in dirs:writes.add(destination)
        if call in reads and (call not in {'open','openat','openat2'} or 'O_WRONLY' not in args):
            if p is not None:
                # Resolve generated lnInclude links to the actual sealed header.
                resolved=relative(path.resolve(strict=False))
                candidate=resolved if resolved is not None else p
                if candidate in frozen or not any(candidate==g or candidate.startswith(g+'/') for g in writes):queries.add(candidate)
            elif success:external.add(str(path))
    # A missing input observed before it was generated remains an input. A
    # later live file at that path changes conditional generation/reuse.
    absence_dependencies=exclusive_absences|{p for p in queries if p not in frozen and p not in dirs
        and any(p==g or p.startswith(g+'/') for g in writes)}
    def ignored(p):
        return (p.split('/')[0]=='.vibeflow' or compiler_cache(p,config['name']) or
            (p not in absence_dependencies and any(p==g or p.startswith(g+'/') for g in writes)))
    queries.update(exclusive_absences)
    queries={p for p in queries if not ignored(p)}
    # A queried ancestor already proved by an observed sealed file needs no
    # separate root materialization (especially for S3).
    present={p for p in queries if p in frozen}
    queries={p for p in queries if p not in dirs or not any(p=='.' or q.startswith(p+'/') for q in present)}
    enumerated={p for p in enumerated if not ignored(p)}
    for p in present:
        actual=stage/translate(p)
        if file_digest(actual)!={k:frozen[p][k] for k in ['size','sha256']}:
            raise ValueError('Actual compiler consumed an input changed from its seal: '+p)
    policy={'query_schema':QUERY_SCHEMA,'direct_paths':sorted(queries),'search_roots':[],
        'enumerated_directories':sorted(enumerated),'generated_paths':sorted(writes),
        'absence_dependencies':sorted(p for p in absence_dependencies if not compiler_cache(p,config['name']) and not p.startswith('.vibeflow/'))}
    return query_projection(frozen,dirs,config['name'],policy),{'records':records,'bytes':size,
        'events':len(events),'external_paths_observed':sorted(external)}


def compiler_cache(path,name):
    base='UDF/'+name+'/'
    if not path.startswith(base):return False
    tail=path[len(base):]
    return (tail=='lnInclude' or tail.startswith('lnInclude/') or
        re.match(r'Make/(?:linux|darwin|mingw|windows)[^/]+(?:/|$)',tail) is not None or
        tail in {'libmyFunctionObject.so','libvibeflowVelocityBoundary.so','libvibeflowUniformAcceleration.so',
                 'libvibeflowKEpsilon.so','vibeflowDiffusionFoam',
                 'udf-build.log','udf-load.log'})


def projection(files,name,policy,directories=None):
    if policy.get('query_schema') in {QUERY_SCHEMA,'vibeflow.udf-file-queries.v1'}:
        return query_projection(files,_parents(files) if directories is None else directories,name,policy)
    direct=policy['direct_paths'];roots=policy['search_roots']
    generated=set(policy.get('generated_paths',[]))
    if len(direct)>10000 or len(roots)>256 or len(generated)>10000:raise ValueError('UDF dependency selection exceeds its bound')
    for path in [*direct,*roots,*generated]:
        if path!='.' and (PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or '\\' in path):
            raise ValueError('UDF dependency selection escaped the case')
    selected={p:files.get(p) for p in direct if p not in generated and not compiler_cache(p,name)}
    selected.update({p:r for p,r in files.items() if p not in generated and not compiler_cache(p,name)
        and any(root=='.' or p.startswith(root+'/') for root in roots)})
    return {'schema':SCHEMA,'policy':{'direct_paths':sorted(direct),'search_roots':sorted(roots),'generated_paths':sorted(generated)},
        'files':dict(sorted(selected.items()))}


def make_dependencies(text):
    """Read GCC make syntax as data, including escaped spaces/#/$/backslash."""
    if len(text)>16777216:raise ValueError('UDF dependency record exceeds its bound')
    text=re.sub(r'\\\r?\n','',text)
    # GNU ld also emits phony rules for its prerequisites. Only the first
    # logical rule belongs to the actual linked output.
    text=next((line for line in text.splitlines() if line.strip() and not line.lstrip().startswith('#')),'')
    escaped=False;separator=None
    for i,char in enumerate(text):
        if escaped:escaped=False;continue
        if char=='\\':escaped=True
        elif char==':':separator=i;break
    if separator is None:raise ValueError('Compiler dependency record has no target')
    value=[];word='';i=separator+1
    while i<len(text):
        char=text[i]
        if char=='\\' and i+1<len(text):i+=1;word+=text[i]
        elif char=='$' and i+1<len(text) and text[i+1]=='$':word+='$';i+=1
        elif char.isspace():
            if word:value.append(word);word=''
        else:word+=char
        i+=1
    if word:value.append(word)
    if not value or len(value)>10000:raise ValueError('Compiler dependency inventory is empty or too large')
    return value


def collect(stage,udf,config,translate,log):
    """Executed in the real native process, after successful actual wmake."""
    stage=stage.resolve();name=config['name'];frozen=config['source']['files']
    direct=set();search=set();external=set();dep_records=[];make_probes=[];generated=set()
    queried=None;query_evidence=None
    if config.get('dependency_tracking',{}).get('file_queries'):
        queried,query_evidence=collect_file_queries(stage,udf,config,translate)
        generated.update(queried['policy']['generated_paths'])
    def locate(value,*,directory=False,optional=False):
        path=Path(value)
        if not path.is_absolute():path=udf/path
        path=path.resolve(strict=False)
        if path.is_relative_to(stage):
            relative=translate(path.relative_to(stage).as_posix(),reverse=True)
            if relative in generated or relative.startswith('.vibeflow/') or compiler_cache(relative,name):return
            if directory:search.add(relative or '.')
            else:
                direct.add(relative);expected=frozen.get(relative)
                if expected is None:
                    if not optional or path.exists():raise ValueError('UDF consumed an input absent from its seal: '+relative)
                elif not path.is_file() or file_digest(path)!={k:expected[k] for k in ['size','sha256']}:
                    raise ValueError('UDF compiler input differs from its seal: '+relative)
        elif any(path.is_relative_to(Path(base)) for base in ['/usr/include','/usr/lib','/lib','/usr/bin','/bin']):
            external.add(str(path))
        else:raise ValueError('UDF dependency is outside its seal and managed runtime: '+str(path))
    transcript=log.read_text(encoding='utf8',errors='strict')
    lines=re.sub(r'\\\r?\n',' ',transcript).splitlines();commands=[];cpp=set()
    for line in lines:
        matched=re.match(r"Reading makefile '(.+)'(?:\s|\.)",line.strip())
        if matched:locate(matched.group(1),optional=True)
        try:args=shlex.split(line.strip())
        except ValueError:continue
        if not args:continue
        if Path(args[0]).name=='g++':commands.append(args)
        if Path(args[0]).name=='cpp' and '|' in args:
            probe=args[:args.index('|')]
            if any(v in {'Make/files','Make/options'} for v in probe):cpp.add(tuple(probe))
        # --trace emits actual expanded preprocessor commands, including ones
        # normally hidden with @. Replay only this fixed cpp dependency query.
        if len(args)==1 and re.search(r'\.(?:o|a|so)(?:\.\d+)*$',args[0]):
            candidate=Path(args[0]);candidate=candidate if candidate.is_absolute() else udf/candidate
            if candidate.is_file():commands.append(['LINK_INPUT',args[0]])
    if config.get('udf_kind')=='solver':
        expected=udf/'vibeflowDiffusionFoam'
        linked=any('-c' not in args and '-shared' not in args and '-o' in args
                   and (udf/args[args.index('-o')+1]).resolve()==expected.resolve() for args in commands)
    else:
        linked=any('-shared' in args for args in commands)
    if not any('-c' in args for args in commands) or not linked:
        raise ValueError('Actual compiler/linker commands are missing from wmake trace')
    dep_files=[]
    for args in commands:
        if '-c' in args and '-o' in args:
            output=Path(args[args.index('-o')+1]);output=output if output.is_absolute() else udf/output
            for file in [output,output.with_suffix('.d')]:
                if not file.resolve().is_relative_to(stage):raise ValueError('UDF compiler output escaped its stage')
                generated.add(translate(file.resolve().relative_to(stage).as_posix(),reverse=True))
            dep_files.append(output.with_suffix('.d'))
    if not dep_files:raise ValueError('Actual compiler emitted no dependency records')
    for file in sorted(set(dep_files)):
        data=file.read_bytes();dep_records.append({'path':translate(file.relative_to(stage).as_posix(),reverse=True),
            'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        for value in make_dependencies(data.decode('utf8')):locate(value)
    link=stage/'.vibeflow/link-inputs.d';data=link.read_bytes()
    dep_records.append({'kind':'linker','path':'.vibeflow/link-inputs.d','size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    for value in make_dependencies(data.decode('utf8')):locate(value)
    for args in commands:
        if args[0]=='LINK_INPUT':locate(args[1]);continue
        i=1
        while i<len(args):
            arg=args[i];value=None
            for flag in ['-iquote','-isystem','-idirafter','-I','-L']:
                if arg==flag:
                    i+=1
                    if i>=len(args):raise ValueError('Compiler search flag has no directory')
                    value=args[i];break
                if arg.startswith(flag) and len(arg)>len(flag):value=arg[len(flag):];break
            if value is not None:locate(value,directory=True)
            i+=1
    if len(cpp)!=2:raise ValueError('wmake did not expose both actual Make preprocessors')
    for command in sorted(cpp):
        args=list(command)
        # An argv list is passed directly to the real cpp, never to a shell.
        args[0]=config['cpp_path']
        result=subprocess.run([*args,'-M','-MT','udf-make-input'],cwd=udf,capture_output=True,timeout=30)
        if result.returncode or len(result.stdout)>16777216:raise ValueError('Actual Make input dependency probe failed')
        for value in make_dependencies(result.stdout.decode('utf8')):locate(value)
        make_probes.append({'argv':[*args,'-M','-MT','udf-make-input'],'returncode':result.returncode,
            'dependency_sha256':hashlib.sha256(result.stdout).hexdigest()})
    policy={'direct_paths':sorted(direct),'search_roots':sorted(search),'generated_paths':sorted(generated)}
    if queried is not None:
        policy={**queried['policy'],'direct_paths':sorted(set(queried['policy']['direct_paths'])|direct),
            'generated_paths':sorted(generated)}
    value=projection(frozen,name,policy,config['source']['directories'])
    value.update(helper_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        compiler_dependency_records=dep_records,make_preprocessor_probes=make_probes,
        managed_runtime_paths_observed=sorted(external),complete_runtime_closure=False)
    if query_evidence is not None:value['file_query_evidence']=query_evidence
    return value
