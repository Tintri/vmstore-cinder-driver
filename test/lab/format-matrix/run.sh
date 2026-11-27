#!/usr/bin/env bash
# NFS volume format matrix on VMstore (VME-29). For each volume operation it records the
# real format of the active file on the share, the format Cinder records
# (admin_metadata['format'], or none) and the format libvirt opens, as in Platform9's
# "NFS Volume Format Matrix". Runbook: docs/runbooks/format-matrix-lab-test.md
#
# Usage: run.sh [--two-host | --flip-phase create|after|check|cleanup] <lab.env>
#   (default)             one host: Platform9 steps 1 to 19
#   --two-host            migrate a volume to the other cinder-volume host, then move the VM
#   --flip-phase create   create a VM and volumes while vmstore_qcow2_volumes is on, keep them
#   --flip-phase after    after the backend was switched off and restarted: the flip rows, then
#                         delete everything
#   --flip-phase check    after the switch: operations on the kept volumes while their records
#                         are still what phase create left (out of sync with the files); keeps
#                         everything, so it can run again after a driver swap or an upgrade
#   --flip-phase cleanup  delete what phase create and the checks kept
#
# Creates only objects named <PREFIX>-* (the volume Nova creates for a boot from image is
# renamed to <PREFIX>-nova-vol) and deletes them on exit, including on failure or Ctrl-C.
# Flip phases create and check keep them. Never writes to volume files, except the pattern
# phase create writes into its own spare volumes (qemu-io) to check data after a move.
# Exit: 0 every row matched (n/a allowed), 1 a row failed, 2 precheck or setup problem.
set -u
MODE=single
case "${1:-}" in
  --two-host) MODE=two-host; shift ;;
  --flip-phase)
    case "${2:-}" in create | after | check | cleanup) MODE=flip-$2; shift 2 ;; *) set -- ;; esac ;;
esac
[ -f "${1:-}" ] || { echo "usage: $0 [--two-host | --flip-phase create|after|check|cleanup] <lab.env>"; exit 2; }
# shellcheck disable=SC1090
. "$1"
NEED="CLI_SSH CLI_RC REGION AZ SRC_COMPUTE_ID VOLUME_TYPE IMAGE_ID NETWORK_ID PROJECT FLAVOR_RAM_MB PREFIX CINDER_SSH CINDER_VENV CINDER_CONF_DIR"
[ "$MODE" = two-host ] && NEED="$NEED DST_COMPUTE_ID VOLUME_HOSTS"
for v in $NEED; do [ -n "${!v:-}" ] || { echo "missing $v in $1"; exit 2; }; done
SRC_COMPUTE_SSH=${SRC_COMPUTE_SSH:-}; DST_COMPUTE_SSH=${DST_COMPUTE_SSH:-}; DST_RP_NAME=${DST_RP_NAME:-}
LOG_SSH=${LOG_SSH:-$CINDER_SSH}                                 # every host serving VOLUME_TYPE
CINDER_LOG=${CINDER_LOG:-/var/log/pf9/cindervolume-base.log}   # cinder-volume log (PCD path)
PCD_RELEASE=${PCD_RELEASE:-}   # free-text label for the report, for example "Aug P2"
LIVE=${LIVE:-0}                # --two-host: 1 = live migrate to DST instead of a cold migration
FORCE=${FORCE:-0}              # flip phases: 1 = run even if the option is not what the phase expects
FLIP_SPARES=${FLIP_SPARES:-3}  # flip create: detached volumes of each kind kept, one per check
LABEL=${LABEL:-}               # flip check: free-text label for the report, for example "3.0.12, before the DB script"
VOL_API=3.42                   # Block Storage API version for in-use extend

HERE=$(cd "$(dirname "$0")" && pwd)
RDIR=/root/$PREFIX-lab
if [ "${MODE#flip-}" != "$MODE" ]; then
  FLIPDIR="${TMPDIR:-/tmp}/$PREFIX-flip"; STATE=$FLIPDIR/state
  OUT=$FLIPDIR/${MODE#flip-}-$(date -u +%Y%m%dT%H%M%SZ); mkdir -p "$OUT" || exit 2
else
  OUT=$(mktemp -d "${TMPDIR:-/tmp}/$PREFIX-lab.XXXXXX")
fi
exec > >(tee "$OUT/run.log") 2>&1
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15"
UUID_RE='^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
GIB=1073741824

ts() { date -u +%T; }
cli() { $SSH "$CLI_SSH" "source $CLI_RC >/dev/null 2>&1; export OS_REGION_NAME=$REGION; $*" 2>&1 | grep -v setlocale; return 0; }
on() { local t=$1; shift; $SSH "$t" "$@" 2>&1 | grep -v setlocale; return 0; }
show() { sed 's/^/  cli: /'; }
clean() { tr '|\n' '/ ' | sed 's/ *$//' | cut -c1-160; }   # text safe for a markdown table cell

# Status from `show` output; "deleted" when the API says it does not exist, "unknown" on
# CLI or network trouble (never counted as a final state).
st_of() { local l; l=$(grep -v '^ *$' <<<"$1" | tail -1)
  if [[ "$l" =~ ^[a-z_-]+$ ]]; then echo "$l"
  elif grep -qiE 'no (volume|snapshot) with a name or id|could not be found|HTTP 404' <<<"$1"; then echo deleted
  else echo unknown; fi; }
vstat() { st_of "$(cli "openstack volume show $1 -f value -c status")"; }
sstat() { st_of "$(cli "openstack volume snapshot show $1 -f value -c status")"; }
vsize() { cli "openstack volume show $1 -f value -c size 2>/dev/null" | tail -1; }
vhost() { cli "openstack volume show $1 -f value -c os-vol-host-attr:host 2>/dev/null" | tail -1; }
vmig() { cli "openstack volume show $1 -f value -c os-vol-mig-status-attr:migstat 2>/dev/null" | tail -1; }
sfield() { cli "openstack server show $1 -f value -c $2 2>/dev/null" | tail -1; }
wait_vol() { local s i
  for ((i = 0; i < 1200; i += 10)); do s=$(vstat "$1")
    [[ "$s" =~ ^(creating|downloading|extending|deleting|maintenance|attaching|detaching|reserved|retyping|uploading|unknown)$ ]] || break; sleep 10
  done; echo "$s"; }
wait_snap() { local s i
  for ((i = 0; i < 1200; i += 10)); do s=$(sstat "$1")
    [[ "$s" =~ ^(creating|deleting|unknown)$ ]] || break; sleep 10
  done; echo "$s"; }
wait_mig() { local m i
  for ((i = 0; i < 1200; i += 10)); do m=$(vmig "$1")
    [[ "$m" =~ ^(success|error)$ ]] && break; [[ "$m" =~ ^(None|)$ && $i -ge 60 ]] && break; sleep 10
  done; echo "${m:-None}"; }
wait_server() { # VM, wanted status -> final status (stops early on ERROR)
  local s t i
  for ((i = 0; i < 600; i += 10)); do s=$(sfield "$1" status); t=$(sfield "$1" OS-EXT-STS:task_state)
    { [ "$s" = "$2" ] && [ "$t" = None ]; } && break; [ "$s" = ERROR ] && break; sleep 10
  done; echo "$s"; }
CREATED=$OUT/created.ids   # volume IDs in creation order (a file: new_vol runs in subshells)
new_vol() { # name, create arguments -> volume ID
  local id; id=$(cli "openstack volume create $2 $1 -f value -c id" | tail -1)
  [[ "$id" =~ $UUID_RE ]] || id=$(cli "openstack volume list --name $1 -f value -c ID" | head -1)
  [[ "$id" =~ $UUID_RE ]] && echo "$id" >> "$CREATED"; echo "$id"; }
new_snap() { # name, volume, extra args -> snapshot ID
  local id; id=$(cli "openstack volume snapshot create ${3:-} --volume $2 $1 -f value -c id" | tail -1)
  [[ "$id" =~ $UUID_RE ]] || id=$(cli "openstack volume snapshot list --name $1 -f value -c ID" | head -1)
  echo "$id"; }

# Operation results: "ok", "failed:<why>" or "refused:<why>" (an API capability refusal).
op_vol() { local s; s=$(wait_vol "$1"); [ "$s" = "$2" ] && echo ok || echo "failed:volume $s"; }
op_snap() { [[ "$1" =~ $UUID_RE ]] || { echo "failed:snapshot not created"; return; }
  local s; s=$(wait_snap "$1"); [ "$s" = available ] && echo ok || echo "failed:snapshot $s"; }
op_vm() { local s; s=$(wait_server "$1" "$2")
  [ "$s" = "$2" ] && echo ok || echo "failed:VM $s $(sfield "$1" fault | clean)"; }
vm_cycle() { # VM, action (stop-start | soft | hard) -> op result
  case "$2" in
    stop-start) cli "openstack server stop $1" | show >&2; local r; r=$(op_vm "$1" SHUTOFF)
      [ "$r" = ok ] || { echo "$r"; return; }
      cli "openstack server start $1" | show >&2 ;;
    soft) cli "openstack server reboot --soft $1" | show >&2 ;;
    hard) cli "openstack server reboot --hard $1" | show >&2 ;;
  esac
  op_vm "$1" ACTIVE; }
