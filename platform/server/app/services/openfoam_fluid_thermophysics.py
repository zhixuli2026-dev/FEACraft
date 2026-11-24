"""Bind confirmed materials to perfect-gas and two-phase VOF dictionaries."""

import math
import re

from .case_boundary_inputs import active_material_names, confirmed, material_for_active_load, reference_names, scalar
from .case_materials import quantity_si
from .casedict import parse_casedict
from .openfoam_acoustic_medium import native_quantity
from .openfoam_boundary_inputs import _existing


GAS_PATH = 'constant/thermophysicalProperties'
VOF_PATH = 'constant/transportProperties'
PATHS = {'perfectGas': GAS_PATH, 'VOF': VOF_PATH}
SOLVERS = {'perfectGas': 'rhoCentralFoam', 'VOF': 'interFoam'}
DERIVED_INPUTS = {'perfectGas': 'generated_perfect_gas', 'VOF': 'generated_vof'}
THERMO_TYPE = {'type': 'hePsiThermo', 'mixture': 'pureMixture', 'transport': 'const', 'thermo': 'hConst',
               'equationOfState': 'perfectGas', 'specie': 'specie', 'energy': 'sensibleInternalEnergy'}
GAS_FIELDS = {'specie': {'molWeight': 'molarMass'}, 'thermodynamics': {'Cp': 'heatCapacity', 'Hf': 'formationEnthalpy'},
              'transport': {'mu': 'viscosity', 'Pr': 'prandtlNumber'}}


def selection(case):
    problem = case.get('physicalProblem')
    models = problem.get('governingModels') if isinstance(problem, dict) else None
    fluid = models.get('fluid') if isinstance(models, dict) else None
    if isinstance(fluid, dict) and ('thermophysicalModel' in fluid or 'phaseMaterials' in fluid):
        return fluid
    return None


def _material(entries, name, phases):
    material = entries.get(name) if isinstance(name, str) else None
    if (not isinstance(material, dict) or not confirmed(material) or material.get('role') != 'fluid'
            or material.get('phase') not in phases):
        raise ValueError('流动模型要求引用相类型明确的已确认流体材料')
    return material


def _perfect_gas(case, fluid, entries):
    if (fluid['thermophysicalModel'] != 'perfectGas' or fluid.get('compressibility') != 'compressible'
            or fluid.get('multiphase', 'none') != 'none' or fluid.get('flowRegime', 'laminar') not in ('laminar', 'inviscid')):
        raise ValueError('perfectGas 需要可压缩单相层流或无黏流动')
    name = material_for_active_load(case,fluid.get('material'))
    material = _material(entries, name, ('gas',))
    properties = {native: quantity_si(field, material.get(field, 0 if field == 'formationEnthalpy' else None))
                  for fields in GAS_FIELDS.values() for native, field in fields.items()}
    if properties['Cp'] <= 8314.46261815324 / properties['molWeight']:
        raise ValueError('理想气体的定容比热 Cp − R 必须为正')
    if fluid.get('flowRegime') == 'inviscid' and properties['mu'] != 0:
        raise ValueError('无黏理想气体需要零动力黏度')
    return {'model': 'perfectGas', 'material': name, 'properties': properties}


def _vof(case, fluid, entries):
    if (fluid.get('multiphase') != 'VOF' or fluid.get('compressibility', 'incompressible') != 'incompressible'
            or fluid.get('flowRegime', 'laminar') != 'laminar'):
        raise ValueError('声明的双相 VOF 需要不可压缩层流')
    names = reference_names(fluid.get('phaseOrder'))
    refs = fluid['phaseMaterials']
    if (len(names) != 2 or not isinstance(refs, dict) or set(refs) != set(names)
            or set(names) & {'FoamFile', 'phases', 'sigma'}):
        raise ValueError('VOF 的 phaseOrder 和 phaseMaterials 需要同样的两个原生相名称')
    active = active_material_names(case)
    if active and set(active) != {value for value in refs.values() if isinstance(value,str)}:
        raise ValueError('VOF 活跃载荷的材料必须与原生两相绑定一致')
    phases = []
    for name in names:
        material = _material(entries, refs[name], ('gas', 'liquid'))
        density = quantity_si('density', material.get('density'))
        viscosity = scalar(quantity_si('viscosity', material.get('viscosity')) / density)
        if viscosity <= 0:
            raise ValueError('VOF 的运动黏度需要为正')
        phases.append({'name': name, 'material': refs[name], 'density_kg_m3': density, 'viscosity_m2_s': viscosity})
    return {'model': 'VOF', 'phases': phases, 'surface_tension_n_m': quantity_si('surfaceTension', fluid.get('surfaceTension'))}


def declared(case):
    fluid = selection(case)
    if fluid is None:
        return None
    if (str(fluid.get('enabled', 'true')).lower() not in {'true', 'yes'} or fluid.get('turbulence', 'none') != 'none'
            or fluid.get('turbulenceModel', 'laminar') != 'laminar'):
        raise ValueError('声明的材料流动模型需要启用，且使用原生层流设置')
    if 'thermophysicalModel' in fluid and 'phaseMaterials' in fluid:
        raise ValueError('请选择单相 perfectGas 或双相 VOF 材料配置')
    materials = case.get('materials')
    entries = materials.get('entries') if isinstance(materials, dict) else None
    if not isinstance(entries, dict):
        raise ValueError('声明的流动模型需要 materials.entries')
    return _perfect_gas(case, fluid, entries) if 'thermophysicalModel' in fluid else _vof(case, fluid, entries)


