"""Directional h-refinement inputs and native evidence."""

import re

from .case_evidence import file_record
from .case_mesh_schema import directional_refinement
from .mesh_jacobian import CELL_COUNTS


SET = 'vibeRefinementCells'
BASE = 'checkMesh-base.log'
SELECT = 'topoSet-refine.log'
REFINE = 'refineMesh.log'
LOGS = (BASE, SELECT, REFINE)


def dictionaries(refinement):
    coordinates={key:' '.join(repr(component) for component in refinement[key]) for key in ('min','max')}
    direction={'x':'tan1','y':'tan2','z':'normal'}[refinement['axis']]
    header='FoamFile { version 2.0; format ascii; class dictionary; object %s; }\n'
    return {
        'controlDict': (header%'controlDict') + 'application refineMesh;\nstartFrom startTime; startTime 0;\n'
            'stopAt endTime; endTime 1; deltaT 1;\nwriteControl timeStep; writeInterval 1;\n'
            'writeFormat ascii; writePrecision 12; writeCompression off;\n'
            'timeFormat general; timePrecision 6; runTimeModifiable false;\n',
        'topoSetDict': (header%'topoSetDict') + f'actions ({{ name {SET}; type cellSet; action new;\n'
            f'source boxToCell; sourceInfo {{ box ({coordinates["min"]}) ({coordinates["max"]}); }} }});\n',
        'refineMeshDict': (header%'refineMeshDict') + f'set {SET};\ncoordinateSystem global;\n'
            'globalCoeffs { tan1 (1 0 0); tan2 (0 1 0); }\n'
            f'directions ({direction});\nuseHexTopology no; geometricCut yes; writeMesh no;\n',
    }


def selected_count(log):
    values=re.findall(r'^\s*cellSet '+SET+r' now size (\d+)\s*$',log,re.M)
    if len(values)!=1 or int(values[0])<1:
        raise ValueError('加密范围未选中实际网格单元')
    return int(values[0])


def evidence(root, run_dir, declaration, final_log):
    refinement=directional_refinement(declaration)
    paths={name:root/run_dir/name for name in LOGS}
    logs={name:path.read_text(encoding='utf-8',errors='replace') for name,path in paths.items()}
    original=CELL_COUNTS.findall(logs[BASE])
    refined=CELL_COUNTS.findall(final_log)
    selected=selected_count(logs[SELECT])
    if ('Mesh OK.' not in logs[BASE] or not re.search(r'^End\s*$',logs[REFINE],re.M)
            or len(original)!=1 or len(refined)!=1 or not selected<=int(original[0])
            or int(refined[0])<int(original[0])+selected):
        raise ValueError('原生方向加密的网格和单元数证据不一致')
    return {'axis':refinement['axis'],'bounds_m':[list(refinement['min']),list(refinement['max'])],
            'base_cells':int(original[0]),'selected_cells':selected,'refined_cells':int(refined[0]),
            'logs':{name:file_record(root,path) for name,path in paths.items()}}