both() { [ "$1" = ok ] && echo "$2" || echo "$1"; }   # first failure of two op results
extend_detached() { # volume, GB -> op result
  cli "openstack volume set --size $2 $1" | show >&2
  local r; r=$(op_vol "$1" available); [ "$r" = ok ] || { echo "$r"; return; }
  [ "$(vsize "$1")" = "$2" ] && echo ok || echo "failed:size stays $(vsize "$1") GB"; }
extend_attached() { # volume, GB -> op result (a refusal by the API is a capability limit)
  local old o s z; old=$(vsize "$1")
  o=$(cli "openstack --os-volume-api-version $VOL_API volume set --size $2 $1"); [ -n "$o" ] && show <<<"$o" >&2
  s=$(wait_vol "$1"); z=$(vsize "$1")
  if [ "$s" = in-use ] && [ "$z" = "$2" ]; then wait_virtual "$1" $(($2 * GIB)) >/dev/null; echo ok
  elif [ "$s" = in-use ] && [ "$z" = "$old" ]; then echo "refused:in-use extend not applied, volume stays in-use at $old GB (backend reports no online extend)${o:+: $(clean <<<"$o")}"
  else echo "failed:volume $s, size $z GB"; fi; }
attach() { cli "openstack server add volume $1 $2" | show >&2; op_vol "$2" in-use; }
detach() { cli "openstack server remove volume $1 $2" | show >&2; op_vol "$2" available; }
boot_vol() { # name, boot volume, host -> VM ID
  local vm; vm=$(cli "openstack server create --flavor $FLV --volume $2 --network $NETWORK_ID --availability-zone $AZ:$3 --wait $1 -f value -c id" | tail -1)
  [[ "$vm" =~ $UUID_RE ]] || vm=$(cli "openstack server list --all-projects --name '^$1\$' -f value -c ID" | head -1)
  echo "$vm"; }
boot_image() { # name, host -> VM ID; Nova creates the boot volume on VOLUME_TYPE
  local vm; vm=$(cli "openstack --os-compute-api-version 2.67 server create --flavor $FLV --block-device source_type=image,uuid=$IMAGE_ID,destination_type=volume,volume_size=1,volume_type=$VOLUME_TYPE,boot_index=0,delete_on_termination=true --network $NETWORK_ID --availability-zone $AZ:$2 --wait $1 -f value -c id" | tail -1)
  [[ "$vm" =~ $UUID_RE ]] || vm=$(cli "openstack server list --all-projects --name '^$1\$' -f value -c ID" | head -1)
  echo "$vm"; }
vm_volumes() { # VM -> IDs of attached volumes
  cli "openstack server show $1 -f json -c volumes_attached" | python3 -c 'import json, sys
try:
    print(" ".join(v.get("id", "") for v in (json.load(sys.stdin).get("volumes_attached") or [])))
except ValueError:
    pass'; }
# PCD confirms cold migrations and resizes by itself within seconds (Nova's
# resize_confirm_window), so VERIFY_RESIZE may be over before it is seen. Wait for either,
# confirm only if still needed (a 400 or 409 means it was confirmed meanwhile), then judge by
# the result: the host or the flavor, not the states on the way. A migration Nova could not
# schedule also ends ACTIVE, on the old host.
finish_resize() { # VM -> op result once the VM is ACTIVE again
  local s t i
  for ((i = 0; i < 600; i += 10)); do s=$(sfield "$1" status); t=$(sfield "$1" OS-EXT-STS:task_state)
    [ "$t" = None ] && [[ "$s" =~ ^(VERIFY_RESIZE|ACTIVE|ERROR)$ ]] && break; sleep 10
  done
  [ "$s" = VERIFY_RESIZE ] && cli "openstack server resize confirm $1" | show >&2
  op_vm "$1" ACTIVE; }
on_host() { local h; h=$(sfield "$1" OS-EXT-SRV-ATTR:host)
  [ "$h" = "$2" ] && echo ok || echo "failed:VM on ${h:-unknown}, not $2"; }
cold_migrate() { # VM, host -> op result (the VM must end ACTIVE on that host)
  cli "openstack --os-compute-api-version 2.56 server migrate --host $2 --wait $1" | show >&2
  local r; r=$(finish_resize "$1"); [ "$r" = ok ] || { echo "$r"; return; }
  on_host "$1" "$2"; }
resize_vm() { # VM, flavor ID, flavor name -> op result (the VM must end ACTIVE on that flavor)
  cli "openstack server resize --flavor $2 --wait $1" | show >&2
  local r f; r=$(finish_resize "$1"); [ "$r" = ok ] || { echo "$r"; return; }
  f=$(sfield "$1" flavor)   # "<name> (<id>)", or original_name='<name>' from compute API 2.47
  [[ "$f" == *"$2"* || "$f" == *"'$3'"* || "$f" == "$3 "* ]] && echo ok || echo "failed:flavor still $(clean <<<"$f")"; }
live_migrate() { cli "openstack --os-compute-api-version 2.79 server migrate --live-migration --host $2 --wait $1" | show >&2
  local r; r=$(op_vm "$1" ACTIVE); [ "$r" = ok ] || { echo "$r"; return; }
  on_host "$1" "$2"; }

# Cinder record and file, read on CINDER_SSH (read-only).
volrec() { on "$CINDER_SSH" "cd $RDIR && (set +u; set -a; . $CINDER_VENV/cinder.env; set +a; $CINDER_VENV/bin/python volume_record.py --conf-dir $CINDER_CONF_DIR $*)" | grep '^VOL'; }
field() { sed -nE "s/.* $1=([^ ]+).*/\1/p" <<<"$2"; }
fpath() { local l; l=$(volrec "$1"); [ -n "$l" ] || return 0
  on "$CINDER_SSH" "m=\$(findmnt -n -o TARGET -S '$(field share "$l")' | grep '$CINDER_VENV' | head -1); echo \$m/volume-$(field name_id "$l")" | tail -1; }
# Active file of a volume -> "<raw|qcow2|qcow2-overlay|missing> <virtual bytes>". Follows the
# .info file Cinder's NFS code keeps for snapshot chains, if any; qcow2-overlay = qcow2 with
# a backing file. JSON parsed as a whole: newer qemu-img lists child nodes first.
fileinfo() { local p; p=$(fpath "$1"); [ -n "$p" ] || { echo "missing 0"; return 0; }
  on "$CINDER_SSH" "PY=$CINDER_VENV/bin/python; p='$p'
    if [ -f \"\$p.info\" ]; then a=\$(\$PY -c 'import json, sys; print(json.load(open(sys.argv[1])).get(\"active\", \"\"))' \"\$p.info\" 2>/dev/null); [ -n \"\$a\" ] && p=\$(dirname \"\$p\")/\$a; fi
    [ -e \"\$p\" ] || { echo missing 0; exit; }
    if [ \"\$(head -c4 \"\$p\" | od -An -tx1 | tr -d ' ')\" = 514649fb ]; then
      qemu-img info -U -f qcow2 --output=json \"\$p\" | \$PY -c 'import json, sys
d = json.load(sys.stdin)
print(\"qcow2-overlay\" if d.get(\"backing-filename\") else \"qcow2\", d.get(\"virtual-size\", 0))'
    else echo raw \$(stat -c %s \"\$p\"); fi" | tail -1; }
wait_virtual() { # volume, bytes -> last fileinfo (Nova applies an in-use qcow2 resize later)
  local f i; for ((i = 0; i < 180; i += 10)); do f=$(fileinfo "$1"); [ "${f##* }" = "$2" ] && break; sleep 10; done; echo "$f"; }
compute_ssh() { case "$1" in "$SRC_COMPUTE_ID") echo "$SRC_COMPUTE_SSH" ;; "${DST_COMPUTE_ID:-}") echo "$DST_COMPUTE_SSH" ;; esac; }
libvirt_of() { # VM, file name id -> driver type libvirt opens it with, "-" if not attached
  local h s t; h=$(sfield "$1" OS-EXT-SRV-ATTR:host); s=$(compute_ssh "$h")
  [ -n "$s" ] || { echo unchecked; return 0; }
  t=$(on "$s" "virsh dumpxml $1" | awk -v n="volume-$2" '/<driver name=.qemu. type=/ {match($0, /type=.[a-z0-9]+./); t = substr($0, RSTART + 6, RLENGTH - 7)} index($0, n) && /source file/ {print t; exit}')
  echo "${t:--}"; }

# One matrix row. row <#> <operation> <op result> <volume> [<vm>] [<GB the disk must have>] [<note>]
ROWS=(); RC=0; NPASS=0; NFAIL=0; NNA=0
row() {
  local num=$1 op=$2 opres=$3 vol=$4 vm=${5:-} want=${6:-} note=${7:-}
  local l rec nid info fmt virt base lv=- res why='' verdict=PASS
  l=$(volrec "$vol"); rec=$(field record "$l"); nid=$(field name_id "$l")
  case "$rec" in "" | None) rec=none ;; esac
  info=$(fileinfo "$vol"); fmt=${info%% *}; virt=${info##* }; base=${fmt%-overlay}
  [ -n "$vm" ] && lv=$(libvirt_of "$vm" "${nid:-$vol}")
  case "$opres" in
    failed:*) res=failed; why=${opres#failed:} ;;
    refused:*) res=n/a; why=${opres#refused:} ;;
    *) if [ "$fmt" = missing ]; then res=failed; why="volume file not found"
       elif [ "$rec" != none ] && [ "$rec" != "$base" ]; then res="record drift"
       elif [ "$lv" != - ] && [ "$lv" != unchecked ] && [ "$lv" != "$base" ]; then res="libvirt mismatch"
       elif [ -n "$want" ] && [ "$virt" != $((want * GIB)) ]; then res="silent failure"; why="Cinder $want GB, disk $virt bytes"
       else res=match; fi ;;
  esac
  case "$res" in match) NPASS=$((NPASS + 1)) ;; n/a) verdict=N/A; NNA=$((NNA + 1)) ;; *) verdict=FAIL; RC=1; NFAIL=$((NFAIL + 1)) ;; esac
  [ -n "$why" ] && why=$(clean <<<"$why")
  note="$why${why:+${note:+; }}$note"
  ROWS+=("| $num | $op | $fmt | $rec | $lv | $res | $note |")
  echo "  ROW $num $op: file=$fmt record=$rec libvirt=$lv -> $res${note:+ ($note)} [$verdict]"
}
not_run() { # #, operation, why: a row that could not run because an earlier step failed
  ROWS+=("| $1 | $2 | - | - | - | not run | $3 |"); RC=1; NFAIL=$((NFAIL + 1)); echo "  ROW $1 $2: not run ($3) [FAIL]"; }
