"""Standard-library WSL compiler and recorder; no application imports or shell."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import signal
import subprocess
import sys
import sysconfig
import threading
import xml.etree.ElementTree as ET

RECIPES=('xelatex','pdflatex','lualatex')
TRUSTED=('/usr/share/texlive','/usr/share/texmf','/usr/local/share/texmf',
    '/usr/share/fonts','/usr/local/share/fonts','/usr/share/fontconfig','/etc/texmf','/etc/fonts','/var/lib/texmf','/var/cache/fontconfig',
    '/usr/lib','/usr/share/perl','/usr/share/perl5','/usr/share/biber','/usr/share/locale',
    '/usr/share/icu','/usr/share/misc','/usr/share/zoneinfo','/usr/share/ghostscript','/usr/share/poppler')
RUNTIME_CONFIG={'/etc/ld.so.cache','/etc/localtime','/etc/nsswitch.conf','/etc/passwd',
    '/etc/group','/etc/papersize','/etc/paperspecs','/etc/machine-id','/etc/locale.alias'}
ALIAS_RESERVED = {'bin','boot','dev','etc','lib','lib32','lib64','proc','run','sbin','sys','usr','var'}
ALIAS_RUNTIME = ('/usr/bin','/usr/sbin','/usr/lib64','/usr/lib/libbtparse.so.2','/usr/lib/texlive','/usr/lib/ghostscript',
                 '/usr/lib/locale','/usr/share','/usr/local','/etc/alternatives','/etc/fonts','/etc/texmf',
                 '/var/lib/texmf','/var/cache/fontconfig')
ALIAS_DEVICES = ('null','zero','random','urandom','tty')
ALIAS_ENV = {'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':'/tmp','LANG':'C.UTF-8'}


def alias_name(value):
    path = PurePosixPath(value)
    if (not value.startswith('/') or value.startswith('//') or len(value) > 384
            or path.as_posix() != value or len(path.parts) < 2 or '..' in path.parts
            or path.parts[1] in ALIAS_RESERVED or any(ord(char) < 32 or char in '\\<>\x7f' for char in value)):
        raise ValueError('LaTeX input alias must be an absolute file path outside system directories')
    return value


def _mount(*args):
    subprocess.run(['/usr/bin/mount', *args], check=True)


def _bind(root, source, target, *, readonly=True):
    destination = root / str(target).lstrip('/')
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        destination.mkdir(exist_ok=True)
    else:
        destination.touch(exist_ok=False)
    _mount('--bind', str(source), str(destination))
    if readonly:
        _mount('-o', 'remount,bind,ro', str(destination))


def _enter_alias_root(config, aliases, files):
    """Expose only registered input paths inside this compilation's mount namespace."""
    folder = config.parent
    root = folder / 'native-root'
    root.mkdir(exist_ok=False)
    _mount('-t', 'tmpfs', '-o', 'size=8m,nosuid,nodev', 'tmpfs', str(root))
    runtime = (*ALIAS_RUNTIME, sysconfig.get_path('stdlib'),
               '/usr/lib/' + str(sysconfig.get_config_var('MULTIARCH')))
    for value in dict.fromkeys(runtime):
        path = Path(value)
        if path.exists() and (path == Path('/usr/local') or not path.is_relative_to('/usr/local')):
            _bind(root, path, path)
    for name in ('bin','sbin','lib','lib64'):
        path = Path('/') / name
        if path.is_symlink():
            (root / name).symlink_to(os.readlink(path))
        elif path.is_dir():
            _bind(root, path, path)
    (root / 'etc').mkdir(exist_ok=True)
    for name in RUNTIME_CONFIG - {'/etc/passwd','/etc/group','/etc/machine-id'}:
        path = Path(name)
        if path.is_file():
            shutil.copyfile(path, root / name.lstrip('/'))
    (root / 'etc/passwd').write_text('latex:x:0:0:LaTeX:/tmp:/bin/sh\n', encoding='ascii')
    (root / 'etc/group').write_text('latex:x:0:\n', encoding='ascii')
    for name in ALIAS_DEVICES:
        path = Path('/dev') / name
        _bind(root, path, path, readonly=False)
    (root / 'tmp').mkdir()
    (root / 'proc').mkdir()
    _mount('-t', 'proc', 'proc', str(root / 'proc'))
    _bind(root, folder, folder, readonly=False)
    for target, relative in aliases.items():
        path = Path(alias_name(target))
        if path.is_relative_to(folder) or folder.is_relative_to(path) or relative not in files:
            raise ValueError('LaTeX input alias overlaps its workspace or has no frozen source')
        source = folder / 'case' / relative
        if not source.resolve().is_relative_to(folder / 'case') or not source.is_file():
            raise ValueError('LaTeX input alias source is outside its frozen case')
        _bind(root, source, path)
    os.chroot(root)
    os.chdir(folder)


