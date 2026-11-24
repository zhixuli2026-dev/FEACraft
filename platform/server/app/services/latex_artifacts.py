"""Frozen LaTeX resources, native recompilation and semantic PDF comparison."""
from contextlib import contextmanager
import gzip
import hashlib
import io
import json
import os
from pathlib import Path,PurePosixPath
import re
import subprocess
import uuid

from .. import storage
from . import attempt_workspace as workspace
from .fs_directory import remove_owned
from .jobrunner import win_to_wsl_path
from .report_artifacts import encoded
from .report_build import capture_report_guard

NATIVE=Path(__file__).with_name('latex_native.py')
RECIPES=('xelatex','pdflatex','lualatex')
SKIP={'.vibeflow','build','out','compositions','latex-history','latex-requests'}
ASSETS={'.tex','.ltx','.bib','.bst','.sty','.cls','.clo','.def','.cfg','.bbx','.cbx','.lbx','.json','.xml','.txt',
    '.png','.jpg','.jpeg','.pdf','.eps','.ps','.svg','.csv','.dat','.tikz','.pgf','.table','.ttf','.otf'}


def main_path(value):
    path=workspace.canonical_path(value.strip())
    if not path.endswith('.tex') or any(part.casefold() in SKIP for part in path.split('/')):
        raise ValueError('LaTeX main must be a case-owned .tex source outside generated directories')
    if any(ord(char)<32 or char in '$%~`"\x7f' for char in PurePosixPath(path).name):
        raise ValueError('LaTeX main contains unsupported TeX or command-substitution characters')
    return path


def channel(main):
    return 'latex:'+hashlib.sha256(main_path(main).casefold().encode('utf-8')).hexdigest()


def paths(main):
    path=PurePosixPath(main_path(main))
    base=(path.parent/'build'/path.stem).as_posix()
    return {key:base+suffix for key,suffix in {'pdf':'.pdf','synctex':'.synctex.gz','bbl':'.bbl',
        'manifest':'.latex-manifest.json','toolchain':'.latex-toolchain.json','log':'.latex.log'}.items()}


def history(execution_id):
    if str(uuid.UUID(execution_id))!=execution_id:
        raise ValueError('Invalid LaTeX invocation')
    return 'E-report/latex-history/'+execution_id+'/sources/'


def normalize_resources(values):
    if not isinstance(values,list) or len(values)>64:
        raise ValueError('LaTeX resources must select at most 64 case paths')
    result=sorted({workspace.canonical_path(value) for value in values})
    if any(any(part.casefold() in {'.vibeflow','compositions','latex-history'} for part in value.split('/')) for value in result):
        raise ValueError('LaTeX resources cannot select execution metadata or publication history')
    return result


def resources(root,main,extra):
    parent=PurePosixPath(main_path(main)).parent
    selected=[]
    # Keep the complete source subtree, plus standard resource types anywhere
    # in the case. The recorder rejects any actual read outside this envelope.
    for directory,dirs,files in os.walk(root,followlinks=False):
        folder=Path(directory)
        dirs[:]=sorted(name for name in dirs if name.casefold() not in SKIP and not name.startswith('.'))
        for name in dirs:
            workspace.ordinary(folder/name)
        for name in sorted(files):
            path=folder/name
            relative=path.relative_to(root).as_posix()
            in_subtree=parent==PurePosixPath('.') or PurePosixPath(relative).is_relative_to(parent)
            if in_subtree or path.suffix.lower() in ASSETS:
                workspace.ordinary(path)
                selected.append(relative)
    for relative in normalize_resources(extra):
        path=workspace.checked_path(root,relative)
        if path.is_file(): selected.append(relative)
        else:
            for directory,dirs,files in os.walk(path,followlinks=False):
                folder=Path(directory)
                dirs[:]=[name for name in dirs if name.casefold() not in SKIP and not name.startswith('.')]
                for name in dirs: workspace.ordinary(folder/name)
                selected.extend((folder/name).relative_to(root).as_posix() for name in files)
    if set(selected).intersection(paths(main).values()):
        raise ValueError('LaTeX inputs cannot include this main file\'s generated outputs')
    if main not in selected:
        raise ValueError('LaTeX main source does not exist')
    return workspace.inventory(root,selected)


