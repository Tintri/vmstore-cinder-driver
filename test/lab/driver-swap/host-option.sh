#!/usr/bin/env bash
# Switch vmstore_qcow2_volumes for one backend on a PCD cinder-volume host, and undo it.
# Runbook: docs/runbooks/driver-swap.md. Normally driven by swap.sh from a workstation.
#
# Usage (as root on the cinder-volume host):
#   host-option.sh set <backend_section> vmstore_qcow2_volumes <true|false> --file <config file>
#   host-option.sh rollback        restore every file changed by `set` from its oldest backup
#   host-option.sh status <backend_section>
#
# Which file: PCD writes cinder.conf from the cluster blueprint, and its host agent rewrites
# it within seconds of a hand edit (seen 2026-11-26 on PCD 2026.8: 9 to 12 s). The script
# waits OPTION_SETTLE_SECONDS (default 45) after the edit and stops without restarting if the
# file changed; on PCD, change the option through the cluster blueprint. The
# config directory is read in sorted order and the last value wins, so a value in
# cinder_override.conf takes precedence over cinder.conf; whether the host agent also
# rewrites cinder_override.conf is not known. The script therefore does not choose: pass
# --file, and check `status` again after any blueprint change. For results that must match
# what customers will do, change the option through the blueprint instead.
#
# Only this option can be changed. The file is backed up first (SWAP_DIR/option-backups/),
# the edit changes or adds one line and keeps everything else, and a failed restart restores
# the file and restarts again. Restarting cinder-volume restarts every backend on the host.
set -u
CMD=${1:-}; [ $# -gt 0 ] && shift
SWAP_DIR=${SWAP_DIR:-$(cd "$(dirname "$0")" && pwd)}
VENV=${CINDER_VENV:-/opt/pf9/pf9-cindervolume-base}
PY=$VENV/bin/python
CONF_DIR=${CINDER_CONF_DIR:-/opt/pf9/etc/pf9-cindervolume-base/conf.d}
LOG=${CINDER_LOG:-/var/log/pf9/cindervolume-base.log}
SERVICE=${CINDER_SERVICE:-pf9-cindervolume-base}
OPT=vmstore_qcow2_volumes
BK_ROOT=$SWAP_DIR/option-backups
HIST=$SWAP_DIR/history.log

say() { echo "[$(date -u +%T)] $*"; }
die() { say "ABORT: $*"; exit 1; }
record() { echo "$(date -u '+%Y-%m-%d %H:%M:%S') $*" >> "$HIST"; }
[ "$(id -u)" = 0 ] || die "run as root"
mkdir -p "$BK_ROOT" || die "cannot create $BK_ROOT"
exec 9>"$SWAP_DIR/.lock"
command -v flock >/dev/null && { flock -n 9 || die "another driver or option change is running on this host"; }

effective() { # section -> value and the file it comes from (sorted order, last wins), per file listing
  local f
  for f in "$CONF_DIR"/*.conf; do [ -f "$f" ] && "$PY" - "$f" "$1" "$OPT" <<'EOF'
import sys
path, want, opt = sys.argv[1:4]
section = None
for line in open(path, encoding='utf-8', errors='replace'):
    s = line.strip()
    if s.startswith('[') and ']' in s:
        section = s[1:s.index(']')].strip()
    elif section in (want, 'backend_defaults') and '=' in s and not s.startswith('#'):
        key, value = s.split('=', 1)
        if key.strip() == opt:
            print(section, value.strip(), path)
EOF
  done; }
restart_and_wait() {
  local start n
  start=$(date +"%Y-%m-%d %H:%M:%S")
  systemctl restart "$SERVICE" || return 1
  for _ in $(seq 1 36); do
    n=$(awk -v s="$start" '$0 >= s' "$LOG" | grep -c 'Starting volume driver VmstoreNfsDriver (')
    [ "$n" -gt 0 ] && break; sleep 5
  done
  [ "$n" -gt 0 ] || { say "no 'Starting volume driver VmstoreNfsDriver' line within 3 minutes"; return 1; }
  sleep 20
  say "started $n backend(s); errors since the restart:"
  awk -v s="$start" '$0 >= s' "$LOG" | grep -E " ERROR | CRITICAL " | cut -c1-220 | sort | uniq -c | head -20
  return 0; }
show_effective() {
  local out; out=$(effective "$1")
  echo "$OPT for [$1], in reading order (the last line wins; backend_defaults only if no [$1] line):"
  [ -n "$out" ] && sed 's/^/  /' <<<"$out" || echo "  not set anywhere (driver default False)"; }

set_option() {
  [ $# -ge 3 ] || die "usage: $0 set <backend_section> $OPT <true|false> --file <config file>"
  local section=$1 opt=$2 value=$3 file='' bk
  shift 3
  while [ $# -gt 0 ]; do case "$1" in --file) file=${2:-}; shift 2 ;; *) die "unknown argument $1" ;; esac; done
  [ -n "$section" ] || die "usage: $0 set <backend_section> $OPT <true|false> --file <config file>"
  [ "$opt" = "$OPT" ] || die "only $OPT can be changed with this tool"
  case "$value" in true | false) ;; *) die "value must be true or false" ;; esac
  [ -n "$file" ] || die "--file is required (see the comment at the top of this script and the runbook)"
  file=$(readlink -f "$file"); [ -f "$file" ] || die "no file $file"
  [ "$(dirname "$file")" = "$(readlink -f "$CONF_DIR")" ] || die "$file is not in $CONF_DIR"
  grep -qE "^\[[[:space:]]*${section}[[:space:]]*\]" "$CONF_DIR"/*.conf || die "no [$section] section in $CONF_DIR"
  show_effective "$section"
  # Numbered dirs keep the creation order even within one second.
  bk=$(printf '%s/%04d-%s' "$BK_ROOT" $(($(ls -1d "$BK_ROOT"/[0-9]*/ 2>/dev/null | wc -l) + 1)) "$(date -u +%Y%m%dT%H%M%SZ)")
  [ ! -e "$bk" ] && mkdir -p "$bk" && cp -a "$file" "$bk/" || die "backup failed"
  { echo "file $file"; echo "section $section"; echo "set $OPT $value"; md5sum "$file"; } > "$bk/RECORD"
  say "backup $bk/$(basename "$file")"
  "$PY" - "$file" "$section" "$OPT" "$value" <<'EOF' || { cp -a "$bk/$(basename "$file")" "$file"; die "edit failed, file restored"; }
