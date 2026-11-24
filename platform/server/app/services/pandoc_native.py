"""Fixed Pandoc profiles with bounded, separate stdin/stdout/stderr files.

Runs in an API-owned Windows scope. No application imports or shell, and no
control messages share the tool's stdin or output streams.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import threading

SCHEMA='vibeflow.pandoc-call.v1'
PURPOSES={'pandoc_parse','pandoc_compile','pandoc_version','pandoc_cite'}
STDOUT_LIMIT=32*1024*1024
STDERR_LIMIT=1024*1024
CITATION_OUTPUT_LIMIT=1024*1024
CITATION_INPUTS={'input.md':32768,'refs.bib':1024*1024,'style.csl':512*1024}
FIELDS={'schema','purpose','pandoc','root','extension','ast','output','input_sha256'}


def digest(value):
    return isinstance(value,str) and len(value)==64 and all(c in '0123456789abcdef' for c in value)


def command(settings):
    if not isinstance(settings,dict) or set(settings) not in (FIELDS,FIELDS|{'reference_doc'},FIELDS|{'citation_inputs'}) or settings['schema']!=SCHEMA:
        raise ValueError('Invalid Pandoc invocation settings')
    purpose=settings['purpose']
    if purpose not in PURPOSES:raise ValueError('Unsupported Pandoc purpose')
    reference=settings.get('reference_doc')
    if reference is not None and (purpose!='pandoc_compile' or settings['extension']!='docx'):
        raise ValueError('Reference document requires DOCX compilation')
    for key in ('pandoc','root'):
        if not isinstance(settings[key],str) or not Path(settings[key]).is_absolute():
            raise ValueError('Pandoc requires absolute executable and working paths')
    pandoc=settings['pandoc']
    if purpose=='pandoc_cite':
        inputs=settings.get('citation_inputs')
        if (not isinstance(inputs,dict) or set(inputs)!=CITATION_INPUTS.keys()
                or not all(map(digest,inputs.values()))
                or any(settings[key] is not None for key in ('ast','output','extension','input_sha256'))):
            raise ValueError('Citations require a fixed input bundle')
        return [pandoc,'--sandbox','--from=markdown','--to=html','--wrap=none',
                '--citeproc','--bibliography=refs.bib','--csl=style.csl','input.md'],15
    if 'citation_inputs' in settings:raise ValueError('Unexpected citation inputs')
    if purpose=='pandoc_parse':
        if any(settings[key] is not None for key in ('ast','output','extension')):
            raise ValueError('Parsing cannot request a compiled output')
        if not digest(settings['input_sha256']):
            raise ValueError('Parsing requires frozen stdin')
        return [pandoc,'--from=markdown','--to=json'],30
    if settings['input_sha256'] is not None:raise ValueError('Unexpected native stdin')
    if purpose=='pandoc_version':
        if any(settings[key] is not None for key in ('ast','output','extension')):
            raise ValueError('Version probing cannot request a compiled output')
        return [pandoc,'--version'],20
    extension=settings['extension']
    if extension not in {'html','docx'}:raise ValueError('Unsupported report output')
    for key in ('ast','output'):
        if not isinstance(settings[key],str) or not Path(settings[key]).is_absolute():
            raise ValueError('Compilation requires absolute owned paths')
    argv=[pandoc,settings['ast'],'--from=json','--resource-path='+settings['root'],'-o',settings['output']]
    if reference is not None:
        if (not isinstance(reference,str) or not Path(reference).is_absolute()
                or not Path(reference).resolve().is_relative_to(Path(settings['root']).resolve())
                or Path(reference).suffix.lower()!='.docx'):
            raise ValueError('Reference DOCX must belong to the frozen working directory')
        argv+=['--reference-doc',reference]
    if extension=='html':argv+=['--standalone','--embed-resources','--mathml','--metadata','title=报告']
    return argv,120


def execute(config,expected_sha256):
    raw=config.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected_sha256:raise ValueError('Pandoc configuration changed before release')
    settings=json.loads(raw)
    argv,timeout=command(settings);folder=config.parent
    data=None
    cwd=settings['root']
    output_limit=STDOUT_LIMIT
    if settings['purpose']=='pandoc_cite':
        verify_citation_inputs(folder,settings['citation_inputs'])
        cwd=folder
        output_limit=CITATION_OUTPUT_LIMIT
    if settings['purpose']=='pandoc_parse':
        source=folder/'input.md'
        if source.stat().st_size>STDOUT_LIMIT:raise ValueError('Pandoc stdin exceeds its limit')
        data=source.read_bytes()
        if hashlib.sha256(data).hexdigest()!=settings['input_sha256']:
            raise ValueError('Pandoc stdin changed before execution')
    process=subprocess.Popen(argv,cwd=cwd,stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    errors=[];overflow=threading.Event()
    def terminate():
        try:process.kill()  # Popen retains this created process handle.
        except ProcessLookupError:pass
    def copy(stream,name,limit):
        count=0
        try:
            # Diagnostics must be visible to the owning executor while Pandoc
            # is still running, including a partial pipe buffer before a stall.
            diagnostic=name=='stderr.bin'
            with (folder/name).open('xb',buffering=0 if diagnostic else -1) as output:
                while True:
                    chunk=stream.read1(4096) if diagnostic else stream.read(4096)
                    if not chunk:break
                    count+=len(chunk)
                    if count>limit:
                        overflow.set();terminate();break
                    output.write(chunk)
        except BaseException as error:
            errors.append(type(error).__name__);terminate()
        finally:stream.close()
    def feed():
        try:
            process.stdin.write(data);process.stdin.flush()
        except BrokenPipeError:pass
        except BaseException as error:
            errors.append(type(error).__name__);terminate()
        finally:
            try:process.stdin.close()
            except BrokenPipeError:pass
    threads=[threading.Thread(target=copy,args=(process.stdout,'stdout.bin',output_limit),daemon=True),
        threading.Thread(target=copy,args=(process.stderr,'stderr.bin',STDERR_LIMIT),daemon=True)]
    if data is not None:threads.append(threading.Thread(target=feed,daemon=True))
    for thread in threads:thread.start()
    try:
        code=process.wait(timeout=timeout)
    finally:
        if process.poll() is None:terminate()
        process.wait(timeout=5)
        for thread in threads:thread.join(timeout=5)
    if overflow.is_set() or errors or any(thread.is_alive() for thread in threads):
        raise ValueError('Pandoc streams did not complete within their bounds')
    result={'schema':'vibeflow.pandoc-result.v1','returncode':code,'files':{}}
    for name in ('stdout.bin','stderr.bin'):
        value=(folder/name).read_bytes()
        result['files'][name]={'size':len(value),'sha256':hashlib.sha256(value).hexdigest()}
    print(json.dumps(result,sort_keys=True),flush=True)


def verify_citation_inputs(folder,expected):
    for name,limit in CITATION_INPUTS.items():
        path=folder/name
        if path.stat().st_size>limit or hashlib.sha256(path.read_bytes()).hexdigest()!=expected[name]:
            raise ValueError('Frozen citation input changed')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--sha256',required=True);args=parser.parse_args()
    execute(args.config,args.sha256)
