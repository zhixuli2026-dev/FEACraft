"""Project a fixed geometry-frame acceleration onto the native global axes."""

import math
from itertools import combinations

from .casedict_schema import finite_vector, valid_cell_zone


def moving_region(problem):
    if not isinstance(problem, dict) or 'movingFrame' not in problem:
        return None
    frame = problem['movingFrame']
    if (problem.get('coordinateFrame') != 'moving' or not isinstance(frame, dict)
            or not valid_cell_zone(frame.get('region'))):
        raise ValueError('movingFrame 需要 coordinateFrame moving 和有效的 region 引用')
    return frame['region']


def global_gravity(problem,geometry):
    value=finite_vector(problem.get('gravityVector'))
    coordinates=problem.get('gravityCoordinates','global')
    if coordinates=='global':
        return value
    if coordinates!='geometry' or problem.get('coordinateFrame','inertial')!='inertial':
        raise ValueError('geometry 重力分量需要固定的惯性坐标系')
    frame=geometry.get('coordinateSystem') if isinstance(geometry,dict) else None
    if not isinstance(frame,dict):
        raise ValueError('geometry 重力分量需要 geometry.coordinateSystem 的三个坐标轴')
    axes=[]
    for name in ('xAxis','yAxis','zAxis'):
        if name not in frame:
            raise ValueError('geometry 重力分量缺少 geometry.coordinateSystem.'+name)
        axis=finite_vector(frame[name])
        scale=max(map(abs,axis))
        if not scale:
            raise ValueError('geometry.coordinateSystem.'+name+' 不能是零矢量')
        scaled=tuple(component/scale for component in axis)
        norm=math.hypot(*scaled)
        axes.append(tuple(component/norm for component in scaled))
    for first,second in combinations(axes,2):
        if abs(math.fsum(a*b for a,b in zip(first,second)))>1e-9:
            raise ValueError('geometry 重力分量需要三个相互正交的坐标轴')
    vector=tuple(math.fsum(a*b for a,b in zip(value,column)) for column in zip(*axes))
    if not all(map(math.isfinite,vector)):
        raise ValueError('坐标变换后的重力分量须为有限值')
    return vector
