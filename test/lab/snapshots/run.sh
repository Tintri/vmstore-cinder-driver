#!/usr/bin/env bash
# Snapshot delete lab test (VME-34): provider_id recorded at create, delete after the
# VMstore snapshot was removed out of band, delete of a snapshot with a clone, delete of
# a snapshot without provider_id (as created by 3.0.11).
# Runbook: docs/runbooks/snapshot-delete-lab-test.md
#
# Usage: run.sh <lab.env>
#
# Creates only volumes and snapshots named <PREFIX>-* (IDs recorded at creation) and
# deletes them on exit, including on failure or Ctrl-C. Other objects are never modified.
# Exit: 0 all checks passed (SKIP allowed), 1 a check failed, 2 precheck or setup problem.
set -u
[ -f "${1:-}" ] || { echo "usage: $0 <lab.env>"; exit 2; }
# shellcheck disable=SC1090
. "$1"
for v in CLI_SSH CLI_RC REGION VOLUME_TYPE PREFIX; do
  [ -n "${!v:-}" ] || { echo "missing $v in $1"; exit 2; }
done
CINDER_SSH=${CINDER_SSH:-}
if [ -n "$CINDER_SSH" ]; then
  for v in CINDER_VENV CINDER_CONF_DIR; do
    [ -n "${!v:-}" ] || { echo "missing $v in $1 (needed with CINDER_SSH)"; exit 2; }
  done
fi
LOG_SSH=${LOG_SSH:-$CINDER_SSH}                                 # hosts whose cinder-volume log is searched
CINDER_LOG=${CINDER_LOG:-/var/log/pf9/cindervolume-base.log}   # cinder-volume log on those hosts (PCD path)

HERE=$(cd "$(dirname "$0")" && pwd)
RDIR=/root/$PREFIX-lab
START_EPOCH=$(date +%s); START=$(date -u '+%Y-%m-%d %H:%M:%S')
# Host logs use the host's local clock: filter them from the same instant in the host's time zone.
LOG_SINCE="awk -v s=\"\$(date -d @$START_EPOCH '+%Y-%m-%d %H:%M:%S')\" 'substr(\$0,1,19) >= s'"
OUT=$(mktemp -d "${TMPDIR:-/tmp}/$PREFIX-lab.XXXXXX")
exec > >(tee "$OUT/run.log") 2>&1
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15"
UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'

ts() { date -u +%T; }
cli() { $SSH "$CLI_SSH" "source $CLI_RC >/dev/null 2>&1; export OS_REGION_NAME=$REGION; $*" 2>&1 | grep -v setlocale; return 0; }
on() { local t=$1; shift; $SSH "$t" "$@" 2>&1 | grep -v setlocale; return 0; }
show() { sed 's/^/  cli: /'; }

# Status of a volume or snapshot from `show` output: the status, "deleted" when the API
# says it does not exist, "unknown" otherwise (CLI or network trouble never counts as deleted).
st_of() { local l; l=$(grep -v '^ *$' <<<"$1" | tail -1)
  if [[ "$l" =~ ^[a-z_-]+$ ]]; then echo "$l"
  elif grep -qiE 'no (volume|snapshot) with a name or id|could not be found|HTTP 404' <<<"$1"; then echo deleted
  else echo unknown; fi; }
vstat() { st_of "$(cli "openstack volume show $1 -f value -c status")"; }
sstat() { st_of "$(cli "openstack volume snapshot show $1 -f value -c status")"; }
vhost() { cli "openstack volume show $1 -f value -c os-vol-host-attr:host 2>/dev/null" | tail -1; }
wait_vol() { local s i
  for ((i = 0; i < 1200; i += 10)); do s=$(vstat "$1")
    [[ "$s" =~ ^(creating|downloading|deleting|maintenance|reserved|unknown)$ ]] || break; sleep 10
  done; echo "$s"; }
wait_snap() { local s i
  for ((i = 0; i < 1200; i += 10)); do s=$(sstat "$1")
    [[ "$s" =~ ^(creating|deleting|unmanaging|backing-up|restoring|unknown)$ ]] || break; sleep 10
  done; echo "$s"; }
new_vol() { # name, create arguments -> volume ID
  local id; id=$(cli "openstack volume create $2 $1 -f value -c id" | tail -1)
  [[ "$id" =~ $UUID_RE ]] || id=$(cli "openstack volume list --name $1 -f value -c ID" | head -1)
  echo "$id"; }
new_snap() { # name, volume ID -> snapshot ID
  local id; id=$(cli "openstack volume snapshot create --volume $2 $1 -f value -c id" | tail -1)
  [[ "$id" =~ $UUID_RE ]] || id=$(cli "openstack volume snapshot list --name $1 -f value -c ID" | head -1)
  echo "$id"; }
