#!/bin/bash
# J16/F58：user+mount+net+PID namespace 的最小根文件系统。
set -euo pipefail
CASE_DIR="$(realpath -e -- "$1")"
METADATA_HELPER="$(dirname "$(realpath -e -- "$0")")/tree_metadata_native.py"
COMMAND="$2"
MODE="${3:-legacy}"
if [[ "$MODE" != legacy && "$MODE" != --external-export && "$MODE" != --persistent-export ]]; then exit 64; fi
# Both supported callers convert an absolute Windows workspace to /mnt/<drive>.
# A namespace-local /tmp avoids colliding on PPID=0 across concurrent sandboxes
# and disappears with the mount namespace, including after forced termination.
if [[ "$CASE_DIR" != /mnt/* ]]; then exit 64; fi
mount --make-rprivate /
mount -t tmpfs -o size=1m tmpfs /tmp
ROOT="/tmp/vf-sandbox-root"
mkdir -p "$ROOT"
mount -t tmpfs -o size=512m tmpfs "$ROOT"
mkdir -p "$ROOT"/{usr,bin,lib,lib64,sbin,etc,proc,sys,dev,tmp,case,output,opt,oldroot}
for path in usr bin lib lib64 sbin etc sys; do
  mount --rbind "/$path" "$ROOT/$path"
  mount -o remount,ro,bind "$ROOT/$path"
done
PARAVIEW=/opt/vibeflow-paraview-5.11.2
if [[ -x "$PARAVIEW/bin/pvpython" ]]; then
  mkdir -p "$ROOT$PARAVIEW"
  mount --bind "$PARAVIEW" "$ROOT$PARAVIEW"
  mount -o remount,ro,bind "$ROOT$PARAVIEW"
fi
mount -t proc proc "$ROOT/proc"
for dev in null zero urandom; do
  touch "$ROOT/dev/$dev"
  mount --bind "/dev/$dev" "$ROOT/dev/$dev"
done
mount -t tmpfs -o size=128m tmpfs "$ROOT/tmp"
STAGE="$ROOT/tmp/case"
mkdir -p "$STAGE"
if [[ "$MODE" == --persistent-export ]]; then
  VOLUME="${4:?persistent volume root required}/disk/case"
  mkdir -p "$VOLUME"
  mount --bind "$VOLUME" "$STAGE"
fi
# Runtime journals stay outside the bounded user volume. Only the portable
# tree descriptor is needed to reconstruct Linux modes and S3 empty dirs.
shopt -s dotglob nullglob
for entry in "$CASE_DIR"/*; do
  name="${entry##*/}"
  if [[ "${name,,}" != .vibeflow ]]; then cp -a -- "$entry" "$STAGE/"; fi
done
if [[ -f "$CASE_DIR/.vibeflow/tree-state.json" ]]; then
  mkdir -p "$STAGE/.vibeflow"
  cp -a -- "$CASE_DIR/.vibeflow/tree-state.json" "$STAGE/.vibeflow/tree-state.json"
fi
chmod -R a+rwX "$STAGE"
python3 -I -S "$METADATA_HELPER" "$STAGE"
mount --rbind "$STAGE" "$ROOT/case"
if [[ "$MODE" == legacy ]]; then mount --rbind "$CASE_DIR" "$ROOT/output"; fi
cd "$ROOT"
pivot_root . oldroot
umount -l /oldroot
export HOME=/tmp
ulimit -v 2097152
ulimit -t 3600
set +e
unshare --user --map-user=65534 --map-group=65534 /bin/bash -lc "cd /case && $COMMAND"
STATUS=$?
set -e
if [[ "$MODE" == legacy ]]; then cp -a "/tmp/case/." "/output/"; fi
exit "$STATUS"
