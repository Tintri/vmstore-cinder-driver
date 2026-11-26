#!/usr/bin/env bash
# Volume groups and group snapshots lab test (VME-38). The driver does not implement the
# group calls, so Cinder runs its generic group code on top of the driver's single-volume
# calls. This checks that path end to end, including the VMstore side of group snapshots.
# Runbook: docs/runbooks/volume-groups-lab-test.md
#
# Usage: run.sh <lab.env>
#
# Creates only objects named <PREFIX>-* plus the unnamed volumes Cinder creates for groups
# made from a source (IDs recorded at creation), and deletes them on exit, including on
# failure or Ctrl-C. Other volumes, snapshots and groups are never modified.
# Exit: 0 all checks passed (SKIP allowed), 1 a check failed, 2 precheck or setup problem.
set -u
[ -f "${1:-}" ] || { echo "usage: $0 <lab.env>"; exit 2; }
# shellcheck disable=SC1090
. "$1"
for v in CLI_SSH CLI_RC REGION VOLUME_TYPE CINDER_SSH CINDER_VENV CINDER_CONF_DIR PREFIX; do
  [ -n "${!v:-}" ] || { echo "missing $v in $1"; exit 2; }
done

HERE=$(cd "$(dirname "$0")" && pwd)
LAB=$(cd "$HERE/.." && pwd)
RDIR=/root/$PREFIX-lab
OUT=$(mktemp -d "${TMPDIR:-/tmp}/$PREFIX-lab.XXXXXX")
exec > >(tee "$OUT/run.log") 2>&1
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15"
: > "$OUT/ids"; : > "$OUT/snaps"   # every volume and member snapshot this run created

