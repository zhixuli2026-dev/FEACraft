"""Render native conductive and convective heat transfer boundaries."""

from .case_boundary_inputs import confirmed, convection_values, scalar
from .case_materials import quantity_si


def _conductivity(entry, parsed):
    materials = parsed.get('materials',{})
    entries = materials.get('entries',{}) if isinstance(materials,dict) else {}
    material = entries.get(entry.get('material')) if isinstance(entries,dict) else None
    if not isinstance(material,dict) or not confirmed(material):
        raise ValueError('热流或对流边界需要已确认的命名材料')
    return quantity_si('thermalConductivity',material.get('thermalConductivity'))


def boundary(entry,parsed,solver):
    if (solver != 'laplacianFoam' or entry.get('role') not in {'wall','heatSource'}
            or entry.get('condition','fixedGradient') != 'fixedGradient'
            or entry.get('timeProfile','constant') != 'constant' or entry.get('unit') != 'W/m2'):
        raise ValueError('导热热流边界需要恒定 W/m2、壁面角色与 fixedGradient')
    gradient = scalar(scalar(entry.get('value'))/_conductivity(entry,parsed))
    return f'type fixedGradient; gradient uniform {gradient!r};'


def convection(entry, parsed, cfg):
    if cfg['solver'] != 'laplacianFoam' or 'convection_diffusivity' not in cfg:
        raise ValueError('对流换热边界需要单区域 laplacianFoam 和热扩散系数')
    coefficient, kelvin = convection_values(entry)
    conductivity = _conductivity(entry,parsed)
    diffusivity = cfg['convection_diffusivity']
    return (f'type externalWallHeatFluxTemperature; mode coefficient; h constant {coefficient!r}; '
            f'Ta constant {kelvin!r}; kappaMethod function; kappaValue constant {conductivity!r}; '
            f'alphaValue constant {diffusivity!r}; value uniform {kelvin!r};')
