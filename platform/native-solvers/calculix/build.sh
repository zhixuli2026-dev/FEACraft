#!/usr/bin/env bash
# Build the CalculiX C/Fortran solver as a shared library, plus a job entry point.
set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "Usage: bash build.sh SOURCE_ARCHIVE_DIRECTORY NEW_OUTPUT_DIRECTORY [PATCH ...]" >&2
    exit 2
fi

adapter=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
archives=$(realpath -e -- "$1")
output=$(realpath -m -- "$2")
if [[ -e $output ]]; then
    echo "Use a new output directory: $output" >&2
    exit 2
fi

(
    cd "$archives"
    sha256sum -c <<'SHA256'
9c88385c10fb04f5dc6c4e98027a51bebdd8aee3920e05190d6c1dd08357d6e7  ccx_2.23.src.tar.bz2
a84559a0e987a1e423055ef4fdf3035d55b65bbe4bf915efaa1a35bef7f8c5dd  spooles.2.2.tgz
SHA256
)

workspace=$(mktemp -d)
release=
cleanup() {
    rm -rf -- "$workspace"
    if [[ -n $release ]]; then
        rm -rf -- "$release"
    fi
}
trap cleanup EXIT
tar -xjf "$archives/ccx_2.23.src.tar.bz2" -C "$workspace"
bash "$adapter/../apply-patches.sh" "$workspace/CalculiX/ccx_2.23" "${@:3}"
cd "$workspace"
mkdir SPOOLES.2.2
tar -xzf "$archives/spooles.2.2.tgz" -C SPOOLES.2.2
make -C SPOOLES.2.2 lib CC=gcc CFLAGS='-O2 -std=gnu89 -fPIC'

cd CalculiX/ccx_2.23/src
# Same GCC compatibility fix as the registered CalculiX Linux deployment.
python3 - <<'PYTHON'
from pathlib import Path
path = Path("readnewmesh.c")
lines = path.read_text().splitlines(keepends=True)
if lines[467].strip() != "return NULL;":
    raise SystemExit("Unexpected CalculiX readnewmesh source")
lines[467] = "  return;\n"
path.write_text("".join(lines))
PYTHON
flags=(-O2 -fPIC -I ../../../SPOOLES.2.2 -DARCH=Linux -DSPOOLES -DARPACK -DMATRIXSTORAGE -DNETWORKOUT)
make -j "${CALCULIX_BUILD_JOBS:-4}" ccx_2.23.a CFLAGS="${flags[*]}" \
    FFLAGS='-O2 -cpp -fallow-argument-mismatch -fPIC'
./date.pl
gcc "${flags[@]}" -Dmain=feacraft_calculix_run -c ccx_2.23.c -o source-entry.o
parent=$(dirname -- "$output")
mkdir -p -- "$parent"
release=$(mktemp -d -- "$parent/.feacraft-calculix.XXXXXX")
gfortran -shared -Wl,-z,defs -o "$release/libfeacraft_calculix.so" source-entry.o \
    ccx_2.23.a ../../../SPOOLES.2.2/spooles.a \
    -l:libarpack.so.2 -l:liblapack.so.3 -l:libblas.so.3 -lpthread -lm -fopenmp
gcc -o "$release/ccx" "$adapter/entry.c" -L"$release" -lfeacraft_calculix \
    '-Wl,-rpath,$ORIGIN'
(
    cd "$release"
    sha256sum ccx libfeacraft_calculix.so > SHA256SUMS
)
mv -T -- "$release" "$output"
