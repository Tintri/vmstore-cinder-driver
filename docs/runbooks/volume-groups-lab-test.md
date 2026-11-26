# Runbook: volume groups and group snapshots lab test (VME-38)

Script: `test/lab/volume-groups/run.sh`. Issue: [VME-38](https://ime-ddn.atlassian.net/browse/VME-38).

## What it tests

The driver does not implement Cinder's group calls (`create_group`,
`create_group_snapshot`, `create_group_from_src` and the rest). For group types without
`consistent_group_snapshot_enabled`, Cinder then runs its own generic code, which calls the
driver once per volume: `create_snapshot` for each member of a group snapshot,
`create_volume_from_snapshot` or `create_cloned_volume` for a group made from a source,
`delete_snapshot` and `delete_volume` on delete. Group snapshots made this way are not
crash-consistent across volumes (one snapshot after the other). The script checks that this
path works on a VMstore backend, including what is left on the array.

| Check | What is verified |
| --- | --- |
| **G1** | A private group type with no specs, and a group on `VOLUME_TYPE`, become `available`. |
| **G2** | Two 1 GiB volumes created in the group are `available` and belong to it. Each gets its own byte pattern in two 64 MiB regions (`qemu-io`, on the cinder-volume host), and its format record agrees with its file (see below). |
| **G3** | A group snapshot becomes `available` with two member snapshots, each `available` and present on VMstore. |
| **G4** | After both volumes are overwritten with new patterns, a group made from the group snapshot has two volumes holding the patterns at snapshot time, with records that agree with their files. |
| **G5** | A group made from the source group has two volumes holding the current patterns. |
| **G6** | Removing a volume from the group and adding it back updates its `group_id` both times. |
| **G7** | A second group snapshot, with no volumes made from it, is deleted in Cinder and on VMstore. |
| **G8** | Deleting the group snapshot that G4's volumes came from, while that group still exists. |
| **G9** | Deleting every group with its volumes removes the groups, the volumes and their files. |

**Format record.** The driver hands a file to Nova as raw when its format record (`admin_metadata['format']`) or the file says raw, otherwise as the file's own format, so a volume with no record is used in its real format (clones of qcow2 volumes have none, by design since 3.0.11). A check passes when the record is absent (`record=None`) or equals the file's format, and fails on a contradiction: `raw` on a qcow2 file, or a stale `qcow2` on a raw file.

**Why G8 matters.** VMstore refuses to delete a snapshot that volumes created from it still
use (ERR-API-3107), and since VME-34 the driver reports that as `SnapshotIsBusy` so Cinder
keeps the snapshot. Cinder's generic group code catches `SnapshotIsBusy`, marks the member
`available`, and then destroys every member record and the group snapshot anyway (read in
upstream `cinder/volume/manager.py`, `_delete_group_snapshot_generic` and
`delete_group_snapshot`). If VMstore refuses, the snapshots stay on the array with no Cinder
record: G8 fails with "orphaned on the array". That outcome means the driver needs its own
`delete_group_snapshot` (VME-38) before groups can be called supported. If VMstore deletes
them, or Cinder keeps the group snapshot, G8 passes.

## Expected results

| Driver | G1 to G7 | G8 | G9 |
| --- | --- | --- | --- |
| 3.0.12 (to be measured) | PASS expected (generic path, no driver change) | FAIL expected if VMstore refuses busy snapshots (see above) | PASS |

## Prerequisites

- The `cinder` command (python-cinderclient, API 3.14 or later) on the CLI host, next to
  `openstack`. The precheck checks it.
- ssh keys (BatchMode) to the CLI host and the cinder-volume host. Admin credentials stay on
  the CLI host (`CLI_RC` is sourced there, never copied).
- On the cinder-volume host: `qemu-io` and the cinder-volume service's Python
  (`CINDER_VENV`), and the type's share mounted. VMstore is queried with the installed
  driver's own REST client and the backend's configuration, read in place
  (`test/lab/snapshots/snapshot_lab.py`).
- No VM, image or Nova capacity is needed.

## Run

```bash
cd test/lab/volume-groups
cp lab.env.example my-lab.env     # edit hosts, type
./run.sh my-lab.env               # about 10 minutes
```

