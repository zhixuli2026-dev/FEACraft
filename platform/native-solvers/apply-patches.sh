#!/usr/bin/env bash
# Apply explicitly selected patches to an extracted upstream source tree.
set -euo pipefail

source_root=$1
shift
for patch_file in "$@"; do
    patch_file=$(realpath -e -- "$patch_file")
    sha256sum -- "$patch_file"
    patch --directory "$source_root" --strip=1 --batch --forward --fuzz=0 --input "$patch_file"
done
