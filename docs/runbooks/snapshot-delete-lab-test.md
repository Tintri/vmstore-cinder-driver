# Runbook: snapshot delete lab test (VME-34)

Script: `test/lab/snapshots/run.sh`. Issue: [VME-34](https://ime-ddn.atlassian.net/browse/VME-34) (action item of VMTS-279).

## What it tests

Before 3.0.12 the driver never recorded which VMstore snapshot belongs to a Cinder
snapshot: delete looked it up by description (`snapshot-<id>`) and treated "not found"
as success, so a lookup that failed for another reason could leave the array snapshot
behind or end in `error_deleting`. With VME-34, create records the VMstore snapshot
UUID in the Cinder snapshot's `provider_id`, delete uses it (falling back to the name
for older snapshots), and a NotFound from VMstore counts as success only after a fresh
listing confirms the snapshot is gone.

| Check | What is verified |
| --- | --- |
| **S1** | A new snapshot's `provider_id` equals the UUID of the one VMstore snapshot described `snapshot-<id>`. A normal delete then removes both. |
| **S2** | The VMstore snapshot is deleted out of band (by the helper, through the driver's own client), then the Cinder snapshot is deleted. Passes when Cinder ends `deleted` and VMstore has no snapshot left. With VME-34 this is the path where VMstore answers NotFound and the driver lists again before reporting success. |
| **S3** | Snapshot with a clone: volume from the snapshot, then delete the snapshot. Records Cinder's status, whether VMstore kept the snapshot, and the log line. Then deletes the clone and the parent. Passes when Cinder ended `deleted` and no VMstore snapshot is left once the clone and parent are gone; a leftover is reported as a leak. Behaviour with a live clone follows the decision in issue 05 (today: if VMstore refuses because of the clone, the driver logs a warning and Cinder marks the snapshot deleted). |
| **S4** | A snapshot without `provider_id`, as created by 3.0.11: the helper clears `provider_id` on a new test snapshot, then Cinder deletes it. Passes when Cinder ends `deleted` and the VMstore snapshot is gone (found by name). For a real upgrade, see "S4 with a real upgrade". |

## Expected results

| Driver | S1 | S2 | S3 | S4 |
| --- | --- | --- | --- | --- |
| 3.0.11 (from the code, not measured) | FAIL: `provider_id` not set | PASS: the name lookup finds nothing, delete is a no-op | to be measured (depends on whether deleting the parent removes the array snapshot) | PASS: every 3.0.11 delete works by name |
| 3.0.12-beta before VME-34 | as 3.0.11 (same create and delete code) | as 3.0.11 | to be measured | as 3.0.11 |
| 3.0.12 with VME-34 | PASS | PASS | PASS | PASS |

## Prerequisites and access levels

- **Required:** ssh key (BatchMode) to the CLI host (`CLI_SSH`) with admin credentials
  in `CLI_RC`. The file is sourced there and never copied. A VMstore-backed volume type
  (`VOLUME_TYPE`) and quota for two 1 GB volumes, one clone and four snapshots.
- **Optional, needed by S1, S2 and S4:** ssh key to a host running cinder-volume for
  that type (`CINDER_SSH`), with the service's Python and environment (`CINDER_VENV`,
  `CINDER_CONF_DIR`; the defaults are PCD paths). The run copies `snapshot_lab.py` to
  `/root/<PREFIX>-lab` there and removes it at the end.
- **Optional:** ssh to every host serving the type (`LOG_SSH`) to show the
  cinder-volume log lines of each delete. The log path defaults to
  `/var/log/pf9/cindervolume-base.log` (used on PCD cinder hosts in earlier lab work;
  not checked on PCD 2026.8.1), overridden with `CINDER_LOG`. Log times are compared
  with the run's start in UTC.
- python-cinderclient on the CLI host is optional: with it the run prints the
  `driver_version` the running cinder-volume reports (`cinder get-pools --detail`).

Without `CINDER_SSH`, S1, S2 and S4 report SKIP and S3 checks Cinder status only.

### How the helper reaches VMstore

`snapshot_lab.py` runs with the cinder-volume service's own Python and configuration.
It reads the backend's section (from the volume's host `host@backend#pool`), imports
the driver class named by `volume_driver` and the `api` and `options` modules next to
it, and builds the driver's own REST client from that section, as cinder-volume does.
VMstore credentials stay in the service configuration: the helper never prints, logs or
writes them, and it silences the driver's logging. `array-delete` and
`clear-provider-id` refuse any snapshot not named `<PREFIX>-*`.

## Run

