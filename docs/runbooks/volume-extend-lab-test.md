# Runbook: extend of attached volumes lab test (VME-10)

Script: `test/lab/volume-extend/run.sh`. Issues: [VME-10](https://ime-ddn.atlassian.net/browse/VME-10)
(support request [VMTS-243](https://ime-ddn.atlassian.net/browse/VMTS-243)). Design: `docs/adr/0004`,
PR #16.

## What it tests

Since 3.0.11 the driver reports `online_extend_support: True`, so Cinder accepts an
extend of an `in-use` volume. What happens to the file depends on its format:

- **raw:** the driver grows the file (`truncate`) and checks its size. Cinder then sends
  Nova a `volume-extended` event, and Nova's `virDomainBlockResize` makes the guest see
  the new size.
- **qcow2, instance running:** the driver leaves the file alone (the share is mounted
  `nolock`, so a resize from the Cinder host could corrupt an image qemu holds). Nova
  resizes it through qemu.
- **qcow2, instance shut off:** Nova skips the resize. If Nova confirms every attached
  instance is `SHUTOFF` with no task (`vmstore_online_extend_nova_check`, default
  True), the driver resizes the file offline and checks Nova again afterwards.
- **Safety net:** on a new attach, `initialize_connection` grows a file smaller than the
  volume size (an extend Nova did not apply), unless the volume is attached elsewhere.

| Check | What is verified |
| --- | --- |
| **E1** | A raw volume attached to a running VM, holding a 256 MiB pattern, is extended 1 to 2 GB. Passes when Cinder shows `in-use` at 2 GB, the file is 2 GiB, `virsh domblkinfo` on the compute host gives the guest 2 GiB, and the pattern reads back. |
| **E2** | The same with a qcow2 volume. Passes when Cinder shows `in-use` at 2 GB, the image's virtual size becomes 2 GiB (Nova resized it), the guest is given 2 GiB, and after detach `qemu-img check` is clean and the pattern reads back. |
| **E3** | A qcow2 volume attached to a VM that is then stopped is extended 1 to 2 GB. Passes when the driver resized the file while the VM was shut off (virtual size 2 GiB, `qemu-img check` clean) and the guest is given 2 GiB after the VM starts. |
| **E4** | A detached raw test volume whose file the script shrinks to 512 MiB is attached. Passes when the file is back to 1 GiB after the attach and the cinder-volume log shows the warning "smaller than its size ... growing it before attach". |

The script needs no guest login: "the guest is given" means the capacity the hypervisor
reports for that disk (`virsh domblkinfo`), which is what the guest sees after a block
resize. For a test with I/O running in the guest, see "Manual add-on: guest I/O".

E1 and E4 use volumes created from the image, which are always raw files. E2 and E3 use
empty volumes, which are qcow2 only with `vmstore_qcow2_volumes = True`; with False they
are raw and E2 and E3 report SKIP. Run the suite once with each setting. The precheck reads
both options from the backend's own section (then `[backend_defaults]`), in the order
cinder-volume reads the config directory.

## Expected results

| Driver | E1 | E2 | E3 | E4 |
| --- | --- | --- | --- | --- |
| 3.0.10 and earlier | FAIL: the scheduler refuses in-use extends (`online_extend_support` False) | FAIL, same | FAIL, same | FAIL: no attach-time grow |
| 3.0.11 (#16) | PASS | PASS | PASS | PASS |
| 3.0.12 | PASS | PASS | PASS | PASS |

The 3.0.11 rows come from #16's lab run on PCD staging (2026-11-09), which covered E1 to
E3 by hand with guest I/O; this script has not been run yet.

With `vmstore_online_extend_nova_check = False`, E3 reports SKIP: the driver then never
resizes an attached qcow2 file, and the guest sees the new size only after the volume is
attached again.

## Prerequisites and access levels

- **Required:** ssh key (BatchMode) to the CLI host (`CLI_SSH`) with admin credentials in
  `CLI_RC`. The file is sourced there and never copied. The CLI and Cinder must accept
  Block Storage API 3.42 (in-use extend); the precheck tests it.
- A VMstore-backed volume type (`VOLUME_TYPE`) and quota for five 1 GB volumes growing to
  2 GB. A compute host (`SRC_COMPUTE_ID`) with room for one small VM, a small bootable
  image (cirros, `IMAGE_ID`) and a network (`NETWORK_ID`). The VM boots from a volume on
  the type, with a private flavor (`FLAVOR_RAM_MB`, 256 by default).
- **Recommended:** ssh key to a host running cinder-volume for the type and mounting its
  share (`CINDER_SSH`, with `CINDER_VENV` and `CINDER_CONF_DIR`; the defaults are PCD
  paths). It needs `qemu-img`, `qemu-io` and the service's Python; the run copies
  `volume_record.py` (read-only) to `/root/<PREFIX>-lab` there and removes it at the end.
  Without it, E2, E3 and E4 report SKIP and E1 checks only Cinder and the guest size.
- **Recommended:** ssh key to the compute host (`SRC_COMPUTE_SSH`) with `virsh`. Without
  it, the guest size checks are listed as "not checked" in the results.
- **Optional:** ssh to every host serving the type (`LOG_SSH`) for cinder-volume log
  lines. The log path defaults to `/var/log/pf9/cindervolume-base.log` (used on PCD
  cinder hosts in earlier lab work; not checked on PCD 2026.8.1), overridden with
  `CINDER_LOG`. Log times are compared with the run's start in UTC. E4 needs the log
  line when the log is readable.
- python-cinderclient on the CLI host is optional: with it the run prints the
  `driver_version` the running cinder-volume reports (`cinder get-pools --detail`).

The precheck also prints `vmstore_qcow2_volumes` and `vmstore_online_extend_nova_check`
from `CINDER_CONF_DIR` (only those two lines are read).

## Run

```bash
cd test/lab/volume-extend
cp lab.env.example my-lab.env     # edit hosts, IDs, type, image, network, PREFIX
./run.sh my-lab.env               # about 20 minutes
```

The variable names are the same as in the other suites (`test/lab/volume-migration`,
`test/lab/snapshots`), so one env file per lab can serve all of them.

Run it from a workstation; it drives everything over ssh. Output goes to the terminal
and to a log file under `$TMPDIR` (path printed at the end).

Exit codes: `0` every check passed (SKIP allowed), `1` a check failed, `2` precheck or
setup problem (nothing tested).

## Reading the output

- `### precheck`: CLI access and API 3.42, services, image, network, leftover
  `<PREFIX>-*` objects, tools on the hosts, the two driver options, log access.
- `### create volumes`: each volume's status and file (`<format> <file bytes> <virtual
  bytes>`), the backend and the running driver version. Expected: `-boot`, `-e1-raw`,
  `-e4-raw` are `raw 1073741824`; `-e2-empty` and `-e3-empty` are `qcow2` (a small file,
  virtual size 1073741824) with `vmstore_qcow2_volumes = True`.
- `### boot a VM`: the VM must be `ACTIVE` on `SRC_COMPUTE_ID`.
- `### E1` to `### E4` (E3 runs last, because it stops and starts the VM): Cinder status
  and size after the extend, file and guest size, pattern and `qemu-img check` results,
  relevant cinder-volume log lines, then `RESULT`. "not tested" in a FAIL means a setup
  step failed (attach, stop), not necessarily the extend. "not checked" in a PASS lists
  checks this lab gives no access for.
- `=== cleanup` and `=== summary`: what was deleted, whether any other server or volume
  changed during the run (the script never touches them; a change means someone else was
  working on the lab), and the E1 to E4 table.

## Manual add-on: guest I/O

#16's acceptance covered extends under continuous guest I/O. To repeat it, before E1's
extend, log in to the VM console (`openstack console url show <PREFIX>-vm`, cirros
login), and on the data disk (usually `/dev/vdb`):

```sh
while true; do dd if=/dev/zero of=/dev/vdb bs=1M count=200 seek=300 conv=fsync 2>/dev/null; done &
cat /sys/block/vdb/size   # 512-byte sectors: 2097152 before, 4194304 after the extend
dmesg | tail              # no I/O errors
```

Stop the loop when E1 has finished. It writes MiB 300 to 500, outside the 256 MiB
pattern the script checks.

## Safety and cleanup

- Objects: VM `<PREFIX>-vm`, flavor `<PREFIX>-flavor` (private), volumes `<PREFIX>-boot`,
  `-e1-raw`, `-e2-empty`, `-e3-empty`, `-e4-raw`. The precheck refuses to start if any
  `<PREFIX>-*` server, volume or flavor exists.
- Cleanup runs on any exit, including failures and Ctrl-C: the VM, leftover attachments
  of the test volumes, the volumes (error states reset to `available` first; only the
  test's own), the flavor, and the helper directory on the cinder-volume host.
- **Writes to volume files outside Cinder:** the script writes a 256 MiB pattern into
  `<PREFIX>-e1-raw` and `<PREFIX>-e2-empty` before they are attached, and E4 shrinks the
  file of `<PREFIX>-e4-raw` to 512 MiB with `truncate` while it is detached. Both touch
  only the run's own volumes; the path is checked to be `volume-<name_id>` of that volume
  first. Never point the script at a lab where these writes are not acceptable.
- E3 stops and starts the test VM. No other VM is touched.
- The script lists all other servers and volumes before and after, and reports any
  difference.

### Manual cleanup after a hard interruption

If the workstation or ssh session dies (cleanup cannot run), on the CLI host:

```bash
source /root/admin.rc; export OS_REGION_NAME=<region>
openstack server list --all-projects --name '^vme10-'            # delete --wait each
openstack volume list --all-projects --long | grep ' vme10-'      # set --state available if stuck, then delete
openstack flavor list --all | grep vme10-                         # delete
```

and remove `/root/vme10-lab` on the cinder-volume host. Replace `vme10` with your
`PREFIX`.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `volume type ... not found, or the CLI or Cinder does not accept API 3.42` | Old python-openstackclient on the CLI host, or the type name is wrong. In-use extend needs API 3.42. |
| E1 or E2 `Cinder in-use, size 1 GB` | The scheduler refused the extend: driver older than 3.0.11 (`online_extend_support` False), or not enough space on the share. See `openstack volume message list`. |
| E1 or E2 guest size stays 1 GiB | Nova did not apply the `volume-extended` event. Check nova-compute's log on the compute host for the instance (`extend_volume`). |
| E2 virtual size stays 1 GiB | Same as above: for qcow2 only Nova resizes a running image. |
| E3 `not resized offline` | `vmstore_online_extend_nova_check` is False, the `[nova]` credentials in the Cinder config do not work, or Nova still reported a task or power state other than shut down. The cinder-volume log names the reason. |
| E4 `no grow warning in the cinder-volume log` | The attach ran on a host missing from `LOG_SSH`, or the log is not in UTC. The file size result above it still shows whether the grow happened. |
| VM not `ACTIVE` or on another host | Capacity on `SRC_COMPUTE_ID` (placement memory), or the image cannot boot from a volume of this type. |
| E2 and E3 SKIP | Empty volumes are raw (`vmstore_qcow2_volumes = False`). Expected in the raw-only run. The SKIP line shows the value read from the backend's own section. |
| precheck `WARNING: ... but a new empty volume is a <format> file` | The option on `CINDER_SSH` does not match the file the backend created: another host serving the backend has a different value (check every host with `swap.sh status`), or the option is set in a file read later (`cinder_override.conf`). |

## Run history

| Date | Lab | Driver | vmstore_qcow2_volumes | E1 | E2 | E3 | E4 | Notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
