#!/usr/bin/env bash
# Volume migration lab test (VME-29): format record and qcow2 data after a Cinder generic
# migration, live migration of a VM using a migrated volume, detached extend afterwards.
# Runbook: docs/runbooks/volume-migration-lab-test.md
#
# Usage: run.sh [--cinder-only] <lab.env>
#
# Creates only objects named <PREFIX>-* (IDs recorded at creation) and deletes them on exit,
# including on failure or Ctrl-C. Other VMs and volumes are never modified.
# Exit: 0 all checks passed, 1 a check failed, 2 precheck or setup problem.
set -u
CINDER_ONLY=0
[ "${1:-}" = --cinder-only ] && { CINDER_ONLY=1; shift; }
[ -f "${1:-}" ] || { echo "usage: $0 [--cinder-only] <lab.env>"; exit 2; }
# shellcheck disable=SC1090
. "$1"
for v in CLI_SSH CLI_RC REGION AZ SRC_COMPUTE_SSH SRC_COMPUTE_ID DST_COMPUTE_SSH DST_COMPUTE_ID \
         DST_RP_NAME VOLUME_HOSTS VOLUME_TYPE CINDER_SSH CINDER_VENV CINDER_CONF_DIR IMAGE_ID \
         NETWORK_ID PROJECT FLAVOR_RAM_MB PREFIX; do
  [ -n "${!v:-}" ] || { echo "missing $v in $1"; exit 2; }
done

HERE=$(cd "$(dirname "$0")" && pwd)
RDIR=/root/$PREFIX-lab
NOVA_LOG=${NOVA_LOG:-/var/log/pf9/ostackhost.log}   # nova-compute log on the compute hosts (PCD path)
START_EPOCH=$(date +%s); START=$(date -u '+%Y-%m-%d %H:%M:%S')
# Host logs use the host's local clock: filter them from the same instant in the host's time zone.
LOG_SINCE="awk -v s=\"\$(date -d @$START_EPOCH '+%Y-%m-%d %H:%M:%S')\" 'substr(\$0,1,19) >= s'"
OUT=$(mktemp -d "${TMPDIR:-/tmp}/$PREFIX-lab.XXXXXX")
exec > >(tee "$OUT/run.log") 2>&1
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15"

ts() { date -u +%T; }
cli() { $SSH "$CLI_SSH" "source $CLI_RC >/dev/null 2>&1; export OS_REGION_NAME=$REGION; $*" 2>&1 | grep -v setlocale; return 0; }
on() { local t=$1; shift; $SSH "$t" "$@" 2>&1 | grep -v setlocale; return 0; }
compute_ssh() { case "$1" in "$SRC_COMPUTE_ID") echo "$SRC_COMPUTE_SSH";; "$DST_COMPUTE_ID") echo "$DST_COMPUTE_SSH";; esac; }

vstat() { cli "openstack volume show $1 -f value -c status 2>/dev/null"; }
vmig() { cli "openstack volume show $1 -f value -c os-vol-mig-status-attr:migstat 2>/dev/null"; }
vhost() { cli "openstack volume show $1 -f value -c os-vol-host-attr:host 2>/dev/null"; }
vsize() { cli "openstack volume show $1 -f value -c size 2>/dev/null"; }
sfield() { cli "openstack server show $1 -f value -c $2 2>/dev/null"; }
wait_vol() { local s i
  for ((i = 0; i < 1200; i += 10)); do s=$(vstat "$1")
    [[ "$s" =~ ^(creating|downloading|extending|deleting|maintenance|retyping|attaching|detaching|reserved|uploading)$ ]] || break; sleep 10
  done; echo "${s:-deleted}"; }
wait_mig() { local m i
  for ((i = 0; i < 1200; i += 10)); do m=$(vmig "$1")
    [[ "$m" =~ ^(success|error)$ ]] && break; [[ "$m" =~ ^(None|)$ && $i -ge 60 ]] && break; sleep 10
  done; echo "${m:-None}"; }

# Cinder record and file checks on CINDER_SSH.
volrec() { on "$CINDER_SSH" "cd $RDIR && (set +u; set -a; . $CINDER_VENV/cinder.env; set +a; $CINDER_VENV/bin/python volume_record.py --conf-dir $CINDER_CONF_DIR $*)" | grep '^VOL'; }
field() { sed -nE "s/.* $1=([^ ]+).*/\1/p" <<<"$2"; }
fpath() { local l; l=$(volrec "$1"); [ -n "$l" ] || return 0
  on "$CINDER_SSH" "m=\$(findmnt -n -o TARGET -S '$(field share "$l")' | grep '$CINDER_VENV' | head -1); echo \$m/volume-$(field name_id "$l")"; }
