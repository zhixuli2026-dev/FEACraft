"""Use public solver parameters and the same configured tools as initial submit."""
from pathlib import Path
from .workflow_state import ExecutionConflict


def _executable(parameters, configured):
    expected=str(Path(configured).resolve())
    supplied=parameters.get('executable')
    if supplied is not None and (not isinstance(supplied,str) or str(Path(supplied).resolve())!=expected):
        raise ExecutionConflict('invalid_request','重跑使用服务端当前已配置的工具，不能指定其他可执行文件')
    return {**parameters,'executable':expected}


def public_parameters(handler, parameters):
    if handler.key=='calculix.solve':
        from .calculix import calculix_executable
        return _executable(parameters,calculix_executable())
    if handler.key=='elmer.solve':
        from .elmer import elmer_executable
        return _executable(parameters,elmer_executable())
    if handler.key=='moose.solve':
        from .moose import moose_executable
        executable = moose_executable()
        if parameters.get('executable',executable)!=executable:
            raise ExecutionConflict('invalid_request','重跑使用服务端当前已配置的 MOOSE 程序')
        return {**parameters,'executable':executable}
    if handler.key=='code-aster.solve':
        from .code_aster import code_aster_executable
        executable = code_aster_executable()
        if parameters.get('executable',executable)!=executable:
            raise ExecutionConflict('invalid_request','重跑使用服务端当前已配置的 Code_Aster 程序')
        return {**parameters,'executable':executable}
    if handler.key=='su2.solve':
        from .su2 import su2_executable
        parameters=dict(parameters)
        if 'config_path' in parameters:
            if 'input_path' in parameters:
                raise ExecutionConflict('invalid_request','SU2重跑不能同时指定config_path和input_path')
            parameters['input_path']=parameters.pop('config_path')
        return _executable(parameters,su2_executable())
    if handler.key=='paraview.run':
        from .paraview import paraview_executable
        parameters=dict(parameters);backend=parameters.setdefault('backend','pvpython')
        if backend not in ('pvpython','pvbatch'):
            raise ExecutionConflict('invalid_request','ParaView重跑只支持已配置的pvpython或pvbatch')
        return _executable(parameters,paraview_executable(backend))
    return parameters
