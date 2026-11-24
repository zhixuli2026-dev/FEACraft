"""Transfer a sealed OpenFOAM pressure resultant or conformal surface load."""

import json
import math
import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from .attempt_workspace import checked_path, digest_file
from .case_boundary_inputs import scalar
from .conformal_points import matched_nodes
from .file_read import ArchivedPublication
from .postprocess_artifacts import LATEST, result_dir
from .publication_result_reads import archive_json


NODE_SET = re.compile(r'[A-Za-z_][A-Za-z0-9_-]{0,79}')


class PressureTransferRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')

    publication_id: UUID
    surface: str = Field(min_length=1,max_length=128)
    patch: str = Field(min_length=1,max_length=128)
    time: FiniteFloat = Field(ge=0)
    node_set: str = Field(min_length=1,max_length=80)
    distribution: Literal['uniformResultant','conformalNodal'] | None = None
    match_tolerance_m: FiniteFloat | None = Field(default=None,gt=0)


def _extract(metrics, request):
    rows = metrics['extractions']['surfaceIntegrals']
    matches = [row for row in rows if row.get('id') == request['surface']
               and row.get('patch') == request['patch'] and row.get('time') == request['time']
               and row.get('mode') == 'pressureForce' and row.get('unit') == 'N'
               and (row.get('field') == 'p' or row.get('field') == 'pa' and 'reference_density_kg_m3' not in row)]
    if len(matches) != 1:
        raise ValueError('Select one archived pressure-force surface, patch and time in newtons')
    row = matches[0]
    vector = row.get('integral')
    area = scalar(row.get('area'))
    if (not isinstance(vector, list) or len(vector) != 3 or area <= 0
            or type(row.get('face_count')) is not int or row['face_count'] < 1):
        raise ValueError('Invalid archived pressure-force result')
    result = {'force_n': [scalar(value) for value in vector], 'source_area_m2': area,
              'source_faces': row['face_count'], 'source_field': row['field']}
    if request.get('distribution') == 'conformalNodal':
        table = row.get('nodal_loads')
        if not isinstance(table,dict):
            raise ValueError('Conformal transfer requires archived pressureForce with nodalLoads enabled')
        values = {}
        for key in ('positions_m','forces_n'):
            items = table.get(key)
            if not isinstance(items,list) or not items or any(not isinstance(vertex,list) or len(vertex)!=3 for vertex in items):
                raise ValueError('Invalid archived OpenFOAM nodal loads')
            values[key] = [[scalar(component) for component in vertex] for vertex in items]
        if len(values['positions_m'])!=len(values['forces_n']):
            raise ValueError('OpenFOAM nodal loads do not match their surface points')
        for axis, force in enumerate(result['force_n']):
            components = [load[axis] for load in values['forces_n']]
            if abs(math.fsum(components)-force)>1e-12*max(1.,math.fsum(map(abs,components))):
                raise ValueError('Archived nodal pressure loads do not conserve the resultant')
        result['nodal_loads'] = values
    return result


def select(prefix, publication, request):
    if not NODE_SET.fullmatch(request['node_set']):
        raise ValueError('Pressure load requires a named CalculiX node set')
    request = {**request, 'time': scalar(request['time']), 'node_set': request['node_set'].upper()}
    distribution = request.get('distribution') or 'uniformResultant'
    if distribution == 'conformalNodal':
        request['match_tolerance_m'] = request.get('match_tolerance_m') or 1e-7
    elif request.get('match_tolerance_m') is not None:
        raise ValueError('Pressure match tolerance applies only to conformal nodes')
    request['distribution'] = distribution
    with ArchivedPublication(prefix, publication) as archive:
        if archive.manifest['descriptor'].get('handler') != 'postprocess.v1':
            raise ValueError('Pressure transfer requires a published native solve extraction')
        pointer = archive_json(archive, LATEST)
        path = result_dir(pointer['job_id']) + '/metrics.json'
        if pointer['metrics_path'] != path:
            raise ValueError('Pressure transfer source metrics have an invalid path')
        source = _extract(archive_json(archive, path), request)
        expected = archive.manifest['files'][path]
    return {**request, **source, 'metrics_path': path,
            'metrics_sha256': expected['sha256'], 'manifest_sha256': publication.manifest_sha256}


def verify_sealed(root, transfer):
    path = checked_path(root, transfer['metrics_path'])
    if digest_file(path)['sha256'] != transfer['metrics_sha256']:
        raise ValueError('Sealed pressure metrics differ from the selected publication')
    with path.open(encoding='utf-8') as stream:
        values = _extract(json.load(stream), transfer)
    declared = {'source_field':'p',**transfer}
    if any(declared.get(key) != value for key, value in values.items()):
        raise ValueError('Sealed pressure load differs from the archived OpenFOAM extraction')


def match_nodes(transfer, members, coordinates):
    positions = transfer['nodal_loads']['positions_m']
    forces = transfer['nodal_loads']['forces_n']
    if len(positions)!=len(members):
        raise ValueError('Conformal pressure transfer requires the same number of source and target points')
    selected = {node:coordinates[node] for node in members}
    matched = matched_nodes(positions,selected,transfer['match_tolerance_m'])
    return [{'node_id':node, 'force_n':force} for node,force in sorted(zip(matched,forces,strict=True))]