def normalize_input_aliases(values):
    from .latex_native import alias_name

    if not isinstance(values, dict) or len(values)>32:
        raise ValueError('LaTeX accepts at most 32 absolute input aliases')
    result = {}
    for name, relative in sorted(values.items()):
        name = alias_name(name)
        relative = workspace.canonical_path(relative)
        if any(part.casefold() in SKIP or part.startswith('.') for part in relative.split('/')):
            raise ValueError('LaTeX input aliases must select ordinary case inputs')
        if any(parent.as_posix() in result for parent in PurePosixPath(name).parents):
            raise ValueError('LaTeX input aliases cannot overlap')
        result[name] = relative
    if len(json.dumps(result, ensure_ascii=False).encode('utf-8'))>8192:
        raise ValueError('LaTeX input aliases exceed 8 KiB')
    return result


def source(root,main,extra=None,*,input_aliases=None,validated_gate=False,gate_prefix=None):
    prefix=gate_prefix if gate_prefix is not None else root.relative_to(storage._fs_root().resolve()).as_posix()
    guard=capture_report_guard(prefix,validated_gate=validated_gate,root=root)
    extra=normalize_resources(extra or [])
    aliases=normalize_input_aliases(input_aliases or {})
    for path in set(aliases.values()):
        if not workspace.checked_path(root,path).is_file():
            raise ValueError('A LaTeX input alias must select one regular file')
    files=resources(root,main,sorted(set(extra)|set(aliases.values())))
    metadata=['0-caseDict/caseDict',*guard['evidence']]
    if guard['postprocess_pointer_sha256'] is not None:
        metadata.append('D-post/data/latest-postprocess.json')
    files.update(workspace.inventory(root,metadata))
    return {'main':main_path(main),'resources':extra,'files':files,'report_guard':guard,
            **({'input_aliases':aliases} if aliases else {})}


def selection(root,records,published):
    wanted=set(records)-set(published)
    selected=[]
    def visit(folder):
        relative=folder.relative_to(root).as_posix()
        names=[path for path in wanted if path.startswith(relative+'/')]
        if relative!='.' and names:
            actual=set()
            eligible=True
            for directory,dirs,files in os.walk(folder,followlinks=False):
                if any(name.casefold() in SKIP or name.startswith('.') or (Path(directory)/name).is_symlink() for name in dirs):
                    eligible=False
                    break
                actual.update((Path(directory)/name).relative_to(root).as_posix() for name in files)
            if eligible and actual==set(names):
                selected.append(relative)
                return
        for child in sorted(folder.iterdir()):
            name=child.relative_to(root).as_posix()
            if name in wanted:
                selected.append(name)
            elif child.is_dir() and any(path.startswith(name+'/') for path in wanted):
                visit(child)
    visit(root)
    if len(selected)>256:
        raise ValueError('LaTeX source envelope exceeds 256 independent selections; group resources in directories')
    return selected


def probe(distro,recipe):
    try:
        from .native_execution_client import run_latex
        result=run_latex('latex_probe',distro,recipe=recipe,capture=True)
    except (OSError,subprocess.TimeoutExpired) as error:
        raise ValueError('LaTeX toolchain probe did not finish') from error
    if result.returncode:
        raise ValueError('LaTeX toolchain probe failed: '+(result.stderr or result.stdout).decode('utf-8','replace')[-600:])
    return json.loads(result.stdout)