snap_ok() { # snapshot ID -> 0 when available
  [[ "$1" =~ $UUID_RE ]] || { echo "  snapshot create returned no ID"; return 1; }
  local s; s=$(wait_snap "$1"); echo "  snapshot $1: $s"; [ "$s" = available ]; }

# Helper on CINDER_SSH: Cinder records, and VMstore through the driver's own client.
BACKEND=
helper() { [ -n "$CINDER_SSH" ] || return 0
  on "$CINDER_SSH" "cd $RDIR && (set +u; set -a; . $CINDER_VENV/cinder.env; set +a; $CINDER_VENV/bin/python snapshot_lab.py --conf-dir $CINDER_CONF_DIR --prefix $PREFIX ${BACKEND:+--backend $BACKEND} $*)"; }
arr() { # snapshot ID -> UUIDs of VMstore snapshots described snapshot-<ID>, "error", or "unchecked"
  [ -n "$CINDER_SSH" ] || { echo unchecked; return 0; }
  local o; o=$(helper array-list "$1")
  if ! grep -qx END <<<"$o"; then
    echo "  array-list $1 failed: $(grep -m1 '^ERROR' <<<"$o" || echo "${o:0:300}")" >&2; echo error; return 0
  fi
  awk '$1 == "ARRAY" {print $2}' <<<"$o" | paste -sd' ' -; }
pid() { helper snapshot "$1" | sed -nE 's/^SNAP .* provider_id=([^ ]*) .*/\1/p'; }
logs_for() { # ID -> cinder-volume log lines (no DEBUG) for it since START, from every LOG_SSH host
  [ -n "$LOG_SSH" ] || return 0
  local h; echo "  cinder-volume log for $1 since $START:"
  for h in $LOG_SSH; do
    on "$h" "$LOG_SINCE $CINDER_LOG 2>/dev/null | grep -a '$1' | grep -av ' DEBUG ' | sed -E 's/req-[0-9a-f-]+//' | cut -c1-260 | tail -6" | sed "s|^|    $h: |"
  done; }
