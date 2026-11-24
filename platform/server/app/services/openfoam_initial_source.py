"""Resolve initial fields against an immutable serial solver publication."""

import asyncio
from dataclasses import dataclass
from functools import partial
import gzip
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re

from sqlalchemy import select

from .. import storage
from ..models import ArtifactPublication, Job, JobAttempt
from .async_ops import finish_before_cancel
from .case_boundary_inputs import scalar
from .casedict import parse_casedict
from .mesh_import_source import CORE_FILES
from .openfoam_boundary_inputs import FIELDS, SOLVER_FIELDS, _normalize, source_fields
from .solve_artifacts import EVIDENCE


_MAX_FIELD_BYTES = 16 * 1024 * 1024
_NUMBER = r'[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?'
_VECTOR = re.compile(r'\(\s*(' + _NUMBER + r')\s+(' + _NUMBER + r')\s+(' + _NUMBER + r')\s*\)')


def _validate_components(text, field):
    if field == 'U':
        match = _VECTOR.fullmatch(text)
        if match is None:
            raise ValueError('源速度场需要三个有限的向量分量')
        return tuple(scalar(number) for number in match.groups())
    if re.fullmatch(_NUMBER, text) is None:
        raise ValueError('源标量场包含无效数值')
    value = scalar(text)
    if field == 'T' and value <= 0:
        raise ValueError('源温度场需高于绝对零度')
    return value


def _internal_field(data, field, compressed):
    if compressed:
        with gzip.GzipFile(fileobj=BytesIO(data)) as stream:
            data = stream.read(_MAX_FIELD_BYTES + 1)
    if len(data) > _MAX_FIELD_BYTES:
        raise ValueError('源初值超过可封存的字段大小')
    text = re.sub(r'/\*.*?\*/|//[^\n]*', ' ', data.decode('ascii'), flags=re.S)
    header = re.search(r'\bFoamFile\s*\{([^{}]+)\}', text)
    dimensions = re.findall(r'\bdimensions\s+(\[[^\]]+\])\s*;', text)
    values = re.findall(r'\binternalField\s+([^;]+);', text)
    header = parse_casedict(header[1]) if header else {}
    kind, expected_dimensions = FIELDS[field]
    if (header.get('format') != 'ascii' or header.get('class') != kind or header.get('object') != field
            or len(dimensions) != 1 or _normalize(dimensions[0]) != _normalize('['+expected_dimensions+']')
            or len(values) != 1):
        raise ValueError('源初值需要唯一的 ASCII OpenFOAM 字段及一致的量纲：'+field)
    value = values[0].strip()
    if value.startswith('uniform '):
        _validate_components(value[8:].strip(), field)
        return value
    element = 'vector' if field == 'U' else 'scalar'
    match = re.fullmatch(r'nonuniform\s+List<'+element+r'>\s+(\d+)\s*\((.*)\)', value, re.S)
    if match is None:
        raise ValueError('源初值需要 uniform 或受支持的 nonuniform 列表：'+field)
    payload = match[2]
    if field == 'U':
        count = cursor = 0
        for row in _VECTOR.finditer(payload):
            if payload[cursor:row.start()].strip():
                raise ValueError('源速度场包含不完整的向量')
            _validate_components(row[0], field)
            count += 1
            cursor = row.end()
        if payload[cursor:].strip():
            raise ValueError('源速度场包含不完整的向量')
    else:
        words = payload.split()
        count = len(words)
        for word in words:
            _validate_components(word, field)
    if count == 0 or count != int(match[1]):
        raise ValueError('源初值列表长度不一致：'+field)
    # Preserve the solver's reported decimal precision in the target field.
    return value


