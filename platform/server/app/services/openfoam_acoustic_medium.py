"""Generate the declared constant medium for the native pressure-wave solver."""

import math
import re

from .case_boundary_inputs import scalar
from .casedict import parse_casedict


PATH = 'constant/transportProperties'
SPEED_UNITS = {'m/s':1.,'km/s':1000.}


def declared(case):
    problem = case.get('physicalProblem')
    models = problem.get('governingModels') if isinstance(problem,dict) else None
    model = models.get('acoustic') if isinstance(models,dict) else None
    if not isinstance(model,dict) or 'soundSpeed' not in model:
        return None
    if (model.get('formulation','pressureWave')!='pressureWave'
            or str(model.get('enabled','true')).lower() not in {'true','yes'}):
        raise ValueError('明确声速需要启用 pressureWave 声学模型')
    quantity = model['soundSpeed']
    if (not isinstance(quantity,dict) or set(quantity)!={'value','unit'}
            or not isinstance(quantity['unit'],str) or quantity['unit'] not in SPEED_UNITS):
        raise ValueError('声速需要 value 和单位 m/s 或 km/s')
    speed = scalar(scalar(quantity['value'])*SPEED_UNITS[quantity['unit']])
    density = scalar(problem.get('referenceDensity'))
    if speed<=0 or density<=0:
        raise ValueError('声速和参考密度必须为正')
    return {'speed_m_s':speed,'density_kg_m3':density}


def requested(text):
    return declared(parse_casedict(text))


def dictionary(medium):
    return ('FoamFile { version 2.0; format ascii; class dictionary; object transportProperties; }\n'
            f"c0 {medium['speed_m_s']!r};\nrho {medium['density_kg_m3']!r};\n").encode('ascii')


def native_quantity(value, field, dimensions, *, permit_zero=False):
    if isinstance(value,list):
        entry = re.fullmatch(r'(?:'+re.escape(field)+r'\s+)?\[([^]]+)\]\s*(\S+)', ' '.join(value))
        if entry is None or tuple(scalar(item) for item in entry[1].split())!=dimensions:
            raise ValueError('原生 transportProperties.'+field+' 的量纲与声明不符')
        value = entry[2]
    result = scalar(value)
    if result < 0 or not permit_zero and result == 0:
        raise ValueError('原生 transportProperties.'+field+' 必须为正')
    return result


def prepare(root, run_dir, medium):
    if medium is None:
        return {}
    from .openfoam_boundary_inputs import _existing

    path = run_dir+'/'+PATH
    native = _existing(root,path)
    if native is None:
        return {path:dictionary(medium)}
    for name,key,dimensions in (('c0','speed_m_s',(0,1,-1,0,0,0,0)),('rho','density_kg_m3',(1,-3,0,0,0,0,0))):
        if not math.isclose(native_quantity(native.get(name),name,dimensions),medium[key],rel_tol=1e-12,abs_tol=0):
            raise ValueError('原生 transportProperties.'+name+' 与声明的声学介质不一致')
    return {}