Run it from a workstation; it drives everything over ssh. Output goes to the terminal and to
a log file under `$TMPDIR` (path printed at the end).

Exit codes: `0` every check passed (SKIP allowed), `1` a check failed, `2` precheck or setup
problem (nothing tested). When G1, G2 or G3 fails, the later checks are reported as SKIP and
cleanup runs.

## Reading the output

- `### precheck`: the backend of the type and the hosts serving it, then the driver version
  the cinder-volume host would load.
- `### G2`: each volume as `record=<r|None> file=<f> pattern=<p> host=<h>`. `pattern` is the byte
  both regions hold (`5a`, `3c`, `a5`, `c3`) or `none`.
- `### G3`, `### G7`, `### G8`: member snapshots with their Cinder status and how many
  VMstore snapshots carry their name.
- `=== cleanup` and `=== summary`: what was deleted, any VMstore snapshot of this run removed
  from the array, whether any other volume, snapshot or group changed during the run, and the
  G1 to G9 table.

## Safety and cleanup

- Named objects: the private group type `<PREFIX>-gtype`, groups `<PREFIX>-group`,
  `<PREFIX>-from-snap`, `<PREFIX>-from-group`, volumes `<PREFIX>-a` and `<PREFIX>-b`, group
  snapshots `<PREFIX>-gsnap1` and `<PREFIX>-gsnap2` (Cinder names member snapshots after
  them). Volumes of groups made from a source have no name; their IDs are recorded when the
  group is created. The precheck refuses to start if any `<PREFIX>-*` object exists.
- Cleanup runs on any exit: groups made from a source (with their volumes), then group
  snapshots, then the original group, any recorded volume left over (reset to `available`
  first if stuck; only this run's volumes), the group type, and the helper directory on the
  cinder-volume host.
- VMstore snapshots of this run that Cinder no longer tracks (the G8 case) are deleted from
  the array once no volume uses them. `snapshot_lab.py array-delete` deletes only snapshots
  whose Cinder record (deleted or not) is named `<PREFIX>-*`, matched by the
  `snapshot-<id>` description the driver gives VMstore.
- The run writes to volume files directly only for `<PREFIX>-a` and `<PREFIX>-b` (pattern
  fill with `qemu-io`); every other file access is read-only.
- The script lists all other volumes, snapshots and groups before and after, and reports any
  difference.

### Manual cleanup after a hard interruption

If the workstation or ssh session dies (cleanup cannot run), on the CLI host:

```bash
source /root/admin.rc; export OS_REGION_NAME=<region>
C="cinder --os-volume-api-version 3.14"
$C group-list --all-tenants 1 | grep ' vme38-'             # delete -from-* first: $C group-delete --delete-volumes <id>
$C group-snapshot-list --all-tenants 1 | grep ' vme38-'    # $C group-snapshot-delete <id>
openstack volume list --all-projects --long | grep ' vme38-'
$C group-type-list | grep vme38-                            # $C group-type-delete <id> (private types may need the ID from the run log)
```

Then check VMstore for snapshots named after the run's member snapshots (`snapshot-<id>`,
IDs in the run log) and remove `/root/vme38-lab` on the cinder-volume host. Replace `vme38`
with your `PREFIX`.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `'cinder' (python-cinderclient) with group support (API 3.14) not working` | Install python-cinderclient on the CLI host, or use a CLI host that has it. |
| G1 `group error` | Look for `No valid backend` in the scheduler log: the group type must not have `consistent_group_snapshot_enabled` (the driver reports it False). |
| G3 member snapshot `on VMstore: 0` | The driver did not record the VMstore snapshot; compare with the snapshot delete suite (`docs/runbooks/snapshot-delete-lab-test.md`). |
| G8 `orphaned on the array` | Expected while the driver relies on Cinder's generic group code; see "Why G8 matters". Cleanup removes the snapshots. |
| `REFUSED snapshot ... not <PREFIX>-*` in cleanup | A member snapshot was not named after its group snapshot; delete it on VMstore by hand after checking it belongs to the run. |

## Run history

| Date | Lab | Driver | G1 | G2 | G3 | G4 | G5 | G6 | G7 | G8 | G9 | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