na_row() { # #, operation, why: a row the lab cannot run (missing host in lab.env)
  ROWS+=("| $1 | $2 | - | - | - | n/a | $3 |"); NNA=$((NNA + 1)); echo "  ROW $1 $2: n/a ($3) [N/A]"; }

# Data pattern for the spare volumes of the flip test (qemu-io on CINDER_SSH, only on this
# run's own detached volumes): 0x5a in both halves of 1 GiB.
fill() { on "$CINDER_SSH" "qemu-io -f $2 -c 'write -P 0x5a 0 512M' -c 'write -P 0x5a 512M 512M' '$1'" >/dev/null; }
check_fill() { # file path, raw|qcow2 -> "check_rc=<n> reads=<n>/2 pattern_failures=<n>" (raw has no metadata: check_rc=-)
  on "$CINDER_SSH" "if [ '$2' = qcow2 ]; then qemu-img check -U -f qcow2 '$1' >/dev/null 2>&1; echo check_rc=\$?; else echo check_rc=-; fi
    o=\$(qemu-io -r -U -f '$2' -c 'read -P 0x5a 0 512M' -c 'read -P 0x5a 512M 512M' '$1' 2>&1)
    echo reads=\$(echo \"\$o\" | grep -c '^read 536870912/536870912 bytes')/2 pattern_failures=\$(echo \"\$o\" | grep -c 'Pattern verification failed')" | paste -sd' ' -; }
fill_ok() { [[ "$1" =~ ^check_rc=(0|-)\ reads=2/2\ pattern_failures=0$ ]]; }
# Whether Nova's NFS volume code probes the file format on that compute host (Platform9's
# patch PCD-4550, in Aug releases; not in Dec): yes, no or unknown.
nova_probe() {
  [ -n "$1" ] || { echo unknown; return 0; }
  on "$1" "f=\$(find /opt/pf9 /usr/lib/python3/dist-packages -path '*nova/virt/libvirt/volume/nfs.py' 2>/dev/null | head -1)
    if [ -z \"\$f\" ]; then echo unknown; elif grep -q qemu_img_info \"\$f\"; then echo yes; else echo no; fi" | tail -1; }
uses_dst() { [ "$MODE" = two-host ] || { [ "$MODE" = flip-check ] && [ -n "${DST_COMPUTE_ID:-}" ]; }; }

others() { # every server, volume and snapshot not created by this run
  cli "openstack server list --all-projects -f value -c ID -c Name -c Status" | awk -v p="^$PREFIX-" '$2 !~ p {print "server", $1, $NF}' | sort
  cli "openstack volume list --all-projects -f value -c ID -c Name -c Status" | awk -v p="^$PREFIX-" '$2 !~ p {print "volume", $1, $NF}' | sort
  cli "openstack volume snapshot list --all-projects -f value -c ID -c Name -c Status" | awk -v p="^$PREFIX-" '$2 !~ p {print "snapshot", $1, $NF}' | sort; }
ours() { cli "openstack server list --all-projects -f value -c Name; openstack volume list --all-projects -f value -c Name; openstack volume snapshot list --all-projects -f value -c Name; openstack flavor list --all -f value -c Name" | grep -c "^$PREFIX-"; }
running_version() { # driver_version the scheduler reports for pools of BACKEND (needs python-cinderclient)
  cli "cinder get-pools --detail" | awk -F'|' -v b="@$BACKEND#" '
    {k = $2; v = $3; gsub(/ /, "", k); gsub(/^ +| +$/, "", v)}
    k == "driver_version" {d = v} k == "name" {n = v}
    /^\+/ && n != "" {if (index(n, b)) print d; n = ""; d = ""}' | sort -u | paste -sd' ' -; }
option_on() { # ssh host -> effective vmstore_qcow2_volumes for [BACKEND]; reads only that option
  on "$1" "bash -s -- '$CINDER_CONF_DIR' '$BACKEND'" <<'EOF' | tail -1
d=$1; b=$2; s=; sec=; def=
for f in "$d"/*.conf; do   # oslo.config reads a config dir in sorted order; the last value wins
  [ -f "$f" ] || continue
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      \[*) s=${line#[}; s=${s%%]*}; s=$(printf '%s' "$s" | tr -d '[:space:]') ;;
      *=*) k=$(printf '%s' "${line%%=*}" | tr -d '[:space:]')
           [ "$k" = vmstore_qcow2_volumes ] || continue
           v=$(printf '%s' "${line#*=}" | tr -d '[:space:]')
           [ "$s" = "$b" ] && sec="$v ($(basename "$f"))"
           [ "$s" = backend_defaults ] && def="$v ($(basename "$f"), backend_defaults)" ;;
    esac
  done < "$f"
done
echo "${sec:-${def:-not set (driver default False)}}"
EOF
}
driver_started() { # ssh host -> version from the last "Starting volume driver VmstoreNfsDriver (x)" log line
  on "$1" "grep -a 'Starting volume driver VmstoreNfsDriver' $CINDER_LOG 2>/dev/null | tail -1" | sed -nE 's/^([0-9-]+ [0-9:]+).*VmstoreNfsDriver \(([^)]*)\).*/\2 (started \1)/p' | tail -1; }

OPTS=; DRV=; BACKEND=; QON=; QOFF=
precheck() {
  local bad=0 svc h o v
  echo "### precheck $(ts) mode $MODE"
  [ "$(cli "openstack token issue -f value -c id >/dev/null && echo ok")" = ok ] || { echo "  CLI or credentials not working on $CLI_SSH"; exit 2; }
  [[ "$(cli "openstack --os-volume-api-version $VOL_API volume type show $VOLUME_TYPE -f value -c id" | tail -1)" =~ $UUID_RE ]] ||
    { echo "  volume type $VOLUME_TYPE not found, or the CLI or Cinder does not accept API $VOL_API"; bad=1; }
  BACKEND=$(cli "openstack volume type show $VOLUME_TYPE -f json -c properties" | python3 -c 'import json, sys; print((json.load(sys.stdin).get("properties") or {}).get("volume_backend_name", ""))' 2>/dev/null)
  BACKEND=${BACKEND:-$VOLUME_TYPE}
  svc=$(cli "openstack volume service list --service cinder-volume -f value -c Host -c Status -c State" | grep "@$BACKEND enabled up$")
  if [ -n "$svc" ]; then echo "  cinder-volume enabled and up for $BACKEND: $(awk '{print $1}' <<<"$svc" | paste -sd' ' -)"
  else echo "  no cinder-volume service *@$BACKEND enabled and up"; bad=1; fi
  if [ "$MODE" = two-host ] || { [ "$MODE" = flip-check ] && [ -n "${VOLUME_HOSTS:-}" ]; }; then
    for h in $VOLUME_HOSTS; do grep -q "^$h@$BACKEND enabled up$" <<<"$svc" || { echo "  cinder-volume $h@$BACKEND is not enabled and up"; bad=1; }; done
  fi
  o=$(cli "openstack compute service list --service nova-compute -f value -c Host -c Status -c State")
  v=$SRC_COMPUTE_ID; uses_dst && v="$v $DST_COMPUTE_ID"
  for h in $v; do
    grep -q "^$h enabled up$" <<<"$o" || { echo "  nova-compute $h is not enabled and up"; bad=1; }
  done
  [ "$(cli "openstack image show $IMAGE_ID -f value -c status" | tail -1)" = active ] || { echo "  image $IMAGE_ID not active"; bad=1; }
  [ "$(cli "openstack network show $NETWORK_ID -f value -c status" | tail -1)" = ACTIVE ] || { echo "  network $NETWORK_ID not ACTIVE"; bad=1; }
  case "$MODE" in
    flip-after | flip-check | flip-cleanup) ;;
    *) o=$(ours); [ "$o" = 0 ] || { echo "  $o objects named $PREFIX-* already exist: clean them up or change PREFIX"; bad=1; } ;;
  esac
  [ -n "$(on "$CINDER_SSH" "command -v qemu-img && test -x $CINDER_VENV/bin/python && test -f $CINDER_VENV/cinder.env && echo ok" | grep -x ok)" ] ||
    { echo "  $CINDER_SSH lacks qemu-img, $CINDER_VENV/bin/python or $CINDER_VENV/cinder.env"; bad=1; }
  case "$MODE" in flip-create | flip-check)
    [ -n "$(on "$CINDER_SSH" "command -v qemu-io && echo ok" | grep -x ok)" ] || { echo "  $CINDER_SSH lacks qemu-io (data pattern of the spare volumes)"; bad=1; } ;;
  esac
  for h in "$SRC_COMPUTE_SSH" "$(uses_dst && echo "$DST_COMPUTE_SSH")"; do
    [ -n "$h" ] || continue
    [ -n "$(on "$h" "command -v virsh && echo ok" | grep -x ok)" ] || { echo "  $h has no virsh"; bad=1; }
    o=$(nova_probe "$h"); echo "  nova on $h: file format probe (PCD-4550) $o"; PROBE="${PROBE:+$PROBE; }$o"
  done
  [ -n "$SRC_COMPUTE_SSH" ] || echo "  SRC_COMPUTE_SSH not set: the libvirt column reads 'unchecked'"
  if uses_dst && [ "$LIVE" = 1 ] && [ -n "$SRC_COMPUTE_SSH" ] && [ -n "$DST_COMPUTE_SSH" ]; then
    v=$(on "$SRC_COMPUTE_SSH" "qemu-system-x86_64 -machine help" | awk '/\(default\)/ {print $1; exit}')
    on "$DST_COMPUTE_SSH" "qemu-system-x86_64 -machine help" | awk '{print $1}' | grep -qx "${v:-none}" ||
      { echo "  DST QEMU lacks machine type ${v:-?} used by SRC guests: live migration cannot work (run without LIVE=1)"; bad=1; }
  fi
  echo "  driver and option on each host serving $BACKEND:"
  for h in $LOG_SSH; do
    o=$(option_on "$h"); v=$(driver_started "$h")
    echo "    $h: vmstore_qcow2_volumes=$o; driver ${v:-unknown (no start line in $CINDER_LOG)}"
    OPTS="${OPTS:+$OPTS; }${o%% *}"; DRV="${DRV:+$DRV; }${v%% *}"
    case "${o%% *}" in true | True | TRUE | 1 | yes) QON=1 ;; *) QOFF=1 ;; esac
  done
  v=$(running_version); echo "  driver_version reported by cinder-volume pools: ${v:-unknown (cinder get-pools not available)}"
  [ -n "$v" ] && DRV=$v
  [ -n "$QON" ] && [ -n "$QOFF" ] && echo "  WARNING: hosts serving $BACKEND disagree on vmstore_qcow2_volumes"
  [ $bad = 0 ] || exit 2
  echo "  ok"
}

