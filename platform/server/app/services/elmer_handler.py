"""Registered Elmer execution, ownership and publication contract."""
import hashlib
from pathlib import Path
import re
import sys
import uuid

from sqlalchemy import select

from ..models import ArtifactPublication
from . import elmer_artifacts as artifacts
from .attempt_workspace import digest_file
from .case_provision import io
from .case_read_view import inspect
from .compose_artifacts import live_existing, record
from .durable_events import json_text
from .geometry_handler import GeometryHandler
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict


WORKER = Path(__file__).with_name('elmer_worker.py')
ARGUMENTS = {'--input','--executable','--executable-sha256','--execution-id','--timeout','--max-log-bytes'}


class ElmerHandler:
    key = 'elmer.solve'
    version = '1'
    job_type = 'solve'
    publication_kind = 'elmer'
    cancellation = dict(GeometryHandler.cancellation)
    recovery = dict(GeometryHandler.recovery)

    def publication_channel(self, descriptor):
        return 'elmer:'+hashlib.sha256(artifacts.input_path(descriptor['input']).casefold().encode()).hexdigest()[:40]

    async def prepare(self, db, case, user, *, parameters, budget, observations=None):
        if set(parameters) != {'input_path','executable'}:
            raise ValueError('Elmer requires a SIF and the configured solver')
        path = artifacts.input_path(parameters['input_path'])

        def read_inputs(root):
            source = artifacts.dependencies(root,path)
            return source,artifacts.targets(root,source)

        source,before = await io(inspect,case.s3_prefix,read_inputs)
        executable = str(Path(parameters['executable']).resolve())
        tool = (await io(digest_file,Path(executable)))['sha256']
        if observations is None:
            from .quota import check_job_quota
            await check_job_quota(db,user,case_id=case.id)
        identifier = str(uuid.uuid4())
        configuration = {'source':source,'execution_id':identifier}
        spec = {'command':'Elmer solve','case_prefix':case.s3_prefix,'runtime':'windows',
            'argv':[sys.executable,str(WORKER),'--input='+path,'--executable='+executable,
                    '--executable-sha256='+tool,'--execution-id='+identifier,
                    '--timeout='+str(budget['timeout_seconds']),'--max-log-bytes='+str(budget['max_log_bytes'])],
            'timeout_seconds':budget['timeout_seconds'],'max_log_bytes':budget['max_log_bytes'],'evidence_run_dir':None}
        self.validate_spec(spec)
        descriptor = {'handler':'elmer.v1','handler_version':self.version,'case_id':case.id,'input':path,
            'source':source,'configuration':configuration,'executable':executable,'executable_sha256':tool,
            'execution_id':identifier,'worker_sha256':(await io(digest_file,WORKER))['sha256'],
            'targets_before':before,'retired_aliases':{}}
        kind = self.publication_channel(descriptor)
        from .artifact_publication import manifest_for_alias_paths
        previous = await (observations if observations is not None else db).scalars(select(ArtifactPublication)
            .where(ArtifactPublication.case_id==case.id,ArtifactPublication.kind==kind,ArtifactPublication.status=='committed')
            .order_by(ArtifactPublication.generation.desc()).limit(50))
        for row in previous:
            try:
                manifest = await io(manifest_for_alias_paths,case.s3_prefix,row)
                for name,value in manifest['files'].items():
                    descriptor['retired_aliases'].setdefault(name,value)
            except (ValueError,OSError,KeyError):
                continue
        return PreparedStep(self.key,self.version,self.job_type,spec,sorted(source['files']),None,kind,descriptor,
                            generated_inputs={artifacts.CONFIG:json_text(configuration).encode('utf8')})

    def validate_spec(self, spec):
        try:
            argv = spec['argv']
            if (spec.get('command')!='Elmer solve' or spec.get('runtime')!='windows' or spec.get('evidence_run_dir') is not None
                    or not isinstance(argv,list) or len(argv)!=8 or argv[:2]!=[sys.executable,str(WORKER)]):
                raise ValueError('Invalid Elmer worker')
            arguments = dict(arg.split('=',1) for arg in argv[2:])
            if (set(arguments)!=ARGUMENTS or artifacts.input_path(arguments['--input'])!=arguments['--input']
                    or not Path(arguments['--executable']).is_absolute()
                    or not re.fullmatch('[0-9a-f]{64}',arguments['--executable-sha256'])
                    or str(uuid.UUID(arguments['--execution-id']))!=arguments['--execution-id']
                    or int(arguments['--timeout'])!=spec['timeout_seconds'] or not 0<spec['timeout_seconds']<=86400
                    or int(arguments['--max-log-bytes'])!=spec['max_log_bytes'] or not 1024<=spec['max_log_bytes']<=64*1024*1024):
                raise ValueError('Invalid Elmer native arguments')
        except (KeyError,TypeError,ValueError,AttributeError) as error:
            raise ExecutionConflict('evidence_invalid','Elmer command differs from its registered contract') from error

    def source(self, prefix, descriptor):
        return inspect(prefix,artifacts.dependencies,descriptor['input'])

    def before_publish(self, prefix, descriptor):
        for path in descriptor['output_files']:
            content = live_existing(prefix,path)
            actual = record(content) if content is not None else None
            if actual != descriptor['targets_before'].get(path):
                raise ValueError('Elmer output destination changed: '+path)

    def artifacts(self, run, descriptor, output, frozen_files):
        return artifacts.artifacts(run,descriptor,output,frozen_files)
