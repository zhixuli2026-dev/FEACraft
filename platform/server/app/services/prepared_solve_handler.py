"""Prepared-run serial members using the canonical real serial worker."""
import asyncio,hashlib,json,shlex,shutil,uuid
from pathlib import Path
from . import attempt_workspace as workspace,case_file_delta as delta,case_file_queries as queries
from .async_ops import finish_before_cancel
from .case_read_view import inspect,delta_source
from .case_evidence import safe_run_dir
from .compose_artifacts import live_existing,record
from .durable_events import json_text
from .mesh_artifacts import input_snapshot
from .runtime_config import get_runtime_setting
from .solve_artifacts import configuration,managed
from .solve_handler import SolveHandler,WORKER,command
from . import mesh_worker,solve_publication
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict

CONFIG='.vibeflow/prepared/serial.json'
QUERIES='.vibeflow/serial/queries.json'
SOURCE='vibeflow.prepared-serial-source.v1'


class PreparedSolveHandler(SolveHandler):
    key='solve.openfoam.prepared'
    publication_kind='serial'
    recovery={**SolveHandler.recovery,'resume':False,'resume_policy':'explicit_fresh_prepared_run'}

    def publication_channel(self,descriptor):
        return 'serial:'+hashlib.sha256(descriptor['run_dir'].casefold().encode()).hexdigest()[:40]

    async def prepare(self,db,case,user,*,parameters,budget):
        if set(parameters)!={'run_dir','solver'}:raise ValueError('Prepared serial solve requires its run directory and solver')
        run_dir=safe_run_dir(parameters['run_dir'])
        seconds=budget['timeout_seconds'];limit=budget['max_log_bytes']
        if (type(seconds) not in {int,float} or not 1<=seconds<=86400 or int(seconds)!=seconds
                or type(limit) is not int or not 1024<=limit<=67108864):raise ValueError('Invalid prepared serial budget')
        if not shutil.which('wsl.exe'):raise ExecutionConflict('runtime_unavailable','Prepared serial solve requires WSL')
        def read(root):return configuration(root,run_dir),input_snapshot(root,run_dir),delta.snapshot(root),workspace.live_roots(root)
        cfg,engineering,source,paths=await finish_before_cancel(asyncio.to_thread(inspect,case.s3_prefix,read))
        if cfg['solver']!=parameters['solver']:raise ValueError('Prepared serial solver differs from controlDict')
        if 'structural_feedback' in cfg:
            raise ValueError('结构位移反馈需要工作流封存所引用的 CalculiX 发布')
        from .quota import check_job_quota
        await check_job_quota(db,user, case_id=case.id)
        identifier=str(uuid.uuid4());distro=get_runtime_setting('wsl_distro','vibeflow-linux')
        bashrc=get_runtime_setting('openfoam_bashrc','/usr/lib/openfoam/openfoam2406/etc/bashrc')
        config={'schema':'vibeflow.prepared-serial.v1','execution_id':identifier,'run_dir':run_dir,'distro':distro,
            'configuration':cfg,'source':source,'max_log_bytes':limit,
            'retired_files':{p:r for p,r in source['files'].items() if managed(p,run_dir)},
            'case_queries':{'schema':queries.SCHEMA,'helper_sha256':hashlib.sha256(Path(queries.__file__).read_bytes()).hexdigest()}}
        spec={'command':command(run_dir,identifier,distro,bashrc,limit,cfg['solver'])+' --prepared-source '+CONFIG,
            'case_prefix':case.s3_prefix,'runtime':'wsl','argv':None,'timeout_seconds':int(seconds),'max_log_bytes':limit,'evidence_run_dir':None}
        self.validate_spec(spec)
        descriptor={'handler':'prepared-solve.v1','handler_version':self.version,'case_id':case.id,'run_dir':run_dir,
            'solver':cfg['solver'],'source':source,'engineering_source':engineering,'configuration':cfg,
            'prepared_configuration':config,'execution_id':identifier,'distro':distro,
            'worker_sha256':hashlib.sha256(WORKER.read_bytes()).hexdigest(),
            'runner_sha256':hashlib.sha256(Path(mesh_worker.__file__).read_bytes()).hexdigest()}
        return PreparedStep(self.key,self.version,self.job_type,spec,paths,None,self.publication_channel(descriptor),descriptor,
            generated_inputs={CONFIG:json_text(config).encode('utf8')})

    def validate_spec(self,spec):
        try:
            parts=shlex.split(spec['command'])
            if len(parts)!=18 or parts[-2:]!=['--prepared-source',CONFIG]:raise ValueError('Invalid prepared serial worker')
            prefix,tail=spec['command'].rsplit(' --prepared-source ',1)
            if tail!=CONFIG:raise ValueError('Invalid prepared serial source path')
            super().validate_spec({**spec,'command':prefix})
        except (ValueError,KeyError,TypeError) as error:
            raise ExecutionConflict('evidence_invalid','Prepared serial command differs from registered contract') from error

    def source(self,prefix,descriptor):
        if descriptor['source'].get('schema')==SOURCE:
            from .case_query_source import current
            return {'schema':SOURCE,'engineering':solve_publication.source_snapshot(prefix,descriptor['run_dir']),
                'native_case':current(prefix,{**descriptor,'source':descriptor['source']['native_case']})}
        return delta_source(prefix,descriptor)

    def before_publish(self,prefix,descriptor):
        before=delta.file_records(descriptor.get('origin_source',descriptor['source']))
        for path in set(descriptor['output_files'])|set(descriptor['deleted_files']):
            data=live_existing(prefix,path)
            if (record(data) if data is not None else None)!=before.get(path):raise ValueError('Prepared serial destination changed: '+path)

    def artifacts(self,run,descriptor,output,frozen_files):
        config=json.loads(workspace.checked_path(run,CONFIG).read_text(encoding='utf8'))
        origin=descriptor.get('origin_source',descriptor['source'])
        if config!=descriptor['prepared_configuration'] or {p:r for p,r in frozen_files.items() if p!=CONFIG}!=origin['files']:
            raise ValueError('Prepared serial complete seal differs from preparation')
        base=solve_publication.artifacts_for(run,{**descriptor,'source':descriptor['engineering_source']},output,frozen_files)
        actual=delta.snapshot(run)['files']
        if actual!={p:r for p,r in output['files'].items() if p.split('/')[0].casefold()!='.vibeflow'}:
            raise ValueError('Prepared serial outputs changed after capture')
        for path,expected in origin['files'].items():
            if path not in config['retired_files'] and actual.get(path)!=expected:raise ValueError('Prepared serial changed an original input: '+path)
        changed={p:r for p,r in actual.items() if origin['files'].get(p)!=r}
        if any(not p.startswith(descriptor['run_dir']+'/') for p in changed):raise ValueError('Prepared serial wrote outside its run directory')
        records={**base,**changed};deleted={p:r for p,r in origin['files'].items() if p not in actual}
        proof=json.loads(workspace.checked_path(run,QUERIES).read_text(encoding='utf8'))
        if (proof.get('schema')!='vibeflow.serial-queries.v1' or proof['execution_id']!=descriptor['execution_id']
                or proof['prepared_sha256']!=workspace.digest_file(run/CONFIG)['sha256']):raise ValueError('Prepared serial query receipt differs')
        descriptor.update(output_files=records,deleted_files=deleted,query_receipt=proof)
        from .case_query_source import attach
        projected={**descriptor,'configuration':config};attach(projected,proof)
        descriptor['origin_source']=projected['origin_source']
        descriptor['source']={'schema':SOURCE,'engineering':descriptor['engineering_source'],'native_case':projected['source']}
        return records