finfo() { local p; p=$(fpath "$1"); [ -n "$p" ] || { echo "missing 0"; return; }  # -> "<qcow2|raw|missing> <bytes>"
  on "$CINDER_SSH" "p='$p'; [ -e \$p ] || { echo missing 0; exit; }; x=\$(head -c4 \$p | od -An -tx1 | tr -d ' '); [ \$x = 514649fb ] && f=qcow2 || f=raw; echo \$f \$(stat -c %s \$p)"; }
state() { echo "### $1 $(ts)"; shift; local id l
  for id in "$@"; do l=$(volrec "$id")
    echo "  $id record=$(field record "$l") file=$(finfo "$id") status=$(field status "$l") migration=$(field migration "$l") host=$(field host "$l")"
  done; }
check_fill() { # file path, raw|qcow2 -> "check_rc=<n> reads=<n>/2 pattern_failures=<n>" (raw has no metadata: check_rc=-)
  on "$CINDER_SSH" "if [ '$2' = qcow2 ]; then qemu-img check -U -f qcow2 '$1' >/dev/null 2>&1; echo check_rc=\$?; else echo check_rc=-; fi
    o=\$(qemu-io -r -U -f '$2' -c 'read -P 0x5a 0 512M' -c 'read -P 0x5a 512M 512M' '$1' 2>&1)
    echo reads=\$(echo \"\$o\" | grep -c '^read 536870912/536870912 bytes')/2 pattern_failures=\$(echo \"\$o\" | grep -c 'Pattern verification failed')" | paste -sd' ' -; }
disk_type() { # host ssh, vm, file name id -> libvirt driver type of that disk
  on "$1" "virsh dumpxml $2" | awk -v n="volume-$3" '/<driver name=.qemu. type=/ {match($0, /type=.[a-z0-9]+./); t = substr($0, RSTART + 6, RLENGTH - 7)} index($0, n) && /source file/ {print t; exit}'; }
others() { # every server and volume not created by this run
  cli "openstack server list --all-projects --long -f value -c ID -c Name -c Status -c Host" | awk -v p="^$PREFIX-" '$2 !~ p {print "server", $1, $3, $4}' | sort
  cli "openstack volume list --all-projects -f value -c ID -c Name -c Status" | awk -v p="^$PREFIX-" '$2 !~ p {print "volume", $1, $NF}' | sort; }
dst_free_mb() {
  cli "RP=\$(openstack resource provider list --name $DST_RP_NAME -f value -c uuid); openstack resource provider inventory show \$RP MEMORY_MB -f json; echo ===; openstack resource provider usage show \$RP -f json" |
    python3 -c 'import json, sys
inv, use = sys.stdin.read().split("===")
i = json.loads(inv)
used = {u["resource_class"]: u["usage"] for u in json.loads(use)}.get("MEMORY_MB", 0)
print(int((i["total"] - i["reserved"]) * i["allocation_ratio"] - used))' 2>/dev/null; }

RESULTS=(); RC=0
result() { RESULTS+=("$1 | $2 | $3"); echo "  RESULT $1: $2 ($3)"; [ "$2" = FAIL ] && RC=1; return 0; }

precheck() {
  local bad=0 vmbad=0 backend h svc s free src_mt
  echo "### precheck $(ts)"
  [ "$(cli "openstack token issue -f value -c id >/dev/null && echo ok")" = ok ] || { echo "  CLI or credentials not working on $CLI_SSH"; exit 2; }
  backend=$(cli "openstack volume type show $VOLUME_TYPE -f json -c properties" | python3 -c 'import json, sys; print((json.load(sys.stdin).get("properties") or {}).get("volume_backend_name", ""))' 2>/dev/null)
  backend=${backend:-$VOLUME_TYPE}
  svc=$(cli "openstack volume service list --service cinder-volume -f value -c Host -c Status -c State")
  for h in $VOLUME_HOSTS; do grep -q "^$h@$backend enabled up$" <<<"$svc" || { echo "  cinder-volume $h@$backend is not enabled and up"; bad=1; }; done
  [ "$(cli "openstack image show $IMAGE_ID -f value -c status")" = active ] || { echo "  image $IMAGE_ID not active"; bad=1; }
  [ -n "$(on "$CINDER_SSH" "command -v qemu-io && command -v qemu-img && test -x $CINDER_VENV/bin/python && echo ok" | grep -x ok)" ] || { echo "  $CINDER_SSH lacks qemu-io, qemu-img or $CINDER_VENV/bin/python"; bad=1; }
  s=$(cli "openstack server list --all-projects -f value -c Name; openstack volume list --all-projects -f value -c Name; openstack flavor list --all -f value -c Name" | grep -c "^$PREFIX-")
  [ "$s" = 0 ] || { echo "  $s objects named $PREFIX-* already exist: clean them up or change PREFIX"; bad=1; }
  if [ $CINDER_ONLY = 0 ]; then
    [ "$(cli "openstack network show $NETWORK_ID -f value -c status")" = ACTIVE ] || { echo "  network $NETWORK_ID not ACTIVE"; bad=1; }
    svc=$(cli "openstack compute service list --service nova-compute -f value -c Host -c Status -c State")
    for h in "$SRC_COMPUTE_ID" "$DST_COMPUTE_ID"; do grep -q "^$h enabled up$" <<<"$svc" || { echo "  nova-compute $h is not enabled and up"; vmbad=1; }; done
    free=$(dst_free_mb)
    echo "  $DST_RP_NAME placement memory free: ${free:-?} MB (flavor needs $FLAVOR_RAM_MB)"
    [ -n "$free" ] && [ "$free" -ge "$FLAVOR_RAM_MB" ] || vmbad=1
    src_mt=$(on "$SRC_COMPUTE_SSH" "qemu-system-x86_64 -machine help" | awk '/\(default\)/ {print $1; exit}')
    if on "$DST_COMPUTE_SSH" "qemu-system-x86_64 -machine help" | awk '{print $1}' | grep -qx "${src_mt:-none}"; then
      echo "  machine type $src_mt (SRC default) is available on DST"
    else echo "  DST QEMU lacks machine type ${src_mt:-?} used by SRC guests: live migration SRC -> DST cannot work"; vmbad=1; fi
  fi
  [ $bad = 0 ] || exit 2
  [ $vmbad = 0 ] || { echo "  VM steps cannot run; fix the above or rerun with --cinder-only"; exit 2; }
  echo "  ok"
}

VM=; FLV=
cleanup() {
  echo "=== cleanup $(ts)"
  local ids id s a
  [ -z "$VM" ] && VM=$(cli "openstack server list --all-projects --name '^$PREFIX-vm$' -f value -c ID" | head -1)
  [ -n "$VM" ] && { cli "openstack server delete --wait $VM"; echo "  server $VM deleted"; }
  # every <PREFIX>-* volume, including a migration target left behind by a failed migration
  ids=$(cli "openstack volume list --all-projects -f value -c ID -c Name" | awk -v p="^$PREFIX-" '$2 ~ p {print $1}')
  for id in $ids; do s=$(wait_vol "$id")
    if [[ "$s" =~ ^(in-use|attaching|reserved)$ ]]; then
      for a in $(cli "openstack --os-volume-api-version 3.27 volume attachment list --all-projects -f value -c ID -c 'Volume ID'" | awk -v v="$id" '$2 == v {print $1}'); do
        echo "  removing leftover attachment $a of $id"; cli "openstack --os-volume-api-version 3.27 volume attachment delete $a"
      done; s=$(wait_vol "$id")
    fi
    [[ "$s" == error* || "$s" =~ ^(in-use|attaching|reserved|maintenance)$ ]] && { echo "  $id was $s, resetting to available (test volume)"; cli "openstack volume set --state available $id"; }
    cli "openstack volume delete $id"
  done
  for id in $ids; do echo "  volume $id: $(wait_vol "$id")"; done
  [ -z "$FLV" ] && FLV=$(cli "openstack flavor list --all -f value -c ID -c Name" | awk -v n="$PREFIX-flavor" '$2 == n {print $1}')
  [ -n "$FLV" ] && { cli "openstack flavor delete $FLV"; echo "  flavor $FLV deleted"; }
  echo "  left: $(cli "openstack server list --all-projects -f value -c Name; openstack volume list --all-projects -f value -c Name; openstack flavor list --all -f value -c Name" | grep -c "^$PREFIX-") objects named $PREFIX-*"
  on "$CINDER_SSH" "rm -rf $RDIR && echo '  $RDIR removed on $CINDER_SSH'"
  others > "$OUT/others-after.txt"
  if diff -q "$OUT/others-before.txt" "$OUT/others-after.txt" >/dev/null; then echo "  other servers and volumes: unchanged"
  else echo "  other servers and volumes changed during the run (not by this script; review):"; diff "$OUT/others-before.txt" "$OUT/others-after.txt" | sed 's/^/    /' | head -20; fi
  [ $CINDER_ONLY = 0 ] && echo "  $DST_RP_NAME placement memory free: $(dst_free_mb) MB"
  echo "=== summary"
  local r; for r in ${RESULTS[@]+"${RESULTS[@]}"}; do echo "  $r"; done   # bash 3.2: empty array under set -u
  echo "  log: $OUT/run.log"
}

precheck
others > "$OUT/others-before.txt"
on "$CINDER_SSH" "mkdir -p $RDIR" && scp -q -o BatchMode=yes "$HERE/volume_record.py" "$CINDER_SSH:$RDIR/volume_record.py" || { echo "helper copy failed"; exit 2; }
trap 'rc=$?; [ $rc = 0 ] && rc=$RC; cleanup; exit $rc' EXIT

echo "### $(ts) create volumes on $VOLUME_TYPE"
BOOT=; [ $CINDER_ONLY = 0 ] && BOOT=$(cli "openstack volume create --image $IMAGE_ID --size 1 --type $VOLUME_TYPE --bootable $PREFIX-boot -f value -c id" | tail -1)
DATA=$(cli "openstack volume create --image $IMAGE_ID --size 1 --type $VOLUME_TYPE $PREFIX-data -f value -c id" | tail -1)
FILL=$(cli "openstack volume create --size 1 --type $VOLUME_TYPE $PREFIX-filled -f value -c id" | tail -1)
for v in $BOOT $DATA $FILL; do s=$(wait_vol "$v"); echo "  $v: $s"; [ "$s" = available ] || { echo "volume $v not available"; exit 2; }; done

# An empty volume is created in the backend's format (vmstore_qcow2_volumes): R2 fills and
# checks it in that format, so it covers qcow2 backends and raw ones.
FMT=$(finfo "$FILL" | cut -d' ' -f1)
case "$FMT" in raw | qcow2) ;; *) echo "  $FILL file is $FMT, expected raw or qcow2"; exit 2 ;; esac
P=$(fpath "$FILL"); echo "### $(ts) fill $FILL ($FMT file) with pattern 0x5a"
on "$CINDER_SSH" "qemu-io -f $FMT -c 'write -P 0x5a 0 512M' -c 'write -P 0x5a 512M 512M' '$P'" | sed 's/^/  /'
c=$(check_fill "$P" "$FMT"); echo "  before migration: $c"
[[ "$c" == "check_rc="[0-]" reads=2/2 pattern_failures=0" ]] || { echo "  fill did not verify before the migration"; exit 2; }
if [ "$FMT" = qcow2 ]; then w="qcow2 / qcow2 larger than 1 GiB"; else w="raw / raw 1 GiB"; fi
state "A. created (expected: image volume record raw / file raw; filled volume $w)" $BOOT $DATA $FILL