KEEP=0; FLV=; FLV2=; PROBE=; CREATE_OPTS=; CREATE_DRV=
delete_ours() { # every <PREFIX>-* server, volume, snapshot and flavor
  # Volumes newest first (clones and volumes from snapshots before their sources), then the
  # snapshots: VMstore refuses to delete a snapshot a clone still uses, and drivers before
  # 3.0.12 then report it deleted while the array keeps it. A volume that still has
  # snapshots is refused by Cinder and deleted in the next round.
  local ids id s a order
  for id in $(cli "openstack server list --all-projects -f value -c ID -c Name" | awk -v p="^$PREFIX-" '$2 ~ p {print $1}'); do
    cli "openstack server delete --wait $id" | show; echo "  server $id deleted"
  done
  for _ in 1 2 3; do
    ids=$(cli "openstack volume list --all-projects -f value -c ID -c Name" | awk -v p="^$PREFIX-" '$2 ~ p {print $1}')
    order=$( { [ -f "$CREATED" ] && awk '{a[NR] = $0} END {for (i = NR; i > 0; i--) print a[i]}' "$CREATED"; echo "$ids"; } |
      awk -v keep="$(echo $ids)" 'BEGIN {n = split(keep, k, " "); for (i = 1; i <= n; i++) ok[k[i]] = 1} ok[$1] && !seen[$1]++')
    for id in $order; do s=$(wait_vol "$id")
      if [[ "$s" =~ ^(in-use|attaching|reserved)$ ]]; then
        for a in $(cli "openstack --os-volume-api-version 3.27 volume attachment list --all-projects -f value -c ID -c 'Volume ID'" | awk -v v="$id" '$2 == v {print $1}'); do
          echo "  removing leftover attachment $a of $id"; cli "openstack --os-volume-api-version 3.27 volume attachment delete $a" | show
        done; s=$(wait_vol "$id")
      fi
      [[ "$s" == error* || "$s" =~ ^(in-use|attaching|reserved|maintenance)$ ]] && { echo "  $id was $s, resetting to available (test volume)"; cli "openstack volume set --state available $id"; }
      cli "openstack volume delete $id" | show; echo "  volume $id: $(wait_vol "$id")"
    done
    ids=$(cli "openstack volume snapshot list --all-projects -f value -c ID -c Name" | awk -v p="^$PREFIX-" '$2 ~ p {print $1}')
    for id in $ids; do s=$(wait_snap "$id")
      [[ "$s" == error* ]] && cli "openstack volume snapshot set --state available $id"
      cli "openstack volume snapshot delete $id" | show; echo "  snapshot $id: $(wait_snap "$id")"
    done
    [ "$(cli "openstack volume list --all-projects -f value -c Name" | grep -c "^$PREFIX-")" = 0 ] && break
  done
  for id in $(cli "openstack flavor list --all -f value -c ID -c Name" | awk -v p="^$PREFIX-" '$2 ~ p {print $1}'); do
    cli "openstack flavor delete $id" | show; echo "  flavor $id deleted"
  done
  echo "  left: $(ours) objects named $PREFIX-*"; }