ts() { date -u +%T; }
# ssh reads stdin unless told not to: cli and on never do, so they are safe inside read loops;
# on_in passes a script on stdin.
cli() { $SSH "$CLI_SSH" "source $CLI_RC >/dev/null 2>&1; export OS_REGION_NAME=$REGION; $*" </dev/null 2>&1 | grep -v setlocale; return 0; }
on() { local t=$1; shift; $SSH "$t" "$@" </dev/null 2>&1 | grep -v setlocale; return 0; }
on_in() { local t=$1; shift; $SSH "$t" "$@" 2>&1 | grep -v setlocale; return 0; }
C13="cinder --os-volume-api-version 3.13"; C14="cinder --os-volume-api-version 3.14"
prop() { awk -F'|' -v k="$1" '{a = $2; b = $3; gsub(/^ +| +$/, "", a); gsub(/^ +| +$/, "", b)} a == k {print b; exit}'; }
uuid() { grep -oE '[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}' | head -1; }
track() { local i; for i in "$@"; do grep -qx "$i" "$OUT/ids" || echo "$i" >> "$OUT/ids"; done; }
words() { echo $#; }   # word count of the unquoted arguments (macOS wc pads its output)

vstat() { cli "openstack volume show $1 -f value -c status 2>/dev/null"; }
vgroup() { cli "$C13 show $1" | prop group_id; }
gstat() { cli "$C13 group-show $1" | prop status; }
gsstat() { cli "$C14 group-snapshot-show $1" | prop status; }
gvols() { cli "$C13 list --all-tenants 1 --group_id $1" | awk -F'|' '{gsub(/ /, "", $2)} $2 ~ /^[0-9a-f-]{36}$/ {print $2}'; }
members() { # volume, group snapshot name -> "<id> <status>" of its member snapshot (Cinder names
  # member snapshots after the group snapshot); value columns come in the command's order
  cli "openstack volume snapshot list --all-projects --volume $1 -f value -c ID -c Name -c Status" | awk -v n="$2" '$2 == n {print $1, $3}'; }
in_cinder() { [ -n "$(cli "openstack volume snapshot show $1 -f value -c status 2>/dev/null")" ]; }
on_array() { array array-list "$1" | grep -c '^ARRAY'; }
wait_for() { # status function, id -> final status ("deleted" when gone)
  local s i
  for ((i = 0; i < 1200; i += 10)); do s=$($1 "$2")
    [[ "$s" =~ ^(creating|deleting|updating|downloading|extending)$ ]] || break; sleep 10
  done; echo "${s:-deleted}"; }

# Record and file checks on CINDER_SSH (helpers shared with the other lab suites).
volrec() { on "$CINDER_SSH" "cd $RDIR && (set +u; set -a; . $CINDER_VENV/cinder.env; set +a; $CINDER_VENV/bin/python volume_record.py --conf-dir $CINDER_CONF_DIR $*)" | grep '^VOL'; }
array() { # snapshot id -> VMstore snapshot UUIDs for it (works after Cinder deleted the record)
  on "$CINDER_SSH" "cd $RDIR && (set +u; set -a; . $CINDER_VENV/cinder.env; set +a; $CINDER_VENV/bin/python snapshot_lab.py --conf-dir $CINDER_CONF_DIR --prefix $PREFIX $*)"; }
field() { sed -nE "s/.* $1=([^ ]+).*/\1/p" <<<"$2"; }
fpath() { local l; l=$(volrec "$1"); [ -n "$l" ] || return 0
  on "$CINDER_SSH" "m=\$(findmnt -n -o TARGET -S '$(field share "$l")' | grep '$CINDER_VENV' | head -1); echo \$m/volume-$(field name_id "$l")"; }
ffmt() { on "$CINDER_SSH" "p='$1'; [ -e \$p ] || { echo missing; exit; }; x=\$(head -c4 \$p | od -An -tx1 | tr -d ' '); [ \$x = 514649fb ] && echo qcow2 || echo raw"; }
# Each volume gets its own byte pattern in two 64 MiB regions (start and end of 1 GiB).
fill() { on "$CINDER_SSH" "qemu-io -f $2 -c 'write -P 0x$3 0 64M' -c 'write -P 0x$3 960M 64M' '$1' | grep -c '^wrote 67108864/67108864'"; }
pattern() { # path, format -> the pattern both regions hold, or none
  on_in "$CINDER_SSH" "bash -s -- '$1' '$2'" <<'EOF' | tail -1
for c in 5a 3c a5 c3; do
  o=$(qemu-io -r -U -f "$2" -c "read -P 0x$c 0 64M" -c "read -P 0x$c 960M 64M" "$1" 2>&1)
  [ "$(grep -c '^read 67108864/67108864 bytes' <<<"$o")" = 2 ] && ! grep -q 'Pattern verification failed' <<<"$o" && { echo "$c"; exit; }
done
echo none
EOF
}
# The driver hands a file to Nova as raw if its record or the probe says raw, else as the
# probed format: no record means "the file's own format". A record must be absent or match.
record_ok() { [ "$1" = None ] || [ "$1" = "$2" ]; }   # record, file format
describe() { # volume id -> "record=<r|None> file=<f> pattern=<p> host=<h>"
  local l p f r; l=$(volrec "$1"); p=$(fpath "$1"); f=$(ffmt "$p"); r=$(field record "$l")
  echo "record=$r file=$f pattern=$([ "$f" = missing ] && echo none || pattern "$p" "$f") host=$(field host "$l")"; }
check_set() { # label, expected patterns ("5a 3c"), volume ids... -> 0 if the patterns match and no record contradicts its file
  local want=$2 got='' ok=0 id d; shift 2
  for id in "$@"; do d=$(describe "$id"); echo "  $id $d"
    record_ok "$(field record " $d")" "$(field file " $d")" || ok=1; got="$got $(field pattern " $d")"; done
  [ "$(tr ' ' '\n' <<<"$got" | grep . | sort | paste -sd' ' -)" = "$(tr ' ' '\n' <<<"$want" | sort | paste -sd' ' -)" ] || ok=1
  return $ok; }

others() { # every volume, snapshot and group not created by this run
  cli "openstack volume list --all-projects -f value -c ID -c Name -c Status" | awk -v p="^$PREFIX-" '$2 !~ p {print "volume", $1, $NF}' | grep -v -F -f "$OUT/ids" | sort
  cli "openstack volume snapshot list --all-projects -f value -c ID -c Name -c Status" | awk -v p="^$PREFIX-" '$2 !~ p {print "snapshot", $1, $NF}' | sort
  cli "$C13 group-list --all-tenants 1" | awk -F'|' -v p="^$PREFIX-" '{for (i = 2; i <= 4; i++) gsub(/^ +| +$/, "", $i)} $2 ~ /^[0-9a-f-]{36}$/ && $4 !~ p {print "group", $2, $3}' | sort; }
ours_groups() { cli "$C13 group-list --all-tenants 1" | awk -F'|' -v p="^$PREFIX-" '{gsub(/ /, "", $2); gsub(/^ +| +$/, "", $4)} $4 ~ p {print $2, $4}'; }
ours_gsnaps() { cli "$C14 group-snapshot-list --all-tenants 1" | awk -F'|' -v p="^$PREFIX-" '{gsub(/ /, "", $2); gsub(/^ +| +$/, "", $4)} $4 ~ p {print $2}'; }

ALL=(G1 G2 G3 G4 G5 G6 G7 G8 G9); RESULTS=(); RC=0
result() { RESULTS+=("$1 | $2 | $3"); echo "  RESULT $1: $2 ($3)"; [ "$2" = FAIL ] && RC=1; return 0; }
stop() { # reason: the remaining checks cannot run
  local t; for t in "${ALL[@]}"; do printf '%s\n' ${RESULTS[@]+"${RESULTS[@]}"} | grep -q "^$t |" || result "$t" SKIP "not run: $1"; done
  exit 1; }

precheck() {
  local bad=0 svc s
  echo "### precheck $(ts)"
  [ "$(cli "openstack token issue -f value -c id >/dev/null && echo ok")" = ok ] || { echo "  CLI or credentials not working on $CLI_SSH"; exit 2; }
  [ "$(cli "$C14 group-snapshot-list --all-tenants 1 >/dev/null && echo ok" | tail -1)" = ok ] ||
    { echo "  'cinder' (python-cinderclient) with group support (API 3.14) not working on $CLI_SSH"; bad=1; }
  BACKEND=$(cli "openstack volume type show $VOLUME_TYPE -f json -c properties" | python3 -c 'import json, sys; print((json.load(sys.stdin).get("properties") or {}).get("volume_backend_name", ""))' 2>/dev/null)
  BACKEND=${BACKEND:-$VOLUME_TYPE}
  VTID=$(cli "openstack volume type show $VOLUME_TYPE -f value -c id")
  svc=$(cli "openstack volume service list --service cinder-volume -f value -c Host -c Status -c State" | grep "@$BACKEND enabled up$")
  [ -n "$svc" ] || { echo "  no cinder-volume service for $BACKEND is enabled and up"; bad=1; }
  echo "  backend $BACKEND served by: $(awk '{print $1}' <<<"$svc" | paste -sd' ' -)"
  [ -n "$(on "$CINDER_SSH" "command -v qemu-io && test -x $CINDER_VENV/bin/python && test -f $CINDER_VENV/cinder.env && echo ok" | grep -x ok)" ] ||
    { echo "  $CINDER_SSH lacks qemu-io, $CINDER_VENV/bin/python or $CINDER_VENV/cinder.env"; bad=1; }
  s=$(cli "openstack volume list --all-projects -f value -c Name; openstack volume snapshot list --all-projects -f value -c Name" | grep -c "^$PREFIX-")
  s=$((s + $(ours_groups | grep -c .) + $(ours_gsnaps | grep -c .)))
  [ "$s" = 0 ] || { echo "  $s objects named $PREFIX-* already exist: clean them up or change PREFIX"; bad=1; }
  [ $bad = 0 ] || exit 2
  echo "  ok"; }

GT=
cleanup() {
  echo "=== cleanup $(ts)"
  local g n id s u gs l pass
  # Groups made from a source first (their volumes may use a group snapshot), then group
  # snapshots, then the original group: the order that lets VMstore delete every snapshot.
  for pass in derived gsnaps main; do
    case $pass in
      gsnaps) for gs in $(ours_gsnaps); do cli "$C14 group-snapshot-delete $gs"; echo "  group snapshot $gs: $(wait_for gsstat "$gs")"; done ;;
      *) for l in $(ours_groups | tr ' ' '|'); do g=${l%%|*}; n=${l#*|}   # names have no spaces
           { [ "$pass" = main ] && [ "$n" = "$PREFIX-group" ]; } || { [ "$pass" = derived ] && [ "$n" != "$PREFIX-group" ]; } || continue
           track $(gvols "$g"); cli "$C13 group-delete --delete-volumes $g"; echo "  group $n $g: $(wait_for gstat "$g")"
         done ;;
    esac
  done
  for id in $(cat "$OUT/ids"); do s=$(wait_for vstat "$id"); [ "$s" = deleted ] && continue
    [[ "$s" == error* || "$s" =~ ^(in-use|attaching|reserved|maintenance)$ ]] && { echo "  $id was $s, resetting to available (test volume)"; cli "openstack volume set --state available $id"; }
    cli "openstack volume delete $id"; echo "  volume $id: $(wait_for vstat "$id")"
  done
  for id in $(cli "openstack volume list --all-projects -f value -c ID -c Name" | awk -v p="^$PREFIX-" '$2 ~ p {print $1}'); do
    cli "openstack volume delete $id"; echo "  volume $id: $(wait_for vstat "$id")"; done
  [ -z "$GT" ] || { cli "$C13 group-type-delete $GT" >/dev/null; echo "  group type $GT deleted"; }
  # Member snapshots Cinder dropped while VMstore kept them (see G8): remove them from the
  # array now that no volume uses them. snapshot_lab.py refuses snapshots not named $PREFIX-*.
  for id in $(cat "$OUT/snaps"); do u=$(on_array "$id")
    [ "$u" = 0 ] || echo "  snapshot $id still on VMstore ($u): $(array array-delete "$id" | grep -E '^(DELETED|REFUSED|ERROR)' | paste -sd' ' -)"
  done
  n=$(cli "openstack volume list --all-projects -f value -c ID -c Name; openstack volume snapshot list --all-projects -f value -c Name" | grep -c -e "^$PREFIX-" -e " $PREFIX-" -f "$OUT/ids")
  echo "  left: $n volumes or snapshots of this run, $(ours_groups | grep -c .) groups, $(ours_gsnaps | grep -c .) group snapshots"
  on "$CINDER_SSH" "rm -rf $RDIR && echo '  $RDIR removed on $CINDER_SSH'"
  others > "$OUT/others-after.txt"
  if diff -q "$OUT/others-before.txt" "$OUT/others-after.txt" >/dev/null; then echo "  other volumes, snapshots and groups: unchanged"
  else echo "  other volumes, snapshots or groups changed during the run (not by this script; review):"; diff "$OUT/others-before.txt" "$OUT/others-after.txt" | sed 's/^/    /' | head -20; fi
  echo "=== summary"
  local r; for r in ${RESULTS[@]+"${RESULTS[@]}"}; do echo "  $r"; done   # bash 3.2: empty array under set -u
  echo "  log: $OUT/run.log"; }

