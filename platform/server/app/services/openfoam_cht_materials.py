"""Bind confirmed material properties to each native conjugate heat-transfer region."""

import math

from .case_boundary_inputs import active_material_names, confirmed, scalar
from .case_materials import quantity_si
from .openfoam_boundary_inputs import _existing
from .openfoam_fluid_thermophysics import GAS_FIELDS, THERMO_TYPE, thermophysical_dictionary


FLUID = {**THERMO_TYPE, 'type':'heRhoThermo', 'energy':'sensibleEnthalpy'}
LIQUID = {**FLUID, 'equationOfState':'rhoConst'}
SOLID = {**LIQUID, 'type':'heSolidThermo', 'transport':'constIso'}
LIQUID_FIELDS = {**GAS_FIELDS, 'equationOfState':{'rho':'density'}}
SOLID_FIELDS = {**LIQUID_FIELDS, 'transport':{'kappa':'thermalConductivity'}}
MODELS = {'gas': (FLUID, GAS_FIELDS), 'liquid': (LIQUID, LIQUID_FIELDS), 'solid': (SOLID, SOLID_FIELDS)}
ROLES = {'gas': {'fluid'}, 'liquid': {'fluid'}, 'solid': {'solid','conductor','dielectric'}}


def selection(case):
    problem = case.get('physicalProblem')
    models = problem.get('governingModels') if isinstance(problem, dict) else None
    thermal = models.get('thermal') if isinstance(models, dict) else None
    return isinstance(thermal, dict) and thermal.get('materialSource') == 'regions'


def declared(case, regions):
    if not selection(case):
        return None
    thermal = case['physicalProblem']['governingModels']['thermal']
    if (thermal.get('transport') != 'conjugate' or str(thermal.get('enabled', 'true')).lower() not in {'yes','true'}):
        raise ValueError('区域材料需要启用 conjugate 传热模型')
    entries = case['materials']['entries']
    active = active_material_names(case)
    if active and set(active) != {binding['material'] for binding in regions.values()}:
        raise ValueError('共轭传热活跃载荷的材料必须覆盖原生区域绑定')
    result = {}
    for name, binding in regions.items():
        material = entries[binding['material']]
        phase = material.get('phase')
        allowed = {'gas','liquid'} if binding['group'] == 'fluid' else {'solid'}
        if (not isinstance(phase,str) or phase not in allowed or str(material.get('role')) not in ROLES[phase]
                or not confirmed(material)):
            raise ValueError('CHT 区域需要已确认且相类型匹配的材料：'+name)
        _, sections = MODELS[phase]
        try:
            properties = {native: quantity_si(field, material.get(field, 0 if field == 'formationEnthalpy' else None))
                          for fields in sections.values() for native, field in fields.items()}
        except ValueError as error:
            raise ValueError(f'CHT 区域 {name} 材料量：{error}') from error
        if phase == 'gas' and properties['Cp'] <= 8314.46261815324/properties['molWeight']:
            raise ValueError('CHT 气体的定容比热必须为正：'+name)
        result[name] = {'material':binding['material'],'phase':phase,'properties':properties}
    return result


def relative(name):
    return f'constant/{name}/thermophysicalProperties'


def dictionary(material):
    thermo, sections = MODELS[material['phase']]
    return thermophysical_dictionary(thermo,sections,material['properties'])


def prepare(root, run_dir, materials, *, require_native=False):
    generated = {}
    for name, material in (materials or {}).items():
        path = run_dir+'/'+relative(name)
        native = _existing(root,path)
        if native is None:
            if require_native:
                raise ValueError('封存 CHT 输入缺少区域材料：'+name)
            generated[path] = dictionary(material)
            continue
        thermo, sections = MODELS[material['phase']]
        mixture = native.get('mixture')
        if native.get('thermoType') != thermo or not isinstance(mixture,dict):
            raise ValueError('原生 CHT 热物性模型与区域材料不一致：'+name)
        for section, fields in sections.items():
            entries = mixture.get(section)
            if not isinstance(entries,dict) or any(not math.isclose(scalar(entries.get(key)),material['properties'][key],rel_tol=1e-12)
                                                  for key in fields):
                raise ValueError(f'原生 CHT 区域 {name} 的 {section} 与确认材料不一致')
    return generated