@contextmanager
def compile_document(root,main,recipe,distro,epoch,max_log_bytes,files,runtime,input_aliases):
    from .native_execution_client import channel,run_latex
    channel()
    folder=root.parent/('.latex-compile-'+uuid.uuid4().hex)
    folder.mkdir(exist_ok=False)
    try:
        directories=sorted({str(PurePosixPath(path).parent) for path in files if str(PurePosixPath(path).parent)!='.'})
        workspace.copy_records(root,folder/'case',files,directories)
        settings={'main':main,'recipe':recipe,'epoch':epoch,'max_log_bytes':max_log_bytes,'files':files,'runtime':runtime}
        if input_aliases:
            settings['input_aliases'] = input_aliases
        config=folder/'settings.json'
        config.write_bytes(encoded(settings))
        try:
            result=run_latex('latex_compile',distro,config=config)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError('WSL LaTeX invocation timed out') from error
        except OSError as error:
            raise ValueError('Native LaTeX invocation did not finish') from error
        if result.returncode==124:
            raise TimeoutError('LaTeX compilation timed out after 120 seconds')
        if result.returncode:
            raise ValueError('Native LaTeX compilation or dependency verification failed')
        native=json.loads((folder/'native-result.json').read_text(encoding='utf-8'))
        output=folder/'case'/PurePosixPath(main).parent/'out'
        yield folder,output,native
    finally:
        remove_owned(folder,root=root.parent)


def canonical_pdf(data):
    from pypdf import PdfReader
    from pypdf.generic import ArrayObject,DictionaryObject,IndirectObject,StreamObject,NullObject
    reader=PdfReader(io.BytesIO(data),strict=True)
    if reader.is_encrypted or not 1<=len(reader.pages)<=1000:
        raise ValueError('PDF must contain 1-1000 unencrypted pages')
    visited={}
    expanded=0
    def walk(value,depth=0):
        nonlocal expanded
        if depth>512 or len(visited)>100000:
            raise ValueError('PDF object graph exceeds verification limits')
        if isinstance(value,IndirectObject):
            key=(value.idnum,value.generation)
            if key in visited: return {'ref':visited[key]}
            number=len(visited)
            visited[key]=number
            return {'id':number,'value':walk(value.get_object(),depth+1)}
        if isinstance(value,StreamObject):
            decoded=value.get_data()
            expanded+=len(decoded)
            if expanded>256*1024*1024:
                raise ValueError('Expanded PDF exceeds 256 MiB')
            return {'stream':hashlib.sha256(decoded).hexdigest(),'properties':{key:walk(item,depth+1)
                for key,item in sorted(value.items()) if key not in {'/Length','/Filter','/DecodeParms'}}}
        if isinstance(value,DictionaryObject):
            return {key:walk(item,depth+1) for key,item in sorted(value.items())}
        if isinstance(value,ArrayObject): return [walk(item,depth+1) for item in value]
        if isinstance(value,bytes): return {'bytes':value.hex()}
        if isinstance(value,NullObject): return None
        return {'type':type(value).__name__,'value':str(value)}
    # Object offsets, compression and trailer IDs are containers. Every reachable
    # document object, decoded stream, annotation, font, image and Info field stays.
    return {'pages':len(reader.pages),'root':walk(reader.trailer.raw_get('/Root')),
        'info':walk(reader.trailer.raw_get('/Info')) if '/Info' in reader.trailer else None}