precheck
others > "$OUT/others-before.txt"
on "$CINDER_SSH" "mkdir -p $RDIR" && scp -q -o BatchMode=yes "$LAB/volume-migration/volume_record.py" "$LAB/snapshots/snapshot_lab.py" "$CINDER_SSH:$RDIR/" ||
  { echo "helper copy failed"; exit 2; }
echo "  driver on $CINDER_SSH: $(array driver-version --backend "$BACKEND" | grep '^VERSION' | cut -d' ' -f2)"
trap 'rc=$?; [ $rc = 0 ] && rc=$RC; cleanup; exit $rc' EXIT

echo "### $(ts) G1 group type (no consistent snapshot spec) and group"
GT=$(cli "$C13 group-type-create --is-public False $PREFIX-gtype" | uuid)
[ -n "$GT" ] || { echo "  group type not created"; exit 2; }
G=$(cli "$C13 group-create --name $PREFIX-group $GT $VTID" | prop id)
s=$([ -n "$G" ] && wait_for gstat "$G"); echo "  group $G: ${s:-not created}"
if [ "$s" = available ]; then result G1 PASS "group available"
else result G1 FAIL "group ${s:-not created}"; stop "no group"; fi

echo "### $(ts) G2 two volumes in the group, filled with their own pattern"
A=$(cli "$C13 create --group-id $G --volume-type $VTID --name $PREFIX-a 1" | prop id)
B=$(cli "$C13 create --group-id $G --volume-type $VTID --name $PREFIX-b 1" | prop id)
track $A $B; ok=1
for v in $A $B; do s=$(wait_for vstat "$v"); echo "  $v: $s, group $(vgroup "$v")"; [ "$s" = available ] && [ "$(vgroup "$v")" = "$G" ] || ok=0; done
[ -n "$A" ] && [ -n "$B" ] || ok=0
if [ $ok = 1 ]; then
  PA=$(fpath "$A"); PB=$(fpath "$B"); FA=$(ffmt "$PA"); FB=$(ffmt "$PB")
  echo "  fill: a $(fill "$PA" "$FA" 5a)/2 regions, b $(fill "$PB" "$FB" 3c)/2 regions"
  if check_set G2 "5a 3c" $A $B; then result G2 PASS "2 volumes in the group, records agree with files, patterns written"
  else result G2 FAIL "fill or record check failed"; stop "volumes not usable"; fi
