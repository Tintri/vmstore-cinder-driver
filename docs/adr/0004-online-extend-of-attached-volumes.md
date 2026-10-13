# Extend attached volumes: the driver grows raw files, Nova grows qcow2 files

Cinder can extend an attached (in-use) volume: the driver grows the backing storage, then Cinder sends Nova a `volume-extended` event and Nova's libvirt driver calls `virDomainBlockResize` so the guest sees the new size. Nova's NFS volume driver has supported this since Victoria. Upstream's generic `NfsDriver.extend_volume` still refuses attached volumes (bug 1870367) because `qemu-img resize` cannot run on a file a running qemu holds, and its snapshots are qcow2 overlay chains. Neither applies to us: VMstore snapshots are not files, so the active file is always `volume-<id>` (VMTS-243).

We report `online_extend_support: True` and, for attached volumes, choose by file format:

- **raw:** the driver grows the file with `truncate` and verifies its size. Growing a raw file only changes its length, which is safe while a VM writes to it; Nova then makes the guest see it.
- **qcow2:** the driver **never modifies the file while a VM may hold it**. The virtual size lives in the image header and the allocation tables, which a running qemu caches in memory. VMstore shares are mounted `nolock` (`local_lock=all`), so qemu's lock on a compute host is invisible from the Cinder host and `qemu-img resize` does not refuse. Nova/qemu resizes the image instead.
- **qcow2 on shut-off instances:** Nova skips the guest-side resize when the instance is not running, so nothing would grow the file. If Nova (via the `[nova]` credentials) reports every attached instance as `SHUTOFF` with no task in progress, the driver resizes the file with `qemu-img resize`, then re-checks and logs an error if an instance left that state meanwhile. Any doubt (Nova error, hidden `task_state`, a host attachment with no instance, `vmstore_online_extend_nova_check = False`) leaves the file alone.
- **Safety net:** `initialize_connection` grows a file whose virtual size is smaller than the volume size, under the volume lock, but only when no other attachment is in the `attached` state. This repairs any resize Nova skipped or failed before the next consumer opens the file.

Everything is validated (larger size, share space, format) before the file is touched, so a failed extend leaves the volume at its original size. Cinder still marks it `error_extending`; that status comes from Cinder core.

## Evidence

Validated on the Platform9 staging lab (2026-10-10) with continuous write-and-verify I/O: raw grow under I/O plus `blockResize`, qcow2 `blockResize` alone, and offline grows of both all passed with 0 bad blocks out of 363,819. An external `qemu-img resize` of a qcow2 image held by a running VM on another host made qemu mark the image corrupt (`Preventing invalid write on metadata (overlaps with active L1 table)`) on its next metadata write.

## Consequences

- An attached qcow2 extend reported successful by Cinder depends on Nova applying it; Cinder gets no feedback (upstream's `os-extend_volume_completion` flow is not used by Nova). If Nova does not apply it, the guest keeps the old size until the next attach.
- Both the VMstore and our provisioned-capacity scan (ADR-0001) count file length, so an online-extended qcow2 volume is under-counted until data is written.
- The driver now calls the Nova API for attached qcow2 extends; deployments without `[nova]` credentials fall back to the Nova-only path.
