"""Render an explicit incompressible reference pressure in native fvSolution."""

import math
import re

from .case_boundary_inputs import scalar
from .casedict import UNCHANGED_DICT_ENTRY, parse_casedict, set_block
from .openfoam_boundary_inputs import _existing
from .openfoam_numerical_inputs import SOLUTION


ALGORITHMS={'icoFoam':'PISO','pimpleFoam':'PIMPLE'}
UNANCHORED={'zeroGradient','fixedFluxPressure','empty','cyclic','cyclicAMI','symmetry','symmetryPlane','wedge'}
MAX_SOLUTION_BYTES=1024*1024


def declared(case):
    problem=case.get('physicalProblem')
    if not isinstance(problem,dict) or problem.get('referencePressureRole')!='gauge':
        return None
    pressure=scalar(problem.get('referencePressure'))
    density=scalar(problem.get('referenceDensity'))
    if density<=0:
        raise ValueError('压力参考值需要正的 referenceDensity，单位 kg/m³')
    cell=str(problem.get('referencePressureCell','0'))
    if not re.fullmatch(r'0|[1-9][0-9]{0,9}',cell) or int(cell)>2147483647:
        raise ValueError('referencePressureCell 需要有效的原生单元编号')
    return {'cell':int(cell),'pressure_m2_s2':scalar(pressure/density)}


def pressure_field(root,run_dir,*,initial='0',required=False):
    field=_existing(root,run_dir+'/'+initial+'/p')
    if field is None and not required:
        return
    if not isinstance(field,dict):
        raise ValueError('压力参考值需要原生初始压力场 p')
    patches=field.get('boundaryField')
    if not isinstance(patches,dict) or not patches or any(
            not isinstance(patch,dict) or patch.get('type') not in UNANCHORED for patch in patches.values()):
        raise ValueError('压力参考值需要无固定绝对压力的已声明原生边界')
    dimensions=field.get('dimensions')
    if isinstance(dimensions,list):
        dimensions=' '.join(dimensions)
    if not isinstance(dimensions,str) or not re.fullmatch(r'\[\s*0\s+2\s+-2\s+0\s+0\s+0\s+0\s*\]',dimensions):
        raise ValueError('压力参考值需要运动学压力 p，单位 m²/s²')


def prepare(root,run_dir,solver,gauge,*,require_native=False):
    if gauge is None:
        return {}
    if solver not in ALGORITHMS:
        raise ValueError('压力参考值支持托管串行 icoFoam 或 pimpleFoam')
    path=run_dir+'/'+SOLUTION
    native=root/path
    if not native.is_file() or native.with_name(native.name+'.gz').exists():
        raise ValueError('压力参考值需要明确的未压缩原生 fvSolution')
    with native.open('rb') as stream:
        raw=stream.read(MAX_SOLUTION_BYTES+1)
    if len(raw)>MAX_SOLUTION_BYTES:
        raise ValueError('压力参考值的 fvSolution 超过 1 MiB')
    text=raw.decode('utf-8')
    algorithm=ALGORITHMS[solver]
    controls=parse_casedict(text).get(algorithm)
    if not isinstance(controls,dict) or 'pRefPoint' in controls:
        raise ValueError(f'压力参考值需要 fvSolution.{algorithm} 使用原生单元编号')
    additions={}
    for key,value in {'pRefCell':gauge['cell'],'pRefValue':gauge['pressure_m2_s2']}.items():
        if key not in controls:
            additions[key]=value
        elif key=='pRefCell' and not re.fullmatch(r'0|[1-9][0-9]{0,9}',str(controls[key])):
            raise ValueError(f'原生 fvSolution.{algorithm}.pRefCell 需要明确的单元编号')
        elif not math.isclose(scalar(controls[key]),value,rel_tol=0,abs_tol=0 if key=='pRefCell' else 1e-12):
            raise ValueError(f'原生 fvSolution.{algorithm}.{key} 与声明的压力参考值冲突')
    if not additions:
        return {}
    if require_native:
        raise ValueError('封存的 fvSolution 缺少声明的压力参考值')
    preserved=dict.fromkeys(controls,UNCHANGED_DICT_ENTRY)
    return {path:set_block(text,algorithm,{**preserved,**additions}).encode('utf-8')}
