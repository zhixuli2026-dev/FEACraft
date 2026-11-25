#!/usr/bin/env bash
# Build the regular OpenMP SU2 solver library and its standalone job entry point.
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
c6463b667ed81ec8fef1efa015f1368d8ceb29078cfea899a3fad5c7c232f3a9  SU2-v8.5.0.tar.gz
f84d9defb7fdb26e2e22942e32b59933de7f1d8d365849bb28c1741ac0a46c70  meson-5a82ea.tar.gz
e0f7f95ab80d098a2c2bafceefb52e958378302c2150ef7cb9fe2bf4aa81177b  MEL-46205a.tar.gz
f1d28c2205d015490a685b1e5a171c434da87f757746724de3cb85e69621dec2  eigen-d71c30.tar.gz
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
tar -xzf "$archives/SU2-v8.5.0.tar.gz" -C "$workspace"
bash "$adapter/../apply-patches.sh" "$workspace/SU2-8.5.0" "${@:3}"
cd "$workspace/SU2-8.5.0"
tar -xzf "$archives/meson-5a82ea.tar.gz" -C externals/meson --strip-components=1
tar -xzf "$archives/MEL-46205a.tar.gz" -C externals/mel --strip-components=1
tar -xzf "$archives/eigen-d71c30.tar.gz" -C externals/eigen --strip-components=1
python3 preconfigure.py --no-codi --no-medi --no-opdi --no-mpp --no-coolprop --no-mel --no-fado --no-mlpcpp --no-eigen
cp "$adapter/entry.cpp" SU2_CFD/src/feacraft_entry.cpp
cp "$adapter/cli.cpp" SU2_CFD/src/feacraft_cli.cpp
cat "$adapter/targets.meson" >> SU2_CFD/src/meson.build
python3 externals/meson/meson.py setup build -Dwith-mpi=disabled -Dwith-omp=true -Denable-cgns=false \
    -Denable-tecio=false -Dcpu-arch=x86-64
targets=SU2_CFD/src
ninja -C build -j "${SU2_BUILD_JOBS:-4}" "$targets/SU2_CFD-source"
parent=$(dirname -- "$output")
mkdir -p -- "$parent"
release=$(mktemp -d -- "$parent/.feacraft-su2.XXXXXX")
cp "build/$targets/SU2_CFD-source" "$release/SU2_CFD"
cp "build/$targets/libfeacraft_su2.so" "$release/"
(
    cd "$release"
    sha256sum SU2_CFD libfeacraft_su2.so > SHA256SUMS
)
mv -T -- "$release" "$output"
