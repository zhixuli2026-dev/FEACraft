"""CalculiX solver contract: selected dependencies, native proof and guarded aliases."""
import hashlib
from pathlib import Path
import re
import sys
import uuid

from sqlalchemy import select

from ..models import ArtifactPublication
from . import calculix_artifacts as artifacts, calculix_pressure_transfer
from .attempt_workspace import digest_file
from .case_provision import io
from .compose_artifacts import live_existing,record
from .case_read_view import inspect
from .durable_events import json_text
from .geometry_handler import GeometryHandler
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict

WORKER=Path(__file__).with_name('calculix_worker.py')


class CalculixHandler:
    key='calculix.solve';version='1';job_type='solve';publication_kind='calculix'
    cancellation=dict(GeometryHandler.cancellation);recovery=dict(GeometryHandler.recovery)

    def publication_channel(self,descriptor):
        return artifacts.publication_channel(descriptor['input'])

    async def prepare(self,db,case,user,*,parameters,budget,observations=None):
        if not {'input_path','executable'} <= parameters.keys() <= {'input_path','executable','pressure_transfer'}:
            raise ValueError('CalculiX requires an input and configured executable')
        path=artifacts.input_path(parameters['input_path'])
        transfer=references=None
        if 'pressure_transfer' in parameters:
            request=calculix_pressure_transfer.PressureTransferRequest.model_validate(parameters['pressure_transfer']).model_dump(mode='json',exclude_none=True)
            reader=observations if observations is not None else db
            row=await reader.get(ArtifactPublication,request['publication_id'])
            if row is None or row.case_id!=case.id or row.kind!='postprocess' or row.status not in {'committed','superseded'}:
                raise ValueError('Pressure publication is unavailable for this case')
            transfer=await io(calculix_pressure_transfer.select,case.s3_prefix,row,request)
            references=[{'publication_id':row.id,'paths':[transfer['metrics_path']],'require_current':False}]
        def read_inputs(root):
            source=artifacts.dependencies(root,path,pressure_transfer=transfer)
            return source,artifacts.targets(root,source)
        source,before=await io(inspect,case.s3_prefix,read_inputs)
        executable=str(Path(parameters['executable']).resolve())
        digest=(await io(digest_file,Path(executable)))['sha256']
        if observations is None:
            from .quota import check_job_quota
            await check_job_quota(db,user,case_id=case.id)
        identifier=str(uuid.uuid4());configuration={'source':source,'execution_id':identifier}
        spec={'command':'CalculiX solve','case_prefix':case.s3_prefix,'runtime':'windows',
            'argv':[sys.executable,str(WORKER),'--input='+path,'--executable='+executable,'--executable-sha256='+digest,
                '--execution-id='+identifier,'--timeout='+str(budget['timeout_seconds']),'--max-log-bytes='+str(budget['max_log_bytes'])],
            'timeout_seconds':budget['timeout_seconds'],'max_log_bytes':budget['max_log_bytes'],'evidence_run_dir':None}
        self.validate_spec(spec)
        descriptor={'handler':'calculix.v1','handler_version':self.version,'case_id':case.id,'input':path,
            'source':source,'configuration':configuration,'executable':executable,'executable_sha256':digest,
            'execution_id':identifier,'worker_sha256':hashlib.sha256(WORKER.read_bytes()).hexdigest(),
            'targets_before':before,'retired_aliases':{}}
        if references:
            descriptor['input_publications']=references
        kind=self.publication_channel(descriptor)
        from .artifact_publication import manifest_for_alias_paths
        previous=await (observations if observations is not None else db).scalars(select(ArtifactPublication).where(ArtifactPublication.case_id==case.id,
            ArtifactPublication.kind==kind,ArtifactPublication.status=='committed').order_by(ArtifactPublication.generation.desc()).limit(50))
        for row in previous:
            try:
                manifest=await io(manifest_for_alias_paths,case.s3_prefix,row)
                for name,value in manifest['files'].items():descriptor['retired_aliases'].setdefault(name,value)
            except (ValueError,OSError,KeyError):continue
        return PreparedStep(self.key,self.version,self.job_type,spec,sorted(source['files']),references,kind,descriptor,
            generated_inputs={artifacts.CONFIG:json_text(configuration).encode('utf8')})

    def validate_spec(self,spec):
        try:
            args=spec['argv']
            if (spec.get('command')!='CalculiX solve' or spec.get('runtime')!='windows' or spec.get('evidence_run_dir') is not None
                    or not isinstance(args,list) or len(args)!=8 or args[:2]!=[sys.executable,str(WORKER)]):raise ValueError('Invalid worker')
            parts=dict(value.split('=',1) for value in args[2:])
            if (set(parts)!={'--input','--executable','--executable-sha256','--execution-id','--timeout','--max-log-bytes'}
                    or artifacts.input_path(parts['--input'])!=parts['--input'] or not Path(parts['--executable']).is_absolute()
                    or not re.fullmatch('[0-9a-f]{64}',parts['--executable-sha256']) or str(uuid.UUID(parts['--execution-id']))!=parts['--execution-id']
                    or int(parts['--timeout'])!=spec['timeout_seconds'] or int(parts['--max-log-bytes'])!=spec['max_log_bytes']):
                raise ValueError('Invalid native parameters')
            if spec['timeout_seconds']<=0 or spec['max_log_bytes']<=0:raise ValueError('Invalid native budget')
        except (ValueError,KeyError,TypeError,AttributeError) as error:
            raise ExecutionConflict('evidence_invalid','CalculiX command differs from its registered contract') from error

    def source(self,prefix,descriptor):
        return inspect(prefix,artifacts.current_source,descriptor)

    def before_publish(self,prefix,descriptor):
        for path in descriptor['output_files']:
            data=live_existing(prefix,path);actual=record(data) if data is not None else None
            if actual!=descriptor['targets_before'].get(path):raise ValueError('CalculiX output destination changed: '+path)

    def artifacts(self,run,descriptor,output,frozen_files):
        records=artifacts.artifacts(run,descriptor,output,frozen_files)
        descriptor['retired_aliases']={p:r for p,r in descriptor['retired_aliases'].items() if p not in records}
        return records