else result G2 FAIL "volumes in the group not available"; stop "no volumes"; fi

echo "### $(ts) G3 group snapshot"
GS1=$(cli "$C14 group-snapshot-create --name $PREFIX-gsnap1 $G" | prop id)
s=$([ -n "$GS1" ] && wait_for gsstat "$GS1"); echo "  group snapshot $GS1: ${s:-not created}"
ok=$([ "$s" = available ] && echo 1 || echo 0); M1=
for v in $A $B; do m=$(members "$v" "$PREFIX-gsnap1"); id=${m%% *}; st=${m##* }; [ -n "$id" ] || continue
  echo "$id" >> "$OUT/snaps"; M1="$M1 $id"; u=$(on_array "$id"); echo "  member snapshot $id of $v: $st, on VMstore: $u"
  [ "$st" = available ] && [ "$u" = 1 ] || ok=0
done
n=$(words $M1); [ "$n" = 2 ] || ok=0
if [ "$ok" = 1 ]; then result G3 PASS "group snapshot available, 2 member snapshots, each on VMstore"
else result G3 FAIL "group snapshot $s, $n member snapshots"; stop "no group snapshot"; fi

echo "### $(ts) overwrite both volumes after the snapshot (a 0xa5, b 0xc3)"
echo "  a $(fill "$PA" "$FA" a5)/2, b $(fill "$PB" "$FB" c3)/2 regions"

echo "### $(ts) G4 group from the group snapshot"
G4=$(cli "$C14 group-create-from-src --group-snapshot $GS1 --name $PREFIX-from-snap" | prop id)
s=$([ -n "$G4" ] && wait_for gstat "$G4"); echo "  group $G4: ${s:-not created}"
V4=$([ -n "$G4" ] && gvols "$G4"); track $V4
for v in $V4; do echo "  $v: $(wait_for vstat "$v")"; done
if [ "$s" = available ] && [ "$(words $V4)" = 2 ] && check_set G4 "5a 3c" $V4; then
  result G4 PASS "2 volumes with the data at snapshot time, records agree with files"
else result G4 FAIL "group ${s:-not created}, $(words $V4) volumes (expected patterns 5a 3c)"; fi

echo "### $(ts) G5 group from the source group"
G5=$(cli "$C14 group-create-from-src --source-group $G --name $PREFIX-from-group" | prop id)
s=$([ -n "$G5" ] && wait_for gstat "$G5"); echo "  group $G5: ${s:-not created}"
V5=$([ -n "$G5" ] && gvols "$G5"); track $V5
for v in $V5; do echo "  $v: $(wait_for vstat "$v")"; done
if [ "$s" = available ] && [ "$(words $V5)" = 2 ] && check_set G5 "a5 c3" $V5; then
  result G5 PASS "2 volumes with the current data, records agree with files"
else result G5 FAIL "group ${s:-not created}, $(words $V5) volumes (expected patterns a5 c3)"; fi

echo "### $(ts) G6 remove volume b from the group, then add it back"
cli "$C13 group-update --remove-volumes $B $G" >/dev/null; s1=$(wait_for gstat "$G"); g1=$(vgroup "$B")
cli "$C13 group-update --add-volumes $B $G" >/dev/null; s2=$(wait_for gstat "$G"); g2=$(vgroup "$B")
echo "  after remove: group $s1, b in ${g1:-?}; after add: group $s2, b in ${g2:-?}"
if [ "$s1" = available ] && [ "$g1" = None ] && [ "$s2" = available ] && [ "$g2" = "$G" ]; then result G6 PASS "volume removed and added back"
else result G6 FAIL "remove: group $s1, b in $g1; add: group $s2, b in $g2"; fi

echo "### $(ts) G7 group snapshot with no volumes made from it: create, delete"
GS2=$(cli "$C14 group-snapshot-create --name $PREFIX-gsnap2 $G" | prop id)
s=$([ -n "$GS2" ] && wait_for gsstat "$GS2"); m2=$(for v in $A $B; do members "$v" "$PREFIX-gsnap2"; done | cut -d' ' -f1)
for id in $m2; do echo "$id" >> "$OUT/snaps"; done
echo "  group snapshot $GS2: ${s:-not created}, members: $(words $m2)"
if [ "$s" = available ]; then
  cli "$C14 group-snapshot-delete $GS2"; s=$(wait_for gsstat "$GS2"); u=0; left=0
  for id in $m2; do u=$((u + $(on_array "$id"))); in_cinder "$id" && left=$((left + 1)); done
  echo "  after delete: group snapshot $s, member snapshots in Cinder: $left, on VMstore: $u"
  if [ "$s" = deleted ] && [ $left = 0 ] && [ $u = 0 ]; then result G7 PASS "deleted in Cinder and on VMstore"
  else result G7 FAIL "group snapshot $s, $left member snapshots left in Cinder, $u on VMstore"; fi
else result G7 FAIL "second group snapshot ${s:-not created}"; fi

echo "### $(ts) G8 delete the group snapshot that $PREFIX-from-snap was made from, while that group exists"
out=$(cli "$C14 group-snapshot-delete $GS1"); [ -n "$out" ] && echo "  cli: $(head -2 <<<"$out" | paste -sd' ' -)"
s=$(wait_for gsstat "$GS1"); u=0; left=0
for id in $M1; do u=$((u + $(on_array "$id"))); in_cinder "$id" && left=$((left + 1)); done
echo "  group snapshot: $s; member snapshots in Cinder: $left; on VMstore: $u"
if [ "$s" = available ] && [ $left = 2 ]; then result G8 PASS "Cinder kept the group snapshot (refused while volumes use it): $(head -1 <<<"$out" | cut -c1-160)"
elif [ "$s" = deleted ] && [ $u = 0 ]; then result G8 PASS "deleted in Cinder and on VMstore"
elif [ "$s" = deleted ]; then result G8 FAIL "Cinder deleted the group snapshot and its member records, but VMstore kept $u snapshot(s) still used by volumes made from them: orphaned on the array (cleanup removes them)"
else result G8 FAIL "group snapshot $s, $left member snapshots in Cinder, $u on VMstore"; fi

echo "### $(ts) G9 delete every group with its volumes"
ok=1; files=
for v in $(cat "$OUT/ids"); do p=$(fpath "$v"); [ -n "$p" ] && files="$files $p"; done
for g in $G4 $G5 $G; do cli "$C13 group-delete --delete-volumes $g"; s=$(wait_for gstat "$g"); echo "  group $g: $s"; [ "$s" = deleted ] || ok=0; done
for v in $(cat "$OUT/ids"); do s=$(wait_for vstat "$v"); [ "$s" = deleted ] || { echo "  volume $v: $s"; ok=0; }; done
n=$(on "$CINDER_SSH" "n=0; for p in $files; do [ -e \$p ] && n=\$((n + 1)); done; echo \$n" | tail -1)
echo "  volume files left on the share: ${n:-?} of $(words $files)"
if [ $ok = 1 ] && [ "$n" = 0 ]; then result G9 PASS "groups, volumes and files deleted"
else result G9 FAIL "groups or volumes not deleted, or ${n:-?} files left"; fi