write_matrix() { # title -> $OUT/matrix.md (and the run log)
  local f=$OUT/matrix.md r
  {
    echo "# NFS volume format matrix on VMstore: $1"
    echo
    echo "- Date: $(date -u '+%Y-%m-%d %H:%M') UTC${PCD_RELEASE:+, release: $PCD_RELEASE}"
    echo "- Tintri driver: ${DRV:-unknown}"
    echo "- vmstore_qcow2_volumes on the hosts serving the backend: ${OPTS:-unknown}"
    echo "- Nova file format probe (PCD-4550) on the compute hosts: ${PROBE:-unknown}"
    [ -n "$CREATE_DRV" ] && echo "- Kept volumes created with: driver $CREATE_DRV, vmstore_qcow2_volumes $CREATE_OPTS"
    [ -n "$LABEL" ] && echo "- Label: $LABEL"
    echo "- Result: $NPASS match, $NFAIL failed or drifted, $NNA n/a"
    echo
    echo "Cells: real format of the active file / format Cinder records (none = no record) / format libvirt opens (- = not attached). Result: match, record drift (record differs from the file), libvirt mismatch (libvirt type differs from the file), silent failure (Cinder size and disk size differ after an extend), failed (the operation failed), n/a (the API refused an operation the driver does not support)."
    echo
    echo "| # | Operation | Real file | Recorded | libvirt | Result | Notes |"
    echo "| --- | --- | --- | --- | --- | --- | --- |"
    for r in ${ROWS[@]+"${ROWS[@]}"}; do echo "$r"; done
  } > "$f"
  echo "=== matrix ($f)"; cat "$f"; }
cleanup() {
  echo "=== cleanup $(ts)"
  if [ "$KEEP" = 1 ] && [ "$MODE" = flip-create ]; then
    echo "  phase create succeeded: objects kept for --flip-phase check or after (state $STATE)"
  elif [ "$KEEP" = 1 ]; then
    echo "  objects kept for the next check (state $STATE); delete them with --flip-phase cleanup"
  else
    delete_ours
    case "$MODE" in flip-after | flip-cleanup) echo "  state $STATE consumed"; mv "$STATE" "$STATE.done" 2>/dev/null ;; esac
  fi
  on "$CINDER_SSH" "rm -rf $RDIR"
  if [ "$MODE" != flip-create ]; then
    others > "$OUT/others-after.txt"
    if diff -q "$BEFORE" "$OUT/others-after.txt" >/dev/null; then echo "  other servers, volumes and snapshots: unchanged"
    else echo "  other servers, volumes or snapshots changed during the run (not by this script; review):"; diff "$BEFORE" "$OUT/others-after.txt" | sed 's/^/    /' | head -20; fi
  fi
  echo "=== summary: $NPASS match, $NFAIL failed or drifted, $NNA n/a"
  echo "  log: $OUT/run.log"
}

