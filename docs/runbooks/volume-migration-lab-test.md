# Runbook: volume migration lab test (VME-29)

Script: `test/lab/volume-migration/run.sh`. Issue: [VME-29](https://ime-ddn.atlassian.net/browse/VME-29).

## What it tests

The driver has no `migrate_volume`, so Cinder moves our volumes with its generic
migration: create a new volume on the destination, byte-copy the data with `dd`,
then swap the database records. The script runs the scenario Platform9 reported
plus the checks that came out of the investigation:

| Check | What is verified |
| --- | --- |
| **R1** | A volume created from an image (raw file, recorded raw) still records `raw` after it is migrated to the other cinder-volume host. The generic migration creates the target as qcow2 (`vmstore_qcow2_volumes`) and the record swap carries that `qcow2` record onto the original volume ID. |
| **R2** | A qcow2 volume filled with a known pattern is intact after migration: file larger than the virtual size, `qemu-img check` clean, every byte reads back. The copy uses the volume size as the `dd` count, which cuts off qcow2 data stored past the virtual size. |
| **R3** | Platform9's steps: boot-from-volume VM, attach the migrated image volume, live migrate the VM. Passes when the VM is ACTIVE on the destination and the volume is opened with its real file format. |
| **R4** | Detached extend of the migrated image volume (1 to 2 GB). Fails when extend trusts a stale `qcow2` record on a raw file (`Image is not in qcow2 format`). |

## Expected results

| Driver | R1 | R2 | R3 | R4 |
| --- | --- | --- | --- | --- |
| 3.0.11 (measured 2026-11-19, PCD 2026.8.1, region jan) | FAIL: record qcow2, file raw | FAIL: 1074135040 to 1073741824 bytes, 6 qcow2 errors, pattern lost in the second half | not run (destination full, see below) | FAIL: `error_extending` |
| 3.0.12-beta before the VME-29 fix | FAIL | FAIL | to be measured | PASS expected: since VMTS-252 (#18) extend uses the file probe when the record says qcow2 |
| 3.0.12 with the VME-29 fix | PASS | PASS | PASS | PASS |

## Prerequisites

- Two compute hosts in one region, SRC and DST, each running cinder-volume for the
  same backend (`VOLUME_TYPE`); both services enabled and up.
- **QEMU machine types:** DST must offer the machine type SRC guests use. A newer
  QEMU accepts guests from an older one, not the reverse. In the jan lab,
  openstack-4 (Ubuntu 24.04, QEMU 8.2, `pc-i440fx-noble`) can receive guests from
  openstack-5 (Ubuntu 22.04, QEMU 6.2, `pc-i440fx-jammy`), but openstack-4 guests
  cannot move to openstack-5.
- **Placement memory on DST** for the test flavor (256 MB by default). Nova counts
  `(total - reserved) * allocation_ratio - used` from the resource provider, not the
  host's real free memory. Stopped VMs still hold their allocation.
- ssh keys (BatchMode) to the CLI host, both compute hosts and the cinder-volume host.
  Admin credentials stay on the CLI host (`CLI_RC` is sourced there, never copied).
- On the cinder-volume host: `qemu-img`, `qemu-io` and the cinder-volume service's
  Python (`CINDER_VENV`). The defaults are PCD paths; `NOVA_LOG` (environment
  variable) overrides the nova-compute log path, default `/var/log/pf9/ostackhost.log`.
- A small bootable image (cirros) and a VM network.

`run.sh` checks all of this before creating anything and exits with code 2 if
something is missing. The VM checks are skipped with `--cinder-only`.

## Run

```bash
cd test/lab/volume-migration
cp lab.env.example my-lab.env     # edit hosts, IDs, type, image, network
./run.sh my-lab.env               # full run, about 15 minutes
./run.sh --cinder-only my-lab.env # R1, R2, R4 only: no VM, no Nova capacity needed
```

Run it from a workstation; it drives everything over ssh. Output goes to the
terminal and to a log file under `$TMPDIR` (path printed at the end).

Exit codes: `0` every check passed (SKIP allowed), `1` a check failed, `2` precheck or
setup problem (nothing tested).

## Reading the output

- `### precheck`: DST free placement memory and the machine type check.
- `### A. created`: record and file of each volume before migration. Expected:
  image volumes `record=raw file=raw`, the filled volume `record=qcow2 file=qcow2`
  with more than 1073741824 bytes.
- `### B. after volume migration`: same fields after the generic migration, then
  `RESULT R1` and `RESULT R2`.
- `### C. after live migration`: VM host, migration record, libvirt disk type against
  the file format, then `RESULT R3`, followed by nova-compute errors and warnings for
  the VM and the volume from both hosts.
- `### D. after extend`: `RESULT R4`.
- `=== cleanup` and `=== summary`: what was deleted, whether any other server or
  volume changed during the run (the script never touches them; a change means
  someone else was working on the lab), and the R1 to R4 table.

## Safety and cleanup

- Every object is named `<PREFIX>-*`: `-boot`, `-data`, `-filled` volumes, `-vm`,
  and a private `-flavor`. The precheck refuses to start if any such object exists.
- Cleanup runs on any exit, including failures and Ctrl-C. It deletes the VM, every
  `<PREFIX>-*` volume (also a migration target left by a failed migration),
  leftover attachments of those volumes, the flavor, and the helper directory on the
  cinder-volume host. Volumes in an error state are reset to `available` first; these
  are only the test's own volumes.
- The run writes to volume files directly only for `<PREFIX>-filled` (pattern fill
  with `qemu-io`); every other file access is read-only.
- The script snapshots all other servers and volumes before and after, and reports
  any difference.

### Manual cleanup after a hard interruption

If the workstation or ssh session dies (cleanup cannot run), on the CLI host:

```bash
source /root/admin.rc; export OS_REGION_NAME=<region>
openstack server list --all-projects --name '^vme29-'            # delete --wait each
openstack volume list --all-projects --long | grep ' vme29-'      # set --state available if stuck, then delete
openstack flavor list --all | grep vme29-                         # delete
```

and remove `/root/vme29-lab` on the cinder-volume host. Replace `vme29` with your
`PREFIX`.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `placement memory free: N MB (flavor needs 256)` | DST is full in placement. Free a VM on DST (with its owner's agreement), lower `FLAVOR_RAM_MB` (cirros needs about 128 MB), or run `--cinder-only`. |
| `DST QEMU lacks machine type` | Swap SRC and DST (move guests from the older QEMU to the newer one) or pick another host pair. |
| Image volume creation fails, `Could not find any available weighted backend` | Seen in region anotherjan: the backend cannot create volumes from images there. Use another backend or region. |
| R3 `live migration rejected before start` | The scheduler chose no destination (usually capacity). The VM stays ACTIVE on SRC. |
| Volume migrate refused | `--host` must be `<host>@<backend>#<pool>` of the same backend; a type pinned to another backend refuses the move. |

## Run history

| Date | Lab | Driver | R1 | R2 | R3 | R4 | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-11-18 | anotherjan, openstack-2, PCD 2026.1.1 | 3.0.9 | FAIL | (empty volume: cut to 1073741824 bytes) | not run | FAIL | Prototype; raw image volume simulated by hand (image volumes fail on that backend). |
| 2026-11-19 | jan, openstack-5 to openstack-4, PCD 2026.8.1 | 3.0.11 | FAIL | FAIL | not run | FAIL | Prototype of this script. Live migration rejected: openstack-4 had 60 MB placement memory free. |
| 2026-11-19 | jan, PCD 2026.8.1, `--cinder-only` | 3.0.11 | FAIL | FAIL: `check_rc=2`, pattern lost | SKIP | FAIL | This script. Full run stopped at the precheck (60 MB free on openstack-4). Cleanup complete, other objects unchanged. |