if [ $CINDER_ONLY = 0 ]; then
  echo "### $(ts) boot a VM from $PREFIX-boot on SRC"
  FLV=$(cli "openstack flavor create --ram $FLAVOR_RAM_MB --vcpus 1 --disk 0 --private --project $PROJECT $PREFIX-flavor -f value -c id" | tail -1)
  VM=$(cli "openstack server create --flavor $FLV --volume $BOOT --network $NETWORK_ID --availability-zone $AZ:$SRC_COMPUTE_ID --wait $PREFIX-vm -f value -c id" | tail -1)
  [[ "$VM" =~ ^[0-9a-f-]{36}$ ]] || VM=$(cli "openstack server list --all-projects --name '^$PREFIX-vm$' -f value -c ID" | head -1)
  echo "  vm $VM: $(sfield "$VM" status) on $(sfield "$VM" OS-EXT-SRV-ATTR:host)"
  [ "$(sfield "$VM" status)" = ACTIVE ] || { echo "  VM not ACTIVE: $(sfield "$VM" fault | cut -c1-300)"; exit 2; }
fi

echo "### $(ts) migrate the available volumes to the other cinder-volume host"
for v in $DATA $FILL; do cur=$(vhost "$v"); dst=
  for h in $VOLUME_HOSTS; do [ "$h" != "${cur%%@*}" ] && dst="$h@${cur#*@}" && break; done
  echo "  $v: $cur -> $dst"; cli "openstack volume migrate --host '$dst' $v"
