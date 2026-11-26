#!/usr/bin/env bash
# Image cache lab test (VME-36). Cinder's own image volume cache keeps one volume per image
# and backend host, and makes later volumes from that image with the driver's
# create_cloned_volume (plus extend_volume when the new volume is larger). No driver change
# is needed; this checks the path on a VMstore backend, and how each volume was made.
# Runbook: docs/runbooks/image-cache-lab-test.md
#
# Usage: run.sh <lab.env>
#
# The cache is switched on and off through the PCD cluster blueprint
# (image_volume_cache_enabled in the backend), never by this script. With the cache off the
# run is a baseline: every volume is a download, and the cache checks are SKIP.
#
# Creates only a private image <PREFIX>-image (a copy of IMAGE_ID), volumes named <PREFIX>-*
# and the cache volume Cinder makes for that image (image-<its id>, IDs recorded), and
# deletes them on exit, including on failure or Ctrl-C. Other volumes and images are never
# modified.
# Exit: 0 all checks passed (SKIP allowed), 1 a check failed, 2 precheck or setup problem.
set -u
[ -f "${1:-}" ] || { echo "usage: $0 <lab.env>"; exit 2; }
# shellcheck disable=SC1090
. "$1"
for v in CLI_SSH CLI_RC REGION VOLUME_TYPE CINDER_SSH CINDER_VENV CINDER_CONF_DIR IMAGE_ID PREFIX; do
  [ -n "${!v:-}" ] || { echo "missing $v in $1"; exit 2; }
done
CONFIG_SSH=${CONFIG_SSH:-$CINDER_SSH}   # every cinder-volume host serving the backend
IMAGE_STORE=${IMAGE_STORE:-}            # Glance store for the test image (multi-store Glance)
IMAGE_SRC=${IMAGE_SRC:-}                # <ssh host>:<path> of IMAGE_ID's data, if Glance cannot serve it

HERE=$(cd "$(dirname "$0")" && pwd)
RDIR=/root/$PREFIX-lab
OUT=$(mktemp -d "${TMPDIR:-/tmp}/$PREFIX-lab.XXXXXX")
exec > >(tee "$OUT/run.log") 2>&1
SSH="ssh -o BatchMode=yes -o ConnectTimeout=15"
: > "$OUT/cachevols"   # cache volumes Cinder created for this run's image

ts() { date -u +%T; }
# ssh reads stdin unless told not to: cli and on never do; on_in passes a script on stdin.
cli() { $SSH "$CLI_SSH" "source $CLI_RC >/dev/null 2>&1; export OS_REGION_NAME=$REGION; $*" </dev/null 2>&1 | grep -v setlocale; return 0; }
on() { local t=$1; shift; $SSH "$t" "$@" </dev/null 2>&1 | grep -v setlocale; return 0; }
on_in() { local t=$1; shift; $SSH "$t" "$@" 2>&1 | grep -v setlocale; return 0; }
field() { sed -nE "s/.* $1=([^ ]+).*/\1/p" <<<" $2"; }

vstat() { cli "openstack volume show $1 -f value -c status 2>/dev/null"; }
istat() { cli "openstack image show $1 -f value -c status 2>/dev/null"; }
wait_vol() { local s i
  for ((i = 0; i < 1800; i += 5)); do s=$(vstat "$1")
    [[ "$s" =~ ^(creating|downloading|extending|deleting)$ ]] || break; sleep 5
  done; echo "${s:-deleted}"; }

helper() { # ssh host, command args -> tagged lines of cache_lab.py
  on "$1" "cd $RDIR && (set +u; set -a; . $CINDER_VENV/cinder.env; set +a; PYTHONWARNINGS=ignore $CINDER_VENV/bin/python cache_lab.py --conf-dir $CINDER_CONF_DIR ${*:2})"; }