@dataclass(frozen=True)
class Archive:
    prefix: str
    row: ArtifactPublication
    root: Path | None
    manifest: dict

    @classmethod
    def bind(cls,prefix,row):
        from . import artifact_publication
        from .publication_objects import bound_metadata
        if storage._use_s3():
            return cls(prefix,row,None,bound_metadata(prefix,row))
        folder,manifest = artifact_publication.read_manifest(prefix,row)
        return cls(prefix,row,folder/'files',manifest)

    def read(self,path,*,limit):
        from .attempt_objects import read
        from .attempt_workspace import checked_path
        record = self.manifest['files'][path]
        size = record['size']
        if not 0 < size <= limit:
            raise ValueError('源文件或求解证据超过可封存大小')
        if self.root is None:
            raw = read(self.prefix,self.row.immutable_path+'/files/'+path,limit=size)
        else:
            with checked_path(self.root,path).open('rb') as stream:
                raw = stream.read(size+1)
        if len(raw) != size or hashlib.sha256(raw).hexdigest() != record['sha256']:
            raise ValueError('源文件或求解证据的摘要已变化')
        return raw

    def require_unchanged(self):
        if self.bind(self.prefix,self.row).manifest != self.manifest:
            raise ValueError('读取期间源初值或网格发布已变化')

    def copy(self,records,destination):
        if self.root is None:
            from .attempt_objects import verify_members
            destination.mkdir()
            verify_members(self.prefix,self.row.immutable_path+'/files',records,destination=destination)
        else:
            from .attempt_workspace import copy_records
            copy_records(self.root,destination,records,())


def _archive_fields(archive, seed, cfg):
    descriptor = archive.manifest['descriptor']
    if (descriptor.get('handler') not in {'solve.v1','solve.resume.v1'}
            or descriptor.get('configuration', {}).get('solver') != cfg['solver']):
        raise ValueError('初值来源需要同一求解器的串行 OpenFOAM 发布')
    records = archive.manifest['files']
    run = descriptor['run_dir']
    evidence_path = run+'/'+EVIDENCE
    member = partial(archive.read,limit=_MAX_FIELD_BYTES)
    evidence = json.loads(member(evidence_path))
    inputs = {item['path']:item for item in evidence['inputs']}
    outputs = {item['path'] for item in evidence['outputs']}
    if any(run+'/constant/dynamicMeshDict'+suffix in inputs for suffix in ('','.gz')) or any(
            '/polyMesh/' in path for path in outputs):
        raise ValueError('初值来源需要静态网格的求解发布')
    fields = source_fields(cfg, seed)
    time = scalar(seed['source']['time'])
    paths = {}
    for field in fields:
        pattern = re.compile(re.escape(run)+r'/([0-9.eE+-]+)/'+re.escape(field)+r'(?:\.gz)?')
        choices = [path for path in outputs if (match:=pattern.fullmatch(path)) and float(match[1]) == time]
        if len(choices) != 1 or choices[0] not in records:
            raise ValueError('所选时刻没有唯一的已发布初值字段：'+field)
        paths[field] = choices[0]
    if len({path.split('/')[-2] for path in paths.values()}) != 1:
        raise ValueError('初值字段必须来自同一个原生写出时刻')
    values = {field:_internal_field(member(path),field,path.endswith('.gz')) for field,path in paths.items()}
    archive.require_unchanged()
    row = archive.row
    witness = {'mode':seed['initialization'],'publication_id':row.id,'generation':row.generation,
               'attempt_id':row.attempt_id,'manifest_sha256':row.manifest_sha256,'time':time,
               'evidence':{'path':evidence_path,**records[evidence_path]},
               'fields':{field:{'path':path,**records[path]} for field,path in paths.items()}}
    return values,witness,inputs


def _mesh_paths(run):
    return {name:run+'/constant/polyMesh/'+name for name in CORE_FILES}


def _matched_topology(run, records, required):
    return all(path in records and path in required and all(
        records[path][key] == required[path][key] for key in ('size','sha256')) for path in _mesh_paths(run).values())


def _map_published(prefix, origin, target, run, target_run, source_inputs, values):
    from .openfoam_initial_mapping import map_fields

    if not _matched_topology(run,origin.manifest['files'],source_inputs):
        raise ValueError('源网格发布与实际求解的原生拓扑不一致')

    source_records = {path:origin.manifest['files'][path] for path in _mesh_paths(run).values()}
    target_records = {path:target.manifest['files'][path] for path in _mesh_paths(target_run).values()}
    size = sum(record['size'] for records in (source_records,target_records) for record in records.values())
    result, proof = map_fields(prefix,partial(origin.copy,source_records),run,
        partial(target.copy,target_records),target_run,size,values)
    origin.require_unchanged()
    target.require_unchanged()
    proof.update(source_mesh_publication_id=origin.row.id,source_mesh_sha256=origin.row.manifest_sha256,
                 target_mesh_publication_id=target.row.id,target_mesh_sha256=target.row.manifest_sha256)
    return result,proof