done
for v in $DATA $FILL; do echo "  $v: migstat=$(wait_mig "$v") status=$(vstat "$v")"; done
state "B. after volume migration" $DATA $FILL

l=$(volrec "$DATA"); f=$(finfo "$DATA"); r=$(field record "$l"); [ "$r" = None ] && r=raw  # no record means raw
if [ "$r" = "${f%% *}" ]; then result R1 PASS "image volume record $r matches file ${f%% *} after migration"
else result R1 FAIL "image volume record $r but file ${f%% *} after migration"; fi
P=$(fpath "$FILL"); f=$(finfo "$FILL"); c=$(check_fill "$P" "$FMT"); ok=0
if [ "$FMT" = qcow2 ]; then
  [ "${f%% *}" = qcow2 ] && [ "${f##* }" -gt 1073741824 ] && [ "$c" = "check_rc=0 reads=2/2 pattern_failures=0" ] && ok=1
else
  [ "${f%% *}" = raw ] && [ "${f##* }" = 1073741824 ] && [ "$c" = "check_rc=- reads=2/2 pattern_failures=0" ] && ok=1
fi
if [ $ok = 1 ]; then result R2 PASS "filled $FMT volume intact after migration: ${f%% *} ${f##* } bytes, $c"
else result R2 FAIL "filled $FMT volume after migration: ${f%% *} ${f##* } bytes, $c"; fi

