"""Bind a prescribed oscillating wall to native OpenFOAM mesh deformation."""

import math
import re

from .case_boundary_inputs import reference_names, scalar
from .casedict import parse_casedict
from .casedict_schema import finite_vector, valid_cell_zone
from .openfoam_boundary_inputs import _existing


DYNAMIC = 'constant/dynamicMeshDict'
DISPLACEMENT = '0/pointDisplacement'
MOTION_SOLVER = 'displacementLaplacian'
SOLVER = 'pimpleFoam'


def selection(case):
    problem = case.get('physicalProblem')
    return isinstance(problem, dict) and 'deformingFrame' in problem


def declared(case):
    if not selection(case):
        return None
    problem = case['physicalProblem']
    frame = problem['deformingFrame']
    if problem.get('coordinateFrame') != 'deforming' or not isinstance(frame, dict):
        raise ValueError('deformingFrame 需要 coordinateFrame deforming 和有效的控制块')
    moving, fixed = frame.get('movingPatch'), frame.get('fixedPatch')
    free = reference_names(frame.get('freePatches', '()'))
    if not valid_cell_zone(moving) or not valid_cell_zone(fixed) or len(set([moving, fixed, *free])) != len(free)+2:
        raise ValueError('变形需要互不重复的移动壁面、固定壁面和自由边界名称')
    amplitude = list(finite_vector(frame.get('amplitude')))
    omega = scalar(frame.get('angularFrequency'))
    if not any(amplitude) or omega <= 0:
        raise ValueError('变形需要非零米制位移幅值和正的 rad/s 角频率')
    return {'moving_patch': moving, 'fixed_patch': fixed, 'free_patches': sorted(free),
            'amplitude_m': amplitude, 'angular_frequency_rad_s': omega}


def requested(document):
    return declared(parse_casedict(document))


def dynamic_dictionary(frame):
    return ('FoamFile { version 2.0; format ascii; class dictionary; object dynamicMeshDict; }\n'
            'dynamicFvMesh dynamicMotionSolverFvMesh;\nmotionSolverLibs (fvMotionSolvers);\n'
            'motionSolver displacementLaplacian;\ndiffusivity uniform;\n').encode('ascii')


def displacement_dictionary(frame):
    components = ' '.join(map(repr, frame['amplitude_m']))
    moving = frame['moving_patch']
    fixed = frame['fixed_patch']
    omega = frame['angular_frequency_rad_s']
    return ('FoamFile { version 2.0; format ascii; class pointVectorField; object pointDisplacement; }\n'
            'dimensions [0 1 0 0 0 0 0];\ninternalField uniform (0 0 0);\nboundaryField\n{\n'
            f'    {fixed} {{ type fixedValue; value uniform (0 0 0); }}\n'
            f'    {moving} {{ type oscillatingDisplacement; amplitude ({components}); omega {omega!r}; '
            'value uniform (0 0 0); }\n'
            + ''.join(f'    {patch} {{ type zeroGradient; }}\n' for patch in frame['free_patches'])+'}\n').encode('ascii')


def verify_patches(frame, patches):
    walls = {frame['moving_patch'], frame['fixed_patch']}
    if set(patches) != walls | set(frame['free_patches']) or any(patches.get(name) != 'wall' for name in walls):
        raise ValueError('变形边界必须覆盖原生网格，移动和固定边界需要 wall 类型')


def _zero_uniform(value):
    if not isinstance(value, list) or value[:1] != ['uniform'] or any(not isinstance(item, str) for item in value):
        return False
    return finite_vector(' '.join(value[1:])) == (0.0, 0.0, 0.0)


def _check_point_field(native, frame):
    patches = native.get('boundaryField')
    if (not isinstance(patches, dict) or set(patches) != {frame['moving_patch'], frame['fixed_patch'], *frame['free_patches']}
            or ' '.join(native.get('dimensions', [])) != '[0 1 0 0 0 0 0]' or not _zero_uniform(native.get('internalField'))):
        raise ValueError('原生 pointDisplacement 的量纲、初值或边界与声明不一致')
    moving = patches.get(frame['moving_patch'])
    fixed = patches.get(frame['fixed_patch'])
    if (not isinstance(moving, dict) or moving.get('type') != 'oscillatingDisplacement'
            or not isinstance(fixed, dict) or fixed.get('type') != 'fixedValue'
            or not _zero_uniform(moving.get('value')) or not _zero_uniform(fixed.get('value'))
            or list(finite_vector(moving.get('amplitude'))) != frame['amplitude_m']
            or not math.isclose(scalar(moving.get('omega')), frame['angular_frequency_rad_s'], rel_tol=1e-12)
            or any(patches[name] != {'type':'zeroGradient'} for name in frame['free_patches'])):
        raise ValueError('原生 pointDisplacement 的壁面位移或自由边界与声明不一致')


def prepare(root, run_dir, solver, frame, *, require_native=False):
    if frame is None:
        return {}, {}
    if solver != SOLVER:
        raise ValueError('声明的变形坐标系需要串行 pimpleFoam')
    velocity = _existing(root, run_dir+'/0/U')
    patches = velocity.get('boundaryField') if velocity else None
    moving = patches.get(frame['moving_patch']) if isinstance(patches, dict) else None
    if not isinstance(moving, dict) or moving.get('type') != 'movingWallVelocity':
        raise ValueError('变形需要在 0/U 的移动壁面上使用原生 movingWallVelocity')
    dynamic = run_dir+'/'+DYNAMIC
    point_field = run_dir+'/'+DISPLACEMENT
    controls = _existing(root, dynamic)
    displacement = _existing(root, point_field)
    wanted = dynamic_dictionary(frame)
    if controls is not None:
        expected = parse_casedict(wanted.decode('ascii'))
        if {k: v for k, v in controls.items() if k != 'FoamFile'} != {k: v for k, v in expected.items() if k != 'FoamFile'}:
            raise ValueError('原生 dynamicMeshDict 与声明的 Laplacian 变形不一致')
    if displacement is not None:
        _check_point_field(displacement, frame)
    if require_native and (controls is None or displacement is None):
        raise ValueError('封存的变形输入缺少动态网格或点位移字典')
    return ({dynamic:wanted} if controls is None else {},
            {point_field:displacement_dictionary(frame)} if displacement is None else {})


def observed(frame, log):
    if ('Selecting motion solver: '+MOTION_SOLVER not in log
            or not re.search(r'Solving for cellDisplacement[xyz]', log)):
        raise ValueError('原生日志未证明变形位移求解')
    return {'method': MOTION_SOLVER, 'moving_patch': frame['moving_patch'], 'fixed_patch': frame['fixed_patch']}
