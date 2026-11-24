"""Compile interFoam phase-front refinement and check its native topology history."""

import math
import re

from .case_boundary_inputs import scalar
from .casedict import parse_casedict
from .openfoam_boundary_inputs import _existing


SOLVER = 'interFoam'
PATH = 'constant/dynamicMeshDict'
COUNTS = ('refineInterval','maxRefinement','maxCells','nBufferLayers')
LEVELS = ('lowerRefineLevel','upperRefineLevel','unrefineLevel')
_TIME_OR_CHANGE = re.compile(
    r'^Time\s*=\s*(\S+)\s*$|^\s*(Refined|Unrefined) from (\d+) to (\d+) cells\.', re.M)


def _settings(value):
    field = value.get('field')
    if not isinstance(field,str) or not re.fullmatch(r'alpha\.[A-Za-z][A-Za-z0-9_]*',field):
        raise ValueError('Dynamic refinement needs a native alpha.phase fraction field')
    settings = {'field':field}
    for key in COUNTS:
        item = value.get(key)
        if not re.fullmatch(r'(?:0|[1-9][0-9]{0,9})',str(item)):
            raise ValueError('Dynamic refinement needs an integer '+key)
        count = int(item)
        if not (int(key!='nBufferLayers') <= count <= 2_147_483_647):
            raise ValueError('Dynamic refinement count is out of range: '+key)
        settings[key] = count
    settings.update({key:scalar(value.get(key)) for key in LEVELS})
    if (not 0 <= settings['lowerRefineLevel'] < settings['upperRefineLevel'] <= 1
            or settings['unrefineLevel'] < 0):
        raise ValueError('Dynamic refinement needs ordered volume fraction limits and a nonnegative unrefineLevel')
    return settings


def declared(mesh):
    if not isinstance(mesh,dict) or 'dynamicRefinement' not in mesh:
        return None
    values = mesh['dynamicRefinement']
    if mesh.get('adaptivity')!='dynamic' or not isinstance(values,dict) or set(values)!={'field',*COUNTS,*LEVELS}:
        raise ValueError('mesh.dynamicRefinement requires dynamic adaptivity and all phase-front refinement settings')
    return _settings(values)


def requested(document):
    return declared(parse_casedict(document).get('mesh'))


def dictionary(settings):
    field = settings['field']
    fluxes = ('phi','nHatf','rhoPhi','alphaPhi0.'+field.split('.',1)[1],'ghf','alphaPhiUn')
    return ('FoamFile { version 2.0; format ascii; class dictionary; object dynamicMeshDict; }\n'
            'dynamicFvMesh dynamicRefineFvMesh;\n'
            + ''.join(f'{key} {settings[key]};\n' for key in ('field',*COUNTS,*LEVELS))
            + 'correctFluxes ('+' '.join(f'({name} none)' for name in fluxes)+');\n'
            'dumpLevel true;\n').encode('ascii')


def prepare(root,run_dir,solver,settings,*,require_native=False):
    if settings is None:
        return {}
    if solver!=SOLVER:
        raise ValueError('Dynamic phase-front refinement requires serial interFoam')
    path = run_dir+'/'+PATH
    native = _existing(root,path)
    if native is not None:
        if (native.get('dynamicFvMesh')!='dynamicRefineFvMesh' or _settings(native)!=settings
                or native.get('dumpLevel') not in {'true','yes','on','1'}):
            raise ValueError('Native dynamicMeshDict differs from the declared phase-front refinement')
        return {}
    if require_native:
        raise ValueError('Sealed dynamic refinement is missing dynamicMeshDict')
    return {path:dictionary(settings)}


def observed(settings,log,labels):
    time = None
    changes = []
    for marker,operation,start,end in _TIME_OR_CHANGE.findall(log):
        if marker:
            time = float(marker)
            continue
        before,after = int(start),int(end)
        if (time is None or not math.isfinite(time) or time<=0 or not 0 < before <= settings['maxCells']
                or not 0 < after <= settings['maxCells'] or before==after
                or (operation=='Refined')!=(after>before)
                or changes and (changes[-1]['after']!=before or time<changes[-1]['time_s'])):
            raise ValueError('Dynamic refinement log has an inconsistent native topology history')
        changes.append({'time_s':time,'operation':operation.lower(),'before':before,'after':after})
    if not changes:
        raise ValueError('Dynamic refinement did not change the native mesh topology')
    index = 0
    cells = changes[0]['before']
    written = {}
    for label in sorted(labels,key=float):
        while index<len(changes) and changes[index]['time_s']<=float(label)+1e-10:
            cells = changes[index]['after']
            index += 1
        written[label] = cells
    if index != len(changes):
        raise ValueError('Final dynamic mesh change lacks a written native mesh')
    return {'changes':changes,'written_cells':written}
