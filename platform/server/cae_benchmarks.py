"""Isolated, real OpenFOAM geometry-to-report acceptance benchmarks."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import shlex
import sys
import time
import urllib.parse

from app.services.casedict import set_block
from cae_fixtures import cavity_2d, cavity_3d
from testsupport import IsolatedServer

BASHRC = "/usr/lib/openfoam/openfoam2406/etc/bashrc"


def thermal_files():
    h = "FoamFile\n{ version 2.0; format ascii; class %s; object %s; }\n"
    run = "C-run/conduction"
    return {
        f"{run}/system/blockMeshDict": h % ("dictionary", "blockMeshDict") + """
convertToMeters 1;
vertices ((0 0 0) (1 0 0) (1 1 0) (0 1 0) (0 0 1) (1 0 1) (1 1 1) (0 1 1));
blocks (hex (0 1 2 3 4 5 6 7) (20 2 2) simpleGrading (1 1 1));
boundary (
 xMin { type wall; faces ((0 4 7 3)); }
 xMax { type wall; faces ((1 2 6 5)); }
 insulated { type wall; faces ((0 1 5 4) (3 7 6 2) (0 3 2 1) (4 5 6 7)); }
);
""",
        f"{run}/system/controlDict": h % ("dictionary", "controlDict") + """
application laplacianFoam;
startFrom startTime; startTime 0; stopAt endTime; endTime 1; deltaT 1;
writeControl timeStep; writeInterval 1; writeFormat ascii; writePrecision 12;
writeCompression off; timeFormat general; timePrecision 6; runTimeModifiable false;
""",
        f"{run}/system/fvSchemes": h % ("dictionary", "fvSchemes") + """
ddtSchemes { default steadyState; }
gradSchemes { default Gauss linear; }
divSchemes { default none; }
laplacianSchemes { default Gauss linear orthogonal; }
interpolationSchemes { default linear; }
snGradSchemes { default orthogonal; }
""",
        f"{run}/system/fvSolution": h % ("dictionary", "fvSolution") + """
solvers { T { solver PCG; preconditioner DIC; tolerance 1e-12; relTol 0; } }
SIMPLE { nNonOrthogonalCorrectors 0; }
""",
        f"{run}/constant/transportProperties": h % ("dictionary", "transportProperties")
            + "DT [0 2 -1 0 0 0 0] 0.01;\n",
        f"{run}/0/T": h % ("volScalarField", "T") + """
dimensions [0 0 0 1 0 0 0]; internalField uniform 300;
boundaryField {
 xMin { type fixedValue; value uniform 300; }
 xMax { type fixedValue; value uniform 400; }
 insulated { type zeroGradient; }
}
""",
    }


def raw(api, path):
    return api.call("GET", api.prefix + "/raw?path=" + urllib.parse.quote(path, safe=""), raw=True)


def run_benchmark(kind):
    """Run the original numerical criteria through registered source-to-report steps."""
    from registered_cae_benchmarks import run_benchmark as registered
    return registered(kind)
