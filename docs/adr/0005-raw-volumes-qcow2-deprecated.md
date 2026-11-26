# New volumes are raw; vmstore_qcow2_volumes is deprecated

`vmstore_qcow2_volumes` makes the shared Cinder NFS code create new volumes as qcow2 files (`remotefs.py` reads `<driver_prefix>_qcow2_volumes`). The driver default has always been `False`, but some deployments set it to `True` in their backend template. With it on, the format Cinder records for a volume drifts from the file in several paths, and Nova opens volumes with the recorded format on every refresh (migrate, resize, hard reboot):

- A volume created from an image is created qcow2 and recorded qcow2, then the image copy writes a raw file (fixed for new volumes by VMTS-252).
- Cinder's generic volume migration creates the destination as qcow2, keeps the qcow2 record on raw data, and copies qcow2 data with a raw byte count that cuts it at the volume size (VME-29, lab checks R1 and R2).
- Glance cannot store images on a Cinder NFS store whose volumes are qcow2.

qcow2 brings nothing on VMstore: snapshots and clones are taken on the array, not as qcow2 overlays, and raw volumes are sparse files (`vmstore_sparsed_volumes`, default `True`). With the option off, a volume's file is raw when it is created and stays raw, so neither Cinder's record nor Nova's saved format can go stale for new volumes.

## Decision

1. **3.0.12 deprecates the option** (`deprecated_for_removal`, VME-33). A backend with it set to `True` logs a warning at startup, and the docs name raw as the only supported setting. The driver still honours `True`, because deployments switch their backends to raw during an upgrade, not before it.
2. **Hybrid backends are supported** for as long as they exist. A backend can hold raw and qcow2 volumes side by side. Every operation decides a volume's format per volume, from its Cinder record, its file and its attachment, never from the option. Switching a backend from `True` to `False` changes only the volumes created afterwards.
3. **Next: enforce raw for new volumes.** A later release creates every new volume as raw whatever the option says, and logs that the option is ignored. qcow2 volumes created earlier keep working.
4. **Removal.** Once deployments have converted their qcow2 volumes to raw, the option is removed. Converting is a separate, VMstore-side procedure with its own tooling, not part of the driver. The qcow2 handling for existing volumes (format checks, attached qcow2 extend through Nova, ADR-0004) stays until no deployment has qcow2 volumes.

## Consequences

- New deployments and backends switched to `False` get raw volumes only: detached and attached extend grow the file with `truncate`, and nothing can drift between the file and the record.
- Volumes created before the switch keep their qcow2 format and their records. Records written wrongly by earlier releases still need correcting (a Cinder and Nova database script, run by the operator).
- Volume migration (3.0.12, VME-29): a move within the same VMstore share copies nothing, so qcow2 volumes keep working across cinder-volume hosts. A qcow2 file is refused across shares instead of being cut by Cinder's generic copy; `--force-host-copy` bypasses the driver and must not be used for qcow2 volumes.
- Reviews: no change may decide a volume's format by probing guest-writable data alone, and no copy may byte-copy a non-raw file as raw.
