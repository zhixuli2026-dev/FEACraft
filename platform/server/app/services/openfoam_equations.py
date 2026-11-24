"""Match declared continuum conservation equations to the native serial solver."""

import re

from .case_boundary_inputs import reference_names


INCOMPRESSIBLE = {'mass': r'Solving for p,', 'momentum': r'Solving for U[xyz],'}
SOLVERS = {
    'icoFoam': INCOMPRESSIBLE,
    'simpleFoam': INCOMPRESSIBLE,
    'pimpleFoam': INCOMPRESSIBLE,
    'interFoam': {'mass': r'Solving for p_rgh,', 'momentum': r'Solving for U[xyz],'},
    'buoyantBoussinesqSimpleFoam': {
        'mass': r'Solving for p_rgh,', 'momentum': r'Solving for U[xyz],', 'energy': r'Solving for T,',
    },
    'rhoCentralFoam': {'mass': r'Solving for rho,', 'momentum': r'Solving for rhoU[xyz],', 'energy': r'Solving for rhoE,'},
    'laplacianFoam': {'energy': r'Solving for T,'},
}
EQUATIONS = {'mass', 'momentum', 'energy', 'charge', 'species'}


def selection(case):
    problem = case.get('physicalProblem')
    models = problem.get('governingModels') if isinstance(problem, dict) else None
    model = models.get('continuum') if isinstance(models, dict) else None
    return model if isinstance(model, dict) and ('model' in model or 'conservation' in model) else None


def conservation(model):
    if 'conservation' not in model:
        return None
    names = reference_names(model['conservation'])
    if not names or not set(names) <= EQUATIONS:
        raise ValueError('连续介质守恒方程使用 mass、momentum、energy、charge 或 species')
    return names


def configuration(case, solver):
    model = selection(case)
    if model is None:
        return None
    names = conservation(model)
    equations = SOLVERS.get(solver)
    if model.get('model', 'continuum') != 'continuum' or equations is None:
        raise ValueError('当前串行 OpenFOAM 求解器未接入该连续介质模型')
    if names is not None and set(names) != equations.keys():
        raise ValueError(f'原生 {solver} 的有效守恒方程为 '+', '.join(equations))
    return {'model': 'continuum', 'conservation': list(equations)}


def observed(solver, model, log):
    equations = SOLVERS[solver]
    missing = [name for name in model['conservation'] if not re.search(equations[name], log)]
    if missing:
        raise ValueError('原生日志未证明实际求解所声明的守恒方程：'+', '.join(missing))
    return model['conservation']