entries() { helper "$CINDER_SSH" entries "$IMG" | grep '^ENTRY' | sort; }
vol() { helper "$CINDER_SSH" volume "$1" | grep '^VOL'; }
# The driver hands a file to Nova as raw if its record or the probe says raw, else as the
# probed format: no record means "the file's own format". A record must be absent or match.
record_ok() { [ "$1" = None ] || [ "$1" = "$2" ]; }   # record, file format
check() { # volume id -> "record=<r|None> file=<f> vsize=<bytes> same_as_image=<yes|no|error> host=<h> cluster=<c> internal_project=<yes|no>"
  local l p r; l=$(vol "$1"); r=$(field record "$l")
  p=$(on "$CINDER_SSH" "m=\$(findmnt -n -o TARGET -S '$(field share "$l")' | grep '$CINDER_VENV' | head -1); echo \$m/volume-$(field name_id "$l")")
  echo "record=$r $(on_in "$CINDER_SSH" "bash -s -- '$p' '$IMGFMT' '$RDIR/src.img'" <<'EOF' | tail -1
p=$1; [ -e "$p" ] || { echo "file=missing vsize=0 same_as_image=no"; exit; }
x=$(head -c4 "$p" | od -An -tx1 | tr -d ' '); [ "$x" = 514649fb ] && f=qcow2 || f=raw
v=$(qemu-img info -U -f $f --output=json "$p" | sed -nE 's/.*"virtual-size": *([0-9]+).*/\1/p' | head -1)
qemu-img compare -U -f "$2" -F $f "$3" "$p" >/dev/null 2>&1; case $? in 0) c=yes ;; 1) c=no ;; *) c=error ;; esac
echo "file=$f vsize=$v same_as_image=$c"
EOF
) host=$(field host "$l") cluster=$(field cluster "$l") internal_project=$(field internal_project "$l")"; }

# How Cinder made a volume, from the cache entries before and after its creation:
# miss = a new entry appeared (image downloaded, then cloned into a cache volume),
# hit = an existing entry's last_used moved (cloned from the cache volume), none = neither.
how() { # entries before, entries after -> miss|hit|none
  local b=$1 a=$2 vol
  for vol in $(sed -nE 's/.* volume=([^ ]+).*/\1/p' <<<"$a"); do grep -q "volume=$vol " <<<"$b" || { echo miss; return; }; done
  [ -n "$b" ] && [ "$b" != "$a" ] && { echo hit; return; }
  echo none; }
expect() { # host, cluster of the new volume, entries before -> the path Cinder should take
  [ "$CACHE" = on ] && [ "$DIRECT" != 1 ] || { echo none; return; }
  if [ "$2" != None ] && grep -qF " cluster=$2 " <<<"$3"; then echo hit
  elif grep -qF " host=$1 " <<<"$3"; then echo hit
  else echo miss; fi; }

TIMES=(); HITS=0; LAST=   # LAST: the newest TIMES line (bash 3.2 has no negative index)
make_vol() { # name, size GiB -> sets V and prints the checks; returns 1 if a check failed
  local b a t0 s path want c fail=0
  b=$(entries); t0=$SECONDS
  V=$(cli "openstack volume create --image $IMG --size $2 --type $VOLUME_TYPE $1 -f value -c id" | tail -1)
  [[ "$V" =~ ^[0-9a-f-]{36}$ ]] || { echo "  $1 not created: $V"; V=; return 1; }
  s=$(wait_vol "$V"); a=$(entries); path=$(how "$b" "$a")
  for c in $(sed -nE 's/.* volume=([^ ]+).*/\1/p' <<<"$a"); do grep -qx "$c" "$OUT/cachevols" || echo "$c" >> "$OUT/cachevols"; done
  [ "$s" = available ] || { echo "  $1 $V: $s"; return 1; }
  c=$(check "$V"); want=$(expect "$(field host "$c")" "$(field cluster "$c")" "$b")
  if [ "$DIRECT" = maybe ] && [ "$CACHE" = on ]; then   # the first volume shows which path Cinder takes
    case $path in none) DIRECT=1; want=none ;; *) DIRECT=0 ;; esac
    echo "  Glance image volume cloned directly: $([ $DIRECT = 1 ] && echo yes || echo no, Cinder went to the cache)"
  fi
  LAST="$1 $2 GiB: $path, $((SECONDS - t0)) s"; TIMES+=("$LAST"); [ "$path" = hit ] && HITS=$((HITS + 1))
  echo "  $1 $V: $s in $((SECONDS - t0)) s, made by: $path (expected $want); $c"
  [ "$path" = "$want" ] || { echo "  path: $path, expected $want"; fail=1; }
  record_ok "$(field record "$c")" "$(field file "$c")" || { echo "  record contradicts the file"; fail=1; }
  [ "$(field same_as_image "$c")" = yes ] || { echo "  content differs from the image"; fail=1; }
  [ "$(field vsize "$c")" = $(($2 * 1073741824)) ] || { echo "  virtual size $(field vsize "$c"), expected $2 GiB"; fail=1; }
  return $fail; }

others() { # every volume and image not created by this run
  cli "openstack volume list --all-projects -f value -c ID -c Name -c Status" | awk -v p="^$PREFIX-" -v c="^image-${IMG:-none}$" '$2 !~ p && $2 !~ c {print "volume", $1, $NF}' | sort
  cli "openstack image list -f value -c ID -c Name -c Status" | awk -v p="^$PREFIX-" '$2 !~ p {print "image", $1, $NF}' | sort; }

