"""Advance a finite-difference heat field, publishing its grid through Catalyst 2."""

import ctypes
import os
from pathlib import Path


def simulate():
    implementation = Path(os.environ['CATALYST_IMPLEMENTATION_PATHS'])
    # PyDLL retains the GIL when ParaView calls into the running Python interpreter.
    library = ctypes.PyDLL(str(implementation.parent/'libcatalyst.so.3'), mode=ctypes.RTLD_GLOBAL)

    def bind(name, result, *arguments):
        method = getattr(library,name)
        method.restype = result
        method.argtypes = arguments
        return method

    node_type, count = ctypes.c_void_p, ctypes.c_int64
    key, scalar = ctypes.c_char_p, ctypes.c_double
    create = bind('catalyst_conduit_node_create',node_type)
    destroy = bind('catalyst_conduit_node_destroy',None,node_type)
    put_text = bind('catalyst_conduit_node_set_path_char8_str',None,node_type,key,key)
    put_index = bind('catalyst_conduit_node_set_path_int64',None,node_type,key,count)
    put_value = bind('catalyst_conduit_node_set_path_float64',None,node_type,key,scalar)
    put_field = bind('catalyst_conduit_node_set_path_float64_ptr',None,node_type,key,ctypes.POINTER(scalar),count)
    initialize = bind('catalyst_initialize',ctypes.c_int,node_type)
    execute = bind('catalyst_execute',ctypes.c_int,node_type)
    finalize = bind('catalyst_finalize',ctypes.c_int,node_type)

    def check(status, operation):
        if status != 0:
            raise RuntimeError(f'Catalyst 2 {operation} returned status {status}')

    node = create()
    try:
        put_text(node,b'catalyst/scripts/heat/filename',os.fsencode(os.environ['VIBEFLOW_CATALYST_STATE']))
        check(initialize(node),'initialize')
    finally:
        destroy(node)

    grid_size = 9
    center = grid_size*grid_size//2
    node = create()
    try:
        channel = b'catalyst/channels/heat/'
        mesh = channel+b'data/'
        put_text(node,channel+b'type',b'mesh')
        put_text(node,mesh+b'coordsets/coords/type',b'uniform')
        for axis in (b'i',b'j'):
            put_index(node,mesh+b'coordsets/coords/dims/'+axis,grid_size)
        for axis in (b'dx',b'dy'):
            put_value(node,mesh+b'coordsets/coords/spacing/'+axis,1/(grid_size-1))
        put_text(node,mesh+b'topologies/grid/type',b'uniform')
        put_text(node,mesh+b'topologies/grid/coordset',b'coords')
        put_text(node,mesh+b'fields/T/association',b'vertex')
        put_text(node,mesh+b'fields/T/topology',b'grid')
        values = [300.0]*(grid_size*grid_size)
        values[center] = 400.0
        for step in range(5):
            data = (scalar * len(values))(*values)
            put_index(node,b'catalyst/state/timestep',step)
            put_value(node,b'catalyst/state/time',step*.1)
            put_field(node,mesh+b'fields/T/values',data,len(values))
            check(execute(node),f'execute at step {step}')
            print(f'Catalyst 2 step={step} center_K={values[center]:.8f}',flush=True)
            values = diffuse(values,grid_size)
    finally:
        destroy(node)
        node = create()
        try:
            check(finalize(node),'finalize')
        finally:
            destroy(node)


def diffuse(field, size):
    result = list(field)
    for row in range(1,size-1):
        for column in range(1,size-1):
            point = row*size+column
            neighbors = field[point-1]+field[point+1]+field[point-size]+field[point+size]
            result[point] = field[point] + .15*(neighbors-4*field[point])
    return result


if __name__ == '__main__':
    simulate()
