"""Register Code_Aster input sealing, native execution and publication."""
from functools import partial
import hashlib
from pathlib import Path
import re
import sys
import uuid

from ..models import ArtifactPublication
from . import code_aster, code_aster_artifacts as artifacts
from .attempt_workspace import digest_file
from .case_provision import io
from .case_read_view import inspect
from .casedict import CASE_DICT_PATH
from .compose_artifacts import live_existing, record
from .durable_events import json_text
from .geometry_handler import GeometryHandler
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict


WORKER = Path(__file__).with_name('code_aster_worker.py')
NATIVE = Path(__file__).with_name('code_aster_native.py')
STATE = Path(__file__).with_name('code_aster_state.py')
ARGS = {'--input','--executable','--distro','--executable-sha256','--native-sha256',
        '--execution-id','--timeout','--max-log-bytes'}


class CodeAsterHandler:
    key = 'code-aster.solve'
    version = '1'
    job_type = 'solve'
    publication_kind = 'code-aster'
    cancellation = dict(GeometryHandler.cancellation)
    recovery = dict(GeometryHandler.recovery)

    def publication_channel(self, descriptor):
        path = artifacts.input_path(descriptor['input'])
        return 'code-aster:'+hashlib.sha256(path.casefold().encode()).hexdigest()[:40]

    async def prepare(self, db, case, user, *, parameters, budget, observations=None):
        if set(parameters)-{'restart'}!={'input_path','executable'}:
            raise ValueError('Code_Aster requires an .export and the configured solver')
        path = artifacts.input_path(parameters['input_path'])
        executable,distro = await io(code_aster.configured)
        if executable!=parameters['executable']:
            raise ExecutionConflict('runtime_unavailable','Code_Aster installation changed during submission')
        tool = (await io(digest_file,code_aster.host_path(executable,distro)))['sha256']
        native = (await io(digest_file,NATIVE))['sha256']
        read_export = partial(inspect,paths=(path,),max_file_bytes=artifacts.MAX_EXPORT)
        plan = await io(read_export,case.s3_prefix,artifacts.selection,path)
        native_base = plan.get('base',{})
        restart = parameters.get('restart')
        if ('input' in native_base)!=(restart is not None):
            raise ValueError('Code_Aster restart requires both R base D and a published base reference')
        references = None
        if restart is not None:
            if not isinstance(restart,dict) or set(restart)!={'publication_id','checkpoint_path'}:
                raise ValueError('Code_Aster restart requires a publication and base archive path')
            archive = artifacts.path(restart['checkpoint_path'])
            if not archive.endswith('_base.zip') or archive in plan['outputs']:
                raise ValueError('Code_Aster restart requires a distinct published base archive')
            if not isinstance(restart['publication_id'],str):
                raise ValueError('Invalid Code_Aster publication ID')
            publication_id = str(uuid.UUID(restart['publication_id']))
            row = await (observations if observations is not None else db).get(ArtifactPublication,publication_id)
            if row is None or row.case_id!=case.id or not row.kind.startswith('code-aster:'):
                raise ValueError('The selected Code_Aster base is unavailable in this case')
            restart = {'publication_id':publication_id,'checkpoint_path':archive}
            references = [{'publication_id':publication_id,'paths':[archive],'require_current':False}]
        sources = tuple(sorted({path,*plan['inputs']}))
        read_inputs = partial(inspect,paths=sources,optional_paths=(CASE_DICT_PATH,),max_file_bytes=artifacts.MAX_INPUT)
        source = await io(read_inputs,case.s3_prefix,artifacts.dependencies,plan)
        read_outputs = partial(inspect,paths=(),optional_paths=plan['outputs'],max_file_bytes=artifacts.MAX_OUTPUT)
        before = await io(read_outputs,case.s3_prefix,artifacts.targets,plan)
        if observations is None:
            from .quota import check_job_quota
            await check_job_quota(db,user,case_id=case.id)
        identifier = str(uuid.uuid4())
        configuration = {'source':source,'execution_id':identifier,'distro':distro}
        if native_base:
            configuration['engine_sha256'] = await io(code_aster.state_package_digest,executable,distro)
        if restart is not None:
            configuration['restart'] = restart
        timeout = min(budget['timeout_seconds'],plan['settings']['time_limit']+15)
        spec = {'command':'Code_Aster solve','case_prefix':case.s3_prefix,'runtime':'windows',
            'argv':[sys.executable,str(WORKER),'--input='+path,'--executable='+executable,'--distro='+distro,
                    '--executable-sha256='+tool,'--native-sha256='+native,'--execution-id='+identifier,
                    '--timeout='+str(timeout),'--max-log-bytes='+str(budget['max_log_bytes'])],
            'timeout_seconds':timeout,'max_log_bytes':budget['max_log_bytes'],'evidence_run_dir':None}
        descriptor = {'handler':'code-aster.v1','handler_version':self.version,'case_id':case.id,'input':path,
            'source':source,'configuration':configuration,'executable':executable,'executable_sha256':tool,
            'native_sha256':native,'distro':distro,'execution_id':identifier,'targets_before':before,
            'worker_sha256':(await io(digest_file,WORKER))['sha256']}
        if native_base:
            descriptor['common_native_sha256'] = (await io(digest_file,STATE))['sha256']
            spec['argv'].append('--state-sha256='+descriptor['common_native_sha256'])
        if references is not None:
            descriptor['input_publications'] = references
        self.validate_spec(spec)
        return PreparedStep(self.key,self.version,self.job_type,spec,sorted(source['files']),references,
            self.publication_channel(descriptor),descriptor,
            generated_inputs={artifacts.CONFIG:json_text(configuration).encode('utf8')})

    def validate_spec(self, spec):
        try:
            argv = spec['argv']
            if (spec.get('command')!='Code_Aster solve' or spec.get('runtime')!='windows'
                    or spec.get('evidence_run_dir') is not None or not isinstance(argv,list) or len(argv) not in (10,11)
                    or argv[:2]!=[sys.executable,str(WORKER)]):
                raise ValueError('Invalid Code_Aster worker')
            args = dict(value.split('=',1) for value in argv[2:])
            state = args.pop('--state-sha256',None)
            if (set(args)!=ARGS or (len(argv)==11 and (state is None or not re.fullmatch('[0-9a-f]{64}',state)))
                    or artifacts.input_path(args['--input'])!=args['--input']
                    or any(not re.fullmatch('[0-9a-f]{64}',args[key]) for key in ('--executable-sha256','--native-sha256'))
                    or str(uuid.UUID(args['--execution-id']))!=args['--execution-id']
                    or int(args['--timeout'])!=spec['timeout_seconds'] or not 0<spec['timeout_seconds']<=3615
                    or int(args['--max-log-bytes'])!=spec['max_log_bytes'] or not 1024<=spec['max_log_bytes']<=64*1024*1024):
                raise ValueError('Invalid Code_Aster options')
            code_aster.runtime_root(args['--executable'])
            code_aster.host_path(args['--executable'],args['--distro'])
        except (KeyError,TypeError,ValueError,AttributeError) as error:
            raise ExecutionConflict('evidence_invalid','Code_Aster command differs from its registered contract') from error

    def source(self, prefix, descriptor):
        source = descriptor['source']
        paths=tuple(path for path in source['files'] if path!=CASE_DICT_PATH)
        return inspect(prefix,artifacts.dependencies,source['plan'],paths=paths,optional_paths=(CASE_DICT_PATH,),
                       max_file_bytes=artifacts.MAX_INPUT)

    def before_publish(self, prefix, descriptor):
        for path in descriptor['output_files']:
            content = live_existing(prefix,path)
            actual = record(content) if content is not None else None
            if actual!=descriptor['targets_before'].get(path):
                raise ValueError('Code_Aster output destination changed: '+path)

    def artifacts(self, run, descriptor, output, frozen_files):
        return artifacts.artifacts(run,descriptor,output,frozen_files)
