#!/usr/bin/env bash
# Extend of attached volumes lab test (VME-10, VMTS-243, driver 3.0.11 and later):
# E1 in-use extend of a raw volume, E2 in-use extend of a qcow2 volume, E3 extend of a qcow2
# volume whose instance is shut off, E4 attach-time grow of a file smaller than its volume.
# Runbook: docs/runbooks/volume-extend-lab-test.md
#
# Usage: run.sh <lab.env>
#
# Creates only objects named <PREFIX>-* (one VM, a private flavor, five 1 GB volumes; IDs
# recorded at creation) and deletes them on exit, including on failure or Ctrl-C. Writes to
# volume files directly only for its own volumes (pattern fill, and the E4 shrink).
# Exit: 0 all checks passed (SKIP allowed), 1 a check failed, 2 precheck or setup problem.
set -u
[ -f "${1:-}" ] || { echo "usage: $0 <lab.env>"; exit 2; }
# shellcheck disable=SC1090
. "$1"
for v in CLI_SSH CLI_RC REGION AZ SRC_COMPUTE_ID VOLUME_TYPE IMAGE_ID NETWORK_ID PROJECT \
         FLAVOR_RAM_MB PREFIX; do
  [ -n "${!v:-}" ] || { echo "missing $v in $1"; exit 2; }
done
SRC_COMPUTE_SSH=${SRC_COMPUTE_SSH:-}
CINDER_SSH=${CINDER_SSH:-}
if [ -n "$CINDER_SSH" ]; then
  for v in CINDER_VENV CINDER_CONF_DIR; do
    [ -n "${!v:-}" ] || { echo "missing $v in $1 (needed with CINDER_SSH)"; exit 2; }
  done
fi
LOG_SSH=${LOG_SSH:-$CINDER_SSH}                                 # hosts whose cinder-volume log is searched
CINDER_LOG=${CINDER_LOG:-/var/log/pf9/cindervolume-base.log}   # cinder-volume log on those hosts (PCD path)
VOL_API=3.42                                                    # Block Storage API version for in-use extend

HERE=$(cd "$(dirname "$0")" && pwd)
RDIR=/root/$PREFIX-lab
START=$(date -u '+%Y-%m-%d %H:%M:%S')
OUT=$(mktemp -d "${TMPDIR:-/tmp}/$PREFIX-lab.XXXXXX")
exec > >(tee "$OUT/run.log") 2>&1
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15"
UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
GIB=1073741824
PAT=256M; PAT_BYTES=268435456   # pattern 0x5a written at offset 0 of E1 and E2 before attach

ts() { date -u +%T; }
cli() { $SSH "$CLI_SSH" "source $CLI_RC >/dev/null 2>&1; export OS_REGION_NAME=$REGION; $*" 2>&1 | grep -v setlocale; return 0; }
on() { local t=$1; shift; $SSH "$t" "$@" 2>&1 | grep -v setlocale; return 0; }
show() { sed 's/^/  cli: /'; }

# Status from `show` output: the status, "deleted" when the API says it does not exist,
# "unknown" otherwise (CLI or network trouble never counts as a final state).
st_of() { local l; l=$(grep -v '^ *$' <<<"$1" | tail -1)
  if [[ "$l" =~ ^[a-z_-]+$ ]]; then echo "$l"
  elif grep -qiE 'no volume with a name or id|could not be found|HTTP 404' <<<"$1"; then echo deleted
  else echo unknown; fi; }
vstat() { st_of "$(cli "openstack volume show $1 -f value -c status")"; }
vsize() { cli "openstack volume show $1 -f value -c size 2>/dev/null" | tail -1; }
vhost() { cli "openstack volume show $1 -f value -c os-vol-host-attr:host 2>/dev/null" | tail -1; }
sfield() { cli "openstack server show $1 -f value -c $2 2>/dev/null" | tail -1; }
wait_vol() { local s i
  for ((i = 0; i < 1200; i += 10)); do s=$(vstat "$1")
    [[ "$s" =~ ^(creating|downloading|extending|deleting|maintenance|attaching|detaching|reserved|unknown)$ ]] || break; sleep 10
  done; echo "$s"; }
wait_server() { # VM, wanted status -> final status (stops early on ERROR)
  local s t i
  for ((i = 0; i < 600; i += 10)); do s=$(sfield "$1" status); t=$(sfield "$1" OS-EXT-STS:task_state)
    { [ "$s" = "$2" ] && [ "$t" = None ]; } && break; [ "$s" = ERROR ] && break; sleep 10
  done; echo "$s"; }