RESULTS=(); RC=0
result() { RESULTS+=("$1 | $2 | $3"); echo "  RESULT $1: $2 ($3)"; [ "$2" = FAIL ] && RC=1; return 0; }

precheck() {
  local bad=0 svc s h c e=
  echo "### precheck $(ts)"
  [ "$(cli "openstack token issue -f value -c id >/dev/null && echo ok")" = ok ] || { echo "  CLI or credentials not working on $CLI_SSH"; exit 2; }
  BACKEND=$(cli "openstack volume type show $VOLUME_TYPE -f json -c properties" | python3 -c 'import json, sys; print((json.load(sys.stdin).get("properties") or {}).get("volume_backend_name", ""))' 2>/dev/null)
  BACKEND=${BACKEND:-$VOLUME_TYPE}
  svc=$(cli "openstack volume service list --service cinder-volume -f value -c Host -c Status -c State" | grep "@$BACKEND enabled up$")
  [ -n "$svc" ] || { echo "  no cinder-volume service for $BACKEND is enabled and up"; bad=1; }
  echo "  backend $BACKEND served by $(grep -c . <<<"$svc") host(s): $(awk '{print $1}' <<<"$svc" | paste -sd' ' -)"
  read -r IMGFMT s <<<"$(cli "openstack image show $IMAGE_ID -f value -c disk_format -c status" | paste -sd' ' -)"
  [ "$s" = active ] && [[ "$IMGFMT" =~ ^(qcow2|raw)$ ]] || { echo "  image $IMAGE_ID: status ${s:-?}, format ${IMGFMT:-?} (needs an active qcow2 or raw image)"; bad=1; }
  [ -z "$IMAGE_STORE" ] || cli "openstack image create --help" | grep -q -- '--store' || { echo "  IMAGE_STORE set, but this openstack CLI has no --store"; bad=1; }
  for h in $(printf '%s\n' $CINDER_SSH $CONFIG_SSH | awk '!s[$0]++'); do   # CINDER_SSH serves the backend too
    on "$h" "mkdir -p $RDIR" && scp -q -o BatchMode=yes "$HERE/cache_lab.py" "$h:$RDIR/cache_lab.py" || { echo "  helper copy to $h failed"; exit 2; }
    c=$(helper "$h" config "$BACKEND" | grep -E '^(CONFIG|ERROR)'); echo "  $h: $c"
    [[ "$c" == CONFIG* ]] || bad=1
    [ "$(field enabled "$c")" = True ] && [ "$(field internal_project "$c")" = set ] && [ "$(field internal_user "$c")" = set ] && c=on || c=off
    [ -z "$e" ] || [ "$e" = "$c" ] || { echo "  the hosts disagree on the cache (on one, off on another): make them agree through the blueprint"; bad=1; }
    e=$c
  done
  CACHE=${e:-off}
  SCHEMES=$(field direct_url_schemes "$(helper "$CINDER_SSH" config "$BACKEND" | grep '^CONFIG')")
  [ -n "$(on "$CINDER_SSH" "command -v qemu-img && echo ok" | grep -x ok)" ] || { echo "  $CINDER_SSH lacks qemu-img"; bad=1; }
  s=$(cli "openstack volume list --all-projects -f value -c Name; openstack image list -f value -c Name" | grep -c "^$PREFIX-")
  [ "$s" = 0 ] || { echo "  $s objects named $PREFIX-* already exist: clean them up or change PREFIX"; bad=1; }
  [ $bad = 0 ] || exit 2
  echo "  image cache: $CACHE on $BACKEND; allowed_direct_url_schemes: $SCHEMES"
  echo "  ok"; }

