"""Explicit serial solver continuation from an authenticated captured checkpoint."""
import asyncio,hashlib,json,shlex,uuid
from dataclasses import dataclass
from pathlib import Path
from ..models import Job,JobAttempt
from . import solve_resume_artifacts as checkpoint,solve_worker,mesh_worker
from .async_ops import finish_before_cancel
from .solve_handler import SolveHandler,command
from .step_handlers import PreparedStep,validate_binding
from .workflow_state import ExecutionConflict
from . import openfoam_runtime


@dataclass(frozen=True)
class PreparedResume(PreparedStep):
    source_view:object|None=None


def changed_input(expected, actual):
    before, after = expected.get('files', {}), actual.get('files', {})
    path = min((p for p in before.keys() | after.keys() if before.get(p) != after.get(p)), default=None)
    if path:
        return path
    if expected.get('case_input_sha256') != actual.get('case_input_sha256'):
        return '0-caseDict/caseDict'
    return '算例输入目录或文件选择'


class ResumeHandler(SolveHandler):
    key='solve.openfoam.resume'
    version='1'
    recovery={**SolveHandler.recovery,'resume':True,'resume_policy':'explicit_new_attempt_with_validated_native_checkpoint'}

    async def prepare(self,db,case,user,*,parameters,budget):
        if set(parameters)!={'source_attempt_id','time'}:
            raise ExecutionConflict('invalid_request','Resume requires source_attempt_id and an explicit written time')
        attempt=await db.get(JobAttempt,parameters['source_attempt_id'],populate_existing=True)
        job=await db.get(Job,attempt.job_id,populate_existing=True) if attempt else None
        if job is None or job.case_id!=case.id:
            raise ExecutionConflict('invalid_request','Checkpoint attempt is unavailable for this case')
        original=validate_binding(job,attempt)
        if original is None or original.key not in {'solve.openfoam','solve.openfoam.resume'}:
            raise ExecutionConflict('invalid_request','This solver does not have the serial OpenFOAM checkpoint contract')
        descriptor=json.loads(attempt.publication_json)['descriptor']
        from .solve_restart_policy import select_time
        selected_time=select_time(descriptor,parameters['time'])
        from .local_source_reads import read_source
        actual=await finish_before_cancel(asyncio.to_thread(read_source,original,case.s3_prefix,descriptor))
        if actual != descriptor['source']:
            changed = changed_input(descriptor['source'], actual)
            raise ExecutionConflict('input_stale', f'续算输入与原尝试不一致（{changed}）。请恢复原输入或按当前输入重跑。')
        from .publication_inputs import validate
        manifest=json.loads(attempt.input_manifest_json)['workspace'];await validate(db,case,manifest)
        selected=asyncio.create_task(asyncio.to_thread(checkpoint.inspect_source,case.s3_prefix,job,attempt,descriptor,selected_time))
        try:
            data,control,view=await finish_before_cancel(selected)
        except asyncio.CancelledError:
            if not selected.cancelled() and selected.exception() is None:
                await finish_before_cancel(asyncio.to_thread(selected.result()[2].cleanup))
            raise
        except (OSError,ValueError,TypeError,KeyError) as error:
            raise ExecutionConflict('checkpoint_invalid','原执行结果不能作为所选续算检查点：'+str(error)) from error
        try:return self.prepared(case,descriptor,data,control,view,budget)
        except BaseException:
            await finish_before_cancel(asyncio.to_thread(view.cleanup));raise

    def prepared(self,case,descriptor,data,control,view,budget):
        encoded=json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode('utf8')
        run_dir=data['run_dir'];execution_id=str(uuid.uuid4())
        distro,bashrc=openfoam_runtime.configured()
        if distro!=data['toolchain']['distro']:raise ExecutionConflict('runtime_unavailable','Checkpoint belongs to a different native runtime')
        source_build = {key:descriptor[key] for key in ('source_library_dir',) if key in descriptor}
        spec={'command':command(run_dir,execution_id,distro,bashrc,budget['max_log_bytes'],data['configuration']['solver'],**source_build)+
            ' --resume-checkpoint '+shlex.quote(run_dir+'/'+checkpoint.NAME),
            'case_prefix':case.s3_prefix,'runtime':'wsl','argv':None,'evidence_run_dir':None,
            'timeout_seconds':budget['timeout_seconds'],'max_log_bytes':budget['max_log_bytes']}
        spec.update(openfoam_runtime.transport(spec['command'],distro))
        self.validate_spec(spec)
        return PreparedResume(self.key,self.version,self.job_type,spec,[],None,self.publication_kind,
            {'handler':'solve.resume.v1','handler_version':self.version,'source':descriptor['source'],'run_dir':run_dir,
             'configuration':data['configuration'],'execution_id':execution_id,'distro':distro,**source_build,
             'worker_sha256':hashlib.sha256(Path(solve_worker.__file__).read_bytes()).hexdigest(),
             'runner_sha256':hashlib.sha256(Path(mesh_worker.__file__).read_bytes()).hexdigest(),
             'executor_sha256':hashlib.sha256(Path(checkpoint.__file__).read_bytes()).hexdigest(),
             'checkpoint_sha256':hashlib.sha256(encoded).hexdigest(),'input_publications':descriptor.get('input_publications',[]),
             **({'restart_policy':descriptor['restart_policy']} if 'restart_policy' in descriptor else {}),
             **{kind:descriptor[kind] for kind in ('generated_gravity','generated_transport','generated_porosity',
                                                  'generated_motion','generated_fields','generated_numerical',
                                                  'generated_reference_temperature','generated_reference_pressure',
                                                  'generated_acoustic_medium','generated_conduction_material','generated_fluid_model',
                                                  'generated_perfect_gas','generated_vof','generated_rotation',
                                                  'generated_deformation','generated_mesh_displacement','generated_cht_materials')
                if kind in descriptor}},
            generated_inputs={run_dir+'/system/controlDict':control,run_dir+'/'+checkpoint.NAME:encoded},source_view=view)

    def validate_spec(self,spec):
        try:
            parts=shlex.split(spec['command'])
            if len(parts) not in {18,20} or parts[-2:]!=['--resume-checkpoint',parts[7]+'/'+checkpoint.NAME]:raise ValueError('Unexpected resume command')
            if not openfoam_runtime.valid_transport(spec,parts[11]):raise ValueError('Unexpected resume runtime')
            fresh=command(parts[7],parts[9],parts[11],parts[1],int(parts[13]),parts[15],parts[17] if len(parts)==20 else '')
            if spec['command']!=fresh+' --resume-checkpoint '+shlex.quote(parts[-1]):raise ValueError('Noncanonical resume worker command')
            SolveHandler.validate_spec(self,{**spec,'command':fresh,**openfoam_runtime.transport(fresh,parts[11])})
        except (ValueError,KeyError,TypeError,IndexError) as error:
            raise ExecutionConflict('evidence_invalid','续算命令不符合注册求解器协议') from error

    def artifacts(self,run,descriptor,output,frozen_files):
        from .solve_publication import artifacts_for
        return artifacts_for(run,descriptor,output,frozen_files)
