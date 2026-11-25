#!/usr/bin/env bash
# Build the upstream MOOSE framework and combined application.
set -euo pipefail

if [[ $# -lt 4 ]]; then
    echo "Usage: bash build.sh SOURCE_ARCHIVE DEPENDENCY_RUNTIME NEW_BUILD_DIRECTORY NEW_RUNTIME_DIRECTORY [PATCH ...]" >&2
    exit 2
fi
archive=$(realpath -e -- "$1")
deps=$(realpath -e -- "$2")
workspace=$(realpath -m -- "$3")
output=$(realpath -m -- "$4")
if [[ -e $workspace ]]; then
    echo "Use a new build directory on the Linux filesystem: $workspace" >&2
    exit 2
fi
if [[ -e $output || $output == /tmp || $output == /tmp/* ]]; then
    echo "Use a new runtime directory outside /tmp: $output" >&2
    exit 2
fi
command -v patchelf >/dev/null
builder=$(dirname "$(realpath -e -- "${BASH_SOURCE[0]}")")
printf '%s  %s\n' 2a7a49fa91fda5c26e61635adeebf81df5a0635d565b96e218d8f6fbb9abec6a "$archive" | sha256sum -c

mkdir -p -- "$workspace"
tar -xzf "$archive" -C "$workspace"
source="$workspace/moose-08315af82e"
bash "$builder/../apply-patches.sh" "$source" "${@:5}"
cd "$source"
export PATH="$deps/bin:$PATH" LD_LIBRARY_PATH="$deps/lib"
export LIBMESH_DIR="$deps" PETSC_DIR="$deps" WASP_DIR="$deps" METHOD=opt
export MPICH_CC=/usr/bin/gcc MPICH_CXX=/usr/bin/g++ MPICH_FC=/usr/bin/gfortran
export CC=mpicc CXX=mpicxx FC=mpifort
export MOOSE_SKIP_DOCS=1
# Use the host glibc linker inputs with the host compiler. Keep the remaining
# libraries, including MPI, on the installed runtime's dependency paths.
compat="$workspace/host-glibc"
mkdir "$compat"
for library in libc.so libm.so libmvec.so libpthread.a libdl.a librt.a; do
    host="$(gcc -print-file-name="$library")"
    test -f "$host"
    ln -s "$host" "$compat/$library"
done
export LDFLAGS="-L$compat ${LDFLAGS:-}"
jobs=${MOOSE_BUILD_JOBS:-2}
./configure --prefix="$output"
make -C modules/combined -j "$jobs"
python3 "$builder/bundle.py" "$PWD" modules/combined/combined-opt "$deps" "$output"
