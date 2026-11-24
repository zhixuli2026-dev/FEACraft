#!/usr/bin/env bash
# Run from the case root after loading the configured OpenFOAM environment.
set -euo pipefail
gmsh -3 B-mesh/gmsh/box.geo -format msh2 -o B-mesh/gmsh/box.msh > B-mesh/gmsh/gmsh.log 2>&1
gmshToFoam B-mesh/gmsh/box.msh -case C-run/case-gmsh > C-run/case-gmsh/convert.log 2>&1
checkMesh -case C-run/case-gmsh > C-run/case-gmsh/checkMesh.log 2>&1
python3 - <<'PY'
import json, os, pathlib, re, subprocess
root = pathlib.Path('C-run/case-gmsh')
log = (root/'checkMesh.log').read_text()
if 'Mesh OK.' not in log or 'FOAM FATAL' in log:
    raise SystemExit('checkMesh did not verify the mesh')
result = {'schema': 'vibeflow.gmsh-example.v1', 'mesh_ok': True,
          'uid': os.getuid(), 'gmsh_version': subprocess.check_output(['gmsh','-version'], stderr=subprocess.STDOUT, text=True).strip(),
          'cells': int(re.search(r'^\s*cells:\s*(\d+)', log, re.M)[1])}
(root/'toolchain.json').write_text(json.dumps(result, indent=2)+'\n')
print(json.dumps(result))
PY
