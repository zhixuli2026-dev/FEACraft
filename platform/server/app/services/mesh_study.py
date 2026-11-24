"""Compare measured quantities on immutable levels of one OpenFOAM case."""

import asyncio
import math
import re

from ..models import ArtifactPublication
from .async_ops import finish_before_cancel
from .file_read import ArchivedPublication
from .mesh_artifacts import ARTIFACTS
from .postprocess_artifacts import LATEST
from .publication_result_reads import archive_json


class MeshStudyInput(ValueError):
    """A measured level cannot be compared with the requested study."""


def read_level_record(prefix,row,path):
    with ArchivedPublication(prefix,row) as archive:
        record=archive_json(archive,path)
        if path==LATEST:
            record=archive_json(archive,record['metrics_path'])
        return archive.manifest['descriptor'],record


async def level_record(db,case,identifier,kind,path):
    row=await db.get(ArtifactPublication,identifier)
    if row is None or row.case_id!=case.id or row.kind!=kind or row.status not in {'committed','superseded'}:
        raise MeshStudyInput('所选网格、求解或后处理发布不可读取')
    return await finish_before_cancel(asyncio.to_thread(read_level_record,case.s3_prefix,row,path))


def upstream(descriptor,path=None):
    references=descriptor.get('input_publications',[])
    if not references or (path is not None and path not in (references[0].get('paths') or ())):
        raise MeshStudyInput('网格比较需要已绑定的上游版本')
    return references[0]['publication_id']


def requested_metric(metrics,name):
    matches=[value for value in metrics['metrics'] if value.get('name')==name]
    if len(matches)!=1:
        raise MeshStudyInput('每级网格需要同一项唯一的已测量指标')
    item=matches[0]
    value,unit=item['value'],item['unit']
    if type(value) not in (int,float) or not math.isfinite(value) or not isinstance(unit,str):
        raise MeshStudyInput('网格比较指标须为有单位记录的有限标量')
    return value,unit


async def level(db,case,post_id,metric):
    post,measurements=await level_record(db,case,post_id,'postprocess',LATEST)
    solve_id=upstream(post)
    run=post['run_dir']
    solve,evidence=await level_record(db,case,solve_id,'solve',run+'/solve-evidence.json')
    mesh_id=upstream(solve,ARTIFACTS)
    mesh,index=await level_record(db,case,mesh_id,'mesh',ARTIFACTS)
    geometry_path='A-geo/validation.json'
    geometry_id=upstream(mesh,geometry_path)
    _,geometry=await level_record(db,case,geometry_id,'geometry',geometry_path)
    if (post.get('handler')!='postprocess.v1' or solve.get('handler')!='solve.v1'
            or mesh.get('mesh_source')!='blockMesh' or mesh['run_dir']!=run or solve['run_dir']!=run):
        raise MeshStudyInput('网格比较需要同一运行目录的 blockMesh、串行 OpenFOAM 和托管后处理发布')
    post_source=post['source']
    solver_source=solve['source']
    fingerprint=post_source['case_input_sha256']
    if fingerprint!=solver_source['case_input_sha256'] or fingerprint!=mesh['source']['case_input_sha256']:
        raise MeshStudyInput('网格结果引用的工程输入不一致')
    grid_input=run+'/system/blockMeshDict'
    native_grid=index['mesh_source_input']
    grid_record={key:native_grid[key] for key in ('size','sha256')}
    if (native_grid['path']!=grid_input or solver_source['files'].get(grid_input)!=grid_record
            or mesh['source']['files'].get(grid_input)!=grid_record):
        raise MeshStudyInput('发布缺少原生网格脚本及求解输入快照')
    grid_geometry=index.get('mesh_study_geometry_sha256','')
    if mesh.get('mesh_study_geometry_version')!=1 or not re.fullmatch(r'[a-f0-9]{64}',grid_geometry):
        raise MeshStudyInput('所选网格缺少可比较的原生几何证明，请重新划分标准 blockMesh 网格')
    physical={path:record for path,record in solver_source['files'].items() if path!=grid_input}
    signature=(run,fingerprint,geometry['source_sha256'],grid_geometry,physical,evidence['configuration'],
        post_source['postprocess_sha256'],post_source['reference_files'])
    cells=index['volume_cells']
    if type(cells) is not int or cells<1:
        raise MeshStudyInput('网格发布缺少有效的单元计数')
    value,unit=requested_metric(measurements,metric)
    return signature,{'cells':cells,'value':value,'unit':unit,'geometry_publication_id':geometry_id,
        'mesh_publication_id':mesh_id,'solve_publication_id':solve_id,'postprocess_publication_id':post_id}


async def compare(db,case,identifiers,metric,absolute,relative):
    if len(set(identifiers))!=len(identifiers):
        raise MeshStudyInput('请为每级网格选择不同的后处理发布')
    measurements=[await level(db,case,identifier,metric) for identifier in identifiers]
    signature=measurements[0][0]
    if any(candidate!=signature for candidate,_ in measurements[1:]):
        raise MeshStudyInput('请比较具有相同几何、初值、求解配置和后处理定义的网格')
    levels=sorted((record for _,record in measurements),key=lambda record:record['cells'])
    if len({record['cells'] for record in levels})!=len(levels) or len({record['unit'] for record in levels})!=1:
        raise MeshStudyInput('各级网格须使用不同的单元数和一致的量纲')
    changes=[]
    for coarse,fine in zip(levels,levels[1:]):
        delta=abs(fine['value']-coarse['value'])
        limit=absolute+relative*abs(fine['value'])
        if not math.isfinite(delta) or not math.isfinite(limit):
            raise MeshStudyInput('网格指标差值或容差超出有限数值范围')
        changes.append({'from_cells':coarse['cells'],'to_cells':fine['cells'],'absolute_change':delta,
                        'tolerance':limit,'within_tolerance':delta<=limit})
    return {'run_dir':signature[0],'metric':metric,'unit':levels[0]['unit'],'levels':levels,'changes':changes,
            'finest_pair_within_tolerance':changes[-1]['within_tolerance']}
