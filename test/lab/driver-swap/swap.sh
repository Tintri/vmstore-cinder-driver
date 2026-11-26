#!/usr/bin/env bash
# Put a driver build or a vmstore_qcow2_volumes value on lab cinder-volume hosts for a test,
# and return them to the state they were in before. Runbook: docs/runbooks/driver-swap.md
#
# Usage (from a workstation, in this repository):
#   swap.sh deploy <lab.env> <git-ref> [<host>...]       driver from `git archive <ref>`
#   swap.sh rollback <lab.env> [<host>...]               back to the driver found before the first deploy
#   swap.sh status <lab.env> [<host>...]                 driver and option state of each host
#   swap.sh option <lab.env> <host> <section> <true|false> [--file <config file>]
#   swap.sh option-rollback <lab.env> <host>             undo every option change on that host
# Hosts are ssh targets (root); default SWAP_HOSTS from the env file. deploy, rollback and
# option restart cinder-volume, which restarts EVERY backend on the host: they list the
# backends and ask for "yes" unless --yes is given.
#
# Env file: PREFIX (host dir /root/<PREFIX>-driver-swap, keep it the same until rollback),
# SWAP_HOSTS, CINDER_VENV, CINDER_CONF_DIR, CINDER_LOG, OPTION_FILE (default for --file).
# No credentials are read or copied.
set -u
CMD=${1:-}; ENVF=${2:-}
usage() { sed -n '5,10p' "$0" | sed 's/^# //'; exit 2; }
[ -n "$CMD" ] && [ -f "$ENVF" ] || usage
shift 2
# shellcheck disable=SC1090
. "$ENVF"
PREFIX=${PREFIX:-lab}
CINDER_VENV=${CINDER_VENV:-/opt/pf9/pf9-cindervolume-base}
CINDER_CONF_DIR=${CINDER_CONF_DIR:-/opt/pf9/etc/pf9-cindervolume-base/conf.d}
CINDER_LOG=${CINDER_LOG:-/var/log/pf9/cindervolume-base.log}
YES=0; FILE=${OPTION_FILE:-}; ARGS=()
while [ $# -gt 0 ]; do case "$1" in --yes) YES=1; shift ;; --file) FILE=${2:-}; shift 2 ;; *) ARGS+=("$1"); shift ;; esac; done
HERE=$(cd "$(dirname "$0")" && pwd)
RDIR=/root/$PREFIX-driver-swap
FILES="api.py __init__.py nfs.py options.py utils.py"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15"
REMOTE_ENV="SWAP_DIR=$RDIR CINDER_VENV=$CINDER_VENV CINDER_CONF_DIR=$CINDER_CONF_DIR CINDER_LOG=$CINDER_LOG"
SUMMARY=()

hosts_or_default() { # hosts given, else SWAP_HOSTS; none is an error
  local h; if [ $# -gt 0 ]; then h="$*"; else h=${SWAP_HOSTS:-}; fi
  [ -n "$h" ] || { echo "no hosts: give them as arguments or set SWAP_HOSTS in $ENVF" >&2; return 1; }
  printf '%s\n' $h; }
backends_of() { $SSH "$1" "grep -hE '^[[:space:]]*enabled_backends[[:space:]]*=' $CINDER_CONF_DIR/*.conf | tail -1 | sed 's/^[^=]*=[[:space:]]*//'" 2>/dev/null | grep -v setlocale; }
push_scripts() { $SSH "$1" "mkdir -p $RDIR" && scp -q -o BatchMode=yes "$HERE/host-driver.sh" "$HERE/host-option.sh" "$1:$RDIR/"; }
confirm() { # host, what
  local b; b=$(backends_of "$1")
  echo "  $1: $2 restarts cinder-volume for every backend on it: ${b:-unknown}"
  [ "$YES" = 1 ] && return 0
  local a; read -r -p "  type yes to continue: " a; [ "$a" = yes ]; }
remote() { local h=$1; shift; $SSH "$h" "$REMOTE_ENV bash $RDIR/$*" 2>&1 | grep -v setlocale; return "${PIPESTATUS[0]}"; }
note() { SUMMARY+=("$1 | $2 | $3"); }
summary() {
  echo "=== summary"
  local s; for s in ${SUMMARY[@]+"${SUMMARY[@]}"}; do echo "  $s"; done
  [ "$CMD" = status ] || echo "  check: $0 status $ENVF"; }
trap summary EXIT

case "$CMD" in
  deploy)
    REF=${ARGS[0]:-}; [ -n "$REF" ] || usage
    REPO=$(git -C "$HERE" rev-parse --show-toplevel) || exit 2
    SHA=$(git -C "$REPO" rev-parse --verify "$REF^{commit}") || { echo "unknown git ref $REF"; exit 2; }
    BUILD=$(mktemp -d "${TMPDIR:-/tmp}/driver-build.XXXXXX")
    # shellcheck disable=SC2086
    git -C "$REPO" archive "$SHA" $FILES | tar -x -C "$BUILD" || { echo "git archive failed"; exit 2; }
    VERSION=$(sed -nE "s/^[[:space:]]+VERSION = '([^']+)'.*/\1/p" "$BUILD/nfs.py" | head -1)
    [ -n "$VERSION" ] || { echo "no VERSION in nfs.py at $REF"; exit 2; }
    echo "$REF $SHA $VERSION" > "$BUILD/BUILD_INFO"
    echo "### build $REF = ${SHA:0:12}, driver $VERSION"
    HOSTS=$(hosts_or_default ${ARGS[@]+"${ARGS[@]:1}"}) || exit 2
    for h in $HOSTS; do
      confirm "$h" "deploy $VERSION" || { note "$h" deploy "not confirmed, stopped"; exit 1; }
      push_scripts "$h" && $SSH "$h" "mkdir -p $RDIR/build-${SHA:0:12}" &&
        scp -q -o BatchMode=yes "$BUILD"/*.py "$BUILD/BUILD_INFO" "$h:$RDIR/build-${SHA:0:12}/" || { note "$h" deploy "copy failed"; exit 1; }
      if remote "$h" "host-driver.sh deploy $RDIR/build-${SHA:0:12} $VERSION"; then note "$h" deploy "$VERSION running (${SHA:0:12})"
      else note "$h" deploy "FAILED, previous driver restored (see output)"; exit 1; fi
    done ;;
  rollback)
    HOSTS=$(hosts_or_default ${ARGS[@]+"${ARGS[@]}"}) || exit 2
    for h in $HOSTS; do
      confirm "$h" "rollback" || { note "$h" rollback "not confirmed, stopped"; exit 1; }
      push_scripts "$h" || { note "$h" rollback "cannot reach host"; exit 1; }
      if remote "$h" "host-driver.sh rollback"; then note "$h" rollback "pre-test driver running"
      else note "$h" rollback "FAILED (see output)"; exit 1; fi
    done ;;
  status)
    HOSTS=$(hosts_or_default ${ARGS[@]+"${ARGS[@]}"}) || exit 2
    for h in $HOSTS; do
      echo "### $h"
      push_scripts "$h" || { note "$h" status "cannot reach host"; continue; }
      o=$(remote "$h" "host-driver.sh status"); echo "$o"
      p=$(remote "$h" "host-option.sh status"); echo "$p"
      note "$h" driver "$(grep '^STATE:' <<<"$o" | cut -c8-)"
      note "$h" option "$(grep '^STATE:' <<<"$p" | cut -c8-)"
    done ;;
  option)
    [ ${#ARGS[@]} -eq 3 ] || usage
    h=${ARGS[0]}; sec=${ARGS[1]}; val=${ARGS[2]}
    [ -n "$FILE" ] || { echo "give --file <config file> (or OPTION_FILE in the env file): see docs/runbooks/driver-swap.md"; exit 2; }
    confirm "$h" "set [$sec] vmstore_qcow2_volumes=$val in $FILE" || { note "$h" option "not confirmed, stopped"; exit 1; }
    push_scripts "$h" || { note "$h" option "cannot reach host"; exit 1; }
    if remote "$h" "host-option.sh set $sec vmstore_qcow2_volumes $val --file $FILE"; then note "$h" option "[$sec] vmstore_qcow2_volumes=$val"
    else note "$h" option "FAILED, file restored (see output)"; exit 1; fi ;;
  option-rollback)
    h=${ARGS[0]:-}; [ -n "$h" ] || usage
    confirm "$h" "option rollback" || { note "$h" option-rollback "not confirmed, stopped"; exit 1; }
    push_scripts "$h" || { note "$h" option-rollback "cannot reach host"; exit 1; }
    if remote "$h" "host-option.sh rollback"; then note "$h" option-rollback "config as found"
    else note "$h" option-rollback "FAILED (see output)"; exit 1; fi ;;
  *) usage ;;
esac