import sys
path, want, opt, value = sys.argv[1:5]
lines = open(path, encoding='utf-8').read().splitlines(True)
out, section, found, header_at = [], None, False, None
for line in lines:
    s = line.strip()
    if s.startswith('[') and ']' in s:
        section = s[1:s.index(']')].strip()
        if section == want and header_at is None:
            header_at = len(out)
    elif section == want and '=' in s and not s.startswith('#') and s.split('=', 1)[0].strip() == opt:
        out.append('%s = %s\n' % (opt, value)); found = True   # every occurrence in the section
        continue
    out.append(line)
if not found:
    if header_at is not None:
        out.insert(header_at + 1, '%s = %s\n' % (opt, value))
    else:
        if out and not out[-1].endswith('\n'):
            out[-1] += '\n'
        out.append('\n[%s]\n%s = %s\n' % (want, opt, value))
with open(path, 'w', encoding='utf-8') as f:   # same inode: owner and mode stay
    f.writelines(out)
EOF
  say "changed lines:"; diff "$bk/$(basename "$file")" "$file" | sed 's/^/  /'
  # PCD's host agent rewrites cinder.conf from the cluster blueprint within seconds of a
  # hand edit (seen 2026-11-26: 9 to 12 s). Wait, then make sure the edit is still there.
  local edited; edited=$(md5sum "$file" | cut -d' ' -f1)
  sleep "${OPTION_SETTLE_SECONDS:-45}"
  if [ "$(md5sum "$file" | cut -d' ' -f1)" != "$edited" ]; then
    say "ABORT: $file was rewritten within ${OPTION_SETTLE_SECONDS:-45} s of the edit (PCD host agent):"
    show_effective "$section"
    say "nothing restarted. Change $OPT through the PCD cluster blueprint instead."
    record "option $section $OPT=$value in $file REVERTED by the host agent, not restarted"
    exit 1
  fi
  show_effective "$section"
  if ! restart_and_wait; then
    cp -a "$bk/$(basename "$file")" "$file"; say "RESTORED $file after a failed start"
    restart_and_wait || say "service did not come back after the restore: check $SERVICE"
    record "option $section $OPT=$value in $file FAILED, restored"; exit 1
  fi
  record "option $section $OPT=$value in $file, backup $bk"
  say "done: [$section] $OPT = $value in $(basename "$file")"; }

