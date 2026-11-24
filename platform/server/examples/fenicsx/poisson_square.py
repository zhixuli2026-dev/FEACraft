"""Solve a manufactured Poisson problem and measure mesh convergence with DOLFINx."""

import json
import math
from pathlib import Path

import dolfinx
from dolfinx import fem, io, mesh
from dolfinx.fem.petsc import LinearProblem
from mpi4py import MPI
from petsc4py import PETSc
import ufl


def solve(divisions, *, export=False):
    domain = mesh.create_unit_square(MPI.COMM_SELF, divisions, divisions, cell_type=mesh.CellType.triangle)
    space = fem.functionspace(domain, ('Lagrange', 1))
    domain.topology.create_connectivity(1, 2)
    boundary = mesh.exterior_facet_indices(domain.topology)
    dofs = fem.locate_dofs_topological(space, 1, boundary)
    zero = fem.dirichletbc(PETSc.ScalarType(0), dofs, space)
    u, v = ufl.TrialFunction(space), ufl.TestFunction(space)
    point = ufl.SpatialCoordinate(domain)
    x, y = point[0], point[1]
    exact = x * (1 - x) * y * (1 - y)
    load = 2 * (x * (1 - x) + y * (1 - y))
    problem = LinearProblem(
        ufl.inner(ufl.grad(u), ufl.grad(v)) * ufl.dx,
        ufl.inner(load, v) * ufl.dx,
        bcs=[zero], petsc_options_prefix=f'vf_poisson_{divisions}_',
        petsc_options={'ksp_type': 'cg', 'pc_type': 'jacobi', 'ksp_rtol': 1e-12,
                       'ksp_error_if_not_converged': True},
    )
    solution = problem.solve()
    squared = fem.form(ufl.inner(solution - exact, solution - exact) * ufl.dx(metadata={'quadrature_degree': 8}))
    error = math.sqrt(float(fem.assemble_scalar(squared).real))
    if export:
        solution.name = 'u'
        with io.XDMFFile(domain.comm, 'poisson-square.xdmf', 'w') as writer:
            writer.write_mesh(domain)
            writer.write_function(solution)
    return {'divisions': divisions, 'cells': domain.topology.index_map(2).size_local,
            'degrees_of_freedom': space.dofmap.index_map.size_local, 'l2_error': error,
            'iterations': problem.solver.getIterationNumber(), 'reason': problem.solver.getConvergedReason()}


if __name__ == '__main__':
    coarse = solve(24)
    fine = solve(48, export=True)
    Path('fenics-summary.json').write_text(json.dumps({
        'dolfinx': dolfinx.__version__, 'petsc': PETSc.Sys.getVersion(), 'coarse': coarse, 'fine': fine,
        'error_ratio': coarse['l2_error'] / fine['l2_error'],
    }, indent=2) + '\n', encoding='ascii')