running_version() { # driver_version the scheduler reports for pools of BACKEND (needs python-cinderclient)
  cli "cinder get-pools --detail" | awk -F'|' -v b="@$BACKEND#" '
    {k = $2; v = $3; gsub(/ /, "", k); gsub(/^ +| +$/, "", v)}
    k == "driver_version" {d = v} k == "name" {n = v}
    /^\+/ && n != "" {if (index(n, b)) print d; n = ""; d = ""}' | sort -u | paste -sd' ' -; }
ours() { # "<kind> <id> <name>" of every <PREFIX>-* volume and snapshot
  cli "openstack volume snapshot list --all-projects -f value -c ID -c Name" | awk -v p="^$PREFIX-" '$2 ~ p {print "snapshot", $1, $2}'
  cli "openstack volume list --all-projects -f value -c ID -c Name" | awk -v p="^$PREFIX-" '$2 ~ p {print "volume", $1, $2}'; }
others() { # every server, volume and snapshot not created by this run
  cli "openstack server list --all-projects -f value -c ID -c Name -c Status" | awk -v p="^$PREFIX-" '$2 !~ p {print "server", $1, $NF}' | sort
  cli "openstack volume list --all-projects -f value -c ID -c Name -c Status" | awk -v p="^$PREFIX-" '$2 !~ p {print "volume", $1, $NF}' | sort
  cli "openstack volume snapshot list --all-projects -f value -c ID -c Name -c Status" | awk -v p="^$PREFIX-" '$2 !~ p {print "snapshot", $1, $NF}' | sort; }

RESULTS=(); RC=0
result() { RESULTS+=("$1 | $2 | $3"); echo "  RESULT $1: $2 ($3)"; [ "$2" = FAIL ] && RC=1; return 0; }

precheck() {
  local bad=0 n backend svc h
  echo "### precheck $(ts)"
  [ "$(cli "openstack token issue -f value -c id >/dev/null && echo ok")" = ok ] || { echo "  CLI or credentials not working on $CLI_SSH"; exit 2; }
  [[ "$(cli "openstack volume type show $VOLUME_TYPE -f value -c id" | tail -1)" =~ $UUID_RE ]] || { echo "  volume type $VOLUME_TYPE not found"; bad=1; }
  backend=$(cli "openstack volume type show $VOLUME_TYPE -f json -c properties" | python3 -c 'import json, sys; print((json.load(sys.stdin).get("properties") or {}).get("volume_backend_name", ""))' 2>/dev/null)
  backend=${backend:-$VOLUME_TYPE}
  svc=$(cli "openstack volume service list --service cinder-volume -f value -c Host -c Status -c State" | grep "@$backend enabled up$")
  if [ -n "$svc" ]; then echo "  cinder-volume enabled and up for $backend: $(awk '{print $1}' <<<"$svc" | paste -sd' ' -)"
  else echo "  warning: no cinder-volume service *@$backend enabled and up; volume creation may fail"; fi
  n=$(ours | wc -l | tr -d ' ')
  [ "$n" = 0 ] || { echo "  $n volumes or snapshots named $PREFIX-* already exist: clean them up or change PREFIX"; bad=1; }
  if [ -n "$CINDER_SSH" ]; then
    [ -n "$(on "$CINDER_SSH" "test -x $CINDER_VENV/bin/python && test -f $CINDER_VENV/cinder.env && test -d $CINDER_CONF_DIR && echo ok" | grep -x ok)" ] ||
      { echo "  $CINDER_SSH lacks $CINDER_VENV/bin/python, $CINDER_VENV/cinder.env or $CINDER_CONF_DIR"; bad=1; }
  else echo "  CINDER_SSH not set: S1, S2 and S4 will SKIP, S3 checks Cinder status only"; fi
  for h in $LOG_SSH; do
    [ -n "$(on "$h" "test -r $CINDER_LOG && echo ok" | grep -x ok)" ] || echo "  warning: no readable $CINDER_LOG on $h (set CINDER_LOG); log lines will be missing"
  done
  [ $bad = 0 ] || exit 2
  echo "  ok"
}

SNAPS=()
cleanup() {
  echo "=== cleanup $(ts)"
  local ids id s a
  # Snapshots, then volumes created from a snapshot, then snapshots again (one that a
  # clone kept busy can go now), then the source volumes.
  del_snaps() { local ids id s
    ids=$(cli "openstack volume snapshot list --all-projects -f value -c ID -c Name" | awk -v p="^$PREFIX-" '$2 ~ p {print $1}')
    for id in $ids; do s=$(wait_snap "$id")
      [[ "$s" == error* ]] && { echo "  snapshot $id was $s, resetting to available (test snapshot)"; cli "openstack volume snapshot set --state available $id"; }
      cli "openstack volume snapshot delete $id" | show
    done
    for id in $ids; do echo "  snapshot $id: $(wait_snap "$id")"; done; }
  del_snaps
  for pass in clone source; do
    [ "$pass" = source ] && del_snaps
    ids=$(cli "openstack volume list --all-projects -f value -c ID -c Name" |
      awk -v p="^$PREFIX-" -v c="$pass" '$2 ~ p && (($2 ~ /-clone$/) == (c == "clone")) {print $1}')
    for id in $ids; do s=$(wait_vol "$id")
      [[ "$s" == error* || "$s" =~ ^(maintenance|reserved)$ ]] && { echo "  volume $id was $s, resetting to available (test volume)"; cli "openstack volume set --state available $id"; }
      cli "openstack volume delete $id" | show
    done
    for id in $ids; do echo "  volume $id: $(wait_vol "$id")"; done
  done
  # VMstore snapshots of this run's snapshots that outlived them (reported in the checks).
  if [ -n "$CINDER_SSH" ] && [ -n "$BACKEND" ]; then
    for id in ${SNAPS[@]+"${SNAPS[@]}"}; do a=$(arr "$id")
      case "$a" in
        ""|unchecked) ;;
        error) echo "  could not check VMstore for test snapshot $id" ;;
        *) echo "  VMstore snapshot(s) $a of test snapshot $id still on the array: deleting"
           helper array-delete "$id" | grep -E '^(DELETED|ERROR|REFUSED)' | sed 's/^/    /' ;;
      esac
    done
  fi
  echo "  left: $(ours | wc -l | tr -d ' ') volumes or snapshots named $PREFIX-*"
  [ -n "$CINDER_SSH" ] && on "$CINDER_SSH" "rm -rf $RDIR && echo '  $RDIR removed on $CINDER_SSH'"
  others > "$OUT/others-after.txt"
  if diff -q "$OUT/others-before.txt" "$OUT/others-after.txt" >/dev/null; then echo "  other servers, volumes and snapshots: unchanged"
  else echo "  other servers, volumes or snapshots changed during the run (not by this script; review):"; diff "$OUT/others-before.txt" "$OUT/others-after.txt" | sed 's/^/    /' | head -20; fi
  echo "=== summary"
  local r; for r in ${RESULTS[@]+"${RESULTS[@]}"}; do echo "  $r"; done
  echo "  log: $OUT/run.log"
}

