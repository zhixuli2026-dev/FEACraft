"""Register MOOSE preparation, owned native execution and immutable publication."""
from functools import partial
import hashlib
from pathlib import Path
import re
import sys
import uuid

from ..models import ArtifactPublication
from . import moose, moose_artifacts as artifacts, moose_checkpoint
from .attempt_workspace import digest_file
from .case_provision import io
from .case_read_view import inspect
from .casedict import CASE_DICT_PATH
from .compose_artifacts import live_existing, record
from .durable_events import json_text
from .geometry_handler import GeometryHandler
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict


WORKER = Path(__file__).with_name('moose_worker.py')
ARGUMENTS = {'--input','--executable','--distro','--executable-sha256','--execution-id','--timeout','--max-log-bytes'}


class MooseHandler:
    key = 'moose.solve'
    version = '1'
    job_type = 'solve'
    publication_kind = 'moose'
    cancellation = dict(GeometryHandler.cancellation)
    recovery = dict(GeometryHandler.recovery)

    def publication_channel(self, descriptor):
        path = artifacts.input_path(descriptor['input'])
        return 'moose:'+hashlib.sha256(path.casefold().encode()).hexdigest()[:40]

    async def prepare(self, db, case, user, *, parameters, budget, observations=None):
        if set(parameters)-{'restart'} != {'input_path','executable'}:
            raise ValueError('MOOSE requires an input and the configured solver')
        path = artifacts.input_path(parameters['input_path'])
        restart = parameters.get('restart')
        references = None
        if restart is not None:
            if not isinstance(restart,dict) or set(restart)!={'publication_id','checkpoint_path'}:
                raise ValueError('MOOSE restart requires a publication and checkpoint path')
            published = restart['checkpoint_path']
            if not isinstance(published,str) or not published.endswith('_checkpoint.zip'):
                raise ValueError('MOOSE restart requires the published checkpoint ZIP')
            original_input = artifacts.input_path(published.removesuffix('_checkpoint.zip')+'.i')
            if artifacts.output_paths(original_input,checkpoint=True)[-1]!=published:
                raise ValueError('MOOSE checkpoint path is not canonical')
            references = [{'publication_id':str(uuid.UUID(restart['publication_id'])),'paths':[published],'require_current':False}]
            restart = {'publication_id':references[0]['publication_id'],'checkpoint_path':published}
            row = await (observations if observations is not None else db).get(ArtifactPublication,restart['publication_id'])
            if row is None or row.case_id!=case.id or row.kind!=self.publication_channel({'input':original_input}):
                raise ValueError('The selected MOOSE checkpoint is unavailable in this case')
        executable,distro = await io(moose.configured)
        if executable != parameters['executable']:
            raise ExecutionConflict('runtime_unavailable','MOOSE executable changed during submission')
        tool = (await io(digest_file,moose.host_path(executable,distro)))['sha256']

        read_input = partial(inspect,paths=(path,),optional_paths=(CASE_DICT_PATH,))
        source = await io(read_input,case.s3_prefix,artifacts.dependencies,path,restart is not None)
        checkpoint = source.get('checkpoint',False)
        steps = source.get('mesh_adaptivity',{}).get('cycles',0)
        outputs = artifacts.output_paths(path,checkpoint=checkpoint,adaptive_steps=steps)
        if restart is not None and restart['checkpoint_path'] in outputs:
            raise ValueError('MOOSE restart checkpoint input and output must have different paths')
        read_outputs = partial(inspect,paths=(),optional_paths=outputs)
        before = await io(read_outputs,case.s3_prefix,partial(artifacts.targets,checkpoint=checkpoint,adaptive_steps=steps),path)
        if observations is None:
            from .quota import check_job_quota
            await check_job_quota(db,user,case_id=case.id)
        identifier = str(uuid.uuid4())
        configuration = {'source':source,'execution_id':identifier,'distro':distro}
        if restart is not None:
            configuration['restart'] = restart
        spec = {'command':'MOOSE solve','case_prefix':case.s3_prefix,'runtime':'windows',
            'argv':[sys.executable,str(WORKER),'--input='+path,'--executable='+executable,'--distro='+distro,
                    '--executable-sha256='+tool,'--execution-id='+identifier,
                    '--timeout='+str(budget['timeout_seconds']),'--max-log-bytes='+str(budget['max_log_bytes'])],
            'timeout_seconds':budget['timeout_seconds'],'max_log_bytes':budget['max_log_bytes'],'evidence_run_dir':None}
        descriptor = {'handler':'moose.v1','handler_version':self.version,'case_id':case.id,'input':path,
            'source':source,'configuration':configuration,'executable':executable,'executable_sha256':tool,
            'distro':distro,'execution_id':identifier,'worker_sha256':(await io(digest_file,WORKER))['sha256'],
            'targets_before':before}
        if 'model_sha256' in source:
            descriptor['native_sha256'] = (await io(digest_file,moose_checkpoint.NATIVE))['sha256']
            spec['argv'].append('--native-sha256='+descriptor['native_sha256'])
        if references is not None:
            descriptor['input_publications'] = references
        self.validate_spec(spec)
        return PreparedStep(self.key,self.version,self.job_type,spec,sorted(source['files']),references,self.publication_channel(descriptor),
            descriptor,generated_inputs={artifacts.CONFIG:json_text(configuration).encode('utf8')})

    def validate_spec(self, spec):
        try:
            argv = spec['argv']
            if (spec.get('command')!='MOOSE solve' or spec.get('runtime')!='windows' or spec.get('evidence_run_dir') is not None
                    or not isinstance(argv,list) or len(argv) not in (9,10) or argv[:2]!=[sys.executable,str(WORKER)]):
                raise ValueError('Invalid MOOSE worker')
            args = dict(value.split('=',1) for value in argv[2:])
            native = args.pop('--native-sha256',None)
            if (set(args)!=ARGUMENTS or (len(argv)==10 and (native is None or not re.fullmatch('[0-9a-f]{64}',native)))
                    or artifacts.input_path(args['--input'])!=args['--input']
                    or not re.fullmatch('[0-9a-f]{64}',args['--executable-sha256'])
                    or str(uuid.UUID(args['--execution-id']))!=args['--execution-id']
                    or int(args['--timeout'])!=spec['timeout_seconds'] or not 0<spec['timeout_seconds']<=86400
                    or int(args['--max-log-bytes'])!=spec['max_log_bytes'] or not 1024<=spec['max_log_bytes']<=64*1024*1024):
                raise ValueError('Invalid MOOSE options')
            moose.host_path(args['--executable'],args['--distro'])
        except (KeyError,TypeError,ValueError,AttributeError) as error:
            raise ExecutionConflict('evidence_invalid','MOOSE command differs from its registered contract') from error

    def source(self, prefix, descriptor):
        path = descriptor['input']
        return inspect(prefix,artifacts.dependencies,path,'restart' in descriptor['configuration'],paths=(path,),
                       optional_paths=(CASE_DICT_PATH,))

    def before_publish(self, prefix, descriptor):
        for path in descriptor['output_files']:
            content = live_existing(prefix,path)
            actual = record(content) if content is not None else None
            if actual != descriptor['targets_before'].get(path):
                raise ValueError('MOOSE output destination changed: '+path)

    def artifacts(self, run, descriptor, output, frozen_files):
        return artifacts.artifacts(run,descriptor,output,frozen_files)
