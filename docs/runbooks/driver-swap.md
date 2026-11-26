# Runbook: put a driver build or an option value on lab hosts, then return them

Scripts: `test/lab/driver-swap/swap.sh` (workstation), `host-driver.sh` and `host-option.sh`
(copied to and run on each cinder-volume host). Used by the lab suites under `test/lab/` when a
run needs a driver other than the one installed, or a different `vmstore_qcow2_volumes`.

## Before you start: restart impact

`deploy`, `rollback`, `option` and `option-rollback` restart cinder-volume on the host. That
restarts **every backend the host serves**, not only the one under test: other people's
volumes on that host are unavailable for create, delete, attach and snapshot operations for
about a minute (running VMs keep their disks). Lab hosts are shared:

- Tell the other users of the host first, and agree on the window.
- `swap.sh` lists the backends of each host and asks for `yes` before every restart
  (`--yes` skips the question; use it only in agreed windows).
- Change only hosts you will return in the same session, and always finish with `status`.

## How state is kept

On each host, `/root/<PREFIX>-driver-swap/` (keep the same `PREFIX` until the rollback):

- `backups/NNNN-<UTC time>/`: the driver files as they were before each change, with a
  `MANIFEST` (version, md5, owner, mode, driver directory). `0001-...` is the state before the
  first deploy, the **pre-test state**; `rollback` returns there by default.
- `option-backups/NNNN-<UTC time>/`: the config file as it was before each option change, with
  a `RECORD`. The first backup of each file is its pre-test copy.
- `history.log`: one line per change.

The driver directory is the one the cinder-volume service's own Python imports (on PCD Aug and
later under `lib/python3.12`; an old copy under `lib/python3.9` is ignored by the service, and
`status` lists it as unused).

## Deploy a build

```bash
cd test/lab/driver-swap
./swap.sh status my-lab.env root@<host1> root@<host2>          # record the starting state
./swap.sh deploy my-lab.env origin/3.0.12-beta root@<host1> root@<host2>
```

For each host, in order: build the five driver files from `git archive <ref>`, copy them,
back up the installed driver, `py_compile`, check the build's `VERSION`, install with the
original owner and mode, check the service's Python imports the new version, restart, and
wait up to 3 minutes for `Starting volume driver VmstoreNfsDriver (<version>)` in the log. Any
failure after the copy restores the backup (and restarts on it if the restart had happened),
and `swap.sh` stops before the next host. The output ends with the errors logged since the
restart.

Every host serving the backend under test must run the same build: a volume operation can
land on any of them.

## Change vmstore_qcow2_volumes

```bash
./swap.sh option my-lab.env root@<host> <backend section> false --file <config file>
```

Only this option can be changed. The edit changes the option's line in that section (or adds
it) and leaves the rest of the file byte for byte; the file is backed up first, and a failed
restart restores it.

**On PCD, use the cluster blueprint.** Measured 2026-11-26 on PCD 2026.8 (Aug P2): PCD's
host agent rewrote `cinder.conf` from the blueprint 9 to 12 seconds after a hand edit, before
the restart, so cinder-volume came back with the old value. `host-option.sh` now waits
`OPTION_SETTLE_SECONDS` (default 45) after its edit and stops without restarting if the file
changed. Change `vmstore_qcow2_volumes` in the PCD cluster blueprint (it rewrites `cinder.conf`
and restarts cinder-volume on the storage hosts, as customers do), and use `status` to confirm.
Each blueprint apply also rewrites `pf9_identifier_timestamp` in every backend section, so
after a blueprint round trip (option off, then back on) the file differs from the as-found
copy only in those lines. `status` and `rollback` ignore them: such a file counts as
PRE-TEST, and the backup line says "apart from PCD's pf9_identifier_timestamp".

**Which file, elsewhere.** A blueprint change rewrites `cinder.conf` and restarts
cinder-volume, so a hand edit can be overwritten. cinder-volume
reads the config directory in sorted order and the last value wins, so a value in
`cinder_override.conf` takes precedence over `cinder.conf`. Whether PCD's host agent also
rewrites `cinder_override.conf` is not known, so the tool does not choose: give `--file` (or
`OPTION_FILE` in the env file), and run `status` again after any blueprint change on the
cluster. To test exactly what customers will do, change the option through the blueprint
instead and use this tool only to read the state.

Change the option on every host serving the backend, then check with `status` that they agree
(the format-matrix precheck also reports disagreement).

## Return the lab to its pre-test state

```bash
./swap.sh option-rollback my-lab.env root@<host>      # each host where an option was changed
./swap.sh rollback my-lab.env root@<host1> root@<host2>
./swap.sh status my-lab.env root@<host1> root@<host2>
```

`rollback` restores `backups/0001-...` (verified against its MANIFEST), saves the current files
first, restarts and waits for the pre-test version's start line. `option-rollback` restores the
first backup of every file it changed and restarts once. Both report "already" and change
nothing when the host is already there.

`status` ends with one line per host and tool:

| Line | Meaning |
| --- | --- |
| `STATE: PRE-TEST, files and running driver match ...` | driver as found before the first deploy, and running |
| `STATE: PRE-TEST FILES, but the last start line is not ...` | files restored, service not restarted on them |
| `STATE: TEST BUILD <version> installed; pre-test is ...` | a test build is installed: roll back |
| `STATE: UNKNOWN to this tool ...` | no backup in this `PREFIX`'s directory: nothing deployed from here (check other `/root/*-driver-swap` directories) |
| `STATE: PRE-TEST, every changed file matches its first backup` / `STATE: CHANGED ...` | config files as found (PCD's `pf9_identifier_timestamp` lines ignored) / still changed |
| `STATE: no option changed by this tool` | no option backups |

### If the scripts cannot finish

On the host, as root, with `D` the driver directory shown by `status` and `BK` the
`backups/0001-...` directory:

```bash
cd "$BK" && md5sum -c <(grep -E '\.py$' MANIFEST)                       # backup intact
read -r _ o _ g _ m < <(awk '$1 == "owner"' MANIFEST)
for f in api.py __init__.py nfs.py options.py utils.py; do install -m "$m" -o "$o" -g "$g" "$f" "$D/$f"; done
rm -rf "$D/__pycache__"; systemctl restart pf9-cindervolume-base
grep 'Starting volume driver VmstoreNfsDriver' /var/log/pf9/cindervolume-base.log | tail -3
```

For a config file: `cp -a option-backups/0001-.../<file> <conf.d>/<file>` (the first backup of
that file), then restart the same way.

## Checklist for the ticket

```text
Lab hosts changed:            <host list>
Other users told:             <who, when>
Before:                       swap.sh status output (STATE lines), driver <version> on each host
Changes:                      deploy <ref> = <sha> (<version>) at <time>; option [<section>] = <value> in <file> at <time>
Runs:                         <suite, mode, result, evidence location>
Returned:                     option-rollback at <time>; rollback at <time>
After:                        swap.sh status output: every STATE line PRE-TEST or "no option changed"
```