```bash
cd test/lab/snapshots
cp lab.env.example my-lab.env     # edit hosts, volume type, PREFIX
./run.sh my-lab.env               # about 10 minutes
```

Run it from a workstation; it drives everything over ssh. Output goes to the terminal
and to a log file under `$TMPDIR` (path printed at the end).

Exit codes: `0` every check passed (SKIP allowed), `1` a check failed, `2` precheck or
setup problem (nothing tested).

## Reading the output

- `### precheck`: CLI access, volume type, cinder-volume services for the backend,
  leftover `<PREFIX>-*` objects, helper prerequisites and log access.
- `### source volume`: the volume's host and backend section, the driver version the
  running service reports, and the version installed on `CINDER_SSH` (the code on
  disk; it differs from the running one until cinder-volume is restarted).
- `### S1` to `### S4`: per check, Cinder status, `provider_id`, the VMstore
  snapshot UUIDs described `snapshot-<id>` (`[]` is none), cinder-volume log lines,
  then `RESULT`. "not tested" in a FAIL means a setup step failed, not the driver.
- `=== cleanup` and `=== summary`: what was deleted, VMstore snapshots of test
  snapshots that outlived them (deleted at this point), whether any other server,
  volume or snapshot changed during the run, and the S1 to S4 table.

## Safety and cleanup

- Objects: volumes `<PREFIX>-src`, `<PREFIX>-s3-src`, `<PREFIX>-s3-clone`; snapshots
  `<PREFIX>-s1` to `<PREFIX>-s4`. The precheck refuses to start if any `<PREFIX>-*`
  volume or snapshot exists.
- Cleanup runs on any exit, including failures and Ctrl-C: snapshots first, then the
  clone, then the source volumes. Snapshots and volumes in an error state are reset to
  `available` first; these are only the test's own objects. VMstore snapshots of the
  run's snapshots that are still on the array are deleted through the helper.
- Writes outside Cinder's API: S2 deletes the VMstore snapshot of `<PREFIX>-s2`, S4
  clears `provider_id` of `<PREFIX>-s4` in the Cinder database, cleanup deletes
  leftover VMstore snapshots of the run's snapshots. Everything else is read-only.
- The script lists all other servers, volumes and snapshots before and after, and
  reports any difference.

### Manual cleanup after a hard interruption

If the workstation or ssh session dies (cleanup cannot run), on the CLI host:

```bash
source /root/admin.rc; export OS_REGION_NAME=<region>
openstack volume snapshot list --all-projects | grep ' vme34-'   # set --state available if stuck, then delete
openstack volume list --all-projects | grep ' vme34-'            # delete the -clone first, then the others
```

and remove `/root/vme34-lab` on the cinder-volume host. Replace `vme34` with your
`PREFIX`. A VMstore snapshot left behind is described `snapshot-<cinder snapshot id>`
in the VMstore UI.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `helper failed ... no [<backend>] section` | `CINDER_SSH` does not serve this backend, or `CINDER_CONF_DIR` is not the service's config directory. |
| `helper failed ... ConnectionError` or a login error | The cinder-volume host cannot reach VMstore with the backend's settings; the service itself is probably failing too (check its log). |
| `driver_version reported by cinder-volume: unknown` | No python-cinderclient on the CLI host; information only. |
| `installed` version differs from the running one | cinder-volume was not restarted after the driver was replaced. |
| S2 or S4 `FAIL: not tested` | A setup step failed (out-of-band delete, `provider_id` clear); see the lines above it. |
| No log lines | Wrong `CINDER_LOG`, or the delete ran on a host missing from `LOG_SSH`. |

## S4 with a real upgrade

S4 simulates an old snapshot by clearing `provider_id`. To test a snapshot really
created by 3.0.11, by hand, on the CLI host:

1. With 3.0.11 running: create a 1 GB volume `vme34-up-src` on the type and a snapshot
   `vme34-up-snap`. Note the snapshot ID.
2. Upgrade the driver on every cinder-volume host serving the type and restart
   cinder-volume (on PCD: `pf9-cindervolume-base`). Confirm the new version with
   `cinder get-pools --detail` (`driver_version`).
3. Delete the snapshot. Expected: `deleted`, a log line "Deleting snapshot
   snapshot-<id>" followed by "deleted successfully", and no VMstore snapshot
   described `snapshot-<id>` (VMstore UI, or `snapshot_lab.py array-list <id>` on the
   cinder-volume host).
4. Delete `vme34-up-src`.

## Run history

| Date | Lab | Driver | S1 | S2 | S3 | S4 | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- |