IMG=
cleanup() {
  echo "=== cleanup $(ts)"
  local id s h i n
  for id in $(cli "openstack volume list --all-projects -f value -c ID -c Name" | awk -v p="^$PREFIX-" '$2 ~ p {print $1}'); do
    s=$(wait_vol "$id"); [[ "$s" == error* ]] && { echo "  $id was $s, resetting to available (test volume)"; cli "openstack volume set --state available $id"; }
    cli "openstack volume delete $id"; echo "  volume $id: $(wait_vol "$id")"; done
  [ -n "$IMG" ] || IMG=$(cli "openstack image list --private -f value -c ID -c Name" | awk -v n="$PREFIX-image" '$2 == n {print $1}' | head -1)
  if [ -n "$IMG" ]; then
    # Deleting a cache volume through the API also drops its cache entry (Cinder volume API).
    for id in $(cat "$OUT/cachevols") $(entries | sed -nE 's/.* volume=([^ ]+).*/\1/p'); do
      s=$(vstat "$id"); [ -n "$s" ] || continue
      cli "openstack volume delete $id"; echo "  cache volume $id: $(wait_vol "$id")"; done
    cli "openstack image delete $IMG"; echo "  image $IMG deleted"
    for ((i = 0; i < 120; i += 10)); do   # Glance removes the image's own volume when it is stored in Cinder
      n=$(cli "openstack volume list --all-projects --name image-$IMG -f value -c ID" | grep -c .); [ "$n" = 0 ] && break; sleep 10; done
    e=$(entries | grep -c .)
    echo "  cache entries left for the image: $e; volumes named image-$IMG left: $n"
    if [ "$e" = 0 ] && [ "$n" = 0 ]; then result C7 PASS "test volumes, cache volume, cache entry and image removed"
    else result C7 FAIL "$e cache entries, $n image-$IMG volumes left"; fi
  fi
  echo "  left: $(cli "openstack volume list --all-projects -f value -c Name; openstack image list -f value -c Name" | grep -c "^$PREFIX-") objects named $PREFIX-*"
  on "$CLI_SSH" "rm -rf $RDIR && echo '  $RDIR removed on $CLI_SSH'"
  for h in $CONFIG_SSH $CINDER_SSH; do [ "$h" = "$CLI_SSH" ] || on "$h" "rm -rf $RDIR && echo '  $RDIR removed on $h'"; done | sort -u
  others > "$OUT/others-after.txt"
  if diff -q "$OUT/others-before.txt" "$OUT/others-after.txt" >/dev/null; then echo "  other volumes and images: unchanged"
  else echo "  other volumes or images changed during the run (not by this script; review):"; diff "$OUT/others-before.txt" "$OUT/others-after.txt" | sed 's/^/    /' | head -20; fi
  echo "=== summary (image cache $CACHE on $BACKEND)"
  local r; for r in ${TIMES[@]+"${TIMES[@]}"}; do echo "  $r"; done   # bash 3.2: empty array under set -u
  for r in ${RESULTS[@]+"${RESULTS[@]}"}; do echo "  $r"; done
  echo "  log: $OUT/run.log"; }

precheck
others > "$OUT/others-before.txt"
trap 'rc=$?; [ $rc = 0 ] && rc=$RC; cleanup; exit $rc' EXIT

echo "### $(ts) private copy of image $IMAGE_ID: cache entries of this run are then only ours"
on "$CLI_SSH" "mkdir -p $RDIR"
if [ -n "$IMAGE_SRC" ]; then   # Glance's own copy of the data, read in place on that host
  $SSH "${IMAGE_SRC%%:*}" "cat '${IMAGE_SRC#*:}'" </dev/null | $SSH "$CLI_SSH" "cat > $RDIR/src.img"
else cli "openstack image save --file $RDIR/src.img $IMAGE_ID" | sed 's/^/  /'; fi
want=$(cli "openstack image show $IMAGE_ID -f json" | python3 -c 'import json, sys; d = json.load(sys.stdin); print(d.get("os_hash_value") or (d.get("properties") or {}).get("os_hash_value") or "")' 2>/dev/null)
got=$(on "$CLI_SSH" "sha512sum < $RDIR/src.img | cut -d' ' -f1")
echo "  data of $IMAGE_ID: sha512 ${got:0:16}..., Glance says ${want:0:16}..."
[ -n "$want" ] && [ "$got" = "$want" ] || { echo "  image data does not match Glance's hash (set IMAGE_SRC if Glance cannot serve it to $CLI_SSH)"; exit 2; }
IMG=$(cli "openstack image create --disk-format $IMGFMT --container-format bare --private ${IMAGE_STORE:+--store $IMAGE_STORE} --file $RDIR/src.img $PREFIX-image -f value -c id" | tail -1)
[[ "$IMG" =~ ^[0-9a-f-]{36}$ ]] || { echo "  image not created: $IMG"; IMG=; exit 2; }
for ((i = 0; i < 300; i += 10)); do s=$(istat "$IMG"); [ "$s" = active ] && break; sleep 10; done
echo "  image $IMG: $s"; [ "$s" = active ] || exit 2
if [ "$CLI_SSH" != "$CINDER_SSH" ]; then   # the content checks compare volumes with this file
  $SSH "$CLI_SSH" "cat $RDIR/src.img" </dev/null | $SSH "$CINDER_SSH" "mkdir -p $RDIR && cat > $RDIR/src.img"
