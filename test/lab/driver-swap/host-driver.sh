#!/usr/bin/env bash
# Install, roll back and inspect the Tintri VMstore driver on a PCD cinder-volume host.
# Runbook: docs/runbooks/driver-swap.md. Normally driven by swap.sh from a workstation.
#
# Usage (as root on the cinder-volume host):
#   host-driver.sh deploy <build_dir> <version>   install the build, restart, check the start
#   host-driver.sh rollback [<backup_dir>]        default: the oldest backup, i.e. the driver
#                                                 found before the first deploy (pre-test state)
#   host-driver.sh status                         installed and running version, backups, state
#
# SWAP_DIR (default: this script's directory) keeps backups/<UTC time>/ with the driver files
# as found, MANIFEST (version, md5, owner, mode, driver directory) and history.log.
# Every change is backed up first; a failed deploy restores the backup and restarts on it.
# Restarting cinder-volume restarts every backend this host serves.
set -u
CMD=${1:-}; [ $# -gt 0 ] && shift
SWAP_DIR=${SWAP_DIR:-$(cd "$(dirname "$0")" && pwd)}
VENV=${CINDER_VENV:-/opt/pf9/pf9-cindervolume-base}
PY=$VENV/bin/python
LOG=${CINDER_LOG:-/var/log/pf9/cindervolume-base.log}
SERVICE=${CINDER_SERVICE:-pf9-cindervolume-base}
FILES="api.py __init__.py nfs.py options.py utils.py"
BK_ROOT=$SWAP_DIR/backups
HIST=$SWAP_DIR/history.log

say() { echo "[$(date -u +%T)] $*"; }
die() { say "ABORT: $*"; exit 1; }
record() { echo "$(date -u '+%Y-%m-%d %H:%M:%S') $*" >> "$HIST"; }
[ "$(id -u)" = 0 ] || die "run as root"
mkdir -p "$BK_ROOT" || die "cannot create $BK_ROOT"
exec 9>"$SWAP_DIR/.lock"
command -v flock >/dev/null && { flock -n 9 || die "another driver or option change is running on this host"; }

# Ask the service's own Python where Cinder lives: PCD Aug moved it from lib/python3.9 to
# lib/python3.12, and a copy in the old path is silently ignored.
driver_dir() {
  ( set +u; set -a; . "$VENV/cinder.env"; set +a
    cd /tmp && "$PY" -c 'import os, cinder; print(os.path.join(os.path.dirname(cinder.__file__), "volume/drivers/vmstore"))' ) 2>/dev/null; }
file_version() { sed -nE "s/^[[:space:]]+VERSION = '([^']+)'.*/\1/p" "$1" 2>/dev/null | head -1; }
import_version() { # what the service's Python imports
  ( set +u; set -a; . "$VENV/cinder.env"; set +a
    cd /tmp && "$PY" -c 'from cinder import objects; objects.register_all(); from cinder.volume.drivers.vmstore import nfs; print(nfs.VmstoreNfsDriver.VERSION)' ) 2>/dev/null | tail -1; }
running_line() { grep -a 'Starting volume driver VmstoreNfsDriver' "$LOG" 2>/dev/null | tail -1 | cut -c1-200; }
md5_of() { (cd "$1" && md5sum $FILES 2>/dev/null); }
backups() { ls -1d "$BK_ROOT"/[0-9]*/ 2>/dev/null | sed 's#/$##' | sort; }   # NNNN-<UTC time>: creation order
oldest_backup() { backups | head -1; }
new_dir() { # root -> new numbered dir; the number keeps the order even within one second
  local n; n=$(ls -1d "$1"/[0-9]*/ 2>/dev/null | wc -l | tr -d ' ')
  printf '%s/%04d-%s' "$1" $((n + 1)) "$(date -u +%Y%m%dT%H%M%SZ)"; }

restart_and_wait() { # version expected in the start line ("" = any)
  local start n
  start=$(date +"%Y-%m-%d %H:%M:%S")   # the log uses the host's local clock
  systemctl restart "$SERVICE" || return 1
  for _ in $(seq 1 36); do
    n=$(awk -v s="$start" '$0 >= s' "$LOG" | grep -c "Starting volume driver VmstoreNfsDriver (${1}")
    [ "$n" -gt 0 ] && break; sleep 5
  done
  [ "$n" -gt 0 ] || { say "no 'Starting volume driver VmstoreNfsDriver ($1' line within 3 minutes"; return 1; }
  sleep 20
  say "started $n backend(s) on ${1:-the installed driver}; errors since the restart:"
  awk -v s="$start" '$0 >= s' "$LOG" | grep -E " ERROR | CRITICAL " | cut -c1-220 | sort | uniq -c | head -20
  return 0; }

install_files() { # source dir, driver dir, owner, group, mode
  local f
  for f in $FILES; do
    install -m "$5" -o "$3" -g "$4" "$1/$f" "$2/$f" || return 1
    cmp -s "$1/$f" "$2/$f" || return 1
  done
  rm -rf "$2/__pycache__"; }

take_backup() { # driver dir, note -> backup dir
  local d=$1 bk; bk=$(new_dir "$BK_ROOT")
  [ ! -e "$bk" ] && mkdir -p "$bk" && cp -a "$d"/. "$bk"/ || return 1
  { echo "driver_dir $d"; echo "version $(file_version "$d/nfs.py")"
    echo "owner $(stat -c %u "$d/nfs.py") group $(stat -c %g "$d/nfs.py") mode $(stat -c %a "$d/nfs.py")"
    echo "note $2"; md5_of "$bk"; } > "$bk/MANIFEST" || return 1
  echo "$bk"; }

deploy() {
  local build=${1:-} want=${2:-} d bk f owner group mode
  [ -n "$build" ] && [ -n "$want" ] || die "usage: $0 deploy <build_dir> <version>"
  for f in $FILES; do [ -f "$build/$f" ] || die "missing $build/$f"; done
  d=$(driver_dir); [ -f "$d/nfs.py" ] || die "no driver found by the service's Python"
  say "driver directory: $d (installed $(file_version "$d/nfs.py"))"
  for f in $FILES; do "$PY" -m py_compile "$build/$f" || die "py_compile $f"; done
  [ "$(file_version "$build/nfs.py")" = "$want" ] || die "build is $(file_version "$build/nfs.py"), not $want"
  bk=$(take_backup "$d" "before deploying $want from $build") || die "backup failed"
  say "backup $bk ($(file_version "$bk/nfs.py"))"
  owner=$(stat -c %u "$d/nfs.py"); group=$(stat -c %g "$d/nfs.py"); mode=$(stat -c %a "$d/nfs.py")
  restore() {
    say "RESTORING $bk: $*"
    install_files "$bk" "$d" "$owner" "$group" "$mode" && (cd "$d" && md5sum -c --quiet <(grep -E '\.py$' "$bk/MANIFEST")) &&
      say "backup restored (md5 ok)" || say "RESTORE FAILED: copy $bk by hand"
    [ "$restarted" = yes ] && { restart_and_wait "$(file_version "$bk/nfs.py")" || say "service did not come back on the backup: check $SERVICE"; }
    record "deploy $want FAILED ($*), restored $bk"
    exit 1; }
  local restarted=no
  install_files "$build" "$d" "$owner" "$group" "$mode" || restore "install"
  [ "$(import_version)" = "$want" ] || restore "import test"
  say "installed: nfs.py $(md5sum "$d/nfs.py" | cut -c1-32)"
  restarted=yes
  restart_and_wait "$want" || restore "start check"
  record "deploy $want from $build, backup $bk"
  say "done: $want running; pre-test backup $(oldest_backup)"; }

rollback() {
  local bk=${1:-$(oldest_backup)} d v cur
  [ -n "$bk" ] && [ -f "$bk/MANIFEST" ] || die "no backup to roll back to (looked in $BK_ROOT)"
  (cd "$bk" && md5sum -c --quiet <(grep -E '\.py$' "$bk/MANIFEST")) || die "backup $bk does not match its MANIFEST"
  d=$(awk '$1 == "driver_dir" {print $2}' "$bk/MANIFEST"); v=$(file_version "$bk/nfs.py")
  [ "$d" = "$(driver_dir)" ] || die "backup is for $d, the service now uses $(driver_dir)"
  if (cd "$d" && md5sum -c --quiet <(grep -E '\.py$' "$bk/MANIFEST")) >/dev/null 2>&1 &&
     [[ "$(running_line)" == *"VmstoreNfsDriver ($v)"* ]]; then
    say "already on $bk ($v), installed and running"; record "rollback to $bk: already there"; return 0; fi
  cur=$(take_backup "$d" "before rolling back to $bk") || die "backup of the current files failed"
  say "current files saved in $cur"
  read -r _ owner _ group _ mode < <(awk '$1 == "owner"' "$bk/MANIFEST")
  install_files "$bk" "$d" "$owner" "$group" "$mode" || die "install failed: copy $bk to $d by hand"
  (cd "$d" && md5sum -c --quiet <(grep -E '\.py$' "$bk/MANIFEST")) || die "md5 mismatch after the copy"
  [ "$(import_version)" = "$v" ] || die "import test failed after the copy"
  restart_and_wait "$v" || die "service did not start on $v: check $SERVICE"
  record "rollback to $bk ($v)"
  say "done: rolled back to $bk ($v)"; }

status() {
  local d v old ov f
  d=$(driver_dir); v=$(file_version "$d/nfs.py"); old=$(oldest_backup)
  echo "host: $(hostname)"
  echo "driver directory used by the service: ${d:-unknown}"
  echo "installed: ${v:-?} (nfs.py md5 $(md5sum "$d/nfs.py" 2>/dev/null | cut -c1-32))"
  echo "imported by the service's Python: $(import_version)"
  echo "last start in the log: $(running_line)"
  find /opt/pf9 -path '*cinder/volume/drivers/vmstore/nfs.py' 2>/dev/null | while read -r f; do
    [ "$(dirname "$f")" = "$d" ] || echo "other copy (not used): $f, VERSION $(file_version "$f")"
  done
  echo "backups in $BK_ROOT:"
  backups | while read -r b; do echo "  $b $(file_version "$b/nfs.py") ($(awk '$1 == "note" {sub(/^note /, ""); print}' "$b/MANIFEST"))"; done
  if [ -z "$old" ]; then
    echo "STATE: UNKNOWN to this tool (no backup in $SWAP_DIR): nothing deployed from here; installed ${v:-?}"
  else
    ov=$(file_version "$old/nfs.py")
    if (cd "$d" && md5sum -c --quiet <(grep -E '\.py$' "$old/MANIFEST")) >/dev/null 2>&1; then
      if [[ "$(running_line)" == *"VmstoreNfsDriver ($ov)"* ]]; then echo "STATE: PRE-TEST, files and running driver match $old ($ov)"
      else echo "STATE: PRE-TEST FILES, but the last start line is not $ov: restart $SERVICE"; fi
    else echo "STATE: TEST BUILD ${v:-?} installed; pre-test is $old ($ov): run rollback to return"; fi
  fi
  [ -f "$HIST" ] && { echo "history:"; tail -5 "$HIST" | sed 's/^/  /'; }
  return 0; }

case "$CMD" in
  deploy) deploy "$@" ;;
  rollback) rollback "$@" ;;
  status) status ;;
  *) echo "usage: $0 deploy <build_dir> <version> | rollback [<backup_dir>] | status"; exit 2 ;;
esac