new_vol() { # name, create arguments -> volume ID
  local id; id=$(cli "openstack volume create $2 $1 -f value -c id" | tail -1)
  [[ "$id" =~ $UUID_RE ]] || id=$(cli "openstack volume list --name $1 -f value -c ID" | head -1)
  echo "$id"; }
extend() { # volume, new size in GB -> final status (in-use extend needs API 3.42)
  cli "openstack --os-volume-api-version $VOL_API volume set --size $2 $1" | show >&2
  wait_vol "$1"; }
# CLI output goes to stderr (still logged) so callers capture only the final status.
attach() { cli "openstack server add volume $VM $1" | show >&2; wait_vol "$1"; }
detach() { cli "openstack server remove volume $VM $1" | show >&2; wait_vol "$1"; }

# Cinder record and file checks on CINDER_SSH.
volrec() { [ -n "$CINDER_SSH" ] || return 0
  on "$CINDER_SSH" "cd $RDIR && (set +u; set -a; . $CINDER_VENV/cinder.env; set +a; $CINDER_VENV/bin/python volume_record.py --conf-dir $CINDER_CONF_DIR $*)" | grep '^VOL'; }
field() { sed -nE "s/.* $1=([^ ]+).*/\1/p" <<<"$2"; }
nameid() { local n; n=$(field name_id "$(volrec "$1")"); echo "${n:-$1}"; }   # file is volume-<name_id>
fpath() { local l; l=$(volrec "$1"); [ -n "$l" ] || return 0
  on "$CINDER_SSH" "m=\$(findmnt -n -o TARGET -S '$(field share "$l")' | grep '$CINDER_VENV' | head -1); echo \$m/volume-$(field name_id "$l")" | tail -1; }
