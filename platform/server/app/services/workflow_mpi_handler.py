"""Pin the existing native MPI pipeline to the canonical workflow solve channel.

Native receipts remain MPI receipts. Derived decomposition files stay in the
owned attempt; publishing them into the live controls would stale the mesh.
"""
import asyncio
from dataclasses import replace

from .. import storage
from .async_ops import finish_before_cancel
from .mpi_handler import MpiHandler
from .mpi_artifacts import is_input
from .solve_artifacts import configuration
from .case_evidence import safe_run_dir
from . import solve_publication
from .workflow_state import ExecutionConflict, InvalidStepInput
from .composite_inputs import live_selection,overlay_snapshot


class WorkflowMpiHandler(MpiHandler):
    key='solve.openfoam.mpi'
    publication_kind='solve'

    def publication_channel(self,descriptor):return self.publication_kind

    async def prepare(self,db,case,user,*,parameters,budget,observations=None,resolved_inputs=None):
        if 'run_dir' not in parameters or set(parameters)-{'run_dir','subdomains','mesh_publication_id'}:
            raise ExecutionConflict('invalid_request','Workflow MPI requires its run directory and process count')
        run_dir=safe_run_dir(parameters['run_dir']);count=parameters.get('subdomains',2)
        if type(count) is not int or not 1<=count<=64:
            raise ExecutionConflict('invalid_request','MPI process count must be an integer from 1 to 64')
        seconds=budget['timeout_seconds']
        if type(seconds) not in {int,float} or seconds<1 or int(seconds)!=seconds:
            raise ExecutionConflict('invalid_request','MPI timeout must use positive whole seconds')
        from .mesh_publication import inspect_inputs,_source_snapshot
        def inputs(root,directory):
            try:
                settings=configuration(root,directory)
            except ValueError as error:
                raise InvalidStepInput(str(error)) from error
            if 'convection_diffusivity' in settings:
                raise InvalidStepInput('托管对流换热边界请使用串行求解')
            if 'acoustic_medium' in settings or 'acoustic_density_kg_m3' in settings:
                raise InvalidStepInput('托管声学介质和阻抗边界请使用串行求解')
            if 'pressure_reference' in settings:
                raise InvalidStepInput('声明的压力参考值请使用串行求解')
            if 'conduction_material' in settings:
                raise InvalidStepInput('声明的材料导热请使用串行求解')
            if 'fluid_model' in settings or 'fluid_thermophysics' in settings:
                raise InvalidStepInput('声明的流动模型请使用串行求解')
            if 'rotation' in settings:
                raise InvalidStepInput('声明的 MRF 旋转区域请使用串行求解')
            if 'moving_frame' in settings or 'deformation' in settings:
                raise InvalidStepInput('声明的运动坐标系请使用串行求解')
            if 'continuum' in settings:
                raise InvalidStepInput('声明的连续介质守恒方程请使用串行求解')
            if 'cht_materials' in settings:
                raise InvalidStepInput('声明的区域材料共轭传热请使用串行求解')
            return settings,_source_snapshot(root,directory)
        config,source=await finish_before_cancel(asyncio.to_thread(inspect_inputs,case.s3_prefix,inputs,run_dir))
        from .artifact_publication import current,read_manifest
        from .publication_inputs import resolve
        reader=db if observations is None else observations
        _,mesh=await current(reader,case.id,'mesh')
        if mesh is None or parameters.get('mesh_publication_id',mesh.id)!=mesh.id:
            raise ExecutionConflict('input_stale','Confirmed workflow mesh is no longer current')
        if observations is not None and storage._use_s3():
            from .publication_objects import bound_metadata
            manifest=await finish_before_cancel(asyncio.to_thread(bound_metadata,case.s3_prefix,mesh))
        else:
            _,manifest=await finish_before_cancel(asyncio.to_thread(read_manifest,case.s3_prefix,mesh))
        if manifest['descriptor']['run_dir']!=run_dir:
            raise ExecutionConflict('input_stale','MPI mesh belongs to another run directory')
        paths=sorted(p for p in manifest['files'] if p.startswith(run_dir+'/constant/polyMesh/'))
        if not paths:raise ExecutionConflict('evidence_invalid','MPI has no published mesh files')
        references=[{'publication_id':mesh.id,'paths':paths,'require_current':True}]
        resolved=await resolve(reader,case,references)
        if resolved_inputs is not None:
            resolved_inputs.extend(resolved)
        prepared=await super().prepare(db,case,user,
            parameters={'run_dir':run_dir,'solver':config['solver'],'subdomains':count},
            budget={**budget,'timeout_seconds':int(seconds)})
        native=prepared.descriptor
        source_overlay=overlay_snapshot(native['source'],resolved)
        native['source']=source_overlay;native['configuration']['source']=source_overlay
        from .durable_events import json_text
        from .mpi_artifacts import CONFIG
        generated={**prepared.generated_inputs,CONFIG:json_text(native['configuration']).encode('utf8')}
        # The sealed input union mounts the immutable mesh once. Selecting the
        # same mesh from the live case as well is deliberately rejected by seal.
        live_paths=live_selection(native['source'],paths)
        descriptor={'handler':'workflow-mpi.v1','handler_version':self.version,'case_id':case.id,
            'run_dir':run_dir,'execution_id':native['execution_id'],'subdomains':count,
            'source':source,'input_publications':references,'native':native,
            'worker_sha256':native['worker_sha256'],'native_sha256':native['native_sha256'],'distro':native['distro']}
        return replace(prepared,key=self.key,publication_kind=self.publication_kind,
            input_paths=live_paths,input_publications=references,descriptor=descriptor,generated_inputs=generated)

    def source(self,prefix,descriptor):
        engineering=solve_publication.source_snapshot(prefix,descriptor['run_dir'])
        if descriptor['source'].get('schema')=='vibeflow.workflow-mpi-source.v2':
            from .case_query_source import current
            pinned={p for ref in descriptor['input_publications'] for p in ref['paths']}
            return {'schema':'vibeflow.workflow-mpi-source.v2','engineering':engineering,
                'native_case':current(prefix,descriptor['native'],pinned_paths=pinned,retired_aliases=descriptor.get('retired_aliases'))}
        return engineering

    def artifacts(self,run,descriptor,output,frozen_files):
        records=super().artifacts(run,descriptor['native'],output,frozen_files)
        if descriptor['native']['source'].get('schema')=='vibeflow.case-file-queries.v1':
            engineering=descriptor['source'].get('engineering') if descriptor['source'].get('schema')=='vibeflow.workflow-mpi-source.v2' else descriptor['source']
            descriptor['source']={'schema':'vibeflow.workflow-mpi-source.v2','engineering':engineering,
                'native_case':descriptor['native']['source']}
        # Keep truthful MPI evidence, reconstructed fields, ranks and logs. The
        # generated decomposeParDict remains bound by the native receipt only.
        records={p:r for p,r in records.items() if not is_input(p,descriptor['run_dir'])}
        evidence=descriptor['run_dir']+'/mpi-evidence.json'
        if evidence not in records:raise ValueError('Workflow MPI result is missing its native evidence')
        descriptor['output_files']=records
        descriptor['deleted_files']={p:r for p,r in descriptor['native']['deleted_files'].items()
            if not is_input(p,descriptor['run_dir'])}
        return records

    def before_publish(self,prefix,descriptor):
        return super().before_publish(prefix,descriptor['native'])
