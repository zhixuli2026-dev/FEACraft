#!/usr/bin/env bash
# Build the serial Elmer library, its regular launcher, and the registered FEM modules.
set -euo pipefail

if [[ $# -lt 2 ]]; then
    echo "Usage: bash build.sh SOURCE_ARCHIVE NEW_OUTPUT_DIRECTORY [PATCH ...]" >&2
    exit 2
fi
adapter=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
archive=$(realpath -e -- "$1")
output=$(realpath -m -- "$2")
if [[ -e $output ]]; then
    echo "Use a new output directory: $output" >&2
    exit 2
fi
printf '%s  %s\n' 35bdbbbbe1bca54177859594efd06e86c2bce1f043a1ea0ac9d4e6f2262dcc33 "$archive" | sha256sum -c

workspace=$(mktemp -d)
release=
cleanup() {
    rm -rf -- "$workspace"
    if [[ -n $release ]]; then
        rm -rf -- "$release"
    fi
}
trap cleanup EXIT
tar -xzf "$archive" -C "$workspace"
source="$workspace/elmerfem-ab1ecf1b846f5f4e577434e6195153e3581304ed"
bash "$adapter/../apply-patches.sh" "$source" "${@:3}"
build="$workspace/build"
blas=()
if [[ -n ${ELMER_BLAS_LIBRARIES:-} ]]; then
    blas+=("-DBLAS_LIBRARIES=$ELMER_BLAS_LIBRARIES")
fi
if [[ -n ${ELMER_LAPACK_LIBRARIES:-} ]]; then
    blas+=("-DLAPACK_LIBRARIES=$ELMER_LAPACK_LIBRARIES")
fi
cmake -S "$source" -B "$build" -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$output" \
    -DWITH_MPI=OFF -DWITH_UMFPACK=ON -DWITH_ELMERGUI=OFF "${blas[@]}"
modules=(HeatSolve StressSolve FlowSolve StatElecSolve StatCurrentSolve StatCurrentSolveVec ResultOutputSolve)
cmake --build "$build" --target ElmerSolver "${modules[@]}" \
    --parallel "${ELMER_BUILD_JOBS:-4}"

bundle="$workspace/package"
lib="$bundle/share/elmersolver/lib"
mkdir -p "$bundle/bin" "$lib"
cp "$build/fem/src/ElmerSolver" "$bundle/bin/"
cp "$build/fem/src/libelmersolver.so" "$build/fem/src/libmpi_stubs.so" \
    "$build/matc/src/libmatc.so" "$build/fhutiter/src/libfhuti.so" "$build/mathlibs/src/arpack/libarpack.so" \
    "$source/fem/src/elements.def" "$source/fem/src/SOLVER.KEYWORDS" "$lib/"
for module in "${modules[@]}"; do
    cp "$build/fem/src/modules/$module.so" "$lib/"
done
patchelf --set-rpath '$ORIGIN/../share/elmersolver/lib' "$bundle/bin/ElmerSolver"
for library in "$lib"/*.so; do
    patchelf --set-rpath '$ORIGIN' "$library"
done
(
    cd "$bundle"
    sha256sum bin/ElmerSolver share/elmersolver/lib/* > SHA256SUMS
)
parent=$(dirname -- "$output")
mkdir -p -- "$parent"
release=$(mktemp -d -- "$parent/.feacraft-elmer.XXXXXX")
cp -R "$bundle/." "$release"
mv -T -- "$release" "$output"
