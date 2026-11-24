"""Verify JAX-FEM forward solution and adjoint sensitivity to volumetric source scale."""

from importlib.metadata import version
import json
from pathlib import Path

import jax
from jax import numpy as jnp
import numpy as np

from jax_fem.generate_mesh import Mesh, rectangle_mesh
from jax_fem.problem import Problem
from jax_fem.solver import ad_wrapper
from jax_fem.utils import save_sol


jax.config.update('jax_enable_x64', True)


class ScaledPoisson(Problem):
    def get_tensor_map(self):
        return lambda gradient, _: gradient

    def get_mass_map(self):
        def source(_, point, scale):
            x, y = point
            load = 2 * (x * (1 - x) + y * (1 - y))
            return jnp.array([-scale * load])
        return source

    def set_params(self, scale):
        field = self.fes[0]
        self.internal_vars = [jnp.full((field.num_cells, field.num_quads), scale)]


def on_boundary(point):
    return jnp.any(jnp.isclose(point, 0) | jnp.isclose(point, 1))


def zero_value(_):
    return 0.0


def main():
    geometry = rectangle_mesh(Nx=12, Ny=12, domain_x=1.0, domain_y=1.0)
    elements = geometry.cells_dict['quad']
    problem = ScaledPoisson(mesh=Mesh(geometry.points, elements), vec=1, dim=2, ele_type='QUAD4',
                            dirichlet_bc_info=[[on_boundary], [0], [zero_value]])
    solve = ad_wrapper(problem, solver_options={'umfpack_solver': {}}, adjoint_solver_options={'umfpack_solver': {}})

    def objective(scale):
        return jnp.mean(solve(scale)[0])

    scale = jnp.asarray(1.0)
    solution = solve(scale)[0]
    gradient = float(jax.grad(objective)(scale))
    step = 1e-4
    difference = float((objective(scale + step) - objective(scale - step)) / (2 * step))
    x, y = geometry.points.T
    expected = x * (1 - x) * y * (1 - y)
    error = float(np.max(np.abs(np.asarray(solution[:, 0]) - expected)))
    save_sol(problem.fes[0], solution, str(Path('jax-poisson.vtu').resolve()))
    Path('jax-fem-summary.json').write_text(json.dumps({
        'jax': jax.__version__, 'jax_fem': version('jax-fem'), 'platform': jax.default_backend(),
        'nodes': len(x), 'cells': len(elements), 'maximum_error': error,
        'source_adjoint': gradient, 'source_finite_difference': difference,
    }, indent=2) + '\n', encoding='ascii')


if __name__ == '__main__':
    main()
