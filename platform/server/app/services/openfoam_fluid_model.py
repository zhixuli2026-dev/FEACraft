"""Bind declared incompressible laminar or k-epsilon flow to native OpenFOAM controls."""

import math

from .case_boundary_inputs import scalar
from .casedict import parse_casedict
from .openfoam_boundary_inputs import _existing


PATH='constant/turbulenceProperties'
SOLVERS={'simpleFoam','pimpleFoam'}
SWITCH_ON={'true','yes','on','1'}


def selection(case):
    problem=case.get('physicalProblem')
    models=problem.get('governingModels') if isinstance(problem,dict) else None
    fluid=models.get('fluid') if isinstance(models,dict) else None
    return fluid if (isinstance(fluid,dict) and 'turbulenceModel' in fluid
                     and 'thermophysicalModel' not in fluid and 'phaseMaterials' not in fluid) else None


def declared(case):
    fluid=selection(case)
    if fluid is None:
        return None
    model=fluid['turbulenceModel']
    if model not in ('laminar','kEpsilon'):
        raise ValueError('托管湍流模型支持 laminar 或 kEpsilon')
    family,regime=('none','laminar') if model=='laminar' else ('RANS','turbulent')
    if (str(fluid.get('enabled','true')).lower() not in SWITCH_ON
            or fluid.get('compressibility','incompressible')!='incompressible'
            or fluid.get('multiphase','none')!='none' or fluid.get('flowRegime',regime)!=regime
            or fluid.get('turbulence',family)!=family):
        raise ValueError('托管流动模型需要启用且与声明的不可压缩单相模型一致')
    coefficients=fluid.get('turbulenceCoefficients',{})
    if not isinstance(coefficients,dict) or set(coefficients)-({'Cmu'} if model=='kEpsilon' else set()):
        raise ValueError('kEpsilon 可声明 Cmu 系数')
    result={'model':model,'flow_regime':regime,'family':family}
    if model=='kEpsilon':
        cmu=scalar(coefficients.get('Cmu',.09))
        if cmu<=0:
            raise ValueError('kEpsilon 的 Cmu 必须为正')
        result['cmu']=cmu
    return result


def requested(document):
    return declared(parse_casedict(document))


def ras_model(ras):
    current=ras.get('model')
    legacy=ras.get('RASModel')
    if current is not None and legacy is not None and current!=legacy:
        raise ValueError('原生 RAS.model 与 RASModel 声明冲突')
    return current if current is not None else legacy


def dictionary(model):
    header='FoamFile { version 2.0; format ascii; class dictionary; object turbulenceProperties; }\n'
    if model['model']=='laminar':
        return (header+'simulationType laminar;\n').encode('ascii')
    return (header+'simulationType RAS;\nRAS\n{\n    model kEpsilon;\n    turbulence on;\n    printCoeffs on;\n'
            f"    kEpsilonCoeffs {{ Cmu {model['cmu']!r}; }}\n}}\n").encode('ascii')


def prepare(root,run_dir,solver,model,*,require_native=False):
    if model is None or solver=='icoFoam' and model['model']=='laminar':
        return {}
    if solver not in SOLVERS:
        raise ValueError('声明的不可压缩流动模型需要串行 simpleFoam 或 pimpleFoam')
    path=run_dir+'/'+PATH
    properties=_existing(root,path)
    if properties is None:
        if require_native:
            raise ValueError('封存的流动输入缺少 turbulenceProperties')
        return {path:dictionary(model)}
    expected='laminar' if model['model']=='laminar' else 'RAS'
    if properties.get('simulationType')!=expected:
        raise ValueError('原生 turbulenceProperties.simulationType 与声明的流动模型不一致')
    if expected=='RAS':
        ras=properties.get('RAS')
        if (not isinstance(ras,dict) or ras_model(ras)!='kEpsilon'
                or str(ras.get('turbulence','')).lower() not in SWITCH_ON):
            raise ValueError('原生 turbulenceProperties 需要启用 kEpsilon')
        coefficients=ras.get('kEpsilonCoeffs',{})
        if not isinstance(coefficients,dict) or not math.isclose(scalar(coefficients.get('Cmu',.09)),model['cmu'],
                                                                rel_tol=1e-12,abs_tol=0):
            raise ValueError('原生 kEpsilonCoeffs.Cmu 与声明的流动模型不一致')
    return {}


def observed(solver,model,log):
    simulation='laminar' if model['model']=='laminar' else 'RAS'
    if solver!='icoFoam' and 'Selecting turbulence model type '+simulation not in log.splitlines():
        raise ValueError('原生日志未证明声明的流动类型')
    if simulation=='RAS' and 'Selecting RAS turbulence model '+model['model'] not in log.splitlines():
        raise ValueError('原生日志未证明声明的 RAS 模型')
    return {'simulation_type':simulation,'model':model['model']}