# ---------------------------------------------------------------- setup
case "$MODE" in flip-after | flip-check | flip-cleanup)
  [ -f "$STATE" ] || { echo "no state file $STATE: run --flip-phase create first (same PREFIX and TMPDIR)"; exit 2; }
  # shellcheck disable=SC1090
  . "$STATE"
  # Volumes of every earlier phase, in creation order, so cleanup deletes clones first.
  [ "$MODE" = flip-check ] || cat "$FLIPDIR"/*/created.ids 2>/dev/null > "$OUT/created.ids" ;;
esac
precheck
if [ "$MODE" = flip-after ]; then BEFORE=$FLIPDIR/others-before.txt
else BEFORE=$OUT/others-before.txt; others > "$BEFORE"; fi
on "$CINDER_SSH" "mkdir -p $RDIR" && scp -q -o BatchMode=yes "$HERE/volume_record.py" "$CINDER_SSH:$RDIR/volume_record.py" || { echo "helper copy failed"; exit 2; }
# shellcheck disable=SC2154  # rc is assigned inside the trap string
trap 'rc=$?; [ $rc = 0 ] && rc=$RC; cleanup; exit $rc' EXIT
if [ "${MODE#flip-}" = "$MODE" ] || [ "$MODE" = flip-create ]; then
  FLV=$(cli "openstack flavor create --ram $FLAVOR_RAM_MB --vcpus 1 --disk 0 --private --project $PROJECT $PREFIX-flavor -f value -c id" | tail -1)
  [[ "$FLV" =~ ^[0-9a-zA-Z-]+$ ]] || { echo "  flavor create failed"; exit 2; }
fi

# ---------------------------------------------------------------- one host, steps 1 to 19
single() {
  local E I SI V5 V6 VM1 SB VM2 N r r2
  echo "### $(ts) steps 1 to 7: volumes, detached"
  E=$(new_vol "$PREFIX-empty" "--size 1 --type $VOLUME_TYPE"); row 1 "Create empty volume" "$(op_vol "$E" available)" "$E"
  I=$(new_vol "$PREFIX-image" "--image $IMAGE_ID --size 1 --type $VOLUME_TYPE"); row 2 "Create from image" "$(op_vol "$I" available)" "$I"
  row 3 "Extend, detached (1 to 2 GB)" "$(extend_detached "$I" 2)" "$I" "" 2
  SI=$(new_snap "$PREFIX-snap-offline" "$I"); row 4 "Snapshot, offline" "$(op_snap "$SI")" "$I"
  V5=$(new_vol "$PREFIX-from-snap" "--snapshot $SI --size 3"); row 5 "Create from snapshot (+1 GB)" "$(op_vol "$V5" available)" "$V5" "" 3
  V6=$(new_vol "$PREFIX-clone" "--source $I --size 3"); row 6 "Clone (+1 GB)" "$(op_vol "$V6" available)" "$V6" "" 3
  row 7 "Extend, detached, with a snapshot (2 to 3 GB)" "$(extend_detached "$I" 3)" "$I" "" 3

  echo "### $(ts) steps 8 to 17: VM booted from the clone"
  VM1=$(boot_vol "$PREFIX-vm1" "$V6" "$SRC_COMPUTE_ID"); r=$(op_vm "${VM1:-none}" ACTIVE)
  row 8 "Boot VM from the clone" "$r" "$V6" "$VM1"
  if [ "$r" != ok ]; then
    for s in "9 Attach empty volume" "10 Snapshot, online (boot volume)" "11 Stop / start" "12 Soft reboot" "13 Hard reboot" \
             "14 Extend, attached" "15 Delete online snapshot" "16 Detach" "17 Stop / start after all changes"; do not_run "${s%% *}" "${s#* }" "VM did not boot"; done
  else
    row 9 "Attach empty volume" "$(attach "$VM1" "$E")" "$E" "$VM1"
    SB=$(new_snap "$PREFIX-snap-online" "$V6" --force); row 10 "Snapshot, online (boot volume)" "$(op_snap "$SB")" "$V6" "$VM1"
    r=$(vm_cycle "$VM1" stop-start); row 11 "Stop / start" "$r" "$V6" "$VM1"; row 11b "Stop / start (empty volume)" "$r" "$E" "$VM1"
    r=$(vm_cycle "$VM1" soft); row 12 "Soft reboot" "$r" "$V6" "$VM1"; row 12b "Soft reboot (empty volume)" "$r" "$E" "$VM1"
    r=$(vm_cycle "$VM1" hard); row 13 "Hard reboot" "$r" "$V6" "$VM1"; row 13b "Hard reboot (empty volume)" "$r" "$E" "$VM1"
    r=$(extend_attached "$E" 2); if [ "$r" = ok ]; then row 14 "Extend, attached (1 to 2 GB)" "$r" "$E" "$VM1" 2
    else row 14 "Extend, attached (1 to 2 GB)" "$r" "$E" "$VM1"; fi
    cli "openstack volume snapshot delete $SB" | show; r=$(wait_snap "$SB")
    [ "$r" = deleted ] && r=ok || r="failed:snapshot $r"; row 15 "Delete online snapshot" "$r" "$V6" "$VM1"
    row 16 "Detach" "$(detach "$VM1" "$E")" "$E"
    row 17 "Stop / start after all changes" "$(vm_cycle "$VM1" stop-start)" "$V6" "$VM1"
    cli "openstack server delete --wait $VM1" | show   # frees the host's memory for step 18
  fi

  echo "### $(ts) steps 18 and 19: boot from image, Nova creates the volume"
  VM2=$(boot_image "$PREFIX-vm2" "$SRC_COMPUTE_ID"); r=$(op_vm "${VM2:-none}" ACTIVE)
  N=$(vm_volumes "${VM2:-none}" | awk '{print $1}')
  if [ -z "$N" ]; then not_run 18 "Boot from image, Nova creates the volume" "no volume: $r"; not_run 19 "Stop / start, hard reboot that VM" "step 18 failed"; return; fi
  cli "openstack volume set --name $PREFIX-nova-vol $N" | show; echo "$N" >> "$CREATED"
  row 18 "Boot from image, Nova creates the volume" "$r" "$N" "$VM2"
  if [ "$r" != ok ]; then not_run 19 "Stop / start, hard reboot that VM" "VM did not boot"; return; fi
  r=$(vm_cycle "$VM2" stop-start); r2=$(vm_cycle "$VM2" hard)
  row 19 "Stop / start, hard reboot that VM" "$(both "$r" "$r2")" "$N" "$VM2"
}

# ---------------------------------------------------------------- two hosts
two_host() {
  local VM N D cur dst h m r r2
  echo "### $(ts) two hosts: SRC $SRC_COMPUTE_ID, DST $DST_COMPUTE_ID"
  FLV2=$(cli "openstack flavor create --ram $FLAVOR_RAM_MB --vcpus 2 --disk 0 --private --project $PROJECT $PREFIX-flavor2 -f value -c id" | tail -1)
  VM=$(boot_image "$PREFIX-vm" "$SRC_COMPUTE_ID"); r=$(op_vm "${VM:-none}" ACTIVE)
  N=$(vm_volumes "${VM:-none}" | awk '{print $1}')
  [ -n "$N" ] || { not_run T1 "Boot from image on SRC (Nova-created volume)" "no volume: $r"; return; }
  cli "openstack volume set --name $PREFIX-nova-vol $N" | show; echo "$N" >> "$CREATED"
  row T1 "Boot from image on SRC (Nova-created volume)" "$r" "$N" "$VM"
  [ "$r" = ok ] || return
  D=$(new_vol "$PREFIX-data" "--image $IMAGE_ID --size 1 --type $VOLUME_TYPE"); row T2 "Create data volume from image" "$(op_vol "$D" available)" "$D"
  cur=$(vhost "$D"); dst=
  for h in $VOLUME_HOSTS; do [ "$h" != "${cur%%@*}" ] && dst="$h@${cur#*@}" && break; done
  echo "  migrate $D: $cur -> $dst"; cli "openstack volume migrate --host '$dst' $D" | show
  m=$(wait_mig "$D"); r=ok; [ "$m" = success ] && [ "$(vstat "$D")" = available ] || r="failed:migration $m, volume $(vstat "$D")"
  row T3 "Migrate data volume to the other cinder-volume host" "$r" "$D" "" "" "now on $(vhost "$D" | cut -d'#' -f1 | cut -d@ -f1)"
  row T4 "Attach it to the VM" "$(attach "$VM" "$D")" "$D" "$VM"
  if [ "$LIVE" = 1 ]; then r=$(live_migrate "$VM" "$DST_COMPUTE_ID"); m="Live migrate VM to DST"
  else r=$(cold_migrate "$VM" "$DST_COMPUTE_ID"); m="Cold migrate VM to DST"; fi
  row T5 "$m" "$r" "$N" "$VM"; row T5b "$m (data volume)" "$r" "$D" "$VM"
  r=$(vm_cycle "$VM" stop-start); r2=$(vm_cycle "$VM" hard)
  row T6 "Stop / start, hard reboot on DST" "$(both "$r" "$r2")" "$N" "$VM"; row T6b "Stop / start, hard reboot on DST (data volume)" "$(both "$r" "$r2")" "$D" "$VM"
  r=$(detach "$VM" "$D"); [ "$r" = ok ] && r=$(extend_detached "$D" 2)
  row T7 "Detach, extend the migrated volume (1 to 2 GB)" "$r" "$D" "" 2
  r=$(cold_migrate "$VM" "$SRC_COMPUTE_ID")
  row T8 "Cold migrate back to SRC" "$r" "$N" "$VM"
  if [[ "$FLV2" =~ ^[0-9a-zA-Z-]+$ ]]; then r=$(resize_vm "$VM" "$FLV2" "$PREFIX-flavor2")
  else r="failed:flavor $PREFIX-flavor2 not created"; fi
  row T9 "Resize the VM (2 vCPUs)" "$r" "$N" "$VM"
}

# ---------------------------------------------------------------- flip test
flip_create() {
  local B E VM D r i v p f c IMGS="" QS=""
  [ -n "$QON" ] && [ -z "$QOFF" ] || [ "$FORCE" = 1 ] || { echo "  phase create needs vmstore_qcow2_volumes on for $BACKEND (FORCE=1 overrides)"; exit 2; }
  echo "### $(ts) flip phase create (option on)"
  B=$(new_vol "$PREFIX-flip-boot" "--image $IMAGE_ID --size 1 --type $VOLUME_TYPE --bootable"); r=$(op_vol "$B" available)
  E=$(new_vol "$PREFIX-flip-empty" "--size 1 --type $VOLUME_TYPE")
  row C1 "Create empty volume (option on)" "$(op_vol "$E" available)" "$E"
  [ "$r" = ok ] || { not_run C2 "Boot VM from an image volume" "boot volume $r"; return; }
  VM=$(boot_vol "$PREFIX-flip-vm" "$B" "$SRC_COMPUTE_ID"); r=$(op_vm "${VM:-none}" ACTIVE)
  row C2 "Boot VM from an image volume" "$r" "$B" "$VM"; [ "$r" = ok ] || return
  r=$(attach "$VM" "$E"); row C3 "Attach the empty volume" "$r" "$E" "$VM"
  # Keep the objects whenever the operations worked: a format verdict (C2 drifts on drivers
  # before 3.0.12, which record qcow2 for image volumes) is a finding, not a reason to stop.
  [ "$r" = ok ] || return
  # For phase check: a data volume from an image on the VM, and spare detached volumes, one
  # of each kind per check: image volumes (detached extend) and empty volumes holding a data
  # pattern (move to the other cinder-volume host).
  D=$(new_vol "$PREFIX-flip-data" "--image $IMAGE_ID --size 1 --type $VOLUME_TYPE"); r=$(op_vol "$D" available)
  [ "$r" = ok ] && r=$(attach "$VM" "$D")
  row C4 "Attach a data volume created from an image" "$r" "$D" "$VM"; [ "$r" = ok ] || D=
  for ((i = 1; i <= FLIP_SPARES; i++)); do
    v=$(new_vol "$PREFIX-flip-img-$i" "--image $IMAGE_ID --size 1 --type $VOLUME_TYPE")
    [ "$(op_vol "$v" available)" = ok ] && IMGS="$IMGS $v"
    v=$(new_vol "$PREFIX-flip-q-$i" "--size 1 --type $VOLUME_TYPE")
    [ "$(op_vol "$v" available)" = ok ] || continue
    p=$(fpath "$v"); f=$(fileinfo "$v"); fill "$p" "${f%% *}"; c=$(check_fill "$p" "${f%% *}")
    if fill_ok "$c"; then QS="$QS $v"; else echo "  $v: pattern did not verify ($c)"; fi
  done
  IMGS=${IMGS# }; QS=${QS# }
  if [ -n "$IMGS" ]; then row C5 "Detached image volumes for the checks ($(wc -w <<<"$IMGS" | tr -d ' '))" ok "${IMGS%% *}"
  else not_run C5 "Detached image volumes for the checks" "none became available"; fi
  if [ -n "$QS" ]; then row C6 "Detached empty volumes with a data pattern for the checks ($(wc -w <<<"$QS" | tr -d ' '))" ok "${QS%% *}"
  else not_run C6 "Detached empty volumes with a data pattern for the checks" "none became available or verified"; fi
  {
    echo "VM=$VM"; echo "B=$B"; echo "E=$E"; echo "D=$D"; echo "IMGS='$IMGS'"; echo "QS='$QS'"; echo "FLV=$FLV"
    echo "CREATE_OPTS='$OPTS'"; echo "CREATE_DRV='$DRV'"
    printf 'CREATE_ROWS=(); '; for r in ${ROWS[@]+"${ROWS[@]}"}; do printf 'CREATE_ROWS+=(%q); ' "$r"; done; echo
  } > "$STATE"
  cp "$BEFORE" "$FLIPDIR/others-before.txt"
  KEEP=1
  echo "  kept: VM $VM, boot volume $B, empty volume $E, data volume ${D:-none}, spares: $IMGS $QS (state $STATE)"
  echo "  next: switch $BACKEND to vmstore_qcow2_volumes = false on every host serving it and restart cinder-volume"
  echo "        (PCD: the cluster blueprint; elsewhere test/lab/driver-swap/swap.sh option), then:"
  echo "        $0 --flip-phase check <lab.env> (repeatable, keeps everything) or --flip-phase after (once, then deletes)"
}
flip_after() {
  local C V SE NEW r r2
  [ -n "$QOFF" ] && [ -z "$QON" ] || [ "$FORCE" = 1 ] || { echo "  phase after needs vmstore_qcow2_volumes off for $BACKEND (FORCE=1 overrides)"; trap - EXIT; on "$CINDER_SSH" "rm -rf $RDIR"; exit 2; }
  ROWS=(${CREATE_ROWS[@]+"${CREATE_ROWS[@]}"})
  printf '%s\n' "$B" "$E" >> "$CREATED"
  echo "### $(ts) flip phase after (option off; created with: $CREATE_OPTS, driver $CREATE_DRV)"
  r=$(op_vm "$VM" ACTIVE)
  row F1 "cinder-volume restarted, VM keeps running" "$r" "$B" "$VM"; row F1b "cinder-volume restarted (old empty volume)" "$r" "$E" "$VM"
  [ "$r" = ok ] || return
  r=$(vm_cycle "$VM" stop-start); r2=$(vm_cycle "$VM" hard)
  row F2 "Stop / start, hard reboot" "$(both "$r" "$r2")" "$B" "$VM"; row F2b "Stop / start, hard reboot (old empty volume)" "$(both "$r" "$r2")" "$E" "$VM"
  SE=$(new_snap "$PREFIX-flip-snap" "$E" --force); r=$(op_snap "$SE"); [ "$r" = ok ] && r=$(vm_cycle "$VM" stop-start)
  row F3 "Online snapshot of the old volume, then stop / start" "$r" "$E" "$VM"
  row F4 "Detach the old volume" "$(detach "$VM" "$E")" "$E"
  C=$(new_vol "$PREFIX-flip-clone" "--source $E --size 2"); row F5 "Clone the old volume (+1 GB)" "$(op_vol "$C" available)" "$C" "" 2
  V=$(new_vol "$PREFIX-flip-from-snap" "--snapshot $SE --size 2"); row F6 "Create from snapshot of the old volume (+1 GB)" "$(op_vol "$V" available)" "$V" "" 2
  row F7 "Extend the old volume, detached, it has a snapshot (1 to 2 GB)" "$(extend_detached "$E" 2)" "$E" "" 2
  r=$(attach "$VM" "$C"); [ "$r" = ok ] && r=$(vm_cycle "$VM" stop-start)
  row F8 "Attach the post-flip clone, stop / start" "$r" "$C" "$VM"
  NEW=$(new_vol "$PREFIX-flip-new" "--size 1 --type $VOLUME_TYPE"); row F9 "New empty volume" "$(op_vol "$NEW" available)" "$NEW"
}

# Phase check: what works while the records of volumes created before the switch are still
# what phase create left (image volumes: raw file recorded qcow2 on drivers before 3.0.12),
# for example between a driver swap or a PCD upgrade and the database fix. Repeatable: each
# run uses one spare volume of each kind and puts the VM back on its host.
next_spare() { local id; for id in $1; do grep -qx "$id" "$FLIPDIR/used" 2>/dev/null || { echo "$id"; return 0; }; done; }
reset_if_error() { local s; s=$(vstat "$1")
  [[ "$s" == error* ]] && { echo "  $1 left $s: reset to available (test volume)"; cli "openstack volume set --state available $1"; }; return 0; }
recover_vm() { # kept VM not ACTIVE after a row: reset and hard reboot it, so the next check starts from a running VM
  local s; s=$(cli "openstack server show $1 -f value -c status" | tail -1); [ "$s" = ACTIVE ] && return 0
  echo "  $1 is $s after K12: reset to active and hard reboot (test VM)"
  cli "openstack server set --state active $1" | show >&2; cli "openstack server reboot --hard $1" | show >&2
  [ "$(op_vm "$1" ACTIVE)" = ok ] || echo "  WARNING: $1 did not come back ACTIVE; the next check's K1 to K8 will show it"; }
vmrows() { # #, operation, op result: one row per volume of the VM
  row "$1" "$2 (boot volume)" "$3" "$B" "$VM"
  [ -n "$D" ] && row "${1}b" "$2 (data volume from an image)" "$3" "$D" "$VM"
  row "${1}c" "$2 (empty volume)" "$3" "$E" "$VM"; }
flip_check() {
  local n r m cur other h f t tn v x q p c dst C NEW moved
  [ -n "$QOFF" ] && [ -z "$QON" ] || [ "$FORCE" = 1 ] || { echo "  phase check needs vmstore_qcow2_volumes off for $BACKEND (FORCE=1 overrides)"; trap - EXIT; on "$CINDER_SSH" "rm -rf $RDIR"; exit 2; }
  KEEP=1; D=${D:-}; IMGS=${IMGS:-}; QS=${QS:-}
  n=$(( $(cat "$FLIPDIR/checks" 2>/dev/null || echo 0) + 1 )); echo "$n" > "$FLIPDIR/checks"
  echo "### $(ts) flip check $n${LABEL:+: $LABEL} (kept volumes created with: $CREATE_OPTS, driver $CREATE_DRV)"

  echo "### $(ts) K1 to K4: the VM and the volumes it had before the switch"
  r=$(op_vm "$VM" ACTIVE); vmrows K1 "VM running at the start of the check" "$r"
  vmrows K2 "Stop / start" "$(vm_cycle "$VM" stop-start)"
  vmrows K3 "Soft reboot" "$(vm_cycle "$VM" soft)"
  vmrows K4 "Hard reboot" "$(vm_cycle "$VM" hard)"

  echo "### $(ts) K5 to K8: move and resize the VM"
  if uses_dst; then
    cur=$(sfield "$VM" OS-EXT-SRV-ATTR:host); other=$DST_COMPUTE_ID; [ "$cur" = "$DST_COMPUTE_ID" ] && other=$SRC_COMPUTE_ID
    if [ "$LIVE" = 1 ]; then r=$(live_migrate "$VM" "$other"); m="Live migrate the VM to the other host"
    else r=$(cold_migrate "$VM" "$other"); m="Cold migrate the VM to the other host"; fi
    vmrows K5 "$m" "$r"
    vmrows K6 "Hard reboot on that host" "$(vm_cycle "$VM" hard)"
    if [ -z "$FLV2" ]; then
      FLV2=$(cli "openstack flavor create --ram $FLAVOR_RAM_MB --vcpus 2 --disk 0 --private --project $PROJECT $PREFIX-flavor2 -f value -c id" | tail -1)
      [[ "$FLV2" =~ ^[0-9a-zA-Z-]+$ ]] && echo "FLV2=$FLV2" >> "$STATE" || FLV2=
    fi
    f=$(sfield "$VM" flavor)
    if [[ "$f" == *"$PREFIX-flavor2"* || ( -n "$FLV2" && "$f" == *"$FLV2"* ) ]]; then t=$FLV; tn=$PREFIX-flavor; else t=$FLV2; tn=$PREFIX-flavor2; fi
    if [ -n "$t" ]; then r=$(resize_vm "$VM" "$t" "$tn"); else r="failed:flavor $PREFIX-flavor2 not created"; fi
    vmrows K7 "Resize the VM (vCPUs 1 <-> 2)" "$r"
    h=$(sfield "$VM" OS-EXT-SRV-ATTR:host)
    if [ "$h" = "$cur" ]; then r=ok; else r=$(cold_migrate "$VM" "$cur"); fi
    vmrows K8 "Cold migrate the VM back to its first host" "$r"
  else
    for m in "K5 Migrate the VM to the other host" "K6 Hard reboot on that host" "K7 Resize the VM" "K8 Migrate the VM back"; do
      na_row "${m%% *}" "${m#* }" "one compute host (set DST_COMPUTE_ID and DST_COMPUTE_SSH)"; done
  fi

  echo "### $(ts) K9: detached extend of an image volume created before the switch"
  v=$(next_spare "$IMGS")
  if [ -n "$v" ]; then echo "$v" >> "$FLIPDIR/used"; x=$(( $(vsize "$v") + 1 ))
    row K9 "Extend an image volume created before the switch, detached (+1 GB)" "$(extend_detached "$v" "$x")" "$v" "" "$x"
    reset_if_error "$v"
  else not_run K9 "Extend an image volume created before the switch, detached" "no spare image volume left (FLIP_SPARES)"; fi

  echo "### $(ts) K13 to K15: clone of the data volume, new volume"
  if [ -n "$D" ]; then
    C=$(new_vol "$PREFIX-flip-clone-k$n" "--source $D --size 2"); r=$(op_vol "$C" available)
    row K13 "Clone the data volume from an image (+1 GB)" "$r" "$C" "" 2
    if [ "$r" = ok ]; then
      r=$(attach "$VM" "$C"); [ "$r" = ok ] && r=$(vm_cycle "$VM" hard)
      row K14 "Attach the clone, hard reboot" "$r" "$C" "$VM"; detach "$VM" "$C" >/dev/null
    else not_run K14 "Attach the clone, hard reboot" "clone failed"; fi
  else not_run K13 "Clone the data volume from an image" "no data volume (C4 failed)"; not_run K14 "Attach the clone, hard reboot" "no data volume"; fi
  NEW=$(new_vol "$PREFIX-flip-new-k$n" "--size 1 --type $VOLUME_TYPE"); row K15 "New empty volume" "$(op_vol "$NEW" available)" "$NEW"

  # K10 to K12 run last: an attach of a damaged volume (K11) must not affect K13 to K15.
  echo "### $(ts) K10 to K12: move an empty volume created before the switch to the other cinder-volume host"
  q=$(next_spare "$QS")
  if [ -z "$q" ]; then
    for m in "K10 Move an empty volume created before the switch to the other cinder-volume host" "K11 Attach it, hard reboot" "K12 Detach it, extend detached"; do
      not_run "${m%% *}" "${m#* }" "no spare volume left (FLIP_SPARES)"; done
  elif [ "$(wc -w <<<"${VOLUME_HOSTS:-}" | tr -d ' ')" -lt 2 ]; then
    for m in "K10 Move an empty volume created before the switch to the other cinder-volume host" "K11 Attach it, hard reboot" "K12 Detach it, extend detached"; do
      na_row "${m%% *}" "${m#* }" "one cinder-volume host (set VOLUME_HOSTS)"; done
  else
    echo "$q" >> "$FLIPDIR/used"; cur=$(vhost "$q"); dst=
    for h in $VOLUME_HOSTS; do [ "$h" != "${cur%%@*}" ] && dst="$h@${cur#*@}" && break; done
    echo "  migrate $q: $cur -> $dst"; cli "openstack volume migrate --host '$dst' $q" | show
    m=$(wait_mig "$q"); r=ok; c=; moved=0
    if [ "$m" = success ] && [ "$(vstat "$q")" = available ]; then moved=1
      p=$(fpath "$q"); f=$(fileinfo "$q"); f=${f%% *}; c=$(check_fill "$p" "${f%-overlay}")
      fill_ok "$c" || r="failed:data damaged by the move"
    else r="failed:migration $m, volume $(vstat "$q")"; fi
    row K10 "Move an empty volume created before the switch to the other cinder-volume host" "$r" "$q" "" "" "${c:+data: $c}"
    # K11 and K12 run whenever the volume moved, even with damaged data: they record what an
    # attach and a detached extend do with the file as the move left it (Platform9 saw an extend
    # report the new size without growing a qcow2 file recorded raw).
    if [ "$moved" = 1 ]; then
      r=$(attach "$VM" "$q"); [ "$r" = ok ] && r=$(vm_cycle "$VM" hard)
      row K11 "Attach it, hard reboot" "$r" "$q" "$VM"
      r=$(detach "$VM" "$q"); x=
      [ "$r" = ok ] && { x=$(( $(vsize "$q") + 1 )); r=$(extend_detached "$q" "$x"); }
      row K12 "Detach it, extend detached (+1 GB)" "$r" "$q" "" "$x"; reset_if_error "$q"
      recover_vm "$VM"
    else not_run K11 "Attach it, hard reboot" "the move did not complete"; not_run K12 "Detach it, extend detached" "the move did not complete"; fi
  fi
}
flip_cleanup() { echo "### $(ts) flip cleanup: every $PREFIX-* object kept by phase create and the checks"; KEEP=0; }

case "$MODE" in
  single) single; write_matrix "one host, Platform9 steps 1 to 19" ;;
  two-host) two_host; write_matrix "two hosts, volume migrate then move the VM ($([ "$LIVE" = 1 ] && echo live || echo cold) migration)" ;;
  flip-create) flip_create; write_matrix "flip test, phase create (option on)" ;;
  flip-after) flip_after; write_matrix "flip test, created with the option on, then switched off" ;;
  flip-check) flip_check; write_matrix "flip check $(cat "$FLIPDIR/checks"): volumes created with the option on, used after the switch${LABEL:+ ($LABEL)}" ;;
  flip-cleanup) flip_cleanup ;;
esac
