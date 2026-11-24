"""Solve -u'' = 2, with u(0)=u(1)=0, using PETSc CG on a uniform mesh."""

import csv
import json
from pathlib import Path

from petsc4py import PETSc


def main():
    points = 63
    spacing = 1 / (points + 1)
    matrix = PETSc.Mat().createAIJ((points, points), nnz=3, comm=PETSc.COMM_SELF)
    for row in range(points):
        matrix[row, row] = 2
        if row > 0:
            matrix[row, row - 1] = -1
        if row < points - 1:
            matrix[row, row + 1] = -1
    matrix.assemble()

    solution, rhs = matrix.createVecs()
    rhs.set(2 * spacing**2)
    solver = PETSc.KSP().create(comm=PETSc.COMM_SELF)
    solver.setOperators(matrix)
    solver.setType(PETSc.KSP.Type.CG)
    solver.getPC().setType(PETSc.PC.Type.JACOBI)
    solver.setTolerances(rtol=1e-12, atol=1e-14, max_it=points * 2)
    solver.solve(rhs, solution)
    reason = solver.getConvergedReason()
    if reason <= 0:
        raise RuntimeError(f'PETSc did not converge (reason {reason})')

    residual = rhs.duplicate()
    matrix.mult(solution, residual)
    residual.axpy(-1, rhs)
    with Path('petsc-poisson.csv').open('w', encoding='ascii', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(('x', 'value'))
        for index, value in enumerate(solution.getArray(readonly=True), start=1):
            writer.writerow((index * spacing, float(value)))
    Path('petsc-summary.json').write_text(json.dumps({
        'iterations': solver.getIterationNumber(), 'converged_reason': reason, 'residual_l2': residual.norm(),
        'version': PETSc.Sys.getVersion(), 'solver': solver.getType(), 'preconditioner': solver.getPC().getType(),
    }, indent=2) + '\n', encoding='ascii')


if __name__ == '__main__':
    main()
