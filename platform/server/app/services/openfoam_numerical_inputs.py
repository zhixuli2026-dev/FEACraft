"""Translate caseDict numerical declarations into private native inputs."""

import math
import re

from .case_numerical_method import (
    MOMENTUM_SCHEMES, convergence_required, decomposition_controls, linear_solvers,
    momentum_scheme, nonlinear_controls, time_controls, time_switch,
)
from .casedict import parse_casedict, set_block
from .native_openfoam_residuals import residual_evidence


SOLUTION='system/fvSolution'
DECOMPOSE='system/decomposeParDict'


def mpi_decomposition(document, native, ranks):
    block=parse_casedict(document).get('numericalMethod') or {}
    selection=decomposition_controls(block['decomposition']) if isinstance(block,dict) and 'decomposition' in block else None
    if selection and selection.get('subdomains',ranks)!=ranks:
        raise ValueError('numericalMethod.decomposition 的分区数与本次 MPI 请求不一致')
    if native is not None:
        if not selection:
            return None,None
        parsed=parse_casedict(native.decode('utf-8'))
        if parsed.get('method')!=selection['method'] or str(parsed.get('numberOfSubdomains'))!=str(ranks):
            raise ValueError('numericalMethod.decomposition 与原生 decomposeParDict 的方法或分区数不一致')
        if selection['method']=='simple':
            coefficients=[parsed[name] for name in ('coeffs','simpleCoeffs') if name in parsed]
            if not coefficients:
                raise ValueError('原生 decomposeParDict 缺少 simple 三轴分区数')
            for coeffs in coefficients:
                divisions=coeffs.get('n') if isinstance(coeffs,dict) else None
                native_layout=re.fullmatch(r'\(\s*(\d+)\s+(\d+)\s+(\d+)\s*\)',
                                           ' '.join(divisions) if isinstance(divisions,list) else '')
                if not native_layout or list(map(int,native_layout.groups()))!=selection['divisions']:
                    raise ValueError('numericalMethod.decomposition 与原生 decomposeParDict 的三轴分区数不一致')
        return selection,None
    method=selection['method'] if selection else 'simple'
    text='FoamFile\n{ version 2.0; format ascii; class dictionary; object decomposeParDict; }\n'
    text+=f'numberOfSubdomains {ranks};\nmethod {method};\n'
    if method=='simple':
        divisions=selection['divisions'] if selection else (ranks,1,1)
        text+='coeffs\n{ n ('+' '.join(map(str,divisions))+'); }\n'
    return selection,text


def requested_linear_solvers(document):
    method=parse_casedict(document).get('numericalMethod') or {}
    return linear_solvers(method['linearSolvers']) if 'linearSolvers' in method else None


def solution_dictionary(declarations):
    header='FoamFile { version 2.0; format ascii; class dictionary; object fvSolution; }\n'
    body=set_block(header,'solvers',declarations)
    return set_block(body,'SIMPLE',{'nNonOrthogonalCorrectors':0}).encode('utf-8')


def validate_time(method, native):
    declared=time_controls(method.get('timeControl',{}))
    for key,expected in declared.items():
        value=native.get(key)
        if isinstance(expected,bool):
            value=time_switch(value)
        elif isinstance(expected,float):
            try:
                value=float(value)
            except (TypeError,ValueError):
                value=None
        if value!=expected:
            raise ValueError('numericalMethod.timeControl.'+key+' 与原生 controlDict 不一致')
    if (native.get('writeControl')=='timeStep' and 'writeInterval' in declared
            and not declared['writeInterval'].is_integer()):
        raise ValueError('原生设置按时间步写出，writeInterval 须为正整数')
    return declared


def adaptive_steps(log):
    try:
        values=[float(value) for value in re.findall(r'(?m)^[ \t]*deltaT[ \t]*=[ \t]*(\S+)[ \t]*$',log)]
    except ValueError as error:
        raise ValueError('原生自适应步长包含无法解析的 deltaT') from error
    if not values or any(not math.isfinite(value) or value<=0 for value in values):
        raise ValueError('原生日志未证明有效的自适应 deltaT')
    return values