def synctex(data,case_root,output_root,case_files,*,input_aliases=None):
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
        raw=stream.read(64*1024*1024+1)
    if len(raw)>64*1024*1024:
        raise ValueError('SyncTeX exceeds 64 MiB')
    text=raw.decode('utf-8')
    if not text.startswith('SyncTeX Version:') or 'Postamble:' not in text:
        raise ValueError('Incomplete SyncTeX sidecar')
    prefix=win_to_wsl_path(case_root)
    output=win_to_wsl_path(output_root)
    sources={}
    lines=[]
    for line in text.splitlines():
        match=re.match(r'^Input:(\d+):(.*)$',line)
        if match:
            original=match[2]
            native=PurePosixPath(os.path.normpath(original).replace('\\','/'))
            if native.is_relative_to(PurePosixPath(output)):
                name='@generated/'+native.relative_to(PurePosixPath(output)).as_posix()
            elif original in (input_aliases or {}) or native.is_relative_to(PurePosixPath(prefix)):
                name=(input_aliases or {}).get(original)
                if name is None:
                    name=native.relative_to(PurePosixPath(prefix)).as_posix()
                if name not in case_files:
                    raise ValueError('SyncTeX refers to an unfrozen source')
                sources[original]=name
                name='@case/'+name
            else:
                # System package inputs remain exact and are recorded by .fls.
                name=original
            line='Input:'+match[1]+':'+name
        lines.append(line)
    if not sources:
        raise ValueError('SyncTeX has no owned source mapping')
    return '\n'.join(lines),sources


def compile_id_root(root,value):
    if uuid.UUID(value).hex!=value:
        raise ValueError('Invalid native compile identity')
    return root.parent/('.latex-compile-'+value)/'case'


def normalize_log(data,case_root):
    return data.replace(win_to_wsl_path(case_root).encode('utf-8'),b'@case')


def generate(root,parameters,identity,runtime):
    """Return a complete candidate bundle; validation independently repeats it."""
    from . import registered_latex_worker
    import pypdf
    main,recipe,distro,epoch,execution_id,max_log_bytes=(parameters[key] for key in
        ('main','recipe','distro','epoch','execution_id','max_log_bytes'))
    names=paths(main)
    aliases=parameters.get('input_aliases',{})
    common={'case_id':identity['source']['case_id'],'job_id':identity['source']['job_id'],
        'attempt_id':identity['attempt_id'],'main':main,'recipe':recipe,'distro':distro,
        'epoch':epoch,'execution_id':execution_id,'resources':parameters['resources'],
        'input_publications':identity.get('publications',[])}
    if aliases:
        common['input_aliases']=aliases
    with compile_document(root,main,recipe,distro,epoch,max_log_bytes,identity['files'],runtime,aliases) as (folder,output,native):
        stem=PurePosixPath(main).stem
        files={names['pdf']:(output/(stem+'.pdf')).read_bytes(),
            names['synctex']:(output/(stem+'.synctex.gz')).read_bytes(),names['log']:(folder/'compile.log').read_bytes()}
        if native['has_bbl']:
            files[names['bbl']]=(output/(stem+'.bbl')).read_bytes()
        sources={path:history(execution_id)+path for path in native['case_inputs']}
        files.update({destination:workspace.checked_path(root,path).read_bytes() for path,destination in sources.items()})
        sync,mapping=synctex(files[names['synctex']],folder/'case',output,identity['files'],input_aliases=aliases)
        graph=canonical_pdf(files[names['pdf']])
        manifest={**common,'schema':'vibeflow.latex-publication.v1','inputs':identity['files'],'source_files':sources,
            'artifacts':{path:{'size':len(data),'sha256':hashlib.sha256(data).hexdigest()} for path,data in files.items()},
            'pdf_graph_sha256':hashlib.sha256(encoded(graph)).hexdigest(),
            'synctex_sha256':hashlib.sha256(sync.encode('utf-8')).hexdigest(),'pages':graph['pages']}
        toolchain={**common,'schema':'vibeflow.latex-toolchain.v1','compile_id':folder.name.removeprefix('.latex-compile-'),
            'source_mapping':mapping,'native':native,'pypdf_version':pypdf.__version__,
            'worker_sha256':hashlib.sha256(Path(registered_latex_worker.__file__).read_bytes()).hexdigest(),
            'compiler_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
        files[names['manifest']]=encoded(manifest)
        files[names['toolchain']]=encoded(toolchain)
    return files,manifest,toolchain
