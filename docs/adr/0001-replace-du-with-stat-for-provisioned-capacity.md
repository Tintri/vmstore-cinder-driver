# Replace `du` with `os.stat()` for provisioned capacity

The base Cinder NFS driver calls `du` twice during stats collection: once in `_get_capacity_info` (`du --apparent-size`) and once in `_get_provisioned_capacity` (`du --bytes -s`). On a production VMstore NFS share with many volumes, `du` traverses the full filesystem tree on every call — Cinder polls stats every ~60 seconds, and under high concurrency this became a measurable bottleneck.

We override both `_get_capacity_info` and `_get_provisioned_capacity` to scan only top-level `volume-*` files using `os.stat(filepath).st_size`. Three properties of VMstore NFS make this safe: (1) VMstore reports `st_size` as the logical/promised size for thin-provisioned files, making it semantically equivalent to `du --apparent-size` for a single file; (2) VMstore snapshots are managed inside the appliance and are not visible as files on the NFS filesystem, so scanning only `volume-*` files at the share root is complete; (3) transient clone directories (which do not start with `volume-`) are correctly excluded and do not need to be counted until the clone completes and the file is renamed.

`_update_volume_stats` calls `super()` to stay in the Cinder parent chain and inherit future upstream stats fields; only the two capacity methods are overridden.

## Considered Options

- **Keep `du`**: Accurate but O(files) with full tree traversal — unacceptable latency under concurrent load.
- **Query VMstore REST API for capacity**: No direct stats endpoint exists in the VMstore v310 API.
- **Override the entire `_update_volume_stats` without calling `super()`**: Eliminates both `du` calls but silently diverges from upstream Cinder as new stats fields are added. Rejected in favour of targeted method overrides.