def requested(document, kind):
    model = declared(parse_casedict(document))
    return model if model and model['model'] == kind else None


def thermophysical_dictionary(thermo, sections, properties):
    lines = ['FoamFile { version 2.0; format ascii; class dictionary; object thermophysicalProperties; }']
    controls = ' '.join(f'{key} {value};' for key, value in thermo.items())
    lines += ['thermoType { '+controls+' }', 'mixture', '{']
    for section, fields in sections.items():
        values = ' '.join(f'{field} {properties[field]!r};' for field in fields)
        lines.append(f'    {section} {{ {values} }}')
    lines.append('}')
    return ('\n'.join(lines)+'\n').encode('ascii')


def dictionary(model):
    kind = model['model']
    if kind == 'perfectGas':
        return thermophysical_dictionary(THERMO_TYPE, GAS_FIELDS, model['properties'])
    lines = ['FoamFile { version 2.0; format ascii; class dictionary; object transportProperties; }']
    names = ' '.join(phase['name'] for phase in model['phases'])
    lines.append(f'phases ({names});')
    for phase in model['phases']:
        lines.append(f'{phase["name"]} {{ transportModel Newtonian; nu {phase["viscosity_m2_s"]!r}; '
                     f'rho {phase["density_kg_m3"]!r}; }}')
    lines.append(f'sigma {model["surface_tension_n_m"]!r};')
    return ('\n'.join(lines)+'\n').encode('ascii')


def _match_gas(native, model):
    if native.get('thermoType') != THERMO_TYPE or not isinstance(native.get('mixture'), dict):
        raise ValueError('原生 thermoType 与 perfectGas 热物性设置不一致')
    for section, fields in GAS_FIELDS.items():
        actual = native['mixture'].get(section)
        if not isinstance(actual, dict) or any(not math.isclose(scalar(actual.get(key)), model['properties'][key], rel_tol=1e-12)
                                               for key in fields):
            raise ValueError('原生 thermophysicalProperties 与确认材料不一致：'+section)


def _match_vof(native, model):
    if reference_names(native.get('phases')) != [phase['name'] for phase in model['phases']]:
        raise ValueError('原生 transportProperties.phases 与声明的相顺序不一致')
    for phase in model['phases']:
        actual = native.get(phase['name'])
        if not isinstance(actual, dict) or actual.get('transportModel') != 'Newtonian':
            raise ValueError('原生 VOF 相需要 Newtonian 输运模型：'+phase['name'])
        for name, key, dimension in (('nu', 'viscosity_m2_s', (0, 2, -1, 0, 0, 0, 0)),
                                     ('rho', 'density_kg_m3', (1, -3, 0, 0, 0, 0, 0))):
            if not math.isclose(native_quantity(actual.get(name), name, dimension), phase[key], rel_tol=1e-12):
                raise ValueError('原生 VOF 相与确认材料不一致：'+phase['name']+'.'+name)
    sigma = native.get('sigma')
    sigma = native_quantity(sigma, 'sigma', (1, 0, -2, 0, 0, 0, 0), permit_zero=True)
    if not math.isclose(sigma, model['surface_tension_n_m'], rel_tol=1e-12):
        raise ValueError('原生 VOF 表面张力与声明不一致')


def prepare(root, run_dir, solver, model, *, require_native=False):
    if model is None:
        return {}
    kind = model['model']
    if solver != SOLVERS[kind]:
        raise ValueError(f'声明的 {kind} 材料流动模型需要串行 {SOLVERS[kind]}')
    turbulence = _existing(root, run_dir+'/constant/turbulenceProperties')
    if not turbulence or turbulence.get('simulationType') != 'laminar':
        raise ValueError(f'声明的 {kind} 材料流动需要原生 turbulenceProperties.simulationType laminar')
    path = run_dir+'/'+PATHS[kind]
    native = _existing(root, path)
    if native is None:
        if require_native:
            raise ValueError('封存流动材料缺少原生字典：'+PATHS[kind])
        return {path: dictionary(model)}
    if kind == 'perfectGas':
        _match_gas(native, model)
    else:
        _match_vof(native, model)
    return {}


def observed(model, log):
    kind = model['model']
    if kind == 'perfectGas':
        selected = re.search(r'Selecting thermodynamics package\s*\{([^{}]+)\}', log)
        if not selected or parse_casedict(selected[1]) != THERMO_TYPE:
            raise ValueError('原生日志未证明所声明的 perfectGas 热物性组合')
        return {'equation_of_state': kind}
    names = [phase['name'] for phase in model['phases']]
    if log.count('Selecting incompressible transport model Newtonian') != 2 or f'Min(alpha.{names[0]})' not in log:
        raise ValueError('原生日志未证明所声明的两相 VOF 输运')
    return {'multiphase': kind, 'phases': names}