finfo() { # volume -> "<raw|qcow2|missing|unchecked> <file bytes> <virtual bytes>"
  [ -n "$CINDER_SSH" ] || { echo "unchecked 0 0"; return 0; }
  local p; p=$(fpath "$1"); [ -n "$p" ] || { echo "missing 0 0"; return 0; }
  on "$CINDER_SSH" "p='$p'; [ -e \$p ] || { echo missing 0 0; exit; }; x=\$(head -c4 \$p | od -An -tx1 | tr -d ' '); s=\$(stat -c %s \$p)
    if [ \$x = 514649fb ]; then v=\$(qemu-img info -U -f qcow2 --output=json \$p | sed -nE 's/.*\"virtual-size\": *([0-9]+).*/\1/p' | head -1); echo qcow2 \$s \${v:-0}; else echo raw \$s \$s; fi" | tail -1; }
pwrite() { # path, format -> ok | error (test volume only, before attach)
  on "$CINDER_SSH" "qemu-io -f $2 -c 'write -P 0x5a 0 $PAT' '$1' 2>&1 | grep -q '^wrote $PAT_BYTES/$PAT_BYTES' && echo ok || echo error" | tail -1; }
pcheck() { # path, format -> ok | bad | error (read-only, shares the image lock)
  on "$CINDER_SSH" "o=\$(qemu-io -r -U -f $2 -c 'read -P 0x5a 0 $PAT' '$1' 2>&1); if echo \"\$o\" | grep -q 'Pattern verification failed'; then echo bad; elif echo \"\$o\" | grep -q '^read $PAT_BYTES/$PAT_BYTES'; then echo ok; else echo error; fi" | tail -1; }
qcheck() { # qcow2 path -> qemu-img check exit code (0 clean)
  on "$CINDER_SSH" "qemu-img check -U -f qcow2 '$1' >/dev/null 2>&1; echo \$?" | tail -1; }
wait_virtual() { # volume, expected bytes -> last finfo (Nova applies a qcow2 resize asynchronously)
  local f i; for ((i = 0; i < 180; i += 10)); do f=$(finfo "$1"); [ "$(cut -d' ' -f3 <<<"$f")" = "$2" ] && break; sleep 10; done; echo "$f"; }

# Size the hypervisor gives the guest for a volume (virsh domblkinfo Capacity), on SRC_COMPUTE_SSH.
gsize() { # VM, name_id -> bytes, or empty
  [ -n "$SRC_COMPUTE_SSH" ] || return 0
  on "$SRC_COMPUTE_SSH" "t=\$(virsh domblklist $1 --details 2>/dev/null | awk '/volume-$2/ {print \$3; exit}'); [ -n \"\$t\" ] && virsh domblkinfo $1 \$t 2>/dev/null | awk '/^Capacity:/ {print \$2}'" | tail -1; }
wait_gsize() { # VM, name_id, expected bytes -> last capacity seen
  local g i; for ((i = 0; i < 180; i += 10)); do g=$(gsize "$1" "$2"); [ "$g" = "$3" ] && break; sleep 10; done; echo "$g"; }

LOG_OK=0
logs_for() { # volume ID, pattern -> matching cinder-volume log lines since START (no DEBUG)
  [ -n "$LOG_SSH" ] || return 0
  local h; for h in $LOG_SSH; do
    on "$h" "awk -v s='$START' 'substr(\$0,1,19) >= s' $CINDER_LOG 2>/dev/null | grep -a '$1' | grep -av ' DEBUG ' | grep -aE '$2' | sed -E 's/req-[0-9a-f-]+//' | cut -c1-260 | tail -4" | sed "s|^|$h: |"
  done; }
others() { # every server and volume not created by this run
  cli "openstack server list --all-projects -f value -c ID -c Name -c Status" | awk -v p="^$PREFIX-" '$2 !~ p {print "server", $1, $NF}' | sort
  cli "openstack volume list --all-projects -f value -c ID -c Name -c Status" | awk -v p="^$PREFIX-" '$2 !~ p {print "volume", $1, $NF}' | sort; }
ours() { cli "openstack server list --all-projects -f value -c Name; openstack volume list --all-projects -f value -c Name; openstack flavor list --all -f value -c Name" | grep -c "^$PREFIX-"; }
running_version() { # driver_version the scheduler reports for pools of BACKEND (needs python-cinderclient)
  cli "cinder get-pools --detail" | awk -F'|' -v b="@$BACKEND#" '
    {k = $2; v = $3; gsub(/ /, "", k); gsub(/^ +| +$/, "", v)}
    k == "driver_version" {d = v} k == "name" {n = v}
    /^\+/ && n != "" {if (index(n, b)) print d; n = ""; d = ""}' | sort -u | paste -sd' ' -; }

RESULTS=(); RC=0
result() { RESULTS+=("$1 | $2 | $3"); echo "  RESULT $1: $2 ($3)"; [ "$2" = FAIL ] && RC=1; return 0; }
# Per check: FAILS collects failed sub-checks, NOTES the sub-checks this lab cannot run.
FAILS=; NOTES=
reset_check() { FAILS=; NOTES=; }
fail() { FAILS="${FAILS:+$FAILS; }$1"; }
note() { NOTES="${NOTES:+$NOTES, }$1"; }
judge() { # check, what passed
  if [ -n "$FAILS" ]; then result "$1" FAIL "$FAILS"
  else result "$1" PASS "$2${NOTES:+; not checked: $NOTES}"; fi; }

QCOW2_OPT=?; NOVA_CHECK=?
precheck() {
  local bad=0 backend svc h o
  echo "### precheck $(ts)"
  [ "$(cli "openstack token issue -f value -c id >/dev/null && echo ok")" = ok ] || { echo "  CLI or credentials not working on $CLI_SSH"; exit 2; }
  [[ "$(cli "openstack --os-volume-api-version $VOL_API volume type show $VOLUME_TYPE -f value -c id" | tail -1)" =~ $UUID_RE ]] ||
    { echo "  volume type $VOLUME_TYPE not found, or the CLI or Cinder does not accept API $VOL_API"; bad=1; }
  backend=$(cli "openstack volume type show $VOLUME_TYPE -f json -c properties" | python3 -c 'import json, sys; print((json.load(sys.stdin).get("properties") or {}).get("volume_backend_name", ""))' 2>/dev/null)
  backend=${backend:-$VOLUME_TYPE}
  svc=$(cli "openstack volume service list --service cinder-volume -f value -c Host -c Status -c State" | grep "@$backend enabled up$")
  if [ -n "$svc" ]; then echo "  cinder-volume enabled and up for $backend: $(awk '{print $1}' <<<"$svc" | paste -sd' ' -)"
  else echo "  no cinder-volume service *@$backend enabled and up"; bad=1; fi
  grep -q "^$SRC_COMPUTE_ID enabled up$" <<<"$(cli "openstack compute service list --service nova-compute -f value -c Host -c Status -c State")" ||
    { echo "  nova-compute $SRC_COMPUTE_ID is not enabled and up"; bad=1; }
  [ "$(cli "openstack image show $IMAGE_ID -f value -c status" | tail -1)" = active ] || { echo "  image $IMAGE_ID not active"; bad=1; }
  [ "$(cli "openstack network show $NETWORK_ID -f value -c status" | tail -1)" = ACTIVE ] || { echo "  network $NETWORK_ID not ACTIVE"; bad=1; }
  o=$(ours); [ "$o" = 0 ] || { echo "  $o objects named $PREFIX-* already exist: clean them up or change PREFIX"; bad=1; }
  if [ -n "$CINDER_SSH" ]; then
    [ -n "$(on "$CINDER_SSH" "command -v qemu-io && command -v qemu-img && test -x $CINDER_VENV/bin/python && test -f $CINDER_VENV/cinder.env && echo ok" | grep -x ok)" ] ||
      { echo "  $CINDER_SSH lacks qemu-io, qemu-img, $CINDER_VENV/bin/python or $CINDER_VENV/cinder.env"; bad=1; }
    # Only these two option lines are printed; nothing else is read from the config.
    o=$(on "$CINDER_SSH" "grep -rhE '^[[:space:]]*(vmstore_qcow2_volumes|vmstore_online_extend_nova_check)[[:space:]]*=' $CINDER_CONF_DIR 2>/dev/null" | tr -d ' ')
    QCOW2_OPT=$(sed -n 's/^vmstore_qcow2_volumes=//p' <<<"$o" | tail -1); QCOW2_OPT=${QCOW2_OPT:-default}
    NOVA_CHECK=$(sed -n 's/^vmstore_online_extend_nova_check=//p' <<<"$o" | tail -1); NOVA_CHECK=${NOVA_CHECK:-default (True)}
    echo "  $CINDER_SSH config: vmstore_qcow2_volumes=$QCOW2_OPT vmstore_online_extend_nova_check=$NOVA_CHECK"
  else echo "  CINDER_SSH not set: E2, E3 and E4 will SKIP, E1 checks Cinder and the guest size only"; fi
  if [ -n "$SRC_COMPUTE_SSH" ]; then
    [ -n "$(on "$SRC_COMPUTE_SSH" "command -v virsh && echo ok" | grep -x ok)" ] || { echo "  $SRC_COMPUTE_SSH has no virsh"; bad=1; }
  else echo "  SRC_COMPUTE_SSH not set: the guest size checks will be skipped"; fi
  for h in $LOG_SSH; do
    if [ -n "$(on "$h" "test -r $CINDER_LOG && echo ok" | grep -x ok)" ]; then LOG_OK=1
    else echo "  warning: no readable $CINDER_LOG on $h (set CINDER_LOG); log lines will be missing"; fi
  done
  [ $bad = 0 ] || exit 2
  echo "  ok"
}

VM=; FLV=
cleanup() {
  echo "=== cleanup $(ts)"
  local ids id s a
  [ -z "$VM" ] && VM=$(cli "openstack server list --all-projects --name '^$PREFIX-vm$' -f value -c ID" | head -1)
  [ -n "$VM" ] && { cli "openstack server delete --wait $VM" | show; echo "  server $VM deleted"; }
  ids=$(cli "openstack volume list --all-projects -f value -c ID -c Name" | awk -v p="^$PREFIX-" '$2 ~ p {print $1}')
  for id in $ids; do s=$(wait_vol "$id")
    if [[ "$s" =~ ^(in-use|attaching|reserved)$ ]]; then
      for a in $(cli "openstack --os-volume-api-version 3.27 volume attachment list --all-projects -f value -c ID -c 'Volume ID'" | awk -v v="$id" '$2 == v {print $1}'); do
        echo "  removing leftover attachment $a of $id"; cli "openstack --os-volume-api-version 3.27 volume attachment delete $a" | show
      done; s=$(wait_vol "$id")
    fi
    [[ "$s" == error* || "$s" =~ ^(in-use|attaching|reserved|maintenance)$ ]] && { echo "  $id was $s, resetting to available (test volume)"; cli "openstack volume set --state available $id"; }
    cli "openstack volume delete $id" | show
  done
  for id in $ids; do echo "  volume $id: $(wait_vol "$id")"; done
  [ -z "$FLV" ] && FLV=$(cli "openstack flavor list --all -f value -c ID -c Name" | awk -v n="$PREFIX-flavor" '$2 == n {print $1}')
  [ -n "$FLV" ] && { cli "openstack flavor delete $FLV" | show; echo "  flavor $FLV deleted"; }
  echo "  left: $(ours) objects named $PREFIX-*"
  [ -n "$CINDER_SSH" ] && on "$CINDER_SSH" "rm -rf $RDIR && echo '  $RDIR removed on $CINDER_SSH'"
  others > "$OUT/others-after.txt"
  if diff -q "$OUT/others-before.txt" "$OUT/others-after.txt" >/dev/null; then echo "  other servers and volumes: unchanged"
  else echo "  other servers and volumes changed during the run (not by this script; review):"; diff "$OUT/others-before.txt" "$OUT/others-after.txt" | sed 's/^/    /' | head -20; fi
  echo "=== summary"
  local r; for r in ${RESULTS[@]+"${RESULTS[@]}"}; do echo "  $r"; done
  echo "  log: $OUT/run.log"
}

precheck
others > "$OUT/others-before.txt"
if [ -n "$CINDER_SSH" ]; then
  on "$CINDER_SSH" "mkdir -p $RDIR" && scp -q -o BatchMode=yes "$HERE/volume_record.py" "$CINDER_SSH:$RDIR/volume_record.py" || { echo "helper copy failed"; exit 2; }
fi
trap 'rc=$?; [ $rc = 0 ] && rc=$RC; cleanup; exit $rc' EXIT

echo "### $(ts) create volumes on $VOLUME_TYPE (image volumes are raw files; empty ones follow vmstore_qcow2_volumes)"
BOOT=$(new_vol "$PREFIX-boot" "--image $IMAGE_ID --size 1 --type $VOLUME_TYPE --bootable")
E1V=$(new_vol "$PREFIX-e1-raw" "--image $IMAGE_ID --size 1 --type $VOLUME_TYPE")
E2V=$(new_vol "$PREFIX-e2-empty" "--size 1 --type $VOLUME_TYPE")
E3V=$(new_vol "$PREFIX-e3-empty" "--size 1 --type $VOLUME_TYPE")
E4V=$(new_vol "$PREFIX-e4-raw" "--image $IMAGE_ID --size 1 --type $VOLUME_TYPE")
for v in "$BOOT" "$E1V" "$E2V" "$E3V" "$E4V"; do
  [[ "$v" =~ $UUID_RE ]] || { echo "  volume create failed"; exit 2; }
  s=$(wait_vol "$v"); echo "  $v: $s, file $(finfo "$v")"; [ "$s" = available ] || { echo "  volume $v not available"; exit 2; }
done
host=$(vhost "$E1V"); BACKEND=${host#*@}; BACKEND=${BACKEND%%#*}
v=$(running_version); echo "  backend $BACKEND, driver_version reported by cinder-volume: ${v:-unknown (cinder get-pools not available)}"
E2F=$(finfo "$E2V" | cut -d' ' -f1); E3F=$(finfo "$E3V" | cut -d' ' -f1)

if [ -n "$CINDER_SSH" ]; then
  echo "### $(ts) write pattern 0x5a ($PAT at offset 0) into $PREFIX-e1-raw and $PREFIX-e2-empty (test volumes, before attach)"
  P1=$(fpath "$E1V"); echo "  e1 raw: $(pwrite "$P1" raw)"
  P2=$(fpath "$E2V"); [ "$E2F" = qcow2 ] && echo "  e2 qcow2: $(pwrite "$P2" qcow2)"
fi

echo "### $(ts) boot a VM from $PREFIX-boot on $SRC_COMPUTE_ID"
FLV=$(cli "openstack flavor create --ram $FLAVOR_RAM_MB --vcpus 1 --disk 0 --private --project $PROJECT $PREFIX-flavor -f value -c id" | tail -1)
VM=$(cli "openstack server create --flavor $FLV --volume $BOOT --network $NETWORK_ID --availability-zone $AZ:$SRC_COMPUTE_ID --wait $PREFIX-vm -f value -c id" | tail -1)
[[ "$VM" =~ $UUID_RE ]] || VM=$(cli "openstack server list --all-projects --name '^$PREFIX-vm$' -f value -c ID" | head -1)
[ -n "$VM" ] || { echo "  VM create failed"; exit 2; }
s=$(wait_server "$VM" ACTIVE); h=$(sfield "$VM" OS-EXT-SRV-ATTR:host); echo "  vm $VM: $s on $h"
[ "$s" = ACTIVE ] || { echo "  VM not ACTIVE: $(sfield "$VM" fault | cut -c1-300)"; exit 2; }
[ "$h" = "$SRC_COMPUTE_ID" ] || { echo "  VM is on $h, not $SRC_COMPUTE_ID"; exit 2; }

echo "### E1 $(ts) in-use extend of a raw volume, 1 -> 2 GB"
reset_check; f=$(finfo "$E1V"); n1=$(nameid "$E1V")
if [ -z "$CINDER_SSH" ] && [ -z "$SRC_COMPUTE_SSH" ]; then result E1 SKIP "needs CINDER_SSH or SRC_COMPUTE_SSH"
elif [ "${f%% *}" != raw ] && [ "${f%% *}" != unchecked ]; then result E1 FAIL "not tested: file is ${f%% *}, expected raw"
elif [ "$(attach "$E1V")" != in-use ]; then result E1 FAIL "not tested: attach did not end in-use"
else
  echo "  before: guest size $(gsize "$VM" "$n1")"
  s=$(extend "$E1V" 2); z=$(vsize "$E1V"); echo "  Cinder: $s, size $z GB"
  { [ "$s" = in-use ] && [ "$z" = 2 ]; } || fail "Cinder $s, size $z GB"
  if [ -n "$CINDER_SSH" ]; then
    f=$(finfo "$E1V"); p=$(pcheck "$P1" raw); echo "  file: $f, pattern $p"
    [ "$(cut -d' ' -f2 <<<"$f")" = $((2 * GIB)) ] || fail "file $(cut -d' ' -f2 <<<"$f") bytes"
    [ "$p" = ok ] || fail "pattern $p"
  else note "file and pattern (no CINDER_SSH)"; fi
  if [ -n "$SRC_COMPUTE_SSH" ]; then
    g=$(wait_gsize "$VM" "$n1" $((2 * GIB))); echo "  guest size: ${g:-?}"
    [ "$g" = $((2 * GIB)) ] || fail "guest size ${g:-?}"
  else note "guest size (no SRC_COMPUTE_SSH)"; fi
  judge E1 "raw grown in use, guest sees 2 GB, data intact"
fi

echo "### E2 $(ts) in-use extend of a qcow2 volume, 1 -> 2 GB (Nova resizes it)"
reset_check; n2=$(nameid "$E2V")
if [ -z "$CINDER_SSH" ]; then result E2 SKIP "needs CINDER_SSH to tell the file format"
elif [ "$E2F" != qcow2 ]; then result E2 SKIP "empty volumes are $E2F files here (vmstore_qcow2_volumes=$QCOW2_OPT)"
elif [ "$(attach "$E2V")" != in-use ]; then result E2 FAIL "not tested: attach did not end in-use"
else
  echo "  before: guest size $(gsize "$VM" "$n2")"
  s=$(extend "$E2V" 2); z=$(vsize "$E2V"); echo "  Cinder: $s, size $z GB"
  { [ "$s" = in-use ] && [ "$z" = 2 ]; } || fail "Cinder $s, size $z GB"
  f=$(wait_virtual "$E2V" $((2 * GIB))); echo "  file: $f"
  [ "$(cut -d' ' -f3 <<<"$f")" = $((2 * GIB)) ] || fail "virtual size $(cut -d' ' -f3 <<<"$f")"
  if [ -n "$SRC_COMPUTE_SSH" ]; then
    g=$(wait_gsize "$VM" "$n2" $((2 * GIB))); echo "  guest size: ${g:-?}"
    [ "$g" = $((2 * GIB)) ] || fail "guest size ${g:-?}"
  else note "guest size (no SRC_COMPUTE_SSH)"; fi
  logs_for "$E2V" 'leaving the resize to Nova|Growing|ERROR' | sed 's/^/  log /'
  s=$(detach "$E2V"); c=$(qcheck "$P2"); p=$(pcheck "$P2" qcow2); echo "  after detach: $s, qemu-img check rc=$c, pattern $p"
  [ "$c" = 0 ] || fail "qemu-img check rc=$c"
  [ "$p" = ok ] || fail "pattern $p"
  judge E2 "qcow2 resized by Nova in use, guest sees 2 GB, image clean, data intact"
fi

echo "### E4 $(ts) attach-time grow of a raw file smaller than its volume"
reset_check
if [ -z "$CINDER_SSH" ]; then result E4 SKIP "needs CINDER_SSH"
else
  P4=$(fpath "$E4V"); f=$(finfo "$E4V"); n4=$(nameid "$E4V")
  if [ "${f%% *}" != raw ] || [[ "$P4" != */volume-"$n4" ]]; then result E4 FAIL "not tested: file ${f%% *} at ${P4:-?}"
  else
    echo "  shrinking the test file $P4 to 512 MiB (simulates an extend Nova did not apply)"
    on "$CINDER_SSH" "truncate -s 512M '$P4'"
    b=$(finfo "$E4V" | cut -d' ' -f2); echo "  file before attach: $b bytes"
    if [ "$b" != 536870912 ]; then result E4 FAIL "not tested: shrink left $b bytes"
    elif [ "$(attach "$E4V")" != in-use ]; then result E4 FAIL "attach did not end in-use"
    else
      b=$(finfo "$E4V" | cut -d' ' -f2); echo "  file after attach: $b bytes"
      [ "$b" = "$GIB" ] || fail "file $b bytes after attach"
      l=$(logs_for "$E4V" 'smaller than its size'); [ -n "$l" ] && sed 's/^/  log /' <<<"$l"
      if [ "$LOG_OK" = 0 ]; then note "log (no readable CINDER_LOG)"
      elif [ -z "$l" ]; then fail "no grow warning in the cinder-volume log"; fi
      if [ -n "$SRC_COMPUTE_SSH" ]; then g=$(gsize "$VM" "$n4"); echo "  guest size: ${g:-?}"
        [ "$g" = "$GIB" ] || fail "guest size ${g:-?}"
      else note "guest size (no SRC_COMPUTE_SSH)"; fi
      judge E4 "file grown back to 1 GB on attach"
    fi
  fi
fi

echo "### E3 $(ts) extend of a qcow2 volume whose instance is shut off, 1 -> 2 GB"
reset_check; n3=$(nameid "$E3V")
if [ -z "$CINDER_SSH" ]; then result E3 SKIP "needs CINDER_SSH to tell the file format"
elif [ "$E3F" != qcow2 ]; then result E3 SKIP "empty volumes are $E3F files here (vmstore_qcow2_volumes=$QCOW2_OPT)"
elif [[ "$NOVA_CHECK" =~ ^(False|false|0|no)$ ]]; then result E3 SKIP "vmstore_online_extend_nova_check=$NOVA_CHECK: the driver leaves the file to Nova"
elif [ "$(attach "$E3V")" != in-use ]; then result E3 FAIL "not tested: attach did not end in-use"
else
  P3=$(fpath "$E3V")
  cli "openstack server stop $VM" | show; s=$(wait_server "$VM" SHUTOFF); echo "  vm: $s, power state $(sfield "$VM" OS-EXT-STS:power_state)"
  if [ "$s" != SHUTOFF ]; then result E3 FAIL "not tested: VM did not stop ($s)"
  else
    s=$(extend "$E3V" 2); z=$(vsize "$E3V"); echo "  Cinder: $s, size $z GB"
    { [ "$s" = in-use ] && [ "$z" = 2 ]; } || fail "Cinder $s, size $z GB"
    f=$(finfo "$E3V"); c=$(qcheck "$P3"); echo "  file while shut off: $f, qemu-img check rc=$c"
    [ "$(cut -d' ' -f3 <<<"$f")" = $((2 * GIB)) ] || fail "not resized offline: virtual size $(cut -d' ' -f3 <<<"$f")"
    [ "$c" = 0 ] || fail "qemu-img check rc=$c"
    logs_for "$E3V" 'Growing qcow2|shut off|ERROR' | sed 's/^/  log /'
    cli "openstack server start $VM" | show; s=$(wait_server "$VM" ACTIVE); echo "  vm: $s"
    [ "$s" = ACTIVE ] || fail "VM did not start ($s)"
    if [ -n "$SRC_COMPUTE_SSH" ]; then
      g=$(wait_gsize "$VM" "$n3" $((2 * GIB))); echo "  guest size after start: ${g:-?}"
      [ "$g" = $((2 * GIB)) ] || fail "guest size ${g:-?} after start"
    else note "guest size (no SRC_COMPUTE_SSH)"; fi
    judge E3 "qcow2 resized offline by the driver, image clean, guest sees 2 GB after start"
  fi
fi