rollback_options() {
  local recs f done_files='' bk
  recs=$(ls -1 "$BK_ROOT"/[0-9]*/RECORD 2>/dev/null | sort)
  [ -n "$recs" ] || { say "no option backups in $BK_ROOT: nothing to roll back"; return 0; }
  for r in $recs; do   # oldest first: the first backup of each file is its pre-test copy
    f=$(awk '$1 == "file" {print $2}' "$r"); bk=$(dirname "$r")
    case " $done_files " in *" $f "*) continue ;; esac
    done_files="$done_files $f"
    if cmp -s "$bk/$(basename "$f")" "$f"; then say "$f already as found ($bk)"; continue; fi
    cp -a "$f" "$BK_ROOT/before-rollback-$(date -u +%Y%m%dT%H%M%SZ)-$(basename "$f")"
    cp -a "$bk/$(basename "$f")" "$f" && cmp -s "$bk/$(basename "$f")" "$f" || die "restore of $f failed: copy $bk by hand"
    say "restored $f from $bk"
  done
  restart_and_wait || die "service did not start after the rollback: check $SERVICE"
  record "option rollback:$done_files"
  say "done"; }

status_options() {
  local section=${1:-} r f bk
  [ -n "$section" ] && show_effective "$section"
  echo "option backups in $BK_ROOT:"
  for r in $(ls -1 "$BK_ROOT"/[0-9]*/RECORD 2>/dev/null | sort); do
    f=$(awk '$1 == "file" {print $2}' "$r"); bk=$(dirname "$r")
    if cmp -s "$bk/$(basename "$f")" "$f"; then s="file now as before this change"; else s="file CHANGED since"; fi
    echo "  $bk: $(awk '$1 == "section" {print "[" $2 "]"} $1 == "set" {print $2 "=" $3}' "$r" | paste -sd' ' -) in $f ($s)"
  done
  local seen='' changed=''
  for r in $(ls -1 "$BK_ROOT"/[0-9]*/RECORD 2>/dev/null | sort); do   # first backup of each file = as found
    f=$(awk '$1 == "file" {print $2}' "$r")
    case " $seen " in *" $f "*) continue ;; esac
    seen="$seen $f"; cmp -s "$(dirname "$r")/$(basename "$f")" "$f" || changed="$changed $f"
  done
  if [ -z "$seen" ]; then echo "STATE: no option changed by this tool"
  elif [ -z "$changed" ]; then echo "STATE: PRE-TEST, every changed file matches its first backup:$seen"
  else echo "STATE: CHANGED, differs from the first backup:$changed (run rollback)"; fi; }

case "$CMD" in
  set) set_option "$@" ;;
  rollback) rollback_options ;;
  status) status_options "$@" ;;
  *) echo "usage: $0 set <section> $OPT <true|false> --file <file> | rollback | status [<section>]"; exit 2 ;;
esac