precheck
others > "$OUT/others-before.txt"
if [ -n "$CINDER_SSH" ]; then
  on "$CINDER_SSH" "mkdir -p $RDIR" && scp -q -o BatchMode=yes "$HERE/snapshot_lab.py" "$CINDER_SSH:$RDIR/snapshot_lab.py" || { echo "helper copy failed"; exit 2; }
fi
trap 'rc=$?; [ $rc = 0 ] && rc=$RC; cleanup; exit $rc' EXIT

echo "### $(ts) source volume $PREFIX-src on $VOLUME_TYPE"
SRC=$(new_vol "$PREFIX-src" "--size 1 --type $VOLUME_TYPE")
[[ "$SRC" =~ $UUID_RE ]] || { echo "  volume create failed"; exit 2; }
s=$(wait_vol "$SRC"); echo "  $SRC: $s"
[ "$s" = available ] || { echo "  source volume not available"; exit 2; }
host=$(vhost "$SRC"); BACKEND=${host#*@}; BACKEND=${BACKEND%%#*}
echo "  host $host (backend section $BACKEND)"
v=$(running_version); echo "  driver_version reported by cinder-volume: ${v:-unknown (cinder get-pools not available)}"
if [ -n "$CINDER_SSH" ]; then
  o=$(helper driver-version); grep '^VERSION' <<<"$o" | sed "s|^|  installed on $CINDER_SSH: |"
  grep -qx END <<<"$o" || { echo "  helper failed on $CINDER_SSH: $(grep -m1 '^ERROR' <<<"$o" || echo "${o:0:300}")"; exit 2; }
fi

echo "### S1 $(ts) new snapshot records provider_id (the VMstore snapshot UUID)"
S1=$(new_snap "$PREFIX-s1" "$SRC"); [ -n "$S1" ] && SNAPS+=("$S1")
if ! snap_ok "$S1"; then result S1 FAIL "snapshot not available after create"
elif [ -z "$CINDER_SSH" ]; then result S1 SKIP "needs CINDER_SSH"
else
  p=$(pid "$S1"); a=$(arr "$S1"); echo "  Cinder provider_id=${p:-?}, VMstore snapshot(s) [$a]"
  if [ "$a" = error ]; then result S1 FAIL "could not list VMstore snapshots"
  elif [ -z "$p" ] || [ "$p" = None ] || [ "$a" != "$p" ]; then result S1 FAIL "provider_id=${p:-?}, VMstore snapshot(s) [$a]"
  else
    cli "openstack volume snapshot delete $S1" | show; s=$(wait_snap "$S1"); a=$(arr "$S1")
    echo "  after delete: Cinder $s, VMstore [$a]"
    if [ "$s" = deleted ] && [ -z "$a" ]; then result S1 PASS "provider_id $p is the VMstore snapshot; delete removed both"
    else result S1 FAIL "provider_id correct, but delete left Cinder $s, VMstore [$a]"; fi
  fi
fi

echo "### S2 $(ts) VMstore snapshot removed out of band, then Cinder delete"
S2=$(new_snap "$PREFIX-s2" "$SRC"); [ -n "$S2" ] && SNAPS+=("$S2")
if ! snap_ok "$S2"; then result S2 FAIL "snapshot not available after create"
elif [ -z "$CINDER_SSH" ]; then result S2 SKIP "needs CINDER_SSH"
else
  o=$(helper array-delete "$S2"); grep -E '^(DELETED|ERROR|REFUSED)' <<<"$o" | sed 's/^/  out of band: /'
  a=$(arr "$S2")
  if ! grep -qx END <<<"$o" || ! grep -q '^DELETED' <<<"$o" || [ -n "$a" ]; then
    result S2 FAIL "not tested: could not remove the VMstore snapshot out of band (left [$a])"
  else
    cli "openstack volume snapshot delete $S2" | show; s=$(wait_snap "$S2"); a=$(arr "$S2")
    echo "  after delete: Cinder $s, VMstore [$a]"; logs_for "$S2"
    if [ "$s" = deleted ] && [ -z "$a" ]; then result S2 PASS "delete of a snapshot already gone from VMstore ended deleted"
    else result S2 FAIL "Cinder $s, VMstore [$a]"; fi
  fi
fi

echo "### S4 $(ts) snapshot without provider_id (as created by 3.0.11), then delete"
S4=$(new_snap "$PREFIX-s4" "$SRC"); [ -n "$S4" ] && SNAPS+=("$S4")
if ! snap_ok "$S4"; then result S4 FAIL "snapshot not available after create"
elif [ -z "$CINDER_SSH" ]; then result S4 SKIP "needs CINDER_SSH"
else
  helper clear-provider-id "$S4" | grep -E '^(ERROR|REFUSED)' | sed 's/^/  /'
  p=$(pid "$S4"); a=$(arr "$S4"); echo "  Cinder provider_id=${p:-?}, VMstore snapshot(s) [$a]"
  if [ "$p" != None ]; then result S4 FAIL "not tested: provider_id is still ${p:-?}"
  elif [ -z "$a" ] || [ "$a" = error ]; then result S4 FAIL "not tested: no VMstore snapshot to delete [$a]"
  else
    cli "openstack volume snapshot delete $S4" | show; s=$(wait_snap "$S4"); a=$(arr "$S4")
    echo "  after delete: Cinder $s, VMstore [$a]"; logs_for "$S4"
    if [ "$s" = deleted ] && [ -z "$a" ]; then result S4 PASS "found by name, deleted from VMstore and Cinder"
    else result S4 FAIL "Cinder $s, VMstore [$a]"; fi
  fi
fi

echo "### S3 $(ts) snapshot with a clone, then delete"
# 3.0.12: VMstore refuses (ERR-API-3107) while the clone exists and the driver raises
# SnapshotIsBusy, so Cinder keeps the snapshot available; it deletes once the clone is
# gone. Before 3.0.12 the driver reported success and the VMstore snapshot stayed.
P3=$(new_vol "$PREFIX-s3-src" "--size 1 --type $VOLUME_TYPE"); s=
[[ "$P3" =~ $UUID_RE ]] && s=$(wait_vol "$P3"); echo "  parent $P3: ${s:-create failed}"
S3=
if [ "$s" = available ]; then S3=$(new_snap "$PREFIX-s3" "$P3"); [ -n "$S3" ] && SNAPS+=("$S3"); fi
if [ -z "$S3" ] || ! snap_ok "$S3"; then result S3 FAIL "not tested: parent or snapshot not available"
else
  C3=$(new_vol "$PREFIX-s3-clone" "--snapshot $S3 --size 1"); s=
  [[ "$C3" =~ $UUID_RE ]] && s=$(wait_vol "$C3"); echo "  clone $C3: ${s:-create failed}"
  if [ "$s" != available ]; then result S3 FAIL "not tested: volume from snapshot not available"
  else
    cli "openstack volume snapshot delete $S3" | show; s=$(wait_snap "$S3"); a=$(arr "$S3")
    echo "  delete while the clone exists: Cinder $s, VMstore [$a]"; logs_for "$S3"
    m=$(cli "openstack --os-volume-api-version 3.3 volume message list -f value -c 'Resource UUID' -c 'User Message'" | awk -v r="$S3" '$1 == r {$1 = ""; print}' | head -1)
    [ -n "$m" ] && echo "  user message:$m"
    case "$s" in
      available) k=refused ;;
      deleted) k=reported-deleted ;;
      *) k=other ;;
    esac
    cli "openstack volume delete $C3" | show; c=$(wait_vol "$C3"); echo "  clone: $c"
    s2=$s
    if [ "$k" = refused ]; then
      cli "openstack volume snapshot delete $S3" | show; s2=$(wait_snap "$S3")
      echo "  delete after the clone is gone: Cinder $s2, VMstore [$(arr "$S3")]"
    fi
    cli "openstack volume delete $P3" | show; p=$(wait_vol "$P3"); echo "  parent: $p"
    a2=$(arr "$S3"); echo "  after clone and parent deleted: VMstore [$a2]"
    if [ "$k" = other ]; then result S3 FAIL "delete while the clone exists ended $s (expected available: refused as busy)"
    elif [ "$c" != deleted ] || [ "$p" != deleted ]; then result S3 FAIL "not tested: clone $c, parent $p"
    elif [ "$k" = refused ] && [ "$s2" != deleted ]; then result S3 FAIL "refused while the clone existed, but the delete after it ended $s2"
    elif [ "$a2" = unchecked ]; then result S3 PASS "Cinder: $k while the clone existed, then deleted; VMstore not checked (no CINDER_SSH)"
    elif [ -n "$a2" ]; then result S3 FAIL "leak: VMstore snapshot $a2 remains after the clone and parent were deleted (Cinder: $k while the clone existed)"
    elif [ "$k" = refused ]; then result S3 PASS "refused as busy while the clone existed, deleted from Cinder and VMstore after it"
    else result S3 PASS "reported deleted while the clone existed; VMstore snapshot gone after the clone and parent were deleted"; fi
  fi
fi