def validate_pressure_and_momentum(root,run_dir,solver,method):
    nonlinear=nonlinear_controls(method['nonlinear']) if 'nonlinear' in method else None
    momentum=momentum_scheme(method.get('stabilization'))
    if nonlinear:
        if solver not in {'pimpleFoam','rhoPimpleFoam','buoyantPimpleFoam','buoyantBoussinesqPimpleFoam'}:
            raise ValueError('声明的 PIMPLE 外迭代需要托管 PIMPLE 求解器')
        controls=(root/run_dir/SOLUTION)
        solution=parse_casedict(controls.read_text(encoding='utf-8')).get('PIMPLE') if controls.is_file() else None
        if not isinstance(solution,dict) or any(str(solution.get(key))!=str(value) for key,value in nonlinear['controls'].items()):
            raise ValueError('numericalMethod.nonlinear 与原生 fvSolution.PIMPLE 不一致')
    if momentum:
        convection=root/run_dir/'system/fvSchemes'
        schemes=parse_casedict(convection.read_text(encoding='utf-8')).get('divSchemes') if convection.is_file() else None
        if not isinstance(schemes,dict) or schemes.get('div(phi,U)')!=MOMENTUM_SCHEMES[momentum]:
            raise ValueError('numericalMethod.stabilization.momentumConvection 与原生 fvSchemes 不一致')
    return nonlinear,momentum


def declared_configuration(root,run_dir,solver,method,native_control):
    result={}
    times=validate_time(method,native_control)
    if times:
        result['declared_time_controls']=times
    nonlinear,momentum=validate_pressure_and_momentum(root,run_dir,solver,method)
    if nonlinear:
        result['declared_nonlinear']=nonlinear
    if momentum:
        result['momentum_convection']=momentum
    if 'linearSolvers' in method:
        result['declared_linear_solvers']=linear_solvers(method['linearSolvers'])
    convergence=convergence_required(method)
    if convergence is not None:
        result['require_linear_convergence']=convergence
    return result


def observed_configuration(configuration, log, residuals):
    result={}
    mode=configuration.get('declared_time_controls',{})
    if mode.get('adjustTimeStep') is True:
        steps=adaptive_steps(log)
        result['adaptive_time_steps']={'samples':len(steps),'minimum_s':min(steps),'maximum_s':max(steps),
            'last_s':steps[-1],'changed':min(steps)!=max(steps)}
    nonlinear=configuration.get('declared_nonlinear')
    if nonlinear and nonlinear['controls']['nOuterCorrectors']>1:
        iterations=[int(value) for value in re.findall(r'(?m)^PIMPLE: iteration (\d+)[ \t]*$',log)]
        if not iterations or max(iterations)>nonlinear['controls']['nOuterCorrectors']:
            raise ValueError('原生日志未证明声明的 PIMPLE 外迭代')
        result['pimple_outer_iterations']={'observed':len(iterations),'maximum_index':max(iterations)}
    if 'require_linear_convergence' in configuration:
        linear=residual_evidence(configuration['declared_linear_solvers'],residuals)
        failed=[field for field,record in linear.items() if record['status']!='converged']
        if configuration['require_linear_convergence'] and failed:
            raise ValueError('原生线性求解未证明满足声明容差：'+', '.join(failed))
        result['linear_convergence']=linear
    return result


def native_inputs(root,run_dir,solver,document):
    declarations=requested_linear_solvers(document)
    if declarations is None:
        return {}
    if solver=='chtMultiRegionFoam':
        raise ValueError('共轭传热的线性设置须按实际区域在原生 fvSolution 中指定')
    path=run_dir+'/'+SOLUTION
    native=root/path
    if native.with_suffix('.gz').exists():
        raise ValueError('声明线性求解器时，原生 fvSolution 须为明确的未压缩文本')
    if not native.exists():
        if solver!='laplacianFoam' or set(declarations)!={'T'}:
            raise ValueError('生成完整 fvSolution 当前支持 laplacianFoam 的 T 方程')
        return {path:solution_dictionary(declarations)}
    families=(parse_casedict(native.read_text(encoding='utf-8')).get('solvers') or {})
    for field,declared in declarations.items():
        controls=families.get(field) if isinstance(families,dict) else None
        if not isinstance(controls,dict) or declared!=linear_solvers({field:{name:controls.get(name) for name in declared}})[field]:
            raise ValueError('numericalMethod.linearSolvers.'+field+' 与原生 fvSolution 不一致')
    return {}
