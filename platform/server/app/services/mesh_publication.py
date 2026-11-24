"""Immutable mesh generation/checkMesh validation and C-stage provenance."""
import json

from .. import storage
from . import attempt_workspace as workspace
from . import openfoam_cht, openfoam_numerical_inputs
from .case_evidence import file_record, run_inventory
from .mesh_artifacts import (ARTIFACTS, BOUNDARIES, MESH_FILES, NATIVE_TOOLS, QUALITY, TOOLCHAIN, artifact_index, boundaries,
    imported_boundaries, input_snapshot, managed, quality, selection,mesh_source,required_outputs)
from .mesh_quality import professional_quality_reason


def source_snapshot(prefix, run_dir):
    return inspect_inputs(prefix,_source_snapshot,run_dir)


def inspect_inputs(prefix,reader,run_dir):
    """Materialize semantic mesh/solve inputs, including optional initial fields.

    Written result times and report artifacts are not inputs. Rechecking the
    optional prefixes also detects a newly added or removed initial field.
    """
    from .case_evidence import safe_run_dir
    from .case_read_view import inspect
    run_dir=safe_run_dir(run_dir)
    return inspect(prefix,reader,run_dir,paths=['0-caseDict/caseDict',run_dir+'/system'],
        optional_paths=[run_dir+'/'+name for name in ('constant','0','0.orig')])


def _source_snapshot(root,run_dir):
    workspace.inventory(root,selection(root,run_dir))  # Reject links/special files.
    return input_snapshot(root,run_dir)


def artifacts_for(run, descriptor, output, frozen_files):
    run_dir=descriptor['run_dir']
    expected=descriptor['source']
    source=mesh_source(run)
    if source!=descriptor.get('mesh_source','blockMesh'):
        raise ValueError('Mesh generator differs from the prepared invocation')
    observed=input_snapshot(run,run_dir)
    for path,record in descriptor.get('generated_numerical',{}).items():
        if (path!=run_dir+'/'+openfoam_numerical_inputs.SOLUTION or observed['files'].pop(path,None)!=record
                or any(frozen_files[path][key]!=record[key] for key in ('size','sha256'))):
            raise ValueError('Mesh numerical input differs from the sealed fvSolution')
    if observed!=expected:
        raise ValueError('Mesh input provenance differs from prepared inputs')
    for path,record in expected['files'].items():
        if any(frozen_files[path][key]!=record[key] for key in ('size','sha256')):
            raise ValueError('Mesh inputs differ from the sealed manifest')
    report=json.loads((run/QUALITY).read_text(encoding='utf-8'))
    legacy=descriptor.get('mesh_quality_version')!=2
    with_refinement=descriptor.get('mesh_refinement_version')==1
    recomputed=quality(run,run_dir,legacy=legacy,check_families=descriptor.get('mesh_family_evidence_version')==1,
                       check_refinement=with_refinement,
                       check_boundary_layers=descriptor.get('mesh_boundary_layer_evidence_version')==1,
                       check_dimension=descriptor.get('mesh_dimension_evidence_version')==1,
                       check_preset=descriptor.get('mesh_quality_preset_version')==1)
    if any(report.get(key)!=value for key,value in recomputed.items()):
        raise ValueError('Mesh quality was not derived from sealed native mesh evidence')
    reason=professional_quality_reason(recomputed,recomputed['quality_targets'])
    if reason:
        raise ValueError(reason)
    with_jacobian='jacobian_evidence' in recomputed
    if report.get('inputs') != [file_record(run,path) for path in run_inventory(run,run_dir,results=False)]:
        raise ValueError('Mesh evidence file inventory changed')
    regions = recomputed.get('regions', {})
    if not regions:
        for name in MESH_FILES:
            workspace.checked_path(run,run_dir+'/constant/polyMesh/'+name)
    patches=json.loads((run/BOUNDARIES).read_text(encoding='utf-8'))
    expected_patches = imported_boundaries(run,run_dir,regions) if regions else boundaries(run/run_dir/'constant/polyMesh/boundary')
    if (patches.get('schema')!='vibeflow.mesh-boundaries.v1' or patches.get('run_dir')!=run_dir or
            patches.get('patches')!=expected_patches):
        raise ValueError('Published boundary list differs from the generated mesh')
    toolchain=json.loads((run/TOOLCHAIN).read_text(encoding='utf-8'))
    executable_names=list(NATIVE_TOOLS[source])
    if with_jacobian:
        executable_names.append('foamToVTK')
    if with_refinement:
        executable_names.extend(('topoSet','refineMesh'))
    if (any(item.get('execution_id')!=descriptor['execution_id'] for item in (report,patches,toolchain)) or
            toolchain.get('worker_sha256')!=descriptor['worker_sha256'] or
            toolchain.get('distro')!=descriptor['distro'] or not toolchain.get('openfoam') or
            toolchain.get('mesh_source','blockMesh')!=source or
            not all(toolchain.get('executable_sha256',{}).get(name) for name in
                executable_names)):
        raise ValueError('Mesh output does not belong to this worker invocation')
    if output.get('schema')!='vibeflow.attempt-outputs.v1':
        raise ValueError('Mesh output inventory is missing')
    records={}
    with_artifacts = descriptor.get('mesh_artifact_index_version')==1
    if with_artifacts:
        index = json.loads((run/ARTIFACTS).read_text(encoding='utf-8'))
        if index != artifact_index(run,run_dir,source,descriptor['execution_id'],regions,report['log_path'],
                                  with_study_geometry=descriptor.get('mesh_study_geometry_version')==1):
            raise ValueError('Mesh artifact index differs from the captured native output')
    required=required_outputs(run_dir,source,with_jacobian=with_jacobian,with_artifacts=with_artifacts,
                              with_refinement=with_refinement)
    for path,record in output['files'].items():
        if path in required or (not regions and managed(path,run_dir)):
            actual=workspace.digest_file(workspace.checked_path(run,path))
            if any(actual[key]!=record[key] for key in ('size','sha256')):
                raise ValueError('Mesh output changed after capture')
            records[path]=record
    if not required.issubset(records):
        raise ValueError('Mesh publication is incomplete')
    return records


async def gate_reason(db, case):
    from ..models import JobAttempt
    from .artifact_publication import current
    from .job_execution import completion_visibility
    from .publication_inputs import resolve
    from .workflow_evidence import mesh_evidence_gate_reason
    try:
        head,row=await current(db,case.id,'mesh')
        if head is None:
            return mesh_evidence_gate_reason(case.s3_prefix)
        if row is None:
            return '当前网格尚未发布或已被修改，请重新生成'
        attempt = await db.get(JobAttempt, row.attempt_id)
        if completion_visibility(attempt) == 'pending':
            return '当前网格已发布，正在同步算例工作目录，请稍后重新确认'
        resolved=await resolve(db,case,[{'publication_id':row.id,'paths':None,'require_current':True}])
        from .case_evidence import verify_records
        reason=verify_records(case.s3_prefix,[{'path':path,**record} for path,record in resolved[0].files.items()],
            label='当前网格',required=True)
        if reason:
            return reason
        return None
    except (OSError,ValueError,KeyError,TypeError):
        return '网格发布或其输入/几何依赖已失效，请重新生成'
