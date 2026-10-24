# VMstore Cinder Driver

An OpenStack Cinder NFS volume driver for Tintri VMstore appliances. It bridges Cinder's volume lifecycle (create, clone, snapshot, delete) to VMstore's REST API while mounting volumes as flat files on a single NFS share.

## Language

### Storage topology

**VMstore Appliance**: The Tintri storage appliance. Exposes one NFS share per Cinder backend and manages volumes internally as virtual disks.
_Avoid_: Tintri appliance, storage array, NFS server

**NFS Share**: The single NFS export from a VMstore appliance that Cinder mounts. All Cinder volumes for a given backend are flat files at the root of this share.
_Avoid_: NFS export, share path, mount point

**Virtual Disk (VD)**: VMstore's internal representation of a Cinder volume, as tracked by the hypervisor layer. Required by the VMstore API before snapshot and clone operations can be initiated.
_Avoid_: VM disk, volume handle

### Capacity

**Provisioned Capacity**: The sum of logical (apparent) sizes of Cinder volume files on the NFS share, measured via `os.stat().st_size`. VMstore reports `st_size` as the promised/logical size for thin-provisioned files, not actual blocks consumed.
_Avoid_: allocated capacity, used capacity, disk usage

**Actual Disk Usage**: The physical blocks consumed on VMstore — NOT what this driver tracks. Would require `du` without `--apparent-size` or `st_blocks * 512`.
_Avoid_: (do not conflate with provisioned capacity)

### Snapshot and clone model

**VMstore Snapshot**: A point-in-time copy managed entirely inside VMstore via REST API (`cinder/snapshot`). Not visible as files on the NFS filesystem. Distinct from Cinder's snapshot record — both exist but VMstore's is the authoritative physical copy.
_Avoid_: NFS snapshot, filesystem snapshot

**Temp Clone Directory**: A transient subdirectory (e.g., `clone-<src>-<vol-uuid>/` or `<snapshot-name>-vol-<uuid>/`) created on the NFS share during a VMstore clone operation. Renamed to `volume-<uuid>` and the directory removed before the clone operation returns. Not counted toward provisioned capacity.
_Avoid_: staging directory, clone workspace

### Volume resize

**Online Extend**: Growing a Cinder volume while it is attached to an instance. For a raw volume the driver grows the file and Nova makes the guest see it; for a qcow2 volume only Nova/qemu may grow the file, unless Nova confirms every attached instance is shut off (see ADR-0004).
_Avoid_: live resize, hot extend

**Virtual Size**: The disk size a volume file presents to the guest (`qemu-img info` virtual size). For raw files it equals the file length; for qcow2 it is stored in the image header and can be larger than the file.
_Avoid_: file size (for qcow2), provisioned capacity
