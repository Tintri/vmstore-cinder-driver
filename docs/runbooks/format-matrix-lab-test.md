# Runbook: NFS volume format matrix lab test (VME-29)

Script: `test/lab/format-matrix/run.sh`. Issue: [VME-29](https://ime-ddn.atlassian.net/browse/VME-29).
Driver changes and option flips for a run: [driver-swap.md](driver-swap.md).

## What it tests

Platform9's "NFS Volume Format Matrix" follows a volume through common operations and
records, at each step, three formats that must agree:

- **Real file:** the format of the volume's active file on the share (qcow2 header or raw),
  read with `qemu-img` on a cinder-volume host. `qcow2-overlay` means qcow2 with a backing
  file.
- **Recorded:** the format Cinder stores in the volume's `admin_metadata['format']`; `none`
  when there is no record (attach then probes the file).
- **libvirt:** the driver type in the domain XML of the VM using the volume (`virsh
  dumpxml`), `-` when the volume is not attached.

A record that disagrees with the file breaks VMs on the next Nova refresh (cold or live
migration, resize, hard reboot) once Nova stops probing the file itself. Platform9 asked
Tintri to run the matrix on VMstore with `vmstore_qcow2_volumes = false` on Aug P2 (driver
3.0.9 with their Nova patch) and on Dec (driver 3.0.11 or later, without the patch), and to
report failures. This script runs the same steps and writes the same table.

## Modes

| Command | Steps |
| --- | --- |
| `./run.sh <lab.env>` | One host: Platform9 steps 1 to 19 |
| `./run.sh --two-host <lab.env>` | Two hosts: volume migrate to the other cinder-volume host, then move the VM (T1 to T9). `LIVE=1` live migrates instead of the cold migration in T5 |
| `./run.sh --flip-phase create <lab.env>` | Flip test, part 1 (option on): VM, boot volume and an empty qcow2 volume, kept |
| `./run.sh --flip-phase after <lab.env>` | Flip test, part 2, after the backend was switched to off and restarted: F1 to F9 on the old volumes, then cleanup |

### Steps and Platform9's numbers

One host (same numbers as Platform9's single-host tables):

| # | Operation | Volume shown |
| --- | --- | --- |
| 1 | Create empty volume | empty volume |
| 2 | Create from image | image volume |
| 3 | Extend, detached (1 to 2 GB) | image volume |
| 4 | Snapshot, offline | image volume |
| 5 | Create from snapshot (+1 GB) | new volume (disk must be 3 GB) |
| 6 | Clone (+1 GB) | clone (disk must be 3 GB) |
| 7 | Extend, detached, with a snapshot (2 to 3 GB) | image volume |
| 8 | Boot VM from the clone | clone |
| 9 | Attach empty volume | empty volume |
| 10 | Snapshot, online (boot volume, `--force`) | clone |
| 11, 12, 13 | Stop / start, soft reboot, hard reboot | clone; `b` rows: the empty volume |
| 14 | Extend, attached (1 to 2 GB, API 3.42) | empty volume |
| 15 | Delete online snapshot | clone |
| 16 | Detach | empty volume |
| 17 | Stop / start after all changes | clone |
| 18 | Boot from image, Nova creates the volume (on `VOLUME_TYPE`, compute API 2.67) | Nova's volume, renamed `<PREFIX>-nova-vol` |
| 19 | Stop / start, hard reboot that VM | Nova's volume |

Two hosts (Platform9's "volume migrate, then move the VM" table): T1 boot from image on SRC,
T2 data volume from image, T3 migrate it to the other cinder-volume host, T4 attach, T5 cold
migrate the VM to DST (live with `LIVE=1`), T6 stop / start and hard reboot on DST, T7 detach
and extend the migrated volume, T8 cold migrate back to SRC, T9 resize the VM to a 2-vCPU
flavor. `b` rows show the data volume. PCD confirms cold migrations and resizes by itself
within seconds, so T5, T8 and T9 pass on the result (the VM is ACTIVE on the target host, or
on the new flavor) whether or not the run saw `VERIFY_RESIZE`.

Flip test (Platform9's "created with qcow2 on, then switched off" table): C1 to C3 in phase
create (empty volume, VM booted from an image volume, empty volume attached); F1 cinder-volume
restarted with the VM running, F2 stop / start and hard reboot, F3 online snapshot of the old
volume then stop / start, F4 detach it, F5 clone it (+1 GB), F6 volume from that snapshot
(+1 GB), F7 extend the old volume detached while it has a snapshot, F8 attach the post-flip
clone and stop / start, F9 new empty volume.

### Result of a row

| Result | Rule | Counts as |
| --- | --- | --- |
| match | file, record (or none) and libvirt (or not attached) agree, and after an extend or a "+1 GB" create the disk has the Cinder size | PASS |
| record drift | a record exists and differs from the real file | FAIL |
| libvirt mismatch | libvirt opens the volume with a type other than the real file | FAIL |
| silent failure | the operation succeeded but the disk's virtual size is not the Cinder size | FAIL |
| failed | the operation ended in an error state, timed out, or the volume file is missing | FAIL |
| not run | an earlier step it depends on failed | FAIL |
| n/a | the API did not apply an in-use extend and the volume stayed `in-use` at its old size (backend without online extend, for example 3.0.9) | not counted |

`qcow2-overlay` matches a `qcow2` record. The libvirt column reads `unchecked` when the
compute host has no ssh access; those rows are judged on file and record only.

## Expected results

From the code, not measured. The run history below records what was measured.

| Driver, option | Expected |
| --- | --- |
| 3.0.9, on | 2 record drift (image volume: raw file recorded qcow2); 3 and 7 failed (`Image is not in qcow2 format`); 18 record drift (libvirt still raw: the Nova patch probes the file); 14 n/a; the rest match. Clones and volumes from snapshots (5, 6) have no record |
| 3.0.9, off | every row match except 14 n/a |
| 3.0.11, on | 2, 3, 7 and 18 match (image volumes recorded raw); 14 extends in use |
| 3.0.12, on or off | every row match; T3 and T7 are the volume migration cases VME-29 fixes |

## Prerequisites

- ssh keys (BatchMode) to the CLI host (admin credentials in `CLI_RC`, sourced there, never
  copied) and to a cinder-volume host for `VOLUME_TYPE` (`CINDER_SSH`, required: the file
  and record columns come from it). ssh to the compute hosts for the libvirt column.
- One VM at a time of `FLAVOR_RAM_MB` on SRC (two-host: also on DST), a small bootable image
  (cirros), a VM network. The CLI must accept compute API 2.67 for step 18 and T1 (`--block-device
  ...,volume_type=...`) and volume API 3.42.
- Two-host: two cinder-volume hosts serving the backend (`VOLUME_HOSTS`) and two compute hosts.
  Live migration (`LIVE=1`) needs DST to offer SRC's QEMU machine type; the precheck checks it.

The precheck prints, for every host in `LOG_SSH`, the effective `vmstore_qcow2_volumes` for the
backend (read from the config directory in the order cinder-volume reads it; nothing else is
read) and the driver version from the last start line in the log. The same values head
`matrix.md`. Hosts that disagree are reported.

## Run

```bash
cd test/lab/format-matrix
cp lab.env.example my-lab.env           # placeholders: fill in your lab; keep it out of the repo
PCD_RELEASE="Aug P2" ./run.sh my-lab.env            # one host, about 30 minutes
./run.sh --two-host my-lab.env                      # two hosts, about 30 minutes
```

Flip test (the option must be on for phase create, off for phase after; `FORCE=1` overrides):

```bash
./run.sh --flip-phase create my-lab.env
../driver-swap/swap.sh option my-lab.env <host> <backend section> false --file <config file>   # every host serving the backend
./run.sh --flip-phase after my-lab.env
../driver-swap/swap.sh option-rollback my-lab.env <host>                                     # every host changed
```

Phase create keeps its objects and writes a state file under `${TMPDIR:-/tmp}/<PREFIX>-flip/`;
use the same `PREFIX` and `TMPDIR` for phase after. If phase after finds the option still on,
it stops without deleting anything, so the flip can be done and phase after rerun.

Output: the run log and `matrix.md` in the output directory (path printed at the end). Send
`matrix.md` to Platform9 as it is. Do not commit run outputs to this repository.

Exit codes: `0` every row matched (n/a allowed), `1` a row failed, `2` precheck or setup problem.

## Safety and cleanup

- Only `<PREFIX>-*` objects are created; Nova's boot volume is renamed `<PREFIX>-nova-vol` as
  soon as it exists. The precheck refuses to start if `<PREFIX>-*` objects exist (except flip
  phase after, which uses the kept ones).
- Nothing is written to volume files; file checks use `qemu-img info -U` and read the header.
- Cleanup runs on any exit: VMs, then volumes newest first, then snapshots, repeated until
  nothing is left (a volume with snapshots is deleted in the next round). Snapshots are
  deleted after the volumes made from them because VMstore refuses to delete a snapshot a
  clone still uses, and drivers before 3.0.12 then report it deleted while the array keeps it.
- Other servers, volumes and snapshots are listed before and after the run and any difference
  is reported.

Manual cleanup after a hard interruption, on the CLI host:

```bash
source /root/admin.rc; export OS_REGION_NAME=<region>
openstack server list --all-projects --name '^<PREFIX>-'          # delete --wait each
openstack volume list --all-projects --long | grep ' <PREFIX>-'   # volumes from snapshots and clones first
openstack volume snapshot list --all-projects | grep ' <PREFIX>-'
openstack flavor list --all | grep <PREFIX>-
```

and remove `/root/<PREFIX>-lab` on the cinder-volume host and `${TMPDIR:-/tmp}/<PREFIX>-flip`
on the workstation.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| 18 or T1 not run, `--block-device` or `volume_type` rejected | The CLI or Nova does not accept compute API 2.67. Update python-openstackclient on the CLI host. |
| T5 or T8 `VM ERROR`, `No valid host` | DST (or SRC) has no placement memory for the flavor. Free capacity or lower `FLAVOR_RAM_MB`. |
| T5 or T8 `VM on <host>, not <target>` | Nova did not move the VM (scheduling failed, the VM stays ACTIVE where it was). Check placement capacity on the target and the nova-conductor log. |
| T9 `flavor still ...` | The resize did not apply. Check that SRC has capacity for 2 vCPUs. |
| T3 `migration error` | `VOLUME_HOSTS` must be cinder-volume host names serving the same backend, both up. |
| libvirt column `unchecked` | No `SRC_COMPUTE_SSH` / `DST_COMPUTE_SSH`, or the VM runs on a host not in the env file. |
| precheck: hosts disagree on `vmstore_qcow2_volumes` | Finish or roll back the option change on every host serving the backend first. |

## Run history

| Date | Lab | Release | Driver | Option | Mode | Match | Failed | n/a | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