def record(path):
    digest=hashlib.sha256()
    size=0
    with path.open('rb') as stream:
        for data in iter(lambda:stream.read(1024*1024),b''):
            size+=len(data)
            digest.update(data)
    return {'size':size,'sha256':digest.hexdigest()}


def probe(recipe):
    if recipe not in RECIPES:
        raise ValueError('Unsupported LaTeX recipe')
    result={}
    for name in ('latexmk',recipe,'biber','bibtex','kpsewhich','synctex','python3','strace','xdvipdfmx'):
        executable=shutil.which(name)
        if executable is None:
            raise ValueError('LaTeX tool is unavailable: '+name)
        executable=Path(executable).resolve()
        completed=subprocess.run([str(executable),'-v' if name=='latexmk' else '--version'],
            stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=15)
        # synctex accepts help rather than a stable --version exit status.
        version=completed.stdout.decode('utf-8','replace').strip().splitlines()
        result[name]={'path':str(executable),**record(executable),'version':'\n'.join(version[:4])}
    return result


def execute(argv,cwd,env,log,limit,trace):
    """Bounded transcript, native deadline, and cleanup of this process group."""
    process=subprocess.Popen(argv,cwd=cwd,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
        start_new_session=True)
    overflow=threading.Event()
    def drain():
        size=0
        with log.open('xb') as stream:
            for data in iter(lambda:process.stdout.read(8192),b''):
                size+=len(data)
                if size>limit:
                    overflow.set()
                    try: os.killpg(process.pid,signal.SIGTERM)
                    except ProcessLookupError: pass
                    continue
                stream.write(data)
                print(data.decode('utf-8','replace'),end='',flush=True)
    reader=threading.Thread(target=drain,daemon=True)
    reader.start()
    def bound_trace():
        while process.poll() is None:
            if trace.exists() and trace.stat().st_size>64*1024*1024:
                overflow.set()
                try: os.killpg(process.pid,signal.SIGTERM)
                except ProcessLookupError: pass
                return
            threading.Event().wait(.1)
    observer=threading.Thread(target=bound_trace,daemon=True)
    observer.start()
    try:
        code=process.wait(timeout=120)
        if code or overflow.is_set():
            raise ValueError('LaTeX compilation failed or exceeded its log budget')
    finally:
        # latexmk may have spawned a compiler/bibliography process.
        try: os.killpg(process.pid,signal.SIGTERM)
        except ProcessLookupError: pass
        try: process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait(timeout=3)
        reader.join(timeout=5)
        if reader.is_alive():
            try: os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError: pass
            reader.join(timeout=5)
        process.stdout.close()
        observer.join(timeout=1)
    if reader.is_alive():
        raise ValueError('LaTeX transcript did not finish')


