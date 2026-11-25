#!/usr/bin/env bash
# Build serial Code_Aster 18.1.11 against an existing dependency runtime.
set -euo pipefail

if [[ $# -lt 3 ]]; then
    echo "Usage: bash build.sh SOURCE_ARCHIVE DEPENDENCY_RUNTIME NEW_OUTPUT_DIRECTORY [PATCH ...]" >&2
    exit 2
fi
adapter=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
archive=$(realpath -e -- "$1")
deps=$(realpath -e -- "$2")
output=$(realpath -m -- "$3")
if [[ -e $output ]]; then
    echo "Use a new output directory: $output" >&2
    exit 2
fi
printf '%s  %s\n' 0a8d22aec1d242df7014fe3f9536221f1ac70b8a0fa4c03155eca33a62db8cc1 "$archive" | sha256sum -c

workspace=$(mktemp -d)
trap 'rm -rf -- "$workspace"' EXIT
tar -xzf "$archive" -C "$workspace"
source="$workspace/src-87f699afe0706d84fd180e7a84d88bd0556e669c"
bash "$adapter/../apply-patches.sh" "$source" "${@:4}"
cd "$source"
export PATH="$deps/bin:$PATH" LD_LIBRARY_PATH="$deps/lib"
export CC=/usr/bin/gcc CXX=/usr/bin/g++ FC=/usr/bin/gfortran
export TFELHOME="$deps" DEFINES=ASTER_INT8
export INCLUDES_METIS="$deps/include" LIBPATH_METIS="$deps/lib"
export INCLUDES_SCOTCH="$deps/include" LIBPATH_SCOTCH="$deps/lib"
export INCLUDES_BOOST="$deps/include" LIBPATH_BOOST="$deps/lib"
export INCLUDES_MUMPS="$deps/include $deps/include/mumps_seq" LIBPATH_MUMPS="$deps/lib"
export INCLUDES_MED="$deps/include" LIBPATH_MED="$deps/lib"
export INCLUDES_MEDCOUPLING="$deps/include" LIBPATH_MEDCOUPLING="$deps/lib"
export INCLUDES_HDF5="$deps/include" LIBPATH_HDF5="$deps/lib"
export PYPATH_MEDCOUPLING="$deps/lib/python3.12/site-packages"
export FCFLAGS=-fallow-argument-mismatch FFLAGS=-fallow-argument-mismatch
export CPPFLAGS="-I$deps/include" CXXFLAGS="-I$deps/include"
export LDFLAGS="-L$deps/lib -Wl,-rpath,$deps/lib"
export ASTER_CONFIG="$workspace/aster-dependencies.env"
touch "$ASTER_CONFIG"
printf '%s\n' 'pkginfo = ((18, 1, 11), "87f699afe0706d84fd180e7a84d88bd0556e669c", "18.1.11", "n/a", "n/a", 1, ["no source repository"])' \
    > code_aster/pkginfo.py

./waf.main configure --use-config-dir="$PWD/config" --prefix="$output" \
    --libdir="$output/lib" --site-packages="$output/lib/python3.12/site-packages" \
    --med-libs="med medC medfwrap medimport" --enable-med --enable-hdf5 \
    --enable-mumps --enable-metis --enable-scotch --enable-mfront \
    --mumps-libs="cmumps_seq dmumps_seq smumps_seq zmumps_seq pord_seq mumps_common_seq mpiseq_seq" \
    --disable-aster-subdir --disable-mpi --disable-petsc --without-hg
./waf.main install --destdir="$workspace/install" --jobs="${ASTER_BUILD_JOBS:-3}"
"$deps/bin/python3" "$adapter/bundle.py" --compiled "$workspace/install$output" \
    --dependencies "$deps" --output "$output"