def generated(prefix, target_row, target_manifest, run_dir, mode, declaration):
    from .openfoam_initial_mapping import analytical_fields, target_positions

    target = Archive.bind(prefix,target_row)
    if target.manifest != target_manifest:
        raise ValueError('目标网格发布在读取期间已变化')
    records = {path:target.manifest['files'][path] for path in _mesh_paths(run_dir).values()}
    positions = target_positions(prefix,partial(target.copy,records),run_dir,sum(record['size'] for record in records.values()))
    target.require_unchanged()
    if mode == 'analytical':
        values,details = analytical_fields(positions,declaration),{'cells':len(positions),'formulas':declaration}
    else:
        from .openfoam_initial_measurement import select
        values,details = select(positions,*declaration)
    return values,{'mode':mode,'target_mesh_publication_id':target_row.id,
                   'target_mesh_sha256':target_row.manifest_sha256,**details}


async def resolve(reader, case, seed, cfg, run_dir, target_row, target_manifest):
    mode = seed.get('initialization')
    if mode not in {'restart','mapped'} or cfg['solver'] not in SOLVER_FIELDS:
        raise ValueError('当前求解器和初值方式无法从已发布场初始化')
    rows = await reader.scalars(select(ArtifactPublication).join(JobAttempt,JobAttempt.id == ArtifactPublication.attempt_id)
        .join(Job,Job.id == JobAttempt.job_id).where(ArtifactPublication.case_id == case.id,Job.case_id == case.id,
            ArtifactPublication.attempt_id == seed['source']['attemptId'],ArtifactPublication.kind == 'solve',
            ArtifactPublication.status.in_(('committed','superseded')),Job.status == 'succeeded'))
    rows = list(rows)
    if len(rows) != 1:
        raise ValueError('当前算例找不到可用的初值来源发布')
    prefix = case.s3_prefix
    archive = await finish_before_cancel(asyncio.to_thread(Archive.bind,prefix,rows[0]))
    values,witness,inputs = await finish_before_cancel(asyncio.to_thread(_archive_fields,archive,seed,cfg))
    descriptor = archive.manifest['descriptor']
    origin_run = descriptor['run_dir']
    if mode == 'restart':
        source_topology = {target:inputs[origin] for target,origin in zip(
            _mesh_paths(run_dir).values(),_mesh_paths(origin_run).values()) if origin in inputs}
        if not _matched_topology(run_dir,target_manifest['files'],source_topology):
            raise ValueError('restart 初值需要同一网格拓扑和点坐标')
        return values,witness
    if seed['source'].get('method','cellCenters') != 'cellCenters':
        raise ValueError('映射初值支持 cellCenters')
    topology = set(_mesh_paths(origin_run).values())
    meshes = [ref['publication_id'] for ref in descriptor.get('input_publications',[])
              if topology <= set(ref.get('paths') or [])]
    if len(meshes) != 1:
        raise ValueError('源求解需要唯一的原生网格发布')
    mesh_row = await reader.get(ArtifactPublication,meshes[0],populate_existing=True)
    if mesh_row is None or mesh_row.case_id != case.id or mesh_row.kind != 'mesh' or mesh_row.status not in {'committed','superseded'}:
        raise ValueError('源网格发布当前不可用')
    origin,target = await finish_before_cancel(asyncio.gather(
        asyncio.to_thread(Archive.bind,prefix,mesh_row),asyncio.to_thread(Archive.bind,prefix,target_row)))
    if target.manifest != target_manifest:
        raise ValueError('目标网格发布在读取期间已变化')
    fields,proof = await finish_before_cancel(asyncio.to_thread(_map_published,prefix,origin,target,origin_run,run_dir,inputs,values))
    return fields,dict(witness,mapping=proof)