fi
[ "$(on "$CLI_SSH" "md5sum < $RDIR/src.img")" = "$(on "$CINDER_SSH" "md5sum < $RDIR/src.img")" ] || { echo "  image copy to $CINDER_SSH failed"; exit 2; }
# Glance stores the image as a Cinder volume when its store is cinder. If that volume is on
# the backend under test and Cinder allows cinder:// URLs, Cinder can clone it directly and
# never consult the cache. That also needs Glance to show Cinder the image location, so
# whether it happens is read from the first volume: DIRECT=maybe until then.
GV=$(cli "openstack volume list --all-projects --name image-$IMG -f value -c ID" | head -1); DIRECT=0
if [ -n "$GV" ]; then
  gh=$(cli "openstack volume show $GV -f value -c os-vol-host-attr:host")
  echo "  Glance stored the image as Cinder volume $GV on $gh"
  [[ "$gh" == *"@$BACKEND#"* || "$gh" == *"@$BACKEND" ]] && [[ ",$SCHEMES," == *,cinder,* ]] && DIRECT=maybe
else echo "  Glance did not store the image in Cinder"; fi
skip_why() {
  [ "$CACHE" = on ] || { echo "image cache off on $BACKEND (image_volume_cache_enabled and the internal tenant): baseline run"; return; }
  [ "$DIRECT" = 1 ] && echo "image stored as a Cinder volume on $BACKEND, which Cinder clones directly without the cache; set IMAGE_STORE to a store that is not Cinder"; }

echo "### $(ts) C1 first volume from the image (1 GiB)"
if make_vol "$PREFIX-v1" 1; then result C1 PASS "$LAST"; else result C1 FAIL "see C1 lines"; fi
V1=$V; SKIP_WHY=$(skip_why)

echo "### $(ts) C2 the cache entry"
e=$(entries); echo "${e:-  no cache entries for the image}" | sed 's/^ENTRY/  ENTRY/'
if [ -n "$SKIP_WHY" ]; then
  [ -z "$e" ] && result C2 SKIP "$SKIP_WHY" || result C2 FAIL "cache entries exist although the cache should not be used: $(grep -c . <<<"$e")"
elif [ "$(grep -c . <<<"$e")" = 1 ] && [ -n "$e" ]; then
  cv=$(field volume "$e"); c=$(check "$cv"); echo "  cache volume $cv: $(vstat "$cv"); $c"
  if [ "$(vstat "$cv")" = available ] && [ "$(field internal_project "$c")" = yes ] && record_ok "$(field record "$c")" "$(field file "$c")" && [ "$(field same_as_image "$c")" = yes ]; then
    result C2 PASS "one entry; cache volume available, owned by the internal tenant, record agrees with the file, content matches the image"
  else result C2 FAIL "cache volume: $(vstat "$cv"); $c"; fi
else result C2 FAIL "$(grep -c . <<<"$e") cache entries after the first volume, expected 1"; fi

echo "### $(ts) C3 second volume from the image (1 GiB)"
if make_vol "$PREFIX-v2" 1; then result C3 PASS "$LAST"; else result C3 FAIL "see C3 lines"; fi

echo "### $(ts) C4 larger volume from the image (2 GiB: clone, then extend)"
if make_vol "$PREFIX-v3" 2; then result C4 PASS "$LAST"; else result C4 FAIL "see C4 lines"; fi

echo "### $(ts) C5 delete the first volume (the cache volume was cloned from it), then one more volume"
ok=1
if [ -n "$V1" ]; then cli "openstack volume delete $V1"; s=$(wait_vol "$V1"); echo "  $PREFIX-v1 $V1: $s"; [ "$s" = deleted ] || ok=0; fi
for cv in $(cat "$OUT/cachevols"); do s=$(vstat "$cv"); echo "  cache volume $cv: $s"; [ "$s" = available ] || ok=0; done
make_vol "$PREFIX-v4" 1 || ok=0
if [ $ok = 1 ]; then result C5 PASS "first volume deleted, cache volume kept, $LAST"; else result C5 FAIL "see C5 lines"; fi

if [ -n "$SKIP_WHY" ]; then result C6 SKIP "$SKIP_WHY"
elif [ $HITS -gt 0 ]; then result C6 PASS "$HITS volume(s) made from the cache"
else result C6 SKIP "no volume landed on a host with a cache entry (entries are per cinder-volume host unless clustered); run again"; fi
