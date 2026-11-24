"""ParaView scripts: complete local input snapshot and guarded file publication."""
import asyncio
import hashlib
from pathlib import Path
import re
import sys
import uuid

from . import paraview_artifacts as artifacts, attempt_workspace as workspace, paraview_materials, paraview_pipeline
from .async_ops import finish_before_cancel
from .compose_artifacts import live_existing,record
from .case_read_view import inspect, delta_source
from .durable_events import json_text
from .geometry_handler import GeometryHandler
from .step_handlers import PreparedStep
from .workflow_state import ExecutionConflict

WORKER=Path(__file__).with_name('paraview_worker.py')


class ParaViewHandler:
    key='paraview.run';version='1';job_type='pvpython';publication_kind='paraview'
    cancellation=dict(GeometryHandler.cancellation);recovery=dict(GeometryHandler.recovery)

    def publication_channel(self,descriptor):
        script = descriptor['script']
        configuration = descriptor['configuration']
        subject = (configuration['catalyst']['folder'] if 'catalyst' in configuration
                   else configuration['pipeline']['folder'] if script == artifacts.PIPELINE
                   else artifacts.script_path(script))
        return 'paraview:'+hashlib.sha256(subject.casefold().encode()).hexdigest()[:40]

    async def prepare(self,db,case,user,*,parameters,budget):
        fields = set(parameters)
        required = {'nodes', 'backend', 'executable'}
        catalyst = parameters.get('catalyst')
        if 'catalyst' in fields and (not isinstance(catalyst,dict) or set(catalyst) != {'state_path','version'}):
            raise ValueError('Catalyst requires a state path and interface version')
        if required <= fields <= required | {'animation','extractors','inspection','plugins','volume_format'}:
            from .paraview_catalog import catalogue

            plugins = paraview_pipeline.normalize_plugins(parameters.get('plugins'))
            operators = {'sources':set(),'filters':set()}
            readers = {}
            for name in plugins:
                detail = await finish_before_cancel(asyncio.to_thread(catalogue,name,parameters['executable']))
                formats = paraview_pipeline.PLUGIN_READERS.get(name,{})
                for proxy in detail['proxies']:
                    if proxy['group'] in operators and proxy['name'] in paraview_pipeline.PLUGIN_OPERATORS.get(name,()):
                        operators[proxy['group']].add(proxy['name'])
                    if proxy['group'] == 'sources' and proxy['name'] in formats:
                        readers[proxy['name']] = formats[proxy['name']]
            nodes = paraview_pipeline.normalize(parameters['nodes'], operators, readers)
            animation = paraview_pipeline.normalize_animation(parameters.get('animation'), nodes)
            extractors = paraview_pipeline.normalize_extractors(parameters.get('extractors'), nodes)
            inspection = paraview_pipeline.normalize_inspection(parameters.get('inspection'), nodes, plugins)
            volume_format = parameters.get('volume_format')
            if volume_format is not None and (volume_format not in ('NIfTI','Analyze') or 'AnalyzeNIfTIReaderWriter' not in plugins):
                raise ValueError('Choose the installed Analyze/NIfTI plugin for volume export')
            path = artifacts.PIPELINE
        elif {'script_path', 'backend', 'executable'} <= fields <= {'script_path', 'backend', 'executable', 'catalyst'}:
            nodes = None
            inspection = None
            path = artifacts.script_path(parameters['script_path'])
            if catalyst is not None:
                state = artifacts.script_path(catalyst['state_path'])
                if (state.casefold() == path.casefold() or type(catalyst['version']) is not int
                        or catalyst['version'] not in (1,2)):
                    raise ValueError('Catalyst requires a separate driver and state for version 1 or 2')
                if catalyst['version'] == 2:
                    from .paraview import catalyst_implementation
                    catalyst_implementation(parameters['executable'])
        else:
            raise ValueError('ParaView requires a script or pipeline, backend and configured executable')
        backend=parameters['backend']
        if backend not in {'pvpython','pvbatch'}:raise ValueError('Unsupported ParaView backend')
        def read_inputs(root):
            if nodes is None:
                workspace.checked_path(root,path)
                if catalyst is not None and not workspace.checked_path(root,state).is_file():
                    raise ValueError('Catalyst state must be a Python file')
            else:
                for node in nodes:
                    if node['group'] == 'file':
                        for relative in node.get('files') or [node['file']]:
                            if not workspace.checked_path(root, relative).is_file():
                                raise ValueError('ParaView input must be a dataset file')
            raytrace = paraview_materials.resolve(root,inspection['raytrace']) if inspection and inspection.get('raytrace') else None
            return artifacts.snapshot(root),workspace.live_roots(root),raytrace
        source,paths,raytrace=await finish_before_cancel(asyncio.to_thread(inspect,case.s3_prefix,read_inputs))
        if raytrace is not None:
            inspection['raytrace'] = raytrace
        if nodes is None and source['files'][path]['size']>1024*1024:raise ValueError('ParaView script exceeds 1 MiB')
        if catalyst is not None and source['files'][state]['size'] > 1024*1024:
            raise ValueError('Catalyst state exceeds 1 MiB')
        executable=str(Path(parameters['executable']).resolve())
        digest=await finish_before_cancel(asyncio.to_thread(lambda:hashlib.sha256(Path(executable).read_bytes()).hexdigest()))
        from .quota import check_job_quota
        await check_job_quota(db,user, case_id=case.id)
        identifier=str(uuid.uuid4());configuration={'source':source,'execution_id':identifier,'script':path,'backend':backend}
        generated = {}
        if catalyst is not None:
            configuration['catalyst'] = {'state_path':state,'version':catalyst['version'],
                                         'folder':f'D-post/paraview-catalyst/{identifier}'}
        if nodes is not None:
            folder = f'D-post/paraview-pipelines/{identifier}'
            script = paraview_pipeline.stage(nodes, folder, animation, extractors, inspection, plugins, volume_format)
            generated[path] = script
            configuration['pipeline'] = {'folder': folder, 'nodes': nodes,
                                         'script': {'size': len(script), 'sha256': hashlib.sha256(script).hexdigest()}}
            if animation is not None:
                configuration['pipeline']['animation'] = animation
            if extractors:
                configuration['pipeline']['extractors'] = extractors
            if inspection:
                configuration['pipeline']['inspection'] = inspection
            if plugins:
                configuration['pipeline']['plugins'] = plugins
            if volume_format:
                configuration['pipeline']['volume_format'] = volume_format
        generated[artifacts.CONFIG] = json_text(configuration).encode('utf-8')
        spec={'command':'ParaView '+backend,'case_prefix':case.s3_prefix,'runtime':'windows',
            'argv':[sys.executable,str(WORKER),'--script='+path,'--backend='+backend,'--executable='+executable,
                '--executable-sha256='+digest,'--execution-id='+identifier,'--timeout='+str(budget['timeout_seconds']),
                '--max-log-bytes='+str(budget['max_log_bytes'])],
            'timeout_seconds':budget['timeout_seconds'],'max_log_bytes':budget['max_log_bytes'],'evidence_run_dir':None}
        self.validate_spec(spec)
        descriptor={'handler':'paraview.v1','handler_version':self.version,'case_id':case.id,'script':path,'backend':backend,
            'source':source,'configuration':configuration,'executable':executable,'executable_sha256':digest,
            'execution_id':identifier,'worker_sha256':hashlib.sha256(WORKER.read_bytes()).hexdigest()}
        return PreparedStep(self.key,self.version,self.job_type,spec,paths,None,self.publication_channel(descriptor),
            descriptor,generated_inputs=generated)

    def validate_spec(self,spec):
        try:
            args=spec['argv']
            if (spec.get('runtime')!='windows' or spec.get('evidence_run_dir') is not None
                    or not isinstance(args,list) or len(args)!=9 or args[:2]!=[sys.executable,str(WORKER)]):raise ValueError('Invalid worker')
            parts=dict(value.split('=',1) for value in args[2:])
            if (set(parts)!={'--script','--backend','--executable','--executable-sha256','--execution-id','--timeout','--max-log-bytes'}
                    or (parts['--script'] != artifacts.PIPELINE and artifacts.script_path(parts['--script'])!=parts['--script'])
                    or parts['--backend'] not in {'pvpython','pvbatch'}
                    or spec.get('command')!='ParaView '+parts['--backend'] or not Path(parts['--executable']).is_absolute()
                    or not re.fullmatch('[0-9a-f]{64}',parts['--executable-sha256']) or str(uuid.UUID(parts['--execution-id']))!=parts['--execution-id']
                    or int(parts['--timeout'])!=spec['timeout_seconds'] or int(parts['--max-log-bytes'])!=spec['max_log_bytes']):
                raise ValueError('Invalid native parameters')
            if spec['timeout_seconds']<=0 or spec['max_log_bytes']<=0:raise ValueError('Invalid native budget')
        except (ValueError,KeyError,TypeError,AttributeError) as error:
            raise ExecutionConflict('evidence_invalid','ParaView command differs from its registered contract') from error

    def source(self,prefix,descriptor):
        return delta_source(prefix,descriptor)

    def before_publish(self,prefix,descriptor):
        before=artifacts.file_records(descriptor['source'])
        for path in set(descriptor['output_files'])|set(descriptor['deleted_files']):
            data=live_existing(prefix,path);actual=record(data) if data is not None else None
            if actual!=before.get(path):raise ValueError('ParaView destination changed: '+path)

    def artifacts(self,run,descriptor,output,frozen_files):
        return artifacts.artifacts(run,descriptor,output,frozen_files)
