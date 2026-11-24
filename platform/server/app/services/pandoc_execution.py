"""Preserve Pandoc's stdin and distinct captured streams through an owned child."""
import hashlib
import json
from pathlib import Path
import subprocess
import uuid

from . import native_execution_client as client,pandoc_native

NATIVE=Path(__file__).with_name('pandoc_native.py')


def run(purpose,pandoc,root,*,markdown=None,ast=None,output=None,extension=None,reference_doc=None,citation_inputs=None):
    client.channel()
    root=Path(root).resolve()
    settings={'schema':pandoc_native.SCHEMA,'purpose':purpose,'pandoc':str(Path(pandoc).resolve()),'root':str(root),
        'extension':extension,'ast':str(ast) if ast is not None else None,'output':str(output) if output is not None else None,
        'input_sha256':hashlib.sha256(markdown).hexdigest() if markdown is not None else None}
    if reference_doc is not None:
        settings['reference_doc'] = str(reference_doc)
    if citation_inputs is not None:
        if (citation_inputs.keys()!=pandoc_native.CITATION_INPUTS.keys()
                or any(len(data)>pandoc_native.CITATION_INPUTS[name] for name,data in citation_inputs.items())):
            raise ValueError('Citation inputs exceed their bounds')
        settings['citation_inputs']={name:hashlib.sha256(data).hexdigest() for name,data in citation_inputs.items()}
    argv,timeout=pandoc_native.command(settings)
    folder=root.parent/('.pandoc-call-'+uuid.uuid4().hex)
    folder.mkdir(exist_ok=False)
    try:
        for name,data in (citation_inputs or {}).items():
            (folder/name).write_bytes(data)
        if markdown is not None:
            if len(markdown)>pandoc_native.STDOUT_LIMIT:raise ValueError('Pandoc stdin exceeds its limit')
            (folder/'input.md').write_bytes(markdown)
        config=folder/'settings.json'
        config.write_text(json.dumps(settings,ensure_ascii=False),encoding='utf-8')
        call=client.run_native(purpose,{'config':str(config),'sha256':hashlib.sha256(config.read_bytes()).hexdigest()},
            timeout=timeout+15,capture=True)
        if call.returncode:
            raise client.ChildExecutionError('Owned Pandoc helper did not finish')
        result=json.loads(call.stdout)
        if (not isinstance(result,dict) or set(result)!={'schema','returncode','files'}
                or result['schema']!='vibeflow.pandoc-result.v1' or type(result['returncode']) is not int
                or not -0x80000000<=result['returncode']<=0xffffffff
                or set(result['files'])!={'stdout.bin','stderr.bin'}):
            raise ValueError('Invalid owned Pandoc result')
        captured={}
        from .attempt_workspace import checked_path
        for name,limit in [('stdout.bin',pandoc_native.STDOUT_LIMIT),('stderr.bin',pandoc_native.STDERR_LIMIT)]:
            path=checked_path(folder,name)
            if path.stat().st_size>limit:raise ValueError('Owned Pandoc output exceeds its limit')
            value=path.read_bytes()
            if result['files'][name]!={'size':len(value),'sha256':hashlib.sha256(value).hexdigest()}:
                raise ValueError('Owned Pandoc result bytes changed')
            captured[name]=value
        return subprocess.CompletedProcess(argv,result['returncode'],captured['stdout.bin'],captured['stderr.bin'])
    finally:
        from .fs_directory import remove_owned
        remove_owned(folder,root=root.parent)


def validate(config,root,pandoc,purpose):
    """Return settings only for this attempt's fixed executable/profile/paths."""
    import re
    from .attempt_workspace import checked_path
    config=Path(config)
    if (config.name!='settings.json' or config.parent.parent!=root.parent
            or not re.fullmatch(r'\.pandoc-call-[0-9a-f]{32}',config.parent.name)):
        raise ValueError('Pandoc call configuration belongs to another attempt')
    config=checked_path(root.parent,config.relative_to(root.parent).as_posix())
    if config.stat().st_size>16384:raise ValueError('Pandoc configuration exceeds its limit')
    raw=config.read_bytes();settings=json.loads(raw)
    pandoc_native.command(settings)
    if settings['purpose']!=purpose or Path(settings['root'])!=root or Path(settings['pandoc'])!=Path(pandoc):
        raise ValueError('Pandoc call differs from its registered profile')
    if purpose=='pandoc_parse':
        source=checked_path(root.parent,(config.parent/'input.md').relative_to(root.parent).as_posix())
        if source.stat().st_size>pandoc_native.STDOUT_LIMIT or hashlib.sha256(source.read_bytes()).hexdigest()!=settings['input_sha256']:
            raise ValueError('Pandoc stdin is not the selected request')
    if purpose=='pandoc_cite':
        for name in pandoc_native.CITATION_INPUTS:
            checked_path(root.parent,(config.parent/name).relative_to(root.parent).as_posix())
        pandoc_native.verify_citation_inputs(config.parent,settings['citation_inputs'])
    if purpose=='pandoc_compile':
        ast=Path(settings['ast']);output=Path(settings['output'])
        if (ast.name!='document.json' or output.name!='report.'+settings['extension'] or ast.parent!=output.parent
                or ast.parent.parent!=root.parent or not re.fullmatch(r'\.report-compile-[A-Za-z0-9_-]{1,64}',ast.parent.name)):
            raise ValueError('Pandoc compiler paths belong to another attempt')
        checked_path(root.parent,ast.relative_to(root.parent).as_posix())
        checked_path(root.parent,output.parent.relative_to(root.parent).as_posix())
        try:output.lstat()
        except FileNotFoundError:pass
        else:raise ValueError('Pandoc output already exists')
        if settings.get('reference_doc') is not None:
            reference=Path(settings['reference_doc'])
            checked_path(root,reference.relative_to(root).as_posix())
    return settings,hashlib.sha256(raw).hexdigest()