if [ $CINDER_ONLY = 0 ]; then
  echo "### $(ts) attach $PREFIX-data to the VM"
  cli "openstack server add volume $VM $DATA" >/dev/null; echo "  data: $(wait_vol "$DATA")"
  nid=$(field name_id "$(volrec "$DATA")")
  echo "  disk type on SRC: $(disk_type "$SRC_COMPUTE_SSH" "$VM" "$nid") (file $(finfo "$DATA" | cut -d' ' -f1))"

  echo "### $(ts) live migrate the VM to DST"
  cli "openstack --os-compute-api-version 2.79 server migrate --live-migration --host $DST_COMPUTE_ID --wait $VM" | tail -2 | sed 's/^/  cli: /'
  host=$(sfield "$VM" OS-EXT-SRV-ATTR:host); st=$(sfield "$VM" status)
  mig=$(cli "openstack server migration list --server $VM -f value -c Status -c 'Dest Compute'" | head -1)
  echo "  vm: status=$st host=$host migration=[$mig]"
  [ "$st" = ERROR ] && echo "  fault: $(sfield "$VM" fault | cut -c1-400)"
  hs=$(compute_ssh "$host"); dt=$([ -n "$hs" ] && disk_type "$hs" "$VM" "$nid"); ft=$(finfo "$DATA" | cut -d' ' -f1)
  echo "  disk type on $host: ${dt:-?} (file $ft)"
  # libvirt refuses a live migration to a host whose CPU lacks features the guest uses: a
  # property of the lab's hosts, not of the volume (cold migration is in the format matrix).
  cpu=$(on "$SRC_COMPUTE_SSH" "$LOG_SINCE $NOVA_LOG | grep -a '$VM' | grep -aoE \"guest CPU doesn't match specification: [^:]*: [a-z0-9_,]+\" | tail -1")
  if [ "$host" = "$DST_COMPUTE_ID" ] && [ "$st" = ACTIVE ] && [ "$dt" = "$ft" ]; then result R3 PASS "VM live migrated to DST, migrated volume opened as $dt"
  elif [ -n "$cpu" ] && [ "$st" = ACTIVE ] && [ "$host" = "$SRC_COMPUTE_ID" ] && [ "$dt" = "$ft" ]; then
    result R3 SKIP "lab hosts cannot live-migrate: libvirt \"$cpu\" (DST CPU lacks features of SRC); VM still ACTIVE on SRC, volume opened as $dt"
  elif [[ "$mig" == *None ]]; then result R3 FAIL "live migration rejected before start (no destination selected; check DST capacity and scheduler)"
  else result R3 FAIL "status=$st host=$host migration=[$mig] disk=${dt:-?} file=$ft"; fi
  state "C. after live migration" $DATA
  echo "### logs since $START UTC (errors and warnings for the VM and the data volume)"
  for hs in "$SRC_COMPUTE_SSH" "$DST_COMPUTE_SSH"; do
    echo "  -- $hs nova"
    on "$hs" "$LOG_SINCE $NOVA_LOG | grep -aE '$VM|$DATA' | grep -aE 'ERROR|WARNING' | grep -v 'unexpected event' | sed -E 's/req-[0-9a-f-]+//' | cut -c1-260 | tail -10"
  done
  echo "### $(ts) delete the VM"
  cli "openstack server delete --wait $VM"; VM=
else result R3 SKIP "--cinder-only"
fi

echo "### $(ts) detached extend of $PREFIX-data 1 -> 2 GB"
echo "  data: $(wait_vol "$DATA")"
cli "openstack volume set --size 2 $DATA"; s=$(wait_vol "$DATA")
if [ "$s" = available ] && [ "$(vsize "$DATA")" = 2 ]; then result R4 PASS "detached extend after migration"
else result R4 FAIL "detached extend after migration: status=$s"; fi
state "D. after extend" $DATA
