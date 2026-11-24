"""Bind declared conduction to a named material and an actual native DT."""

import math
import re

from .case_boundary_inputs import active_material_names, confirmed, material_for_active_load, scalar
from .case_materials import quantity_si
from .casedict import parse_casedict
from .openfoam_boundary_inputs import _existing


SOLVER='laplacianFoam'
PATH='constant/transportProperties'


def selection(case):
    problem=case.get('physicalProblem')
    models=problem.get('governingModels') if isinstance(problem,dict) else None
    thermal=models.get('thermal') if isinstance(models,dict) else None
    return thermal if (isinstance(thermal,dict) and thermal.get('transport')=='conduction'
                       and ('material' in thermal or active_material_names(case))) else None


def declared(case):
    thermal=selection(case)
    if thermal is None:
        return None
    if str(thermal.get('enabled','true')).lower() not in {'true','yes'}:
        raise ValueError('材料导热需要启用 thermal 模型')
    name=material_for_active_load(case,thermal.get('material'))
    materials=case.get('materials')
    entries=materials.get('entries') if isinstance(materials,dict) else None
    material=entries.get(name) if isinstance(entries,dict) and isinstance(name,str) else None
    if not isinstance(material,dict) or not confirmed(material):
        raise ValueError('导热需要已确认的命名材料')
    density=quantity_si('density',material.get('density'))
    capacity=quantity_si('heatCapacity',material.get('heatCapacity'))
    conductivity=quantity_si('thermalConductivity',material.get('thermalConductivity'))
    diffusivity=scalar(conductivity/density/capacity)
    if diffusivity<=0:
        raise ValueError('材料计算出的热扩散系数必须为正')
    return {'material':name,'density_kg_m3':density,'heat_capacity_j_kg_k':capacity,
            'conductivity_w_m_k':conductivity,'diffusivity_m2_s':diffusivity}


def requested(document):
    return declared(parse_casedict(document))


def dictionary(material):
    return ('FoamFile { version 2.0; format ascii; class dictionary; object transportProperties; }\n'
            f"DT [0 2 -1 0 0 0 0] {material['diffusivity_m2_s']!r};\n").encode('ascii')


def prepare(root,run_dir,solver,material,*,require_native=False):
    if material is None:
        return {}
    if solver!=SOLVER:
        raise ValueError('声明的材料导热需要托管单区域 laplacianFoam')
    relative=run_dir+'/'+PATH
    native=_existing(root,relative)
    if native is None:
        if require_native:
            raise ValueError('封存的导热输入缺少 transportProperties.DT')
        return {relative:dictionary(material)}
    value=native.get('DT')
    if isinstance(value,list):
        matched=re.fullmatch(r'(?:DT\s+)?\[\s*0\s+2\s+-1\s+0\s+0\s+0\s+0\s*\]\s+(\S+)',' '.join(value))
        if not matched:
            raise ValueError('原生 transportProperties.DT 需要 m²/s 量纲')
        value=matched[1]
    if not math.isclose(scalar(value),material['diffusivity_m2_s'],rel_tol=1e-12,abs_tol=0):
        raise ValueError('原生 transportProperties.DT 与材料导热系数不一致')
    return {}
