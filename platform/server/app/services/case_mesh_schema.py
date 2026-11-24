"""Types and element-family declarations for the caseDict mesh block."""

import re

from .casedict_schema import _bool, _count, _enum, _error, _positive_number, finite_vector, interface_pair
from .mesh_presets import QUALITY_PRESETS


_ENUMS = {
    'meshSource': {'generated','imported','solverNative','external'},
    'meshIntent': {'smoke','coarse','production','validation','meshIndependence'},
    'qualityPreset': set(QUALITY_PRESETS),
    'dimension': {'1','2','3'},
    'topology': {'structured','unstructured','hybrid','overset','conformal','nonConformal'},
    'adaptivity': {'none','h','p','hp','dynamic','goalOriented'},
}
ELEMENT_FAMILIES = {'line','tri','quad','tet','hex','prism','poly','shell','beam','wedge','pyramid','tetWedge'}


def element_families(value):
    members=interface_pair(value)
    if isinstance(members,str):
        members=[members]
    if (not isinstance(members,list) or not members
            or any(not isinstance(item,str) or item not in ELEMENT_FAMILIES for item in members)
            or len(set(members))!=len(members)):
        raise ValueError('单元族须为有效且不重复的名称')
    return set(members)


def directional_refinement(value):
    if not isinstance(value,dict) or set(value)!={'min','max','axis'}:
        raise ValueError('加密需要 min、max 三分量坐标及 axis')
    axis=value['axis']
    if not isinstance(axis,str) or axis not in {'x','y','z'}:
        raise ValueError('加密轴只能为 x、y 或 z')
    try:
        lower,upper=(finite_vector(value[key]) for key in ('min','max'))
    except (ValueError,OverflowError) as error:
        raise ValueError('加密区域坐标须为有限的三分量矢量') from error
    if any(low>=high for low,high in zip(lower,upper)):
        raise ValueError('加密区域每轴的 min 都必须小于 max')
    return {'min':lower,'max':upper,'axis':axis}


def boundary_layer_plan(value):
    if not isinstance(value,dict) or set(value)!={'patch','axis','minLayers','maxFirstLayerHeight','maxGrowth'}:
        raise ValueError('边界层需要 patch、axis、minLayers、maxFirstLayerHeight 与 maxGrowth')
    patch,axis=value['patch'],value['axis']
    if not isinstance(patch,str) or not re.fullmatch(r'[\w.-]+',patch) or axis not in ('x','y','z'):
        raise ValueError('边界层需要有效的网格壁面名称及其法向轴 x、y 或 z')
    errors=[]
    count=_count(value,'minLayers','mesh.boundaryLayers',errors)
    for name in ('maxFirstLayerHeight','maxGrowth'):
        _positive_number(value,name,'mesh.boundaryLayers',errors)
    if errors or count<1 or float(value['maxGrowth'])<1:
        raise ValueError('边界层层数和首层高度上限须大于零，增长率上限须至少为 1')
    return {'patch':patch,'axis':axis,'minLayers':count,'maxFirstLayerHeight':float(value['maxFirstLayerHeight']),
            'maxGrowth':float(value['maxGrowth'])}


def validate(mesh, errors):
    for name, values in _ENUMS.items():
        _enum(mesh,name,values,'mesh',errors)
    if 'mooseAdaptivity' in mesh:
        from .moose_mesh_adaptivity import declared
        try:
            declared(mesh)
        except (ValueError,OverflowError) as error:
            _error(errors,'mesh.mooseAdaptivity','value',str(error))
    if 'dynamicRefinement' in mesh:
        from .openfoam_dynamic_refinement import declared
        try:
            declared(mesh)
        except (ValueError,OverflowError) as error:
            _error(errors,'mesh.dynamicRefinement','value',str(error))
    if 'elementFamilies' in mesh:
        try:
            element_families(mesh['elementFamilies'])
        except ValueError as error:
            _error(errors,'mesh.elementFamilies','enum',str(error))
    if 'targetCellOrElementCount' in mesh:
        count=_count(mesh,'targetCellOrElementCount','mesh',errors)
        if count==0:
            _error(errors,'mesh.targetCellOrElementCount','range','目标单元数必须为正整数')
    if 'boundaryLayerRequired' in mesh:
        _bool(mesh['boundaryLayerRequired'],'mesh.boundaryLayerRequired',errors)
    if 'boundaryLayers' in mesh:
        try:
            boundary_layer_plan(mesh['boundaryLayers'])
        except ValueError as error:
            _error(errors,'mesh.boundaryLayers','value',str(error))
    if 'refinement' in mesh:
        if mesh.get('adaptivity')!='h':
            _error(errors,'mesh.adaptivity','required','方向网格加密需要 adaptivity h')
        try:
            directional_refinement(mesh['refinement'])
        except ValueError as error:
            _error(errors,'mesh.refinement','value',str(error))
    for name in ('characteristicSize','minSize','maxSize','growthRate'):
        _positive_number(mesh,name,'mesh',errors)
    if not any(error['path'] in {'mesh.minSize','mesh.maxSize','mesh.characteristicSize','mesh.growthRate'} for error in errors):
        sizes=[(name,float(mesh[name])) for name in ('minSize','characteristicSize','maxSize') if name in mesh]
        if any(left[1]>right[1] for left,right in zip(sizes,sizes[1:])):
            _error(errors,'mesh','size_order','minSize、characteristicSize、maxSize 需按大小递增')
        if 'growthRate' in mesh and float(mesh['growthRate'])<1:
            _error(errors,'mesh.growthRate','range','尺寸增长率须至少为 1')