def compile_case(config_path, *, scoped=False):
    settings=json.loads(config_path.read_text(encoding='utf-8'))
    aliases = settings.get('input_aliases', {})
    if aliases and not scoped:
        subprocess.run(['/usr/bin/unshare','--user','--map-root-user','--mount','--net','--pid',
            '--fork','--kill-child=SIGKILL',sys.executable,'-I','-S',__file__,'--config',str(config_path),
            '--scoped'], env=ALIAS_ENV, check=True)
        return
    native_sha256 = record(Path(__file__))['sha256']
    if aliases:
        _enter_alias_root(config_path, aliases, settings['files'])
    root=config_path.parent/'case'
    main=root/settings['main']
    if not main.resolve().is_relative_to(root.resolve()) or not main.is_file():
        raise ValueError('LaTeX main is outside its frozen case')
    recipe=settings['recipe']
    runtime=probe(recipe)
    if runtime!=settings['runtime']:
        changed=', '.join(name+' '+runtime[name]['version'] for name in runtime if runtime[name]!=settings['runtime'].get(name))
        raise ValueError('Prepared LaTeX toolchain changed: '+changed)
    output=main.parent/'out'
    output.mkdir(exist_ok=False)
    for path in settings['files']:
        source=root/path
        if source.is_relative_to(main.parent):
            (output/source.relative_to(main.parent).parent).mkdir(parents=True,exist_ok=True)
    empty=config_path.parent/'texmf-home'
    empty.mkdir()
    scratch=config_path.parent/'native-tmp'
    scratch.mkdir()
    controlled_env={'SOURCE_DATE_EPOCH':str(settings['epoch']),'FORCE_SOURCE_DATE':'1',
        'PERL_HASH_SEED':'0','PERL_PERTURB_KEYS':'0','TEXINPUTS':'.:','BIBINPUTS':'.:','BSTINPUTS':'.:',
        'openin_any':'r','openout_any':'p'}
    cache_root=None
    if recipe=='lualatex':
        # LuaTeX probes its first writable font cache and records the probe as
        # an OUTPUT. Keep writes in this compilation's output, while reading
        # the prebuilt system font cache as a fallback.
        cache=output/'texmf-cache';cache.mkdir()
        cache_root=cache.resolve()
        controlled_env['TEXMFCACHE']='out/texmf-cache'+os.pathsep+'/var/lib/texmf'
    base_env = ALIAS_ENV if aliases else os.environ
    env=dict(base_env,**controlled_env,TEXMFHOME=str(empty),TMPDIR=str(scratch))
    argv=['latexmk','-'+recipe,'-no-shell-escape','-norc','-interaction=nonstopmode',
        '-synctex=1','-recorder','-file-line-error','-outdir=out','./'+main.name]
    transcript=config_path.parent/'compile.log'
    trace=config_path.parent/'file-access.trace'
    traced=['strace','-f','-qq','-y','-s','4096','-e','trace=open,openat,openat2,execve,connect','-o',str(trace),*argv]
    if aliases:
        traced = ['/usr/bin/setpriv','--no-new-privs','--bounding-set=-all',
                  '--inh-caps=-all','--ambient-caps=-all',*traced]
    execute(traced,main.parent,env,transcript,settings['max_log_bytes'],trace)
    stem=main.stem
    pdf,sidecar=(output/(stem+suffix) for suffix in ('.pdf','.synctex.gz'))
    if not pdf.is_file() or not sidecar.is_file():
        raise ValueError('LaTeX did not produce its PDF and SyncTeX')
    files=settings['files']
    for path,expected in files.items():
        actual=record(root/path)
        if any(actual[key]!=expected[key] for key in actual):
            raise ValueError('LaTeX modified a frozen input: '+path)
    case_inputs,system_inputs,generated_inputs={},{},{}
    aliases_read={}
    volatile=set()
    # Compilation has finished. Resolve and hash each dependency once per proof,
    # rather than repeating expensive DrvFS metadata walks for every open call.
    root_resolved,output_resolved=root.resolve(),output.resolve()
    trusted=tuple(Path(base) for base in TRUSTED)
    runtime_paths={value['path'] for value in runtime.values()}
    resolved,records,seen={},{},set()
    def dependency_record(path):
        if path not in records: records[path]=record(path)
        return records[path]
    def dependency(value,*,traced=False):
        key=(value,traced)
        if key in seen: return
        seen.add(key)
        if value not in resolved:
            path=Path(value)
            resolved[value]=(path if path.is_absolute() else main.parent/path).resolve()
        path=resolved[value]
        if path.is_relative_to(scratch): return
        if cache_root is not None and path.is_relative_to(cache_root):
            # This directory was created empty for this compile. Lua serializes
            # derived font caches with varying bytecode/table ordering. Record
            # their relative names as runtime intermediates, not source inputs.
            # The real fonts, system cache reads and PDF/SyncTeX still undergo
            # exact dependency hashing and independent compilation checks.
            volatile.add('@lua-font-cache/'+path.relative_to(cache_root).as_posix())
            return
        if path.is_relative_to(Path('/proc')) or path.is_relative_to(Path('/sys')) or (path.parent==Path('/dev') and path.name in ALIAS_DEVICES):
            volatile.add(re.sub(r'^/proc/\d+/', '/proc/<process>/',str(path)))
            return
        if path.is_dir(): return
        if not path.is_file():
            raise ValueError('Recorded LaTeX dependency is missing: '+str(path))
        if str(path) in aliases:
            relative = aliases[str(path)]
            actual = dependency_record(path)
            if any(actual[key] != files[relative][key] for key in actual):
                raise ValueError('LaTeX input alias differs from its frozen source')
            case_inputs[relative] = actual
            aliases_read[str(path)] = relative
        elif path.is_relative_to(output_resolved):
            if not traced: generated_inputs[path.relative_to(output_resolved).as_posix()]=dependency_record(path)
        elif path.is_relative_to(root_resolved):
            relative=path.relative_to(root_resolved).as_posix()
            if relative not in files:
                raise ValueError('LaTeX read an unfrozen case resource: '+relative)
            case_inputs[relative]=dependency_record(path)
        elif str(path) in RUNTIME_CONFIG or str(path) in runtime_paths or any(path.is_relative_to(base) for base in trusted):
            system_inputs[str(path)]=dependency_record(path)
        else:
            raise ValueError('LaTeX read an external or private resource: '+str(path))
    fls=output/(stem+'.fls')
    if not fls.is_file():
        raise ValueError('LaTeX recorder output is missing')
    for line in fls.read_text(encoding='utf-8',errors='strict').splitlines():
        if line.startswith('INPUT '): dependency(line[6:])
        elif line.startswith('OUTPUT '):
            path=(main.parent/line[7:]).resolve()
            if not path.is_relative_to(output_resolved):
                raise ValueError('LaTeX wrote outside its output directory')
    if trace.stat().st_size>64*1024*1024:
        raise ValueError('LaTeX file-access trace exceeds 64 MiB')
    for line in trace.read_text(encoding='utf-8').splitlines():
        if 'connect(' in line and ('AF_INET,' in line or 'AF_INET6,' in line):
            raise ValueError('LaTeX dependencies must be local; network access was recorded')
        if not re.search(r'\bopen(?:at2|at)?\(',line) or 'O_WRONLY' in line: continue
        match=re.search(r'=\s*\d+<([^>]+)>',line)
        if match:
            import codecs
            value=codecs.decode(match[1].encode('utf-8'),'unicode_escape').encode('latin1').decode('utf-8')
            dependency(value.removesuffix(' (deleted)'),traced=True)
    bcf=output/(stem+'.bcf')
    bibliographies=[]
    if bcf.exists():
        tree=ET.fromstring(bcf.read_bytes())
        bibliographies=[(node.text or '').strip() for node in tree.iter() if node.tag.rsplit('}',1)[-1]=='datasource']
        for name in bibliographies:
            if not name or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:',name):
                raise ValueError('Bibliographies must be frozen local files')
            dependency(name)
    aux=(output/(stem+'.aux')).read_text(encoding='utf-8',errors='replace')
    for group in re.findall(r'\\bibdata\{([^}]+)\}',aux):
        for name in group.split(','):
            if name!=stem+'-blx':
                bibliographies.append(name)
                dependency(name if name.endswith('.bib') else name+'.bib')
    bbl=output/(stem+'.bbl')
    if bibliographies and (not bbl.is_file() or not bbl.stat().st_size):
        raise ValueError('Bibliography compilation did not produce its BBL')
    if settings['main'] not in case_inputs:
        raise ValueError('LaTeX did not record the selected main source')
    if probe(recipe)!=runtime:
        raise ValueError('LaTeX toolchain changed during compilation')
    result={'runtime':runtime,'case_inputs':case_inputs,'system_inputs':system_inputs,
        'generated_inputs':generated_inputs,'bibliographies':bibliographies,
        'has_bbl':bbl.is_file(),'native_sha256':native_sha256,'environment':controlled_env}
    result['volatile_runtime_inputs']=sorted(volatile)
    if aliases:
        result['input_aliases_read'] = aliases_read
    (config_path.parent/'native-result.json').write_text(json.dumps(result,ensure_ascii=False,sort_keys=True)+'\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--probe',choices=RECIPES)
    parser.add_argument('--config',type=Path)
    parser.add_argument('--scoped', action='store_true', help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.probe:
        print(json.dumps(probe(args.probe),sort_keys=True))
    elif args.config:
        compile_case(args.config.resolve(), scoped=args.scoped)
    else:
        parser.error('A probe or compile configuration is required')


if __name__=='__main__':
    try:
        main()
    except subprocess.TimeoutExpired:
        print('Native LaTeX compilation timed out',flush=True)
        raise SystemExit(124)
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode if error.returncode > 0 else 1) from error
